"""Bounded manual execution and a reversible valid-token backend rejection probe."""

import copy
import hashlib
import json
import time
from urllib.parse import quote

from scripts.federal_demo.directory import ensure_role
from scripts.federal_demo.operator import OperationError, Operator, command

ARM = "https://management.azure.com"
VERSION = "?api-version=2025-01-01"


def runtime_records(logs) -> list:
    if isinstance(logs, dict) and logs.get("outcome") in ("ok", "error"):
        return [logs]
    if isinstance(logs, list):
        lines = [row.get("Log", "") for row in logs if isinstance(row, dict)]
    elif isinstance(logs, dict):
        lines = [logs.get("Log", "")]
    else:
        lines = str(logs).splitlines()
    records = []
    for line in lines:
        start = line.find("{")
        if start < 0:
            continue
        try:
            record = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("outcome") in ("ok", "error"):
            records.append(record)
    return records


def run(operator: Operator, binding: dict, *, override=None) -> dict:
    operator.verify_target()
    resource_id = binding["resourceId"]
    prefix = f"/subscriptions/{operator.target['subscriptionId']}/resourceGroups/"
    prefix += "rg-mosaic-federal-prod/providers/Microsoft.App/jobs/"
    if not resource_id.casefold().startswith(prefix.casefold()):
        raise OperationError("Only owned Production jobs may be started")
    lock = operator.state_path.with_suffix(".execution.lock")
    try:
        handle = lock.open("x")
    except FileExistsError:
        raise OperationError("Another operator execution holds the local job lock") from None
    with handle:
        try:
            job = operator.request("GET", ARM + resource_id + VERSION, ARM + "/")
            config = job["properties"]["configuration"]
            if config["triggerType"] != "Manual":
                raise OperationError("Job is not configured for manual execution")
            executions = operator.request(
                "GET", ARM + resource_id + "/executions" + VERSION, ARM + "/"
            )
            if any(
                row["properties"]["status"] not in ("Succeeded", "Failed", "Stopped")
                for row in executions["value"]
            ):
                raise OperationError("Job has an active or uncertain execution; no overlap allowed")
            source = job["properties"]["template"]
            if source.get("volumes"):
                raise OperationError("Execution overrides do not support this job's volumes")
            template = {
                key: copy.deepcopy(source[key])
                for key in ("containers", "initContainers") if key in source
            }
            container = template["containers"][0]
            if override is not None:
                container["command"], container["args"] = override
            execution = operator.request(
                "POST", ARM + resource_id + "/start" + VERSION, ARM + "/", template
            )
            if not isinstance(execution, dict) or not execution.get("name"):
                raise OperationError("Start outcome is uncertain; inspect Azure before retrying")
            name = execution["name"]
            operator.remember(f"execution:{binding['key']}", {"name": name, "job": resource_id})
            deadline = time.monotonic() + 420
            while time.monotonic() < deadline:
                status = operator.request(
                    "GET", ARM + resource_id + "/executions/" + quote(name) + VERSION, ARM + "/"
                )["properties"]["status"]
                if status in ("Failed", "Stopped"):
                    raise OperationError(f"Manual job ended with {status}")
                if status == "Succeeded":
                    break
                time.sleep(5)
            else:
                raise OperationError("Job remains active; do not retry an uncertain execution")
            logs = command(
                [
                    "az",
                    "containerapp",
                    "job",
                    "logs",
                    "show",
                    "--subscription",
                    operator.target["subscriptionId"],
                    "--resource-group",
                    "rg-mosaic-federal-prod",
                    "--name",
                    binding["name"],
                    "--execution",
                    name,
                    "--container",
                    container["name"],
                    "--follow",
                    "false",
                    "--tail",
                    "100",
                    "--format",
                    "text",
                ],
                allow_text=True,
            )
            records = runtime_records(logs)
            if len(records) != 1 or records[0]["outcome"] != "ok":
                raise OperationError(
                    "Execution succeeded but its exact runtime evidence is unavailable"
                )
            operator.remember(
                f"result:{binding['key']}",
                {
                    "execution": name,
                    "result": records[0],
                },
            )
            return records[0]
        finally:
            handle.close()
            lock.unlink()


def verify_wrong_client(operator: Operator) -> None:
    operator.verify_target()
    bindings = operator.target.get("mcpBindings", [])
    if len(bindings) != 24:
        raise OperationError("Wrong-client probe requires all 24 owned backend bindings")
    targets = sorted(
        ({"url": row["url"], "audience": row["audience"]} for row in bindings),
        key=lambda item: (item["url"], item["audience"]),
    )
    serialized = json.dumps(targets, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    binding_hash = hashlib.sha256(json.dumps(bindings, sort_keys=True).encode()).hexdigest()
    verified = operator.state["objects"].get("backendVerification")
    if not verified or verified["bindingsSha256"] != binding_hash:
        raise OperationError("Run the owned ARM/anonymous/audience verification first")
    job = next(item for item in operator.target["jobBindings"] if item["key"] == "nasa-brief")
    temporary = []
    try:
        for env in ("dev", "stg", "prod"):
            audience = operator.state["objects"][f"audience:{env}"]
            path = f"/servicePrincipals/{job['principalId']}/appRoleAssignments"
            existing = operator.items(path)
            if any(row["resourceId"] == audience["principalId"] for row in existing):
                raise OperationError(
                    "Unexpected existing probe backend permission; review required"
                )
            assignment_id = ensure_role(
                operator, job["principalId"], audience["principalId"], audience["roleId"]
            )
            temporary.append((path, assignment_id))
            operator.remember(
                f"temporaryBackendRole:{env}",
                {
                    "principalId": job["principalId"],
                    "assignmentId": assignment_id,
                },
            )
        result = run(
            operator,
            job,
            override=(
                ["python", "-m", "federal_runtime.verify_backend"],
                ["--targets-json", serialized],
            ),
        )
        if (
            result.get("targets_sha256") != digest
            or result.get("target_count") != 24
            or result.get("status_counts") != {"403": 24}
        ):
            raise OperationError(
                "Backend proof does not match the exact expected request inventory"
            )
    finally:
        failures = []
        for path, assignment_id in reversed(temporary):
            try:
                operator.graph("DELETE", path + "/" + quote(assignment_id, safe=""))
            except OperationError as error:
                failures.append(f"{assignment_id}: {error}")
        if failures:
            raise OperationError(
                "Failed to remove temporary backend roles: " + "; ".join(failures)
            )
    operator.remember(
        "wrongClientVerification",
        {
            "bindingsSha256": binding_hash,
            "targetsSha256": digest,
            "denied": 24,
            "execution": operator.state["objects"]["execution:nasa-brief"]["name"],
            "temporaryPermissionsRemoved": True,
        },
    )
