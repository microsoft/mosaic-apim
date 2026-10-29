"""Run with python -m scripts.federal_demo; all writes require an explicit --apply."""

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlencode

from scripts.federal_demo import (
    audiences,
    budgets,
    consent,
    deployment,
    directory,
    governance,
    jobs,
    pricing,
    runtime,
    verification,
)
from scripts.federal_demo.estate import inventory
from scripts.federal_demo.operator import OperationError, Operator


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "plan",
            "preflight",
            "prices",
            "directory",
            "audiences",
            "seed-models",
            "seed-directory",
            "seed-tools",
            "operator-consent",
            "operator-model-consent",
            "prepare",
            "bind",
            "bind-core",
            "what-if",
            "authorize-runtime",
            "configure-runtime",
            "seed-jobs",
            "verify-backends",
            "verify-wrong-client",
            "run-jobs",
            "configure-budgets",
            "prepare-runtime",
            "prepare-verified-runtime",
            "prepare-inference",
            "bind-runtime",
            "prepare-model-verification",
            "verify-models",
            "cleanup-model-verification",
        ),
    )
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--invoke-paid", action="store_true")
    args = parser.parse_args(argv)
    try:
        operator = Operator(json.loads(args.target.read_text()), args.state)
        if args.command == "plan":
            print(json.dumps(inventory(), indent=2))
        elif args.command == "prices":
            profiles = (
                ("gpt-5.6-luna", "GlobalStandard"),
                ("gpt-5.6-luna", "DataZoneStandard"),
                ("gpt-5.6-terra", "GlobalStandard"),
                ("gpt-5.1", "DataZoneStandard"),
            )
            print(
                json.dumps(
                    {
                        "quotes": [pricing.fetch_quote(model, sku) for model, sku in profiles],
                        "budgetReadiness": "requires live metering and context verification",
                        "cloudWritesPerformed": False,
                    },
                    indent=2,
                )
            )
        elif args.command == "what-if":
            counts = deployment.plan(
                operator,
                args.target.with_name("federal-parameters.local.json"),
                args.target.with_name("federal-whatif.local.json"),
            )
            print(json.dumps({"changeCounts": counts, "cloudWritesPerformed": False}))
        elif args.command == "preflight":
            operator.verify_target()
            domains = operator.items("/domains?$select=id,isVerified")
            if not any(
                domain["id"] == operator.target["domain"] and domain["isVerified"]
                for domain in domains
            ):
                raise OperationError("The requested UPN domain is not verified")
            prefix = f"/subscriptions/{operator.target['subscriptionId']}"
            query = urlencode({"api-version": "2023-05-01"})
            usage = operator.request(
                "GET",
                f"https://management.azure.com{prefix}/providers/"
                f"Microsoft.CognitiveServices/locations/eastus2/usages?{query}",
                "https://management.azure.com/",
            )
            quotas = [
                item
                for item in usage["value"]
                if item["name"]["value"]
                in (
                    "OpenAI.GlobalStandard.gpt-5.6-luna",
                    "OpenAI.DataZoneStandard.gpt-5.1",
                )
            ]
            requirements = {
                "OpenAI.GlobalStandard.gpt-5.6-luna": 20,
                "OpenAI.DataZoneStandard.gpt-5.1": 10,
            }
            for name, needed in requirements.items():
                quota = next((q for q in quotas if q["name"]["value"] == name), None)
                if quota is None or quota["limit"] - quota["currentValue"] < needed:
                    raise OperationError(f"Insufficient live native quota for {name}")
            print(
                json.dumps(
                    {
                        "targetVerified": True,
                        "domainVerified": True,
                        "quotas": quotas,
                        "cloudWritesPerformed": False,
                    },
                    indent=2,
                )
            )
        elif not args.apply:
            raise OperationError("Writes require --apply after reviewing the plan")
        elif args.command == "directory":
            directory.provision(operator)
            print("Owned demo users and group memberships reconciled; passwords were not printed.")
        elif args.command == "seed-models":
            governance.seed_models(operator)
            print("Model governance applied through MOSAIC; budget readiness is a separate gate.")
        elif args.command == "seed-directory":
            governance.seed_directory(operator)
            print("Six cost centers and the exact demo identity roster registered through MOSAIC.")
        elif args.command == "audiences":
            audiences.provision(operator)
            print(
                "Owned backend audiences reconciled; only configured matching identities admitted."
            )
        elif args.command == "seed-tools":
            governance.seed_tools(operator)
            print("MCP governance applied; verify runtime roles and real tool calls separately.")
        elif args.command == "operator-consent":
            consent.provision(operator)
            print("Owned MOSAIC API delegated access configured for the current operator.")
        elif args.command == "operator-model-consent":
            consent.provision(operator, for_model_verification=True)
            print(
                "Temporary operator model-verification consent configured; no model grant implied."
            )
        elif args.command == "prepare":
            destination = args.target.with_name("federal-parameters.local.json")
            deployment.prepare(operator, destination)
            print(f"Identity bootstrap complete; private ARM parameters written to {destination}.")
        elif args.command == "bind":
            deployment.bind(operator, args.target)
            print("Actual deployment outputs bound; redirect/Graph setup reconciled.")
        elif args.command == "bind-core":
            deployment.bind_core(operator, args.target)
            print("Ready core identity and operator-only onboarding vault bound.")
        elif args.command == "authorize-runtime":
            runtime.authorize(operator)
            print("Verified owned runtime identities received their exact application roles.")
        elif args.command == "configure-runtime":
            deployment.configure_runtime(
                operator, args.target.with_name("federal-parameters.local.json")
            )
            print("Private runtime parameters configured; inference remains disabled.")
        elif args.command == "seed-jobs":
            governance.seed_jobs(operator)
            print("Exact Production workload grants applied.")
        elif args.command == "verify-backends":
            runtime.verify_backends(operator)
            print("Owned backend configuration, anonymous and wrong-audience refusals verified.")
        elif args.command == "verify-wrong-client":
            jobs.verify_wrong_client(operator)
            print("All 24 valid-token wrong-client calls rejected; temporary permissions removed.")
        elif args.command == "run-jobs":
            for binding in operator.target["jobBindings"]:
                jobs.run(operator, binding)
                print(f"{binding['key']}: completed with actual runtime evidence.")
        elif args.command == "configure-budgets":
            budgets.configure(operator)
            print(
                "Verified deployment prices and six budgets configured; blocking proof is separate."
            )
        elif args.command in ("prepare-runtime", "prepare-verified-runtime", "prepare-inference"):
            result = deployment.stage_parameters(
                operator,
                args.target,
                inference=args.command == "prepare-inference",
                verified=args.command == "prepare-verified-runtime",
            )
            print(f"Scoped stage parameters prepared: {result}")
        elif args.command == "bind-runtime":
            deployment.bind_runtime(operator, args.target)
            print("Actual protected runtime outputs bound.")
        elif args.command == "prepare-model-verification":
            verification.prepare(operator)
            print("Temporary real-operator grants prepared; no paid inference invoked.")
        elif args.command == "verify-models":
            if not args.invoke_paid:
                raise OperationError("Paid verification requires --invoke-paid")
            from scripts.federal_demo.verify_models import verify

            def token(scope):
                from scripts.federal_demo.operator import command

                result = command(
                    [
                        "az",
                        "account",
                        "get-access-token",
                        "--tenant",
                        operator.target["tenantId"],
                        "--scope",
                        scope,
                        "--output",
                        "json",
                    ]
                )
                return result["accessToken"]

            targets = operator.state["objects"]["verificationTargets"]["targets"]
            verify(operator, targets, token)
            budgets.configure(operator)
            print(
                "All five real model/context checks passed and "
                "deployment prices/budgets configured."
            )
        elif args.command == "cleanup-model-verification":
            verification.cleanup(operator)
            consent.cleanup_model_verification(operator)
            print("Temporary model-verification grants and consent retired.")
    except (OperationError, ValueError, KeyError, OSError) as error:
        print(f"Federal demo failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
