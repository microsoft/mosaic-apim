"""Temporary grants for the real deploying operator, never fabricated persona requests."""

import logging

from scripts.federal_demo.governance import COUNTER, Mosaic, matches, register_identity
from scripts.federal_demo.operator import OperationError, Operator

NOTE = "Owned temporary federal deployment verification; revoke after bounded probe"


def prepare(operator: Operator) -> list[dict]:
    operator.verify_target()
    ledger = operator.state["objects"].get("verificationAccess")
    if ledger is not None and ledger.get("cleanedUp"):
        raise OperationError(
            "Previous verification access was retired; review before another probe"
        )
    api = Mosaic(operator)
    models = {
        key: value for key, value in operator.state["objects"].items() if key.startswith("model:")
    }
    if len(models) != 5:
        raise OperationError("All five governed model records must exist")
    me = operator.graph("GET", "/me?$select=id,displayName")
    existing = api.request("GET", "/principals")
    found = next((row for row in existing if row["objectId"] == me["id"]), None)
    if ledger is not None and (found is None or ledger.get("principalId") != found["id"]):
        raise OperationError("Verification principal ownership changed; preparation stopped")
    principal = found or register_identity(
        api, me["id"], "user", "Deployment verification operator"
    )
    center = next(row for row in api.request("GET", "/cost-centers") if row["code"] == "general")
    if ledger is None:
        ledger = {
            "principalId": principal["id"],
            "principalCreated": found is None,
            "grants": [],
            "cleanedUp": False,
        }
    operator.remember("verificationAccess", ledger)
    targets = []
    for key, model in models.items():
        subject = {"kind": "user", "id": principal["id"]}
        resource = {"kind": "modelApi", "id": model["id"]}
        grants = api.request("GET", "/entitlements")
        existing_grant = next(
            (
                row
                for row in grants
                if matches(
                    row,
                    {
                        "subject": subject,
                        "resource": resource,
                        "costCenterId": center["id"],
                    },
                )
            ),
            None,
        )
        if existing_grant and existing_grant.get("notes") != NOTE:
            raise OperationError("Operator already has an unowned grant; no adoption or overwrite")
        grant = existing_grant or api.request(
            "POST",
            "/entitlements",
            {
                "subject": subject,
                "resource": resource,
                "costCenterId": center["id"],
                "enabled": True,
                "notes": NOTE,
                "enforcement": {
                    "tokens": {
                        "counterKeyExpression": COUNTER,
                        "tokensPerMinute": 2000,
                        "tokenQuota": 10000,
                        "tokenQuotaPeriod": "Monthly",
                    },
                    "requests": {
                        "counterKeyExpression": COUNTER,
                        "calls": 4,
                        "renewalPeriodSeconds": 60,
                        "callQuota": 10,
                        "callQuotaPeriod": "Monthly",
                    },
                },
            },
        )
        owned = {"id": grant["id"], "publicationId": model["publicationId"]}
        if owned not in ledger["grants"]:
            ledger["grants"].append(owned)
            operator.remember("verificationAccess", ledger)
        api.apply("publications", {"id": model["publicationId"]})
        connection = api.request("GET", f"/entitlements/{grant['id']}/connection")
        targets.append(
            {
                "modelKey": key,
                "publicationId": model["publicationId"],
                "entitlementId": grant["id"],
                "connection": connection,
            }
        )
    operator.remember("verificationTargets", {"targets": targets})
    return targets


def cleanup(operator: Operator) -> None:
    operator.verify_target()
    ledger = operator.state["objects"].get("verificationAccess")
    if not ledger or ledger.get("cleanedUp"):
        return
    api = Mosaic(operator)
    for item in list(ledger["grants"]):
        grant = api.request("GET", f"/entitlements/{item['id']}")
        if grant.get("notes") != NOTE or grant["subject"]["id"] != ledger["principalId"]:
            raise OperationError("Verification grant ownership changed")
        if grant["enabled"]:
            api.request("PATCH", f"/entitlements/{item['id']}", {"enabled": False})
        api.apply("publications", {"id": item["publicationId"]})
        api.request("DELETE", f"/entitlements/{item['id']}")
        ledger["grants"].remove(item)
        operator.remember("verificationAccess", ledger)
    if ledger["principalCreated"]:
        remaining = api.request("GET", "/entitlements")
        if any(row["subject"]["id"] == ledger["principalId"] for row in remaining):
            raise OperationError("Verification principal gained unrelated grants; preserve it")
        references = [
            grant
            for publication in api.request("GET", "/publications")
            for grant in (publication.get("appliedAccess") or {}).get("grants", [])
            if grant["subject"]["id"] == ledger["principalId"]
        ]
        if any(grant["enabled"] for grant in references):
            raise OperationError("Verification principal still has applied runtime access")
        if references:
            ledger["principalRetained"] = "disabled applied-snapshot audit references"
            logging.getLogger(__name__).warning(
                "Keeping the ungranted operator record: disabled applied snapshots retain its "
                "audit references. No publication is unpublished to erase that history."
            )
        else:
            api.request("DELETE", f"/principals/{ledger['principalId']}")
    operator.remember("verificationAccess", ledger | {"cleanedUp": True})
