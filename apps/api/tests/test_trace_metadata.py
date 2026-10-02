"""Every trace MOSAIC writes is one API Management accepts while a call runs.

API Management requires a ``trace`` policy's metadata value. One that evaluates to an empty string
fails the whole call with 500, "Expression value is invalid. The value field is required.", before
it reaches the backend. A key call has no client application and a direct grant has no member, so
those values are empty for every such call. ``append_trace_metadata`` records ``-`` in their place.
These tests hold every metadata element in every governed document to that, and check that the
message the usage queries parse is unchanged.

The guard is spelled out here rather than taken from the code under test, so a broken guard fails.
"""

import ast
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import mosaic_api
import pytest
from mosaic_api.domain import McpAuthMode, ModelAccessSettings
from mosaic_api.integrations.access_policy import (
    TRACE_METADATA_ABSENT,
    append_trace_metadata,
    grant_counter_identity,
    render_governed_policy,
    trace_metadata_value,
)
from mosaic_api.integrations.loganalytics.kql import QueryWindow, calls_query
from mosaic_api.integrations.mcp_access_policy import McpPolicyDocuments
from test_access_policy import (
    _ATTRIBUTION_MESSAGE,
    AUDIENCE,
    _anthropic,
    _full_enforcement,
    _grant,
    _group_grant,
    _publication,
    _snapshot,
    _unmetered,
)
from test_cost_center_policy import _representative_mcp_snapshot, _representative_model_snapshot
from test_mcp_access_policy import _app_grant as _mcp_app_grant
from test_mcp_access_policy import _enforcement as _mcp_enforcement
from test_mcp_access_policy import _grant as _mcp_grant
from test_mcp_access_policy import _group_grant as _mcp_group_grant
from test_mcp_access_policy import _render as _mcp_render
from test_mcp_access_policy import _snapshot as _mcp_snapshot

# The variable, or "-" when it's empty or blank. The backreference holds both reads to one value.
_GUARDED = re.compile(r'@\(String\.IsNullOrWhiteSpace\((?P<value>.+)\) \? "-" : (?P=value)\)')
_BARE = re.compile(r"@\((?P<value>.+)\)")
_VARIABLE = re.compile(r'\(string\)context\.Variables\["(?P<name>[a-z-]+)"\]')
_TERM = re.compile(r'"(?:[^"\\]|\\.)*"|\(string\)context\.Variables\["[a-z-]+"\]')
# Elements whose children run for every call that reaches them, unlike a choose's.
_UNCONDITIONAL = frozenset({"fragment", "policies", "inbound", "backend", "outbound", "on-error"})

GRANT = grant_counter_identity(_publication(), _grant())
MEMBER = "00000001-3333-3333-3333-333333333333"
CLIENT = "44444444-4444-4444-4444-444444444444"
# What the trace's variables hold when it runs, for each way a call is authorized. A token without
# exactly one azp claim records no client, as a key call doesn't.
KEY_CALL = {"mosaic-grant": GRANT, "mosaic-member": "", "mosaic-client": ""}
TOKEN_CALL = {"mosaic-grant": GRANT, "mosaic-member": "", "mosaic-client": CLIENT}
TOKEN_WITHOUT_CLIENT = {"mosaic-grant": GRANT, "mosaic-member": "", "mosaic-client": ""}
GROUP_MEMBER_CALL = {"mosaic-grant": GRANT, "mosaic-member": MEMBER, "mosaic-client": CLIENT}
CALLS = (KEY_CALL, TOKEN_CALL, TOKEN_WITHOUT_CLIENT, GROUP_MEMBER_CALL)
DIRECT = ("mosaic-grant", "mosaic-client")
WITH_GROUPS = ("mosaic-grant", "mosaic-member", "mosaic-client")


def _read(expression: str) -> str:
    variable = _VARIABLE.fullmatch(expression)
    assert variable is not None, f"Not a variable read: {expression!r}"
    return variable["name"]


def _recorded(value: str, variables: Mapping[str, str]) -> str:
    """What API Management records for a metadata value MOSAIC writes, given the variables."""

    if guarded := _GUARDED.fullmatch(value):
        recorded = variables[_read(guarded["value"])]
        return "-" if not recorded.strip() else recorded
    if bare := _BARE.fullmatch(value):
        return variables[_read(bare["value"])]
    assert not value.startswith("@"), f"No model for the expression {value!r}"
    return value


def _message(expression: str, variables: Mapping[str, str]) -> str:
    """A trace's message, which concatenates literals and variables and nothing else."""

    body = _BARE.fullmatch(expression)
    assert body is not None
    terms = [term.group() for term in _TERM.finditer(body["value"])]
    assert " + ".join(terms) == body["value"]
    return "".join(
        json.loads(term) if term.startswith('"') else variables[_read(term)] for term in terms
    )


def _usage_query_keys(traces: str) -> dict[str, str]:
    """The keys MOSAIC's usage query extracts from a gateway log row's TraceRecords.

    The patterns are read from the query itself. KQL's ``extract`` answers "" without a match.
    """

    window = QueryWindow(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, 1, tzinfo=UTC))
    query = calls_query(window, ["mosaic-model"])
    patterns = {
        name: re.compile(pattern.replace('""', '"'))
        for name, pattern in re.findall(
            r'(\w+) = (?:tolower\()?extract\(@"((?:[^"]|"")*)", 1, \w+\)', query
        )
    }
    assert {"attribution", "v", "g", "m", "a"} <= set(patterns)

    def extract(name: str, text: str) -> str:
        match = patterns[name].search(text)
        return match.group(1) if match else ""

    attribution = extract("attribution", traces)
    return {key: extract(key, attribution).lower() for key in ("v", "g", "m", "a")}


def _trace_records(message: str, metadata: Mapping[str, str], placement: str) -> str:
    """A gateway log row's TraceRecords, as JSON text.

    Whether resource logs keep a trace's metadata isn't documented, so it goes before the
    message, after it, or nowhere.
    """

    record: dict[str, object] = {"source": "mosaic", "severity": "Information"}
    properties = [{"name": name, "value": value} for name, value in metadata.items()]
    if placement == "before":
        record["metadata"] = properties
    record["message"] = message
    if placement == "after":
        record["metadata"] = properties
    return json.dumps([record])


def _attribution_trace(xml: str) -> ET.Element:
    [trace] = [
        trace
        for trace in ET.fromstring(xml).iter("trace")
        if "mosaic-attribution" in (trace.findtext("message") or "")
    ]
    return trace


def _key_only_direct_grant() -> str:
    snapshot = _snapshot(
        settings=ModelAccessSettings(keys_enabled=True, entra_enabled=False), audience=None
    )
    return render_governed_policy(_publication(), snapshot).fragment_xml


def _token_only_direct_grant() -> str:
    snapshot = _snapshot(settings=ModelAccessSettings(keys_enabled=False, entra_enabled=True))
    return render_governed_policy(_publication(), snapshot).fragment_xml


def _model_with_group_and_direct_grants() -> str:
    snapshot = _snapshot(grants=[_grant(), _group_grant(2)])
    return render_governed_policy(_publication(), snapshot).fragment_xml


def _mcp_with_group_and_direct_grants() -> str:
    snapshot = _mcp_snapshot(grants=[_mcp_grant(), _mcp_group_grant(2)])
    documents: McpPolicyDocuments = _mcp_render(snapshot=snapshot)
    return documents.fragment_xml


@pytest.mark.parametrize(
    ("fragment", "names", "calls"),
    [
        pytest.param(_key_only_direct_grant, DIRECT, [KEY_CALL], id="model-key-only-direct"),
        pytest.param(
            _token_only_direct_grant,
            DIRECT,
            [TOKEN_CALL, TOKEN_WITHOUT_CLIENT],
            id="model-token-only-direct",
        ),
        pytest.param(
            _model_with_group_and_direct_grants,
            WITH_GROUPS,
            list(CALLS),
            id="model-group-and-direct",
        ),
        pytest.param(
            _mcp_with_group_and_direct_grants,
            WITH_GROUPS,
            [TOKEN_CALL, TOKEN_WITHOUT_CLIENT, GROUP_MEMBER_CALL],
            id="mcp-group-and-direct",
        ),
    ],
)
def test_every_property_is_recorded_even_when_the_call_has_no_value_for_it(
    fragment: Callable[[], str], names: tuple[str, ...], calls: list[dict[str, str]]
) -> None:
    trace = _attribution_trace(fragment())
    metadata = {item.attrib["name"]: item.attrib["value"] for item in trace.findall("metadata")}

    assert tuple(metadata) == names
    for name, value in metadata.items():
        guarded = _GUARDED.fullmatch(value)
        assert guarded is not None, f"{name} can evaluate to an empty value: {value}"
        assert _read(guarded["value"]) == name
    # Exactly as it was, because the usage queries parse it.
    message = trace.findtext("message") or ""
    assert message == _ATTRIBUTION_MESSAGE
    for call in calls:
        recorded = {name: _recorded(value, call) for name, value in metadata.items()}
        assert recorded == {name: call[name] or TRACE_METADATA_ABSENT for name in names}
        text = _message(message, call)
        assert text == (
            f"mosaic-attribution v=1 g={call['mosaic-grant']} m={call['mosaic-member']} "
            f"a={call['mosaic-client']}"
        )
        for placement in ("before", "after", "none"):
            properties = {} if placement == "none" else recorded
            assert _usage_query_keys(_trace_records(text, properties, placement)) == {
                "v": "1",
                "g": call["mosaic-grant"],
                "m": call["mosaic-member"],
                "a": call["mosaic-client"],
            }


def _documents() -> Iterator[tuple[str, str]]:
    """Every governed document, labelled, across the settings, grants and shapes that change it."""

    for keys, entra in ((True, True), (True, False), (False, True), (False, False)):
        grant_sets = [[_grant()], [_grant(enforcement=_full_enforcement())]]
        if entra:
            grant_sets += [[_grant(), _group_grant(2)], [_group_grant(2)]]
        for grants in grant_sets:
            snapshot = _snapshot(
                settings=ModelAccessSettings(keys_enabled=keys, entra_enabled=entra),
                audience=AUDIENCE if entra else None,
                grants=grants,
            )
            for publication in (_publication(), _anthropic(backend_key_name="mosaic-model-key")):
                result = render_governed_policy(publication, snapshot)
                label = f"{publication.api_shape} keys={keys} entra={entra} grants={len(grants)}"
                yield label, result.fragment_xml
                yield f"{label} API policy", result.api_policy_xml
    grants = [_grant(), _group_grant(2)]
    yield "unmetered", render_governed_policy(_anthropic(), _unmetered(grants=grants)).fragment_xml
    model = render_governed_policy(_publication(), _representative_model_snapshot())
    yield "representative model", model.fragment_xml
    mcp_grant_sets = [
        [_mcp_grant()],
        [_mcp_grant(enforcement=_mcp_enforcement()), _mcp_group_grant(2)],
        [_mcp_group_grant(2, enforcement=_mcp_enforcement())],
        [_mcp_grant(), _mcp_group_grant(3), _mcp_app_grant()],
    ]
    backends = ((McpAuthMode.NONE, None), (McpAuthMode.MANAGED_IDENTITY, "api://backend"))
    for mcp_grants in mcp_grant_sets:
        for auth, audience in backends:
            mcp = _mcp_render(
                snapshot=_mcp_snapshot(grants=mcp_grants),
                backend_auth=auth,
                backend_audience=audience,
            )
            label = f"mcp {auth} grants={len(mcp_grants)}"
            yield label, mcp.fragment_xml
            yield f"{label} API policy", mcp.api_policy_xml
            yield f"{label} metadata policy", mcp.metadata_policy_xml
    yield "representative mcp", _mcp_render(snapshot=_representative_mcp_snapshot()).fragment_xml


def _set_for_every_caller(root: ET.Element, before: ET.Element) -> set[str]:
    """The variables a document sets before ``before`` outside any choose, so for every call."""

    parents = {child: parent for parent in root.iter() for child in parent}
    order = list(root.iter())
    names: set[str] = set()
    for element in order[: order.index(before)]:
        if element.tag != "set-variable":
            continue
        ancestor = parents.get(element)
        while ancestor is not None and ancestor.tag in _UNCONDITIONAL:
            ancestor = parents.get(ancestor)
        if ancestor is None:
            names.add(element.attrib["name"])
    return names


def test_no_trace_in_any_governed_document_can_record_an_empty_value() -> None:
    """Fails as soon as any document gains a metadata element that skips the guard."""

    checked = 0
    for label, xml in _documents():
        root = ET.fromstring(xml)
        for trace in root.iter("trace"):
            # The message is required too. Each one starts with a literal.
            message = trace.findtext("message") or ""
            assert message.strip(), f"{label}: a trace has no message"
            if message.startswith("@"):
                assert _message(message, defaultdict(str)).strip()
            initialized = _set_for_every_caller(root, trace)
            for element in trace.iter("metadata"):
                checked += 1
                name, value = element.get("name", ""), element.get("value", "")
                assert name.strip(), f"{label}: a trace property has no name"
                guarded = _GUARDED.fullmatch(value)
                assert guarded is not None, f"{label}: {name} can evaluate to an empty value"
                # Reading a variable that hasn't been set fails the call just the same.
                variable = _read(guarded["value"])
                assert variable in initialized, f"{label}: {name} reads {variable} before it's set"
                for call in CALLS:
                    assert _recorded(value, defaultdict(str, call)).strip()
    assert checked


class _MetadataElements(ast.NodeVisitor):
    """Where the source creates a ``metadata`` element, or writes one as XML text."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.function = ""
        self.found: list[tuple[str, str]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        outer, self.function = self.function, node.name
        self.generic_visit(node)
        self.function = outer

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        function = node.func
        name = function.attr if isinstance(function, ast.Attribute) else ""
        name = function.id if isinstance(function, ast.Name) else name
        # Where each way of creating an element takes its tag.
        position = {"SubElement": 1, "Element": 0, "makeelement": 0}.get(name)
        if position is not None and len(node.args) > position:
            tag = node.args[position]
            if isinstance(tag, ast.Constant) and tag.value == "metadata":
                self.found.append((self.path.name, self.function))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and "<metadata" in node.value:
            self.found.append((self.path.name, self.function))


def test_only_the_guard_adds_metadata_elements() -> None:
    """Fails when code adds a metadata element some other way, even one no test renders."""

    found: list[tuple[str, str]] = []
    for path in sorted(Path(mosaic_api.__file__).parent.rglob("*.py")):
        visitor = _MetadataElements(path)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        found.extend(visitor.found)

    assert found == [("access_policy.py", "append_trace_metadata")]


def test_the_guard_takes_a_bare_expression_and_a_name() -> None:
    value = '(string)context.Variables["mosaic-client"]'
    assert trace_metadata_value(value) == (
        f'@(String.IsNullOrWhiteSpace({value}) ? "-" : {value})'
    )
    assert TRACE_METADATA_ABSENT == "-"
    for expression in ("", "  ", f"@({value})", f" @({value})"):
        with pytest.raises(ValueError, match="C# expression"):
            trace_metadata_value(expression)
    trace = ET.Element("trace")
    with pytest.raises(ValueError, match="name"):
        append_trace_metadata(trace, " ", value)
    append_trace_metadata(trace, "mosaic-client", value)
    assert [element.attrib for element in trace] == [
        {"name": "mosaic-client", "value": trace_metadata_value(value)}
    ]


def test_the_attribution_facet_says_what_an_absent_value_records() -> None:
    for facets in (
        render_governed_policy(_publication(), _snapshot()).facets,
        _mcp_render().facets,
    ):
        facet = next(
            facet
            for facet in facets
            if facet.element == "trace" and "MOSAIC grant" in facet.summary
        )
        assert any(
            "properties" in detail and f" {TRACE_METADATA_ABSENT} " in detail
            for detail in facet.details
        )
