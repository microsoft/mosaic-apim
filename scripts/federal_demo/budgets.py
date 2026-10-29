"""Deployment-scoped prices and budgets; live metering evidence is an explicit prerequisite."""

import hashlib
import json

from scripts.federal_demo.estate import TEAMS
from scripts.federal_demo.governance import Mosaic
from scripts.federal_demo.operator import OperationError, Operator
from scripts.federal_demo.pricing import fetch_quote


def configure(operator: Operator) -> None:
    operator.verify_target()
    proof = operator.state["objects"].get("modelVerification", {})
    models = {
        key: value for key, value in operator.state["objects"].items() if key.startswith("model:")
    }
    if len(models) != 5 or set(proof.get("models", {})) != set(models):
        raise OperationError("All five exact model offerings need live verification before budgets")
    api = Mosaic(operator)
    for key, model in models.items():
        evidence = proof["models"][key]
        publication = api.request("GET", f"/publications/{model['publicationId']}")
        if (
            evidence.get("publicationAppliedAt") != publication["lastAppliedAt"]
            or evidence.get("acceptedStatus") != 200
            or evidence.get("gatewayContextLimitProven") is not True
        ):
            raise OperationError(
                "Model/context evidence is missing or stale; budgets are not ready"
            )
        binding = next(
            item
            for item in operator.target["endpoints"]
            if item["environment"] == key.split(":")[1]
            and item["deploymentName"] == model["deploymentName"]
        )
        deployment = operator.request(
            "GET",
            "https://management.azure.com"
            + binding["resourceId"]
            + "/deployments/"
            + model["deploymentName"]
            + "?api-version=2024-10-01",
            "https://management.azure.com/",
        )
        metadata = deployment["properties"]["model"]
        quote = fetch_quote(metadata["name"], deployment["sku"]["name"])
        payload = {
            "cloud": "commercial",
            "publisher": metadata["format"],
            "model": metadata["name"],
            "version": metadata["version"],
            "deploymentType": deployment["sku"]["name"],
            "deployment": model["endpointId"] + "/" + model["deploymentName"],
            "inputPerMillion": quote["inputPerMillion"],
            "outputPerMillion": quote["outputPerMillion"],
            "cachedInputPerMillion": quote["cachedInputPerMillion"],
            "effectiveFrom": quote["effectiveFrom"],
            "sourceUrl": quote["sourceUrl"],
            "note": "Federal demo only: verified bounded text operation/context envelope. "
            "Full input list-price estimate; not an invoice.",
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        prior = operator.state["objects"].get(f"price:{key}", {})
        if prior.get("payloadSha256") != digest:
            price = api.request("POST", "/pricing/prices", payload)
            operator.remember(f"price:{key}", {"id": price["id"], "payloadSha256": digest})
    centers = {row["code"]: row for row in api.request("GET", "/cost-centers")}
    for team in TEAMS:
        api.request(
            "PUT",
            f"/cost-centers/{centers[team.code]['id']}/budget",
            {
                "amount": team.budget,
                "thresholds": [80, 100],
                "recipients": [],
                "notifyOwners": False,
                "action": "block",
            },
        )
    api.request(
        "PUT",
        "/budgets/organization",
        {
            "amount": sum(team.budget for team in TEAMS),
            "thresholds": [80, 100],
            "recipients": [],
            "action": "continue",
        },
    )
    operator.remember(
        "budgetConfiguration", {"centers": 6, "monthlyUsd": 600, "gatewayBlockingVerified": False}
    )
