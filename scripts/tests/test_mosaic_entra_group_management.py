import json
from contextlib import redirect_stderr
from io import StringIO
from typing import Any
from unittest import mock

import pytest

from scripts import mosaic_entra

BASELINE = ("User.ReadBasic.All", "GroupMember.Read.All", "AgentIdentity.Read.All")


@pytest.mark.parametrize(
    ("options", "extra"),
    [
        ({}, ()),
        ({"grant_group_create": True}, ("Group.Create",)),
        ({"grant_group_member_write": True}, ("GroupMember.ReadWrite.All",)),
        (
            {"grant_group_member_write": True, "grant_service_principal_member_write": True},
            ("GroupMember.ReadWrite.All", "Application.ReadWrite.All"),
        ),
        (
            {
                "grant_group_create": True,
                "grant_group_member_write": True,
                "grant_service_principal_member_write": True,
            },
            ("Group.Create", "GroupMember.ReadWrite.All", "Application.ReadWrite.All"),
        ),
    ],
)
def test_generated_graph_assignments_only_add_explicitly_selected_permissions(
    options: dict[str, bool],
    extra: tuple[str, ...],
) -> None:
    offered = (*BASELINE, *mosaic_entra.GRAPH_OPTIONAL_APPLICATION_PERMISSION_VALUES)
    graph_sp = {
        "id": "fictional-graph-sp",
        "appRoles": [
            {
                "id": f"fictional-role-{value}",
                "value": value,
                "allowedMemberTypes": ["Application"],
                "isEnabled": True,
            }
            for value in offered
        ],
    }
    runner = mock.Mock()
    runner.az_json.side_effect = [
        {"value": [graph_sp]},
        {"value": []},
        *({} for _ in range(len(BASELINE) + len(extra))),
    ]
    with redirect_stderr(StringIO()):
        mosaic_entra.ensure_api_managed_identity_graph_permissions(
            runner, managed_identity_principal_id="fictional-api-sp", **options
        )
    posts = [
        json.loads(call.args[0][call.args[0].index("--body") + 1])
        for call in runner.az_json.call_args_list
        if call.args[0][:3] == ["rest", "--method", "POST"]
    ]
    assert mosaic_entra.GRAPH_APPLICATION_PERMISSION_VALUES == BASELINE
    assert mosaic_entra.graph_application_permission_values(**options) == (*BASELINE, *extra)
    assert [post["appRoleId"] for post in posts] == [
        f"fictional-role-{value}" for value in (*BASELINE, *extra)
    ]
    assert all(
        post["principalId"] == "fictional-api-sp" and post["resourceId"] == "fictional-graph-sp"
        for post in posts
    )


def test_advanced_consent_requires_separate_membership_opt_in_without_any_cli_calls() -> None:
    runner = mock.Mock()
    with pytest.raises(mosaic_entra.OperationFailed):
        mosaic_entra.ensure_api_managed_identity_graph_permissions(
            runner,
            managed_identity_principal_id="fictional-api-sp",
            grant_service_principal_member_write=True,
        )
    runner.az_json.assert_not_called()


@pytest.mark.parametrize(
    "arguments",
    [
        ["preprovision", "--grant-group-create"],
        ["preprovision", "--grant-group-member-write"],
        ["postprovision", "--grant-service-principal-member-write"],
    ],
)
def test_invalid_write_switch_combinations_are_rejected(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        mosaic_entra.parse_args(arguments)
    assert error.value.code == 2


def test_default_cli_keeps_all_writes_opt_out() -> None:
    options = mosaic_entra.parse_args(["postprovision"])
    assert not options.grant_group_create
    assert not options.grant_group_member_write
    assert not options.grant_service_principal_member_write


def test_explicit_cli_switches_reach_postprovision_but_no_bootstrap_is_executed() -> None:
    with (
        mock.patch.object(mosaic_entra, "postprovision") as postprovision,
        mock.patch.object(mosaic_entra, "preprovision") as preprovision,
        mock.patch.object(mosaic_entra, "CliRunner") as runner,
    ):
        assert (
            mosaic_entra.main(
                [
                    "postprovision",
                    "--dry-run",
                    "--grant-group-create",
                    "--grant-group-member-write",
                    "--grant-service-principal-member-write",
                ]
            )
            == 0
        )
    preprovision.assert_not_called()
    runner.assert_called_once_with(dry_run=True)
    postprovision.assert_called_once_with(
        runner.return_value,
        None,
        None,
        grant_group_create=True,
        grant_group_member_write=True,
        grant_service_principal_member_write=True,
    )


def test_optional_permission_denial_names_the_actual_missing_write_consent() -> None:
    runner = mock.Mock()
    values = (*BASELINE, "Group.Create")
    graph_sp: dict[str, Any] = {
        "id": "fictional-graph-sp",
        "appRoles": [
            {
                "id": f"role-{value}",
                "value": value,
                "allowedMemberTypes": ["Application"],
                "isEnabled": True,
            }
            for value in values
        ],
    }
    runner.az_json.side_effect = [
        {"value": [graph_sp]},
        {
            "value": [
                {"resourceId": "fictional-graph-sp", "appRoleId": f"role-{value}"}
                for value in BASELINE
            ]
        },
        mosaic_entra.OperationFailed("Authorization_RequestDenied: Insufficient privileges"),
    ]
    output = StringIO()
    with redirect_stderr(output):
        mosaic_entra.ensure_api_managed_identity_graph_permissions(
            runner, managed_identity_principal_id="fictional-api-sp", grant_group_create=True
        )
    warning = output.getvalue()
    assert "Missing permissions: Group.Create" in warning
    assert "tenant-wide" in warning
    assert "directory lookup will fail" not in warning
