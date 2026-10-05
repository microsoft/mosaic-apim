"""deploy.py against a fake Azure CLI: what each command runs, and what it never runs."""

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

import deploy
from support import OUTPUTS, SUBSCRIPTION, FakeAz, kit, values

EXAMPLE = Path(deploy.__file__).with_name("parameters.example.json")


def run(command: str, fake: FakeAz, **options: Any) -> list[str]:
    lines: list[str] = []
    az = deploy.Az(dry_run=options.pop("dry_run", False), runner=fake, say=lines.append)
    if command == "plan":
        deploy.plan(kit(), az, lines.append)
    elif command == "deploy":
        deploy.deploy(kit(), az, lines.append, skip_smoke=True, sleep=lambda seconds: None)
    else:
        deploy.teardown(kit(), az, lines.append, **options)
    return lines


def test_the_example_parameters_must_be_filled_in_first() -> None:
    with pytest.raises(deploy.KitError, match="Replace the placeholder in subscriptionId"):
        deploy.Kit.load(EXAMPLE)


def test_the_example_names_every_parameter_the_kit_reads() -> None:
    example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert set(example) == set(values())
    assert set(example["protected"]) == set(values()["protected"])
    assert set(example["agent"]) == set(values()["agent"])


def test_valid_parameters_become_the_template_s_parameters() -> None:
    parameters = kit().arm_parameters("abc123", deploy_servers=False)["parameters"]
    assert {name: item["value"] for name, item in parameters.items()} == {
        "resourceGroupName": "rg-mcp-test-servers",
        "location": "eastus2",
        "namePrefix": "mosaic-mcp",
        "logAnalyticsWorkspaceId": values()["logAnalyticsWorkspaceId"],
        "containerRegistryId": values()["containerRegistryId"],
        "imageTag": "abc123",
        "deployServers": False,
        "tenantId": "22222222-2222-2222-2222-222222222222",
        "protectedAudienceClientId": "33333333-3333-3333-3333-333333333333",
        "protectedAudienceAppIdUri": "api://33333333-3333-3333-3333-333333333333",
        "gatewayClientId": "44444444-4444-4444-4444-444444444444",
        "mosaicApiClientId": "55555555-5555-5555-5555-555555555555",
        "modelEndpoint": "https://gateway.example.test/models/chat",
        "modelDeployment": "gpt-test",
        "modelApiVersion": "2024-10-21",
        "modelRuntimeClientId": "66666666-6666-6666-6666-666666666666",
        "modelCostCenter": "research",
        "modelTokenParameter": "max_tokens",
        "modelMaxTokens": 16,
        "minReplicas": 1,
    }


def test_the_audience_uri_defaults_to_api_and_the_audience_client_id() -> None:
    assert kit(protected__audienceAppIdUri="").audience_app_id_uri == (
        "api://33333333-3333-3333-3333-333333333333"
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"resourceGroup": "rg-mosaic"}, "dedicated resource group"),
        ({"resourceGroup": "RG-MOSAIC"}, "dedicated resource group"),
        ({"subscriptionId": "not-a-guid"}, "subscriptionId must be a GUID"),
        ({"namePrefix": "Mosaic_MCP"}, "namePrefix"),
        ({"namePrefix": "a-very-long-name-prefix"}, "namePrefix"),
        (
            {"containerRegistryId": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg/x/y/a"},
            "containerRegistryId",
        ),
        ({"logAnalyticsWorkspaceId": "log-mosaic"}, "logAnalyticsWorkspaceId"),
        ({"imageTag": "tag with spaces"}, "imageTag"),
        ({"minReplicas": 2}, "minReplicas"),
        ({"protected__gatewayClientId": "gateway"}, "protected.gatewayClientId must be a GUID"),
        ({"protected__mosaicApiClientId": ""}, "protected.mosaicApiClientId must be a GUID"),
        ({"protected__audienceAppIdUri": "audience"}, "audienceAppIdUri"),
        ({"agent__modelEndpoint": "http://gateway.example.test/models"}, "modelEndpoint"),
        ({"agent__modelEndpoint": "https://gateway.example.test/models?x=1"}, "modelEndpoint"),
        ({"agent__modelDeployment": "gpt test"}, "agent.modelDeployment"),
        ({"agent__modelRuntimeClientId": "runtime"}, "agent.modelRuntimeClientId must be a GUID"),
        ({"agent__costCenter": "two words"}, "agent.costCenter"),
        ({"agent__tokenParameter": "max_output_tokens"}, "agent.tokenParameter"),
        ({"agent__maxTokens": 1000}, "agent.maxTokens"),
        ({"agent__maxTokens": True}, "agent.maxTokens"),
        ({"location": "<region>"}, "Replace the placeholder in location"),
    ],
)
def test_invalid_parameters_are_refused_before_anything_runs(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(deploy.KitError, match=message):
        kit(**changes)


def test_plan_changes_nothing_and_lists_every_resource_it_would_create() -> None:
    fake = FakeAz()
    lines = run("plan", fake)

    assert fake.changes == []
    [what_if] = [call for call in fake.calls if call[:3] == ["deployment", "sub", "what-if"]]
    assert what_if[what_if.index("--result-format") + 1] == "ResourceIdOnly"
    text = "\n".join(lines)
    for resource_type, name in deploy.planned_resources(kit()):
        assert f"{resource_type:<58} {name}" in text
    assert "mosaic-mcp/m-tools:test-tag" in text
    assert "mosaic-mcp/m-agent:test-tag" in text
    assert lines[-1] == "Plan changed nothing."


def test_plan_carries_on_when_what_if_can_t_preview_the_servers() -> None:
    class NoWhatIf(FakeAz):
        def __call__(self, args: Any, capture: bool) -> Any:
            if list(args[:3]) == ["deployment", "sub", "what-if"]:
                raise deploy.KitError("ResourceGroupNotFound")
            return super().__call__(args, capture)

    lines = run("plan", NoWhatIf())
    assert any(line.startswith("What-if didn't finish") for line in lines)
    assert lines[-1] == "Plan changed nothing."


@pytest.mark.parametrize("command", ["plan", "deploy"])
def test_a_resource_group_the_kit_didn_t_create_is_refused(command: str) -> None:
    fake = FakeAz(group="foreign")
    with pytest.raises(deploy.KitError, match="wasn't created by this kit"):
        run(command, fake)
    assert fake.changes == []


def test_deploy_grants_pull_builds_the_images_then_deploys_the_servers_and_code() -> None:
    fake = FakeAz()
    lines = run("deploy", fake)

    steps = [call[:4] for call in fake.changes]
    assert steps == [
        ["deployment", "sub", "create", "--subscription"],
        ["acr", "build", "--subscription", SUBSCRIPTION],
        ["acr", "build", "--subscription", SUBSCRIPTION],
        ["deployment", "sub", "create", "--subscription"],
        ["functionapp", "deployment", "source", "config-zip"],
    ]
    assert fake.deployed_servers == [False, True]
    builds = [call for call in fake.changes if call[:2] == ["acr", "build"]]
    assert [call[call.index("--image") + 1] for call in builds] == [
        "mosaic-mcp/m-tools:test-tag",
        "mosaic-mcp/m-agent:test-tag",
    ]
    assert all(call[call.index("--registry") + 1] == "crmosaic" for call in builds)
    [code] = [call for call in fake.changes if "config-zip" in call]
    assert code[code.index("--name") + 1] == "mosaic-mcp-protected-x"
    assert code[code.index("--build-remote") + 1] == "true"
    text = "\n".join(lines)
    assert OUTPUTS["agentPrincipalId"] in text
    assert OUTPUTS["protectedUrl"] in text


def test_deploy_tries_the_servers_again_once_when_the_first_attempt_fails() -> None:
    fake = FakeAz(failures=1)
    lines = run("deploy", fake)
    assert fake.deployed_servers == [False, True]
    assert any(line.startswith("The deployment failed (revision failed)") for line in lines)


def test_deploy_stops_when_the_second_attempt_fails_too() -> None:
    with pytest.raises(deploy.KitError, match="revision failed"):
        run("deploy", FakeAz(failures=2))


def test_teardown_dry_run_changes_nothing_and_lists_everything_it_would_delete() -> None:
    fake = FakeAz(group="kit", outputs=OUTPUTS)
    lines = run("teardown", fake, dry_run=True)

    assert fake.changes == []
    would = [line for line in lines if line.startswith("Would run: az ")]
    group = f"--subscription {SUBSCRIPTION} --name rg-mcp-test-servers --yes"
    record = (
        f"--subscription {SUBSCRIPTION} --resource-group rg-mosaic --name mosaic-mcp-registry-pull"
    )
    servers = f"--subscription {SUBSCRIPTION} --name mosaic-mcp-servers"
    assert would == [
        f"Would run: az role assignment delete --ids {OUTPUTS['acrPullRoleAssignmentId']}",
        f"Would run: az group delete {group}",
        f"Would run: az deployment group delete {record}",
        f"Would run: az deployment sub delete {servers}",
    ]
    assert lines[-1] == "Teardown changed nothing."


def test_teardown_removes_the_grant_the_group_and_the_records_and_says_how_to_delete_images() -> (
    None
):
    fake = FakeAz(group="kit", outputs=OUTPUTS)
    lines = run("teardown", fake)

    assert [call[:3] for call in fake.changes] == [
        ["role", "assignment", "delete"],
        ["group", "delete", "--subscription"],
        ["deployment", "group", "delete"],
        ["deployment", "sub", "delete"],
    ]
    assert fake.changes[0][-1] == OUTPUTS["acrPullRoleAssignmentId"]
    assert (
        f"  az acr repository delete --subscription {SUBSCRIPTION} --name crmosaic "
        "--repository mosaic-mcp/m-tools --yes"
    ) in lines
    assert (
        f"  az acr repository delete --subscription {SUBSCRIPTION} --name crmosaic "
        "--repository mosaic-mcp/m-agent --yes"
    ) in lines
    assert lines[-1] == "Teardown is done."


def test_teardown_can_delete_the_image_repositories_too() -> None:
    fake = FakeAz(group="kit", outputs=OUTPUTS)
    run("teardown", fake, delete_images=True)
    deleted = [call for call in fake.changes if call[:3] == ["acr", "repository", "delete"]]
    assert [call[call.index("--repository") + 1] for call in deleted] == [
        "mosaic-mcp/m-tools",
        "mosaic-mcp/m-agent",
    ]


def test_teardown_finds_the_pull_grant_when_the_deployment_record_is_gone() -> None:
    fake = FakeAz(group="kit", outputs=None, records=False)
    run("teardown", fake)
    assert fake.changes[0] == [
        "role",
        "assignment",
        "delete",
        "--ids",
        OUTPUTS["acrPullRoleAssignmentId"],
    ]
    assert [call[:3] for call in fake.changes] == [
        ["role", "assignment", "delete"],
        ["group", "delete", "--subscription"],
    ]


def test_teardown_won_t_touch_a_resource_group_the_kit_didn_t_create() -> None:
    fake = FakeAz(group="foreign", outputs=OUTPUTS)
    with pytest.raises(deploy.KitError, match="teardown won't touch it"):
        run("teardown", fake)
    assert fake.changes == []


def test_the_package_holds_exactly_what_m_protected_runs(tmp_path: Path) -> None:
    with zipfile.ZipFile(deploy.build_protected_package(tmp_path)) as package:
        assert sorted(package.namelist()) == sorted(deploy.PROTECTED_FILES)
    modules = {path.name for path in deploy.PROTECTED.glob("*.py")}
    assert modules <= set(deploy.PROTECTED_FILES)


def test_deploy_dry_run_is_the_plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    parameters = tmp_path / "parameters.json"
    parameters.write_text(json.dumps(values()), encoding="utf-8")
    fake = FakeAz()
    monkeypatch.setattr(deploy, "run_az", fake)
    assert deploy.main(["deploy", "--dry-run", "--parameters", str(parameters)]) == 0
    assert fake.changes == []
    assert any(call[:3] == ["deployment", "sub", "what-if"] for call in fake.calls)


def test_a_missing_parameters_file_says_how_to_make_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert deploy.main(["plan", "--parameters", str(tmp_path / "missing.json")]) == 2
    assert "Copy parameters.example.json" in capsys.readouterr().err


def test_outputs_name_what_each_value_is_for(capsys: pytest.CaptureFixture[str]) -> None:
    deploy.print_outputs(OUTPUTS, print)
    text = capsys.readouterr().out
    for key, label, note in deploy.OUTPUT_LINES:
        assert label in text and note in text and OUTPUTS[key] in text
