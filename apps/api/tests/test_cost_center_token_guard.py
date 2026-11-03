"""Compiler contracts for center membership, not live Entra/APIM token conformance."""

import xml.etree.ElementTree as ET
from typing import Literal

import pytest
from apim_double import expression_error, policy_expression_error
from mosaic_api.domain import EntitlementSubject, EntitlementSubjectKind, ModelAccessSettings
from mosaic_api.integrations.access_policy import _token_lookup
from pydantic import ValidationError
from test_access_policy import _fragment, _grant, _group_grant, _publication, _snapshot
from test_access_policy import _variable_values as _values
from test_mcp_access_policy import _fragment as _mcp_fragment
from test_mcp_access_policy import _grant as _mcp_grant
from test_mcp_access_policy import _snapshot as _mcp_snapshot
from test_pool_policy_governed import GENERAL, RESEARCH
from test_pool_policy_governed import _fragment as _pool_fragment
from test_pool_policy_governed import _grant as _pool_grant
from test_pool_policy_governed import _snapshot as _pool_snapshot

GROUP = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_GROUP = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
Kind = Literal["model", "pool", "mcp"]


def _compiled(kind: Kind, group: str | None) -> ET.Element:
    if kind == "pool":
        return _pool_fragment(
            _pool_snapshot(
                _pool_grant(
                    1,
                    cost_center=RESEARCH if group else GENERAL,
                    required_cost_center_group_id=group,
                )
            )
        )
    fields = {
        "cost_center_id": "center-research" if group else "center-general",
        "cost_center_code": "research" if group else "general",
        "required_cost_center_group_id": group,
    }
    if kind == "mcp":
        return _mcp_fragment(_mcp_snapshot(grants=[_mcp_grant(**fields)]))
    return _fragment(_snapshot(grants=[_grant(**fields)]))


@pytest.mark.parametrize("kind", ["model", "pool", "mcp"])
def test_individual_token_grants_require_the_centers_group(kind: Kind) -> None:
    fragment = _compiled(kind, GROUP.upper())
    lookup = _values(fragment, "mosaic-token-match")[-1]

    assert "var costCenterMember0 = false;" in lookup
    assert 'jwt.Claims.ContainsKey("groups") ? jwt.Claims["groups"] : null' in lookup
    assert "if (groups != null) {" in lookup
    assert "foreach (var centerGroup in groups) {" in lookup
    assert (
        f'if (String.Equals(centerGroup, "{GROUP}", StringComparison.OrdinalIgnoreCase)) '
        "{ costCenterMember0 = true; }"
    ) in lookup
    assert "if (delegated && costCenterMember0 && String.Equals(oid," in lookup
    assert expression_error(lookup) is None
    assert policy_expression_error(ET.tostring(fragment, encoding="unicode")) is None
    assert fragment.find(".//send-request") is None
    assert any("hasgroups" in condition.attrib["condition"] for condition in fragment.iter("when"))


@pytest.mark.parametrize("kind", ["model", "pool", "mcp"])
def test_unguarded_general_grants_do_not_require_group_claims(kind: Kind) -> None:
    fragment = _compiled(kind, None)
    lookup = _values(fragment, "mosaic-token-match")[-1]

    assert "costCenterMember" not in lookup
    assert 'jwt.Claims.ContainsKey("groups")' not in lookup
    assert "if (delegated && String.Equals(oid," in lookup
    assert not any(
        "hasgroups" in condition.attrib["condition"] for condition in fragment.iter("when")
    )


def test_application_grants_require_both_invocation_role_and_center_membership() -> None:
    grant = _grant(
        subject=EntitlementSubject(kind=EntitlementSubjectKind.APPLICATION, id="application"),
        required_cost_center_group_id=GROUP,
    )
    lookup = _token_lookup(_publication(), [grant])

    assert '"Models.Invoke.Application"' in lookup
    assert "if (application && costCenterMember0 && String.Equals(oid," in lookup
    assert expression_error(lookup) is None


def test_a_subgroup_grant_cannot_bypass_primary_center_membership() -> None:
    grant = _group_grant(object_id=OTHER_GROUP, required_cost_center_group_id=GROUP)
    lookup = _token_lookup(_publication(), [grant])

    assert (
        f'if (String.Equals(centerGroup, "{GROUP}", StringComparison.OrdinalIgnoreCase))'
    ) in lookup
    assert f'if (costCenterMember0 && String.Equals(group, "{OTHER_GROUP}",' in lookup
    assert expression_error(lookup) is None


def test_one_claim_scan_serves_multiple_grants_under_the_same_group() -> None:
    lookup = _token_lookup(
        _publication(),
        [
            _grant(1, required_cost_center_group_id=GROUP),
            _grant(2, required_cost_center_group_id=GROUP.upper()),
        ],
    )

    assert lookup.count("var costCenterMember0 = false;") == 1
    assert "costCenterMember1" not in lookup
    assert lookup.count("foreach (var centerGroup in groups)") == 1
    assert lookup.count("if (delegated && costCenterMember0 &&") == 2


@pytest.mark.parametrize("kind", ["model", "pool"])
def test_key_matching_does_not_depend_on_jwt_group_claims(kind: Kind) -> None:
    fragment = _compiled(kind, GROUP)
    lookup = _values(fragment, "mosaic-key-match")[-1]

    assert "context.Subscription" in lookup
    assert "costCenterMember" not in lookup
    assert "jwt.Claims" not in lookup
    assert "return " in lookup


def test_key_only_publication_does_not_add_a_token_guard() -> None:
    fragment = _fragment(
        _snapshot(
            settings=ModelAccessSettings(keys_enabled=True, entra_enabled=False),
            grants=[_grant(required_cost_center_group_id=GROUP)],
        )
    )

    assert "costCenterMember" not in ET.tostring(fragment, encoding="unicode")
    assert fragment.find(".//validate-azure-ad-token") is None
    assert _values(fragment, "mosaic-key-match")[-1]


@pytest.mark.parametrize("kind", ["model", "pool", "mcp"])
@pytest.mark.parametrize("invalid", ["", "not-a-group-id", "{{group}}"])
def test_group_requirement_is_a_guid_not_a_name_or_expression(kind: Kind, invalid: str) -> None:
    with pytest.raises(ValidationError):
        _compiled(kind, invalid)


def test_absent_group_requirement_does_not_change_old_snapshot_documents() -> None:
    assert "requiredCostCenterGroupId" not in _grant().model_dump(mode="json", by_alias=True)
    assert (
        _grant(required_cost_center_group_id=GROUP).model_dump(mode="json", by_alias=True)[
            "requiredCostCenterGroupId"
        ]
        == GROUP
    )
