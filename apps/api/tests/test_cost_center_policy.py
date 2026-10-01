"""Cost centers in the governed policies: header selection, fallback order, refusals and pools.

Compiler contract tests, like ``test_access_policy``. ``policy_snapshots/`` holds the rendered
fragments of a representative model and MCP publication. Set ``MOSAIC_UPDATE_POLICY_SNAPSHOTS=1``
to rewrite them after an intended policy change, and review the diff.
"""

import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from apim_double import expression_error, policy_expression_error
from mosaic_api.domain import (
    COST_CENTER_HEADER,
    AppliedCostCenterPool,
    EntitlementEnforcement,
    EntitlementSubject,
    EntitlementSubjectKind,
    McpAccessSnapshot,
    ModelAccessGrant,
    ModelAccessSettings,
    ModelAccessSnapshot,
)
from mosaic_api.errors import ValidationError
from mosaic_api.integrations.access_policy import (
    COST_CENTER_DENIED,
    COST_CENTER_KEYS_OFF_DENIED,
    COST_CENTER_MISMATCH_DENIED,
    COST_CENTER_REMAINING_QUOTA_TOKENS_HEADER,
    REMAINING_CALLS_HEADER,
    REMAINING_QUOTA_TOKENS_HEADER,
    REMAINING_TOKENS_HEADER,
    cost_center_counter_identity,
    grant_counter_identity,
    render_governed_policy,
)
from test_access_policy import (
    _denial_reasons,
    _fragment,
    _grant,
    _group_grant,
    _publication,
    _requests,
    _snapshot,
    _tokens,
    _variable_values,
)
from test_mcp_access_policy import _grant as _mcp_grant
from test_mcp_access_policy import _publication as _mcp_publication
from test_mcp_access_policy import _render as _mcp_render
from test_mcp_access_policy import _snapshot as _mcp_snapshot

SNAPSHOTS = Path(__file__).parent / "policy_snapshots"
GENERAL = ("costCenter-general", "general")
RESEARCH = ("costCenter-research", "Research")
SALES = ("costCenter-sales", "SALES.emea")
COST_CENTER_VARIABLE = '(string)context.Variables["mosaic-cost-center"]'


def _at(day: int) -> datetime:
    return datetime(2025, 1, day, tzinfo=UTC)


def _charged(cost_center: tuple[str, str], **values: object) -> dict[str, object]:
    return {"cost_center_id": cost_center[0], "cost_center_code": cost_center[1], **values}


def _same_person_grants() -> list[ModelAccessGrant]:
    """One person under three cost centers: Sales is their default, Research the oldest."""

    person = "00000001-3333-3333-3333-333333333333"
    return [
        _grant(1, object_id=person, **_charged(GENERAL, granted_at=_at(2))),
        _grant(
            2,
            object_id=person,
            subject=EntitlementSubject(kind=EntitlementSubjectKind.USER, id="principal-1"),
            **_charged(RESEARCH, granted_at=_at(1)),
        ),
        _grant(
            3,
            object_id=person,
            subject=EntitlementSubject(kind=EntitlementSubjectKind.USER, id="principal-1"),
            **_charged(SALES, granted_at=_at(3), default_cost_center=True),
        ),
    ]


def _token_returns(fragment: ET.Element) -> list[str]:
    """The grant identities the token lookup returns, in the order it tries them."""

    lookup = _variable_values(fragment, "mosaic-token-match")[-1]
    return re.findall(r'return "([0-9a-f]{64}\|[^"]*)";', lookup)


def _identity(grant: ModelAccessGrant) -> str:
    return grant_counter_identity(_publication(), grant)


def _rejection(fragment: ET.Element, reason: str) -> ET.Element:
    """The first refusal recording exactly this reason."""

    pattern = re.compile(rf"r={re.escape(reason)}(?![a-z-])")
    for when in fragment.iter("when"):
        children = list(when)
        for index, child in enumerate(children):
            if child.tag != "trace" or not pattern.search(child.findtext("message") or ""):
                continue
            if index + 1 < len(children) and children[index + 1].tag == "return-response":
                return when
    raise AssertionError(f"No refusal records {reason}")


def _status(when: ET.Element) -> tuple[str, str]:
    response = when.find("return-response")
    assert response is not None
    status = response.find("set-status")
    assert status is not None
    body = response.findtext("set-body") or ""
    return status.attrib["code"], body


def test_the_header_selects_among_the_callers_grants_without_case() -> None:
    fragment = _fragment(_snapshot(grants=_same_person_grants()))

    [header] = _variable_values(fragment, "mosaic-cost-center-header")
    assert f'ContainsKey("{COST_CENTER_HEADER}")' in header
    # Exactly one value, trimmed, matching the code pattern, then lowercased.
    assert "values.Length != 1" in header
    # Braces are escaped so API Management can't read them as a named value.
    assert 'Regex.IsMatch(code, "^[A-Za-z0-9._-]\\u007b1,64\\u007d$")' in header
    assert "return code.ToLowerInvariant();" in header
    lookup = _variable_values(fragment, "mosaic-token-match")[-1]
    for _, code in (GENERAL, RESEARCH, SALES):
        assert f'(cc == "" || cc == "{code.lower()}")' in lookup
    assert expression_error(header) is None
    assert expression_error(lookup) is None


def test_without_a_header_the_default_comes_first_then_the_oldest() -> None:
    general, research, sales = _same_person_grants()
    fragment = _fragment(_snapshot(grants=[general, research, sales]))

    assert _token_returns(fragment) == [
        f"{_identity(sales)}|sales.emea",
        f"{_identity(research)}|research",
        f"{_identity(general)}|general",
    ]


def test_group_grants_follow_every_direct_grant_by_precedence() -> None:
    generous = _group_grant(
        4, **_charged(RESEARCH), enforcement=EntitlementEnforcement(tokens=_tokens(
            tokens_per_minute=50000
        ))
    )
    unlimited = _group_grant(5, **_charged(GENERAL))
    direct = _grant(6, **_charged(GENERAL, granted_at=_at(9)))
    fragment = _fragment(_snapshot(grants=[generous, unlimited, direct]))

    returns = _token_returns(fragment)
    assert returns[0] == f"{_identity(direct)}|general"
    # An unlimited group grant outranks a limited one, whatever its cost center.
    assert returns[1:] == [f"{_identity(unlimited)}|general", f"{_identity(generous)}|research"]


def test_a_malformed_header_or_one_naming_no_grant_is_refused_with_403() -> None:
    fragment = _fragment(_snapshot(grants=_same_person_grants()))

    malformed = _rejection(fragment, "cost-center")
    assert malformed.attrib["condition"] == (
        '@((string)context.Variables["mosaic-cost-center-header"] == "!")'
    )
    assert _status(malformed) == ("403", COST_CENTER_DENIED)
    token_conditions = [
        when.attrib["condition"]
        for when in fragment.iter("when")
        if "r=cost-center " in "".join(
            trace.findtext("message") or "" for trace in when.findall("trace")
        )
    ]
    assert token_conditions == [
        '@(String.IsNullOrEmpty((string)context.Variables["mosaic-token-grant"])'
        ' && (string)context.Variables["mosaic-cost-center-header"] != "")'
    ]
    reasons = _denial_reasons(fragment)
    assert {"cost-center", "cost-center-mismatch"} <= set(reasons)


def test_a_key_charges_its_own_cost_center_and_refuses_another_header() -> None:
    fragment = _fragment(_snapshot(grants=_same_person_grants()))

    mismatch = _rejection(fragment, "cost-center-mismatch")
    assert mismatch.attrib["condition"] == (
        '@((string)context.Variables["mosaic-cost-center-header"] != ""'
        ' && (string)context.Variables["mosaic-cost-center-header"]'
        ' != (string)context.Variables["mosaic-key-cost-center"])'
    )
    assert _status(mismatch) == ("403", COST_CENTER_MISMATCH_DENIED)
    # Then the key's cost center is the selected one, so a token sent with it resolves there.
    assert '@((string)context.Variables["mosaic-key-cost-center"])' in _variable_values(
        fragment, "mosaic-cc"
    )
    keys = _variable_values(fragment, "mosaic-key-match")[-1]
    for grant in _same_person_grants():
        assert f'return "{_identity(grant)}|{grant.cost_center_code.lower()}";' in keys


def test_a_cost_center_without_keys_refuses_its_keys_fail_closed() -> None:
    allowed = _grant(1, **_charged(GENERAL))
    blocked = _grant(2, **_charged(RESEARCH), keys_allowed=False)
    fragment = _fragment(_snapshot(grants=[allowed, blocked]))

    keys = _variable_values(fragment, "mosaic-key-match")[-1]
    blocked_line = (
        f'"{blocked.subscription_name}", StringComparison.OrdinalIgnoreCase)) {{ return "-"; }}'
    )
    assert blocked_line in keys
    off = _rejection(fragment, "keys-off")
    assert off.attrib["condition"] == '@((string)context.Variables["mosaic-key-grant"] == "-")'
    assert _status(off) == ("401", COST_CENTER_KEYS_OFF_DENIED)
    # Without such a grant the sentinel can't occur, and no refusal is rendered for it.
    assert "keys-off" not in _denial_reasons(_fragment(_snapshot(grants=[allowed])))


def test_the_header_is_removed_before_the_backend_call() -> None:
    fragment = _fragment(_snapshot(grants=_same_person_grants()))

    removed = [
        element.attrib["name"]
        for element in fragment.findall("set-header")
        if element.attrib.get("exists-action") == "delete"
    ]
    assert COST_CENTER_HEADER in removed
    backend = next(
        index for index, element in enumerate(fragment) if element.tag == "set-backend-service"
    )
    strip = next(
        index
        for index, element in enumerate(fragment)
        if element.tag == "set-header" and element.attrib["name"] == COST_CENTER_HEADER
    )
    assert strip < backend


def test_the_matched_grant_sets_the_cost_center_code_and_id_for_later_policies() -> None:
    fragment = _fragment(_snapshot(grants=_same_person_grants()))

    assert _variable_values(fragment, "mosaic-cost-center") == [
        '@((bool)context.Variables["mosaic-has-key"]'
        ' ? (string)context.Variables["mosaic-key-cost-center"]'
        ' : (string)context.Variables["mosaic-token-cost-center"])'
    ]
    [ids] = _variable_values(fragment, "mosaic-cost-center-id")
    for cost_center_id, code in (GENERAL, RESEARCH, SALES):
        assert f'if (code == "{code.lower()}") {{ return "{cost_center_id}"; }}' in ids
    assert expression_error(ids) is None
    names = [element.attrib.get("name") for element in fragment.iter("set-variable")]
    # Both are set right after the grant, before the operation guard, trace and limits.
    assert names.index("mosaic-cost-center") == names.index("mosaic-grant") + 1
    assert names.index("mosaic-cost-center-id") == names.index("mosaic-cost-center") + 1


def test_a_pooled_quota_is_a_second_token_limit_keyed_on_cost_center_and_publication() -> None:
    publication = _publication()
    grants = [
        _grant(1, **_charged(RESEARCH), enforcement=EntitlementEnforcement(tokens=_tokens())),
        _grant(2, **_charged(RESEARCH)),
    ]
    pool = AppliedCostCenterPool(
        cost_center_id=RESEARCH[0],
        cost_center_code=RESEARCH[1],
        monthly_tokens=2_000_000,
        monthly_calls=40_000,
    )
    fragment = ET.fromstring(
        render_governed_policy(publication, _snapshot(grants=grants, pools=[pool])).fragment_xml
    )

    identity = cost_center_counter_identity(publication, RESEARCH[0])
    [when] = [
        element
        for element in fragment.iter("when")
        if element.attrib["condition"] == f'@({COST_CENTER_VARIABLE} == "research")'
    ]
    [tokens] = when.findall("llm-token-limit")
    assert tokens.attrib == {
        "counter-key": f"mosaic:governed:cost-center-tokens:{identity}",
        "estimate-prompt-tokens": "true",
        "token-quota": "2000000",
        "token-quota-period": "Monthly",
        "remaining-quota-tokens-header-name": COST_CENTER_REMAINING_QUOTA_TOKENS_HEADER,
    }
    [calls] = when.findall("quota-by-key")
    assert calls.attrib["calls"] == "40000"
    assert calls.attrib["renewal-period"] == "0"
    assert f"mosaic:governed:cost-center-request-quota:{identity}:" in calls.attrib["counter-key"]
    assert policy_expression_error(calls.attrib["counter-key"]) is None
    # Every grant under the cost center draws on the pool, and each keeps its own limit too.
    grant_limits = [
        element.attrib["counter-key"]
        for element in fragment.iter("llm-token-limit")
        if "grant-tokens" in element.attrib["counter-key"]
    ]
    assert grant_limits == [
        f"mosaic:governed:grant-tokens:{grant_counter_identity(publication, grants[0])}"
    ]
    # The pool is counted per publication and per cost center, never shared across them.
    other = cost_center_counter_identity(_publication(id="publication-other"), RESEARCH[0])
    assert other != identity
    assert cost_center_counter_identity(publication, GENERAL[0]) != identity


def test_a_pool_no_enabled_grant_charges_renders_nothing() -> None:
    pool = AppliedCostCenterPool(
        cost_center_id=RESEARCH[0], cost_center_code=RESEARCH[1], monthly_calls=10
    )
    grants = [_grant(1, **_charged(GENERAL)), _grant(2, **_charged(RESEARCH), enabled=False)]
    fragment = _fragment(_snapshot(grants=grants, pools=[pool]))

    assert not [
        element
        for element in fragment.iter("when")
        if element.attrib["condition"] == f'@({COST_CENTER_VARIABLE} == "research")'
    ]


def test_an_unmetered_publication_pools_calls_and_refuses_token_pools() -> None:
    grant = _grant(1, **_charged(RESEARCH))
    calls = AppliedCostCenterPool(
        cost_center_id=RESEARCH[0], cost_center_code=RESEARCH[1], monthly_calls=500
    )
    fragment = _fragment(
        _snapshot(grants=[grant], pools=[calls], publication_enforcement=None)
    )
    [when] = [
        element
        for element in fragment.iter("when")
        if element.attrib["condition"] == f'@({COST_CENTER_VARIABLE} == "research")'
    ]
    assert [child.tag for child in when] == ["quota-by-key"]

    tokens = calls.model_copy(update={"monthly_tokens": 1000})
    with pytest.raises(ValidationError, match="must count calls"):
        render_governed_policy(
            _publication(),
            _snapshot(grants=[grant], pools=[tokens], publication_enforcement=None),
        )


def test_personal_limits_report_whats_left_in_response_headers() -> None:
    limits = EntitlementEnforcement(
        tokens=_tokens(token_quota=90000, token_quota_period="Monthly"),
        requests=_requests(),
    )
    fragment = _fragment(_snapshot(grants=[_grant(1, **_charged(GENERAL), enforcement=limits)]))

    [personal] = [
        element
        for element in fragment.iter("llm-token-limit")
        if "grant-tokens" in element.attrib["counter-key"]
    ]
    assert personal.attrib["remaining-tokens-header-name"] == REMAINING_TOKENS_HEADER
    assert personal.attrib["remaining-quota-tokens-header-name"] == REMAINING_QUOTA_TOKENS_HEADER
    [rate] = fragment.iter("rate-limit-by-key")
    assert rate.attrib["remaining-calls-header-name"] == REMAINING_CALLS_HEADER


def test_the_same_person_under_two_cost_centers_is_two_grants_but_not_twice_under_one() -> None:
    render_governed_policy(_publication(), _snapshot(grants=_same_person_grants()))

    general, research, _ = _same_person_grants()
    duplicate = research.model_copy(
        update={
            "cost_center_id": GENERAL[0],
            "cost_center_code": GENERAL[1],
        }
    )
    with pytest.raises(ValidationError, match="unambiguous"):
        render_governed_policy(_publication(), _snapshot(grants=[general, duplicate]))


@pytest.mark.parametrize(
    ("grants", "message"),
    [
        (
            [_grant(1, cost_center_id="costCenter-a", cost_center_code="bad code")],
            "letters, digits",
        ),
        (
            [
                _grant(1, cost_center_id="costCenter-a", cost_center_code="same"),
                _grant(2, cost_center_id="costCenter-b", cost_center_code="SAME"),
            ],
            "exactly one code",
        ),
        (
            [
                _grant(1, cost_center_id="costCenter-a", cost_center_code="one"),
                _grant(2, cost_center_id="costCenter-a", cost_center_code="two"),
            ],
            "exactly one code",
        ),
    ],
)
def test_codes_must_be_valid_and_name_exactly_one_cost_center(
    grants: list[ModelAccessGrant], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        render_governed_policy(_publication(), _snapshot(grants=grants))


def test_a_disabled_grant_keeping_an_old_or_reused_code_does_not_block_the_plan() -> None:
    """A grant gone from desired state is carried forward disabled, with the code it had.

    Its cost center may have changed code since, or been deleted and its code reused. It is never
    compiled, so it mustn't stop the plan that removes it.
    """

    general = ("costCenter-general", "gen")
    current = _grant(1, **_charged(general))
    recoded = _grant(2, **_charged(GENERAL), enabled=False)
    reused = _grant(3, cost_center_id="costCenter-deleted", cost_center_code="gen", enabled=False)
    pool = AppliedCostCenterPool(
        cost_center_id=general[0], cost_center_code=general[1], monthly_calls=10
    )
    fragment = _fragment(_snapshot(grants=[current, recoded, reused], pools=[pool]))
    assert [value.split("|")[1] for value in _token_returns(fragment)] == ["gen"]

    _mcp_render(
        _mcp_publication(),
        _mcp_snapshot(
            grants=[
                _mcp_grant(1, **_charged(general)),
                _mcp_grant(2, **_charged(GENERAL), enabled=False),
                _mcp_grant(
                    3, cost_center_id="costCenter-deleted", cost_center_code="gen", enabled=False
                ),
            ],
            pools=[pool],
        ),
    )

    # Enabled grants are still held to one code per cost center.
    with pytest.raises(ValidationError, match="exactly one code"):
        render_governed_policy(
            _publication(),
            _snapshot(grants=[current, reused.model_copy(update={"enabled": True})]),
        )


def test_keys_only_publications_still_select_by_key_and_refuse_a_mismatched_header() -> None:
    fragment = _fragment(
        _snapshot(
            grants=_same_person_grants(),
            settings=ModelAccessSettings(keys_enabled=True, entra_enabled=False),
        )
    )
    assert {"cost-center", "cost-center-mismatch"} <= set(_denial_reasons(fragment))
    assert not _variable_values(fragment, "mosaic-token-match")


def test_mcp_policies_select_by_header_pool_calls_and_strip_the_header() -> None:
    person = "00000001-3333-3333-3333-333333333333"
    grants = [
        _mcp_grant(1, object_id=person, **_charged(GENERAL, granted_at=_at(1))),
        _mcp_grant(
            2,
            object_id=person,
            subject=EntitlementSubject(kind=EntitlementSubjectKind.USER, id="principal-1"),
            **_charged(RESEARCH, granted_at=_at(2), default_cost_center=True),
        ),
    ]
    pool = AppliedCostCenterPool(
        cost_center_id=RESEARCH[0], cost_center_code=RESEARCH[1], monthly_calls=900
    )
    publication = _mcp_publication()
    result = _mcp_render(publication, _mcp_snapshot(grants=grants, pools=[pool]))
    fragment = ET.fromstring(result.fragment_xml)

    lookup = _variable_values(fragment, "mosaic-token-match")[-1]
    returns = re.findall(r'return "([0-9a-f]{64}\|[^"]*)";', lookup)
    assert [value.split("|")[1] for value in returns] == ["research", "general"]
    assert COST_CENTER_HEADER in [
        element.attrib["name"]
        for element in fragment.findall("set-header")
        if element.attrib.get("exists-action") == "delete"
    ]
    [when] = [
        element
        for element in fragment.iter("when")
        if element.attrib["condition"] == f'@({COST_CENTER_VARIABLE} == "research")'
    ]
    assert [child.tag for child in when] == ["quota-by-key"]
    assert when[0].attrib["calls"] == "900"
    assert {"cost-center"} <= set(_denial_reasons(fragment))

    tokens = pool.model_copy(update={"monthly_tokens": 10})
    with pytest.raises(ValidationError):
        _mcp_render(publication, _mcp_snapshot(grants=grants, pools=[tokens]))


def _representative_model_snapshot() -> ModelAccessSnapshot:
    limits = EntitlementEnforcement(
        tokens=_tokens(token_quota=90000, token_quota_period="Monthly"),
        requests=_requests(),
    )
    return _snapshot(
        grants=[
            *_same_person_grants(),
            _grant(4, **_charged(RESEARCH, granted_at=_at(4)), enforcement=limits),
            _grant(5, **_charged(SALES), keys_allowed=False),
            _group_grant(6, **_charged(RESEARCH)),
        ],
        pools=[
            AppliedCostCenterPool(
                cost_center_id=RESEARCH[0],
                cost_center_code=RESEARCH[1],
                monthly_tokens=2_000_000,
                monthly_calls=40_000,
            )
        ],
    )


def _representative_mcp_snapshot() -> McpAccessSnapshot:
    return _mcp_snapshot(
        grants=[
            _mcp_grant(1, **_charged(GENERAL, granted_at=_at(1), default_cost_center=True)),
            _mcp_grant(
                2,
                object_id="00000001-3333-3333-3333-333333333333",
                subject=EntitlementSubject(kind=EntitlementSubjectKind.USER, id="principal-1"),
                **_charged(RESEARCH, granted_at=_at(2)),
            ),
        ],
        pools=[
            AppliedCostCenterPool(
                cost_center_id=RESEARCH[0], cost_center_code=RESEARCH[1], monthly_calls=900
            )
        ],
    )


def _model_fragment() -> str:
    return render_governed_policy(_publication(), _representative_model_snapshot()).fragment_xml


def _mcp_fragment() -> str:
    return _mcp_render(_mcp_publication(), _representative_mcp_snapshot()).fragment_xml


@pytest.mark.parametrize(
    ("name", "render"),
    [("model-cost-centers.xml", _model_fragment), ("mcp-cost-centers.xml", _mcp_fragment)],
)
def test_rendered_fragments_match_their_reviewed_snapshots(
    name: str, render: Callable[[], str]
) -> None:
    rendered = render() + "\n"
    path = SNAPSHOTS / name
    if os.environ.get("MOSAIC_UPDATE_POLICY_SNAPSHOTS") == "1":
        SNAPSHOTS.mkdir(exist_ok=True)
        path.write_text(rendered, encoding="utf-8", newline="\n")
    expected = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert rendered == expected, (
        f"{name} changed. If that's intended, rerun with MOSAIC_UPDATE_POLICY_SNAPSHOTS=1 and "
        "review the diff."
    )
