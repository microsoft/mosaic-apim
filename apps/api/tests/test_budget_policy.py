"""The budget check in the governed policies: where it runs, what it refuses, and how it fails.

Compiler contract tests, like ``test_cost_center_policy``. API Management replaces the named value
reference with the gateway's list before it runs the policy; these tests do the same to the
compiled conditions, in Python, to show what a call meets. See ADR 0023.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable

import pytest
from apim_double import policy_expression_error
from mosaic_api.budgets import BLOCKED_COST_CENTERS_NAMED_VALUE, NONE_BLOCKED, budget_key
from mosaic_api.integrations.access_policy import (
    BLOCKED_LIST_UNREADABLE,
    BUDGET_DENIED,
    render_governed_policy,
)
from test_access_policy import (
    _fragment,
    _full_enforcement,
    _grant,
    _publication,
    _snapshot,
    _variable_values,
)
from test_cost_center_policy import (
    GENERAL,
    RESEARCH,
    SALES,
    _charged,
    _rejection,
    _same_person_grants,
    _status,
)
from test_mcp_access_policy import _grant as _mcp_grant
from test_mcp_access_policy import _publication as _mcp_publication
from test_mcp_access_policy import _render as _mcp_render
from test_mcp_access_policy import _snapshot as _mcp_snapshot

REFERENCE = "{{" + BLOCKED_COST_CENTERS_NAMED_VALUE + "}}"
LIMITS = {"llm-token-limit", "rate-limit-by-key", "quota-by-key"}
Render = Callable[[], ET.Element]


def _model() -> ET.Element:
    return _fragment(_snapshot(grants=_same_person_grants()))


def _mcp() -> ET.Element:
    grants = [_mcp_grant(1, **_charged(GENERAL)), _mcp_grant(2, **_charged(RESEARCH))]
    rendered = _mcp_render(_mcp_publication(), _mcp_snapshot(grants=grants))
    return ET.fromstring(rendered.fragment_xml)


def _keys(fragment: ET.Element) -> dict[str, str]:
    [lookup] = _variable_values(fragment, "mosaic-budget-key")
    return dict(re.findall(r'if \(code == "([^"]+)"\) \{ return "([^"]+)"; \}', lookup))


def _meets(fragment: ET.Element, listed: str, code: str) -> str | None:
    """The refusal a call charged to ``code`` meets when the gateway's list holds ``listed``."""

    unreadable = _rejection(fragment, "budget-list").attrib["condition"]
    blocked = _rejection(fragment, "budget").attrib["condition"]
    assert f'var blocked = "{REFERENCE}";' in unreadable
    assert f'var blocked = "{REFERENCE}";' in blocked
    match = re.search(r'IsMatch\(blocked, ("(?:[^"\\]|\\.)*")\)', unreadable)
    assert match is not None
    if not re.fullmatch(json.loads(match.group(1)), listed):
        return "budget-list"
    key = _keys(fragment).get(code, "")
    if key and f",{key}," in f",{listed},":
        return "budget"
    return None


def _position(fragment: ET.Element, found: Callable[[ET.Element], bool]) -> int:
    return next(index for index, child in enumerate(fragment) if found(child))


def _refusal_position(fragment: ET.Element, reason: str) -> int:
    """Where a refusal sits among the fragment's top-level elements, nested or not."""

    when = _rejection(fragment, reason)
    return _position(fragment, lambda child: any(element is when for element in child.iter()))


@pytest.mark.parametrize("render", [_model, _mcp])
def test_each_cost_center_on_the_policy_has_its_key(render: Render) -> None:
    keys = _keys(render())

    for cost_center_id, code in (GENERAL, RESEARCH):
        assert keys[code.lower()] == budget_key(cost_center_id)


@pytest.mark.parametrize("render", [_model, _mcp])
def test_a_blocked_cost_centers_calls_are_refused_with_403_naming_it(render: Render) -> None:
    fragment = render()
    research = budget_key(RESEARCH[0])
    general = budget_key(GENERAL[0])

    assert _meets(fragment, NONE_BLOCKED, "research") is None
    assert _meets(fragment, research, "research") == "budget"
    assert _meets(fragment, research, "general") is None
    assert _meets(fragment, f"{general},{research}", "research") == "budget"
    assert _meets(fragment, f"{research},{general}", "research") == "budget"
    assert _meets(fragment, general, "research") is None
    assert _status(_rejection(fragment, "budget")) == ("403", BUDGET_DENIED)
    assert '(string)context.Variables["mosaic-cost-center"]' in BUDGET_DENIED
    assert "monthly budget" in BUDGET_DENIED


@pytest.mark.parametrize("render", [_model, _mcp])
@pytest.mark.parametrize(
    "listed", ["", "*", "all", "ABCDEF012345", "0123456789ab,", "x,0123456789ab"]
)
def test_a_list_mosaic_didnt_write_refuses_every_call(render: Render, listed: str) -> None:
    fragment = render()

    assert _meets(fragment, listed, "research") == "budget-list"
    assert _meets(fragment, listed, "general") == "budget-list"
    assert _status(_rejection(fragment, "budget-list")) == ("503", BLOCKED_LIST_UNREADABLE)


def test_one_key_never_matches_inside_another() -> None:
    fragment = _model()
    research = budget_key(RESEARCH[0])

    # Only whole keys between commas: a longer value is refused as unreadable, and another key
    # that shares most of its digits doesn't match.
    assert _meets(fragment, f"{research}0", "research") == "budget-list"
    assert _meets(fragment, f"0{research[1:]}", "research") is None


def test_the_check_runs_once_the_cost_center_is_known_and_before_anything_counts() -> None:
    fragment = _fragment(
        _snapshot(grants=[_grant(1, **_charged(SALES), enforcement=_full_enforcement())])
    )

    cost_center_id = _position(
        fragment,
        lambda child: child.tag == "set-variable" and child.get("name") == "mosaic-cost-center-id",
    )
    key = _position(
        fragment,
        lambda child: child.tag == "set-variable" and child.get("name") == "mosaic-budget-key",
    )
    attribution = _position(
        fragment,
        lambda child: child.tag == "trace"
        and "mosaic-attribution" in (child.findtext("message") or ""),
    )
    limits = [
        index
        for index, child in enumerate(fragment)
        if any(element.tag in LIMITS for element in child.iter())
    ]
    budget = _refusal_position(fragment, "budget")
    assert limits
    assert cost_center_id < key < _refusal_position(fragment, "budget-list") < budget
    assert budget < _refusal_position(fragment, "operation") < attribution
    assert all(budget < index for index in limits)
    # Every refusal for who the caller is comes first, so nothing unauthenticated reaches it.
    for reason in ("no-credential", "no-grant", "grant-mismatch", "cost-center"):
        assert _refusal_position(fragment, reason) < key


def test_the_reference_stays_a_reference_and_api_management_can_parse_the_check() -> None:
    rendered = render_governed_policy(_publication(), _snapshot(grants=_same_person_grants()))

    assert rendered.fragment_xml.count(REFERENCE) == 2
    assert policy_expression_error(rendered.fragment_xml) is None
    mcp = _mcp_render(_mcp_publication(), _mcp_snapshot(grants=[_mcp_grant(1)]))
    assert mcp.fragment_xml.count(REFERENCE) == 2
    assert policy_expression_error(mcp.fragment_xml) is None
