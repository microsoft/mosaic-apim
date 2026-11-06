"""Seed through MOSAIC's audited APIs, including reviewed, exact-plan gateway applies."""

import json
import logging
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener

from scripts.federal_demo.estate import ENVIRONMENT_KEYS, PEOPLE, SERVICES, TEAMS, validate_grant
from scripts.federal_demo.operator import NoRedirect, OperationError, Operator
from scripts.federal_demo.workloads import JOBS

COUNTER = "@(context.Subscription.Id)"


def matches(actual, expected) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and matches(actual[key], value) for key, value in expected.items()
        )
    if isinstance(actual, str) and isinstance(expected, str):
        if expected.casefold().startswith("/subscriptions/"):
            return actual.casefold() == expected.casefold()
    return actual == expected


class Mosaic:
    def __init__(self, operator: Operator):
        self.operator = operator
        self.base = operator.target["mosaicApiUrl"].rstrip("/")
        parsed = urlsplit(self.base)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".azurewebsites.net")
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise OperationError("MOSAIC URL must be the configured Azure App Service HTTPS origin")
        self.resource = f"api://{operator.target['apiAppClientId']}"

    def request(self, method: str, path: str, body=None, *, _attempt=0):
        if not path.startswith("/") or path.startswith("//") or ".." in path:
            raise OperationError("MOSAIC path is outside the API boundary")
        headers = {"Authorization": f"Bearer {self.operator.token(self.resource)}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            with build_opener(NoRedirect()).open(
                Request(
                    self.base + "/api/v1" + path,
                    data=data,
                    headers=headers,
                    method=method,
                ),
                timeout=90,
            ) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise OperationError("MOSAIC response exceeds the bounded size")
                return json.loads(raw) if raw else None
        except HTTPError as error:
            raise OperationError(
                f"MOSAIC {method} {path.split('?')[0]} returned HTTP {error.code}; "
                "review server diagnostics before retrying"
            ) from None
        except (URLError, OSError):
            if method == "GET" and _attempt < 2:
                logging.getLogger(__name__).warning("MOSAIC read interrupted; retrying %s", path)
                time.sleep(_attempt + 1)
                return self.request(method, path, body, _attempt=_attempt + 1)
            detail = "read failed" if method == "GET" else "write outcome may be uncertain"
            raise OperationError(f"MOSAIC {method} {path} is unreachable; {detail}") from None

    def ensure(self, path: str, payload: dict, match: dict) -> dict:
        rows = self.request("GET", path)
        found = [row for row in rows if matches(row, match)]
        if len(found) > 1:
            raise OperationError(f"Ambiguous existing records at {path}")
        return found[0] if found else self.request("POST", path, payload)

    def apply(self, collection: str, record: dict) -> None:
        path = f"/{collection}/{record['id']}"
        plan = self.request("POST", path + "/plan")
        self.operator.remember(
            f"plan:{collection}:{record['id']}",
            {
                "id": plan["id"],
                "digest": plan["digest"],
            },
        )
        # The API refuses stale digests/intent; never retry an uncertain apply automatically.
        run = self.request("POST", path + "/apply?plan=" + quote(plan["id"]))
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            current_run = self.request("GET", path + "/runs/" + quote(run["id"], safe=""))
            if (
                current_run["planId"] != plan["id"]
                or current_run["planDigest"] != plan["digest"]
            ):
                raise OperationError("Gateway run does not match the reviewed plan")
            if current_run["status"] == "succeeded":
                current = self.request("GET", path)
                applied_at = current.get("lastAppliedAt")
                if (
                    current["accessState"] != "applied"
                    or not applied_at
                    or not current_run.get("completedAt")
                    or not (
                        datetime.fromisoformat(current_run["startedAt"])
                        <= datetime.fromisoformat(applied_at)
                        <= datetime.fromisoformat(current_run["completedAt"])
                    )
                ):
                    raise OperationError("Gateway run succeeded but its applied snapshot is stale")
                self.operator.remember(
                    f"apply:{collection}:{record['id']}",
                    {"runId": run["id"], "planId": plan["id"], "digest": plan["digest"]},
                )
                return
            if current_run["status"] != "running":
                raise OperationError(
                    "Gateway apply failed or is uncertain; explicit recovery required"
                )
            time.sleep(5)
        raise OperationError("Gateway apply is still pending; inspect its run before resuming")

    def sync(self, collection: str, record: dict) -> None:
        path = f"/{collection}/{record['id']}"
        run = self.request("POST", path + "/sync")
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            current = self.request("GET", f"{path}/sync-runs/{run['id']}")
            if current["status"] == "succeeded":
                return
            if current["status"] in ("failed", "partial", "cancelled"):
                raise OperationError(f"{collection} inventory synchronization failed")
            time.sleep(3)
        raise OperationError("Inventory synchronization is still pending; inspect before resuming")


def register_identity(api: Mosaic, object_id: str, kind: str, label: str, center=None) -> dict:
    payload = {"objectId": object_id, "kind": kind, "label": label}
    if center is not None:
        payload["defaultCostCenterId"] = center
    principal = api.ensure("/principals", payload, {"objectId": object_id})
    if principal["kind"] != kind:
        raise OperationError("Existing principal has a different identity kind")
    return principal


def grant(
    api: Mosaic,
    principal: dict,
    center: dict,
    resource: dict,
    environment: str,
    *,
    general=False,
    calls_only=False,
) -> None:
    validate_grant(center["code"], environment)
    subject_kind = {
        "user": "user",
        "agentUser": "user",
        "securityGroup": "securityGroup",
        "managedIdentity": "application",
        "servicePrincipal": "application",
        "agentIdentity": "application",
    }.get(principal["kind"])
    if subject_kind is None:
        raise OperationError("Unsupported grant subject kind")
    subject = {"kind": subject_kind, "id": principal["id"]}
    limits = {
        "requests": {
            "counterKeyExpression": COUNTER,
            "calls": 10 if calls_only else (1 if general else 2),
            "renewalPeriodSeconds": 60,
            "callQuota": 100 if general else 1000,
            "callQuotaPeriod": "Monthly",
        }
    }
    if not calls_only:
        limits["tokens"] = {
            "counterKeyExpression": COUNTER,
            "tokensPerMinute": 1000 if general else 2000,
            "tokenQuota": 25000 if general else 250000,
            "tokenQuotaPeriod": "Monthly",
        }
    api.ensure(
        "/entitlements",
        {
            "subject": subject,
            "resource": resource,
            "costCenterId": center["id"],
            "enabled": True,
            "enforcement": limits,
            "notes": "Owned federal demonstration grant",
        },
        {"subject": subject, "resource": resource, "costCenterId": center["id"]},
    )
    current = api.request("GET", f"/cost-centers/{center['id']}")
    other_limits = [item for item in current["limits"] if not matches(item["resource"], resource)]
    pool = {"monthlyCalls": 1000 if general else 5000}
    if not calls_only:
        pool["monthlyTokens"] = 100000 if general else 1000000
    api.request(
        "PUT",
        f"/cost-centers/{center['id']}/limits",
        {
            "limits": [*other_limits, {"resource": resource, "pool": pool}],
        },
    )


def seed_directory(operator: Operator) -> tuple[dict, dict]:
    operator.verify_target()
    target = operator.target
    api = Mosaic(operator)
    me = api.request("GET", "/console/me")
    if me.get("role") != "Admin" and "Admin" not in me.get("roles", []):
        raise OperationError("The delegated operator is not a MOSAIC administrator")
    centers = {}
    existing_centers = api.request("GET", "/cost-centers")
    for team in TEAMS:
        existing = next((row for row in existing_centers if row["code"] == team.code), None)
        if (
            team.code != "general"
            and existing
            and existing.get("description") != (f"MOSAIC demo:{target['estate']}")
        ):
            raise OperationError("Unowned cost-center code collision")
        center = existing or api.request(
            "POST",
            "/cost-centers",
            {
                "name": team.name,
                "code": team.code,
                "keysAllowed": False,
                "description": f"MOSAIC demo:{target['estate']}",
            },
        )
        centers[team.code] = center
        operator.remember(f"center:{team.code}", {"id": center["id"]})
    group_principals = {}
    for team in TEAMS:
        group_id = operator.state["objects"][f"group:{team.code}"]["id"]
        principal = register_identity(api, group_id, "securityGroup", team.name)
        group_principals[team.code] = principal
        api.request("PUT", f"/cost-centers/{centers[team.code]['id']}/members/{principal['id']}")
    for person in PEOPLE:
        user = operator.state["objects"][f"user:{person.local_part}"]
        principal = register_identity(
            api, user["id"], "user", person.name, centers[person.center]["id"]
        )
        if principal.get("defaultCostCenterId") != centers[person.center]["id"]:
            api.request(
                "PATCH",
                f"/principals/{principal['id']}",
                {
                    "defaultCostCenterId": centers[person.center]["id"],
                },
            )
    return centers, group_principals


def seed_models(operator: Operator) -> None:
    target = operator.target
    if len(target.get("endpoints", [])) != 5 or set(target.get("gateways", {})) != {
        "dev",
        "stg",
        "prod",
    }:
        raise OperationError("Governance requires five endpoints and three gateway bindings")
    centers, group_principals = seed_directory(operator)
    api = Mosaic(operator)
    gateways = {}
    for env, resource_id in target["gateways"].items():
        gateway = api.ensure(
            "/gateways",
            {
                "azureResourceId": resource_id,
                "environment": ENVIRONMENT_KEYS[env],
                "name": f"Federal {ENVIRONMENT_KEYS[env].title()}",
            },
            {"azureResourceId": resource_id},
        )
        if gateway["environment"] != ENVIRONMENT_KEYS[env]:
            raise OperationError("Gateway classification changed; review before seeding")
        api.request("PATCH", f"/gateways/{gateway['id']}", {"managementMode": "manage"})
        api.sync("gateways", gateway)
        gateways[env] = gateway
    for binding in target["endpoints"]:
        env = binding["environment"]
        if env not in gateways:
            raise OperationError("Endpoint has no approved environment gateway")
        endpoint = api.ensure(
            "/model-endpoints",
            {
                "azureResourceId": binding["resourceId"],
                "name": binding["name"],
                "environment": ENVIRONMENT_KEYS[env],
            },
            {"azureResourceId": binding["resourceId"]},
        )
        if endpoint["environment"] != ENVIRONMENT_KEYS[env]:
            raise OperationError("Endpoint classification changed; review before publishing")
        api.sync("model-endpoints", endpoint)
        publication = api.ensure(
            "/publications",
            {
                "gatewayId": gateways[env]["id"],
                "modelEndpointId": endpoint["id"],
                "deploymentName": binding["deploymentName"],
                "apiName": "mosaic-fed-" + binding["deploymentName"].replace(".", "-"),
                "apiPath": "mosaic/federal/" + binding["deploymentName"].replace(".", "-"),
                "governedAccess": {"keysEnabled": False, "entraEnabled": True},
                "enforcement": {"counterKeyExpression": COUNTER, "tokensPerMinute": 8000},
            },
            {
                "gatewayId": gateways[env]["id"],
                "modelEndpointId": endpoint["id"],
                "deploymentName": binding["deploymentName"],
            },
        )
        if publication["accessState"] != "applied":
            api.apply("publications", publication)
        model = api.request("POST", f"/publications/{publication['id']}/model-api")
        operator.remember(
            f"model:{env}:{binding['deploymentName']}",
            {
                "id": model["id"],
                "publicationId": publication["id"],
                "endpointId": endpoint["id"],
                "deploymentName": binding["deploymentName"],
            },
        )
        if binding["deploymentName"] == "gpt-5.6-luna":
            for team in TEAMS:
                if env in team.environments:
                    grant(
                        api,
                        group_principals[team.code],
                        centers[team.code],
                        {"kind": "modelApi", "id": model["id"]},
                        env,
                        general=team.code == "general",
                    )
            api.apply("publications", publication)
    api.request("PATCH", "/environment-catalog/settings", {"requireClassification": True})


def seed_tools(operator: Operator) -> None:
    operator.verify_target()
    bindings = operator.target.get("mcpBindings", [])
    expected = {(env, service.key) for env in ENVIRONMENT_KEYS for service in SERVICES}
    if {(item["environment"], item["serviceKey"]) for item in bindings} != expected:
        raise OperationError("Tool governance requires all 24 exact environment/service bindings")
    if len(bindings) != 24:
        raise OperationError("Duplicate MCP bindings are not permitted")
    api = Mosaic(operator)
    centers = {row["code"]: row for row in api.request("GET", "/cost-centers")}
    principals = api.request("GET", "/principals")
    gateways = api.request("GET", "/gateways")
    for binding in bindings:
        env = binding["environment"]
        service = next(item for item in SERVICES if item.key == binding["serviceKey"])
        center = centers[service.cost_center(env)]
        gateway = next(
            row
            for row in gateways
            if matches(row, {"azureResourceId": operator.target["gateways"][env]})
        )
        endpoint = api.ensure(
            "/mcp-endpoints",
            {
                "name": service.name,
                "endpoint": binding["url"],
                "environment": ENVIRONMENT_KEYS[env],
                "authMode": "managedIdentity",
                "resourceAudience": binding["audience"],
            },
            {"endpoint": binding["url"]},
        )
        if endpoint["environment"] != ENVIRONMENT_KEYS[env]:
            raise OperationError("MCP environment mismatch")
        if not endpoint["access"]["canDiscover"]:
            logging.getLogger(__name__).warning(
                "MCP registration was not reachable; checking %s once before synchronization",
                service.key,
            )
            endpoint = api.request("POST", f"/mcp-endpoints/{endpoint['id']}/preflight")
            if not endpoint["access"]["canDiscover"]:
                raise OperationError(
                    "MCP preflight still refuses discovery; inspect the saved access verdict"
                )
        api.sync("mcp-endpoints", endpoint)
        publication = api.ensure(
            "/mcp-publications",
            {
                "gatewayId": gateway["id"],
                "mcpEndpointId": endpoint["id"],
                "displayName": service.name,
                "apiName": "mosaic-fed-" + service.key,
                "apiPath": "mosaic/federal/mcp/" + service.key,
            },
            {"gatewayId": gateway["id"], "mcpEndpointId": endpoint["id"]},
        )
        access_centers = [center]
        if env == "prod" and service.key.startswith("faa-"):
            other = "FAA-RND" if center["code"] == "FAA-OPS" else "FAA-OPS"
            access_centers.append(centers[other])
        for access_center in access_centers:
            group_id = operator.state["objects"][f"group:{access_center['code']}"]["id"]
            group = next(row for row in principals if row["objectId"] == group_id)
            grant(
                api,
                group,
                access_center,
                {"kind": "mcpServer", "id": publication["mcpServerId"]},
                env,
                calls_only=True,
            )
        if service.calls_model:
            caller = register_identity(
                api, binding["principalId"], "managedIdentity", service.name, center["id"]
            )
            model = operator.state["objects"][f"model:{env}:gpt-5.6-luna"]
            grant(api, caller, center, {"kind": "modelApi", "id": model["id"]}, env)
            api.apply("publications", {"id": model["publicationId"]})
            api.request(
                "PUT",
                f"/mcp-publications/{publication['id']}/model-caller",
                {"principalId": caller["id"]},
            )
        api.apply("mcp-publications", publication)
        operator.remember(
            f"mcp:{env}:{service.key}",
            {
                "id": publication["mcpServerId"],
                "publicationId": publication["id"],
            },
        )


def seed_jobs(operator: Operator) -> None:
    operator.verify_target()
    bindings = operator.target.get("jobBindings", [])
    if len(bindings) != 5 or {item["key"] for item in bindings} != set(JOBS):
        raise OperationError("Workload governance requires the five exact Production bindings")
    api = Mosaic(operator)
    centers = {row["code"]: row for row in api.request("GET", "/cost-centers")}
    changed_models = set()
    changed_tools = set()
    for binding in bindings:
        label, code, deployment, service_key = JOBS[binding["key"]]
        center = centers[code]
        principal = register_identity(
            api, binding["principalId"], "managedIdentity", label, center["id"]
        )
        api.request("PUT", f"/cost-centers/{center['id']}/members/{principal['id']}")
        model = operator.state["objects"][f"model:prod:{deployment}"]
        tool = operator.state["objects"][f"mcp:prod:{service_key}"]
        grant(api, principal, center, {"kind": "modelApi", "id": model["id"]}, "prod")
        grant(
            api, principal, center, {"kind": "mcpServer", "id": tool["id"]}, "prod", calls_only=True
        )
        changed_models.add(model["publicationId"])
        changed_tools.add(tool["publicationId"])
    for publication_id in sorted(changed_models):
        api.apply("publications", {"id": publication_id})
    for publication_id in sorted(changed_tools):
        api.apply("mcp-publications", {"id": publication_id})
