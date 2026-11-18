"""Standalone cost uncertainty must not change a neighboring pool's shared deployment rate."""

from datetime import UTC, date, datetime

import pytest
from mosaic_api.budgets import Budget, BudgetState, evaluate
from mosaic_api.domain import Entitlement, EntitlementResource, EntitlementSubject
from mosaic_api.pricing import UNKNOWN_DEPLOYMENT, PriceBook, Pricer
from mosaic_api.services.analytics.cost import CostBook, CostTally, Priced
from mosaic_api.services.analytics.models import AnalyticsFilters, AnalyticsSpend
from mosaic_api.services.analytics.scope import Scope
from mosaic_api.services.budgets import _spent
from mosaic_api.services.usage import DailyUsage, _MeasuredPricing
from mosaic_api.services.usage_rollup import merge_apis
from mosaic_api.usage_telemetry import MemberUsage, RolledUpApi, UsageMetrics
from test_pricing import _entry, _facts

DAY = date(2026, 3, 18)
NOW = datetime(2026, 3, 18, tzinfo=UTC)


def _scope() -> Scope:
    return Scope(
        tenant_id="test", filters=AnalyticsFilters(), all_gateways={}, gateways={},
        gateway_ids=None, states={}, governed={}, apis={}, allowed_apis=None,
        resource_ids=None, environments={}, grants={}, links_by_entitlement={}, entitlements={},
        principals_by_object={}, principals_by_id={}, principals_by_app={}, groups={},
        subject_names={},
    )


def test_shared_priced_pool_and_unknown_standalone_keep_separate_cost_lineage() -> None:
    facts = _facts(provider="awsBedrock", cloud="aws")
    rate = _entry("aws-manual", cloud="aws")
    pricer = Pricer(PriceBook([rate]), {facts.key: facts})
    book = CostBook(pricer, _scope(), DAY, {})
    standalone = RolledUpApi(
        api_name="bedrock", resource_id="model-1", kind="model", display_name="Claude",
        model_endpoint_id=facts.endpoint_id, deployment_name=facts.deployment_name,
        cost_telemetry="aggregateOnly", first_seen_at=NOW,
    )
    pool = RolledUpApi(
        api_name="pool", resource_id="pool-1", kind="pool", display_name="Shared Claude",
        first_seen_at=NOW,
    )
    metrics = UsageMetrics(
        requests=1, ok=1, prompt_tokens=1_000_000, completion_tokens=500_000,
        total_tokens=1_500_000,
    )
    served = MemberUsage(
        requests=1, prompt_tokens=metrics.prompt_tokens,
        completion_tokens=metrics.completion_tokens, total_tokens=metrics.total_tokens,
    )
    pool_metrics = metrics.model_copy(update={"members": {facts.key: served}})
    before = pricer.rate(facts.key, DAY)
    _, _, unknown = book.priced_parts(standalone, "day", DAY, metrics)[0]
    key, part, priced = book.priced_parts(pool, "day", DAY, pool_metrics)[0]
    assert key == facts.key == standalone.deployment_key
    assert priced.amount == pytest.approx(2.0)
    assert unknown.amount is None and unknown.unpriced.reason == "incompleteUsage"
    assert pricer.rate(facts.key, DAY) is before
    tally = CostTally()
    tally.record(priced, part, key=key, kind="deployment", label="Pool", detail=None)
    tally.record(unknown, metrics, key="api:bedrock", kind="api", label="Standalone", detail=None)
    summary = tally.summary(book.notes)
    assert summary.total == pytest.approx(2.0)
    assert summary.unpriced_requests == 1 and summary.unpriced_items == 1
    assert summary.unpriced_tokens == 1_500_000
    assert any("incomplete" in note for note in summary.notes)
    partial = AnalyticsSpend(
        month_start=DAY.replace(day=1), days_in_month=31, days_elapsed=18,
        month_to_date=2.0, forecast=None, unpriced_items=1, incomplete_usage=True,
    )
    assert _spent(partial) == pytest.approx(2.0)

    caller = _MeasuredPricing(
        pricer=pricer, deployments={}, tokens={}, today=DAY, unobservable_grants={"g"},
    )
    grant = Entitlement(
        id="g", tenant_id="test", subject=EntitlementSubject(kind="user", id="person"),
        resource=EntitlementResource(kind="modelApi", id="model-1"),
    )
    usage = DailyUsage(1, metrics.prompt_tokens, metrics.completion_tokens)
    assert "unknown" in caller.note(grant, facts.key, {DAY: usage})
    usage.members = {facts.key: served}
    amount, _, note = caller.price_pool_days([facts.key], [(DAY, usage)], DAY)
    assert amount == pytest.approx(2.0) and note is None


@pytest.mark.parametrize("standalone", [False, True])
def test_denied_only_calls_do_not_become_unknown_spend(standalone: bool) -> None:
    tally = CostTally()
    book = CostBook(Pricer(PriceBook([]), {}), _scope(), DAY, {})
    api = RolledUpApi(
        api_name="bedrock", resource_id="model-1", kind="model", display_name="Claude",
        cost_telemetry="aggregateOnly" if standalone else None, first_seen_at=NOW,
    )
    metrics = UsageMetrics(requests=1, denied=1)
    priced = (
        book.priced_parts(api, "day", DAY, metrics)[0][2]
        if standalone else Priced(None, unpriced=UNKNOWN_DEPLOYMENT)
    )
    tally.record(priced, metrics, key="api:test", kind="api", label="API", detail=None)
    assert tally.summary().unpriced_items == 0
    assert tally.summary().unpriced_requests == 0


def test_success_without_token_counts_remains_unknown_only_for_standalone() -> None:
    book = CostBook(Pricer(PriceBook([]), {}), _scope(), DAY, {})
    api = RolledUpApi(
        api_name="bedrock", resource_id="model-1", kind="model", display_name="Claude",
        cost_telemetry="aggregateOnly", first_seen_at=NOW,
    )
    metrics = UsageMetrics(requests=2, ok=1, denied=1)
    priced = book.priced_parts(api, "day", DAY, metrics)[0][2]
    tally = CostTally()
    tally.record(priced, metrics, key="api:test", kind="api", label="API", detail=None)
    assert tally.total is None
    assert tally.summary().unpriced_items == 1
    assert tally.summary().unpriced_requests == 1


@pytest.mark.parametrize("publication_id", [None, "publication-1"])
def test_refreshed_inventory_cannot_erase_remembered_aggregate_cost_lineage(
    publication_id: str | None,
) -> None:
    remembered = RolledUpApi(
        api_name="bedrock", resource_id="model-1", kind="model", display_name="Claude",
        publication_id=publication_id, cost_telemetry="aggregateOnly", first_seen_at=NOW,
    )
    [merged] = merge_apis(
        [remembered], [remembered.model_copy(update={"cost_telemetry": None})], NOW,
    )
    book = CostBook(Pricer(PriceBook([]), {}), _scope(), DAY, {})
    [(_, _, priced)] = book.priced_parts(merged, "day", DAY, UsageMetrics(requests=1, ok=1))
    assert merged.cost_telemetry == "aggregateOnly"
    assert priced.amount is None and priced.unpriced.reason == "incompleteUsage"


@pytest.mark.parametrize("fresh_marker", [None, "aggregateOnly"])
@pytest.mark.parametrize("replacement", [
    {"resource_id": "model-2"},
    {"publication_id": "publication-2"},
    {"publication_id": None},
    {"kind": "pool"},
    {
        "kind": "pool", "resource_id": "pool-2", "publication_id": "pool-2",
        "model_endpoint_id": None, "deployment_name": None,
    },
    {
        "resource_id": "model-2", "publication_id": "publication-2",
        "model_endpoint_id": "endpoint-2", "deployment_name": "replacement-model",
    },
])
def test_reused_api_name_does_not_inherit_a_replaced_models_cost_lineage(
    replacement: dict[str, object], fresh_marker: str | None,
) -> None:
    remembered = RolledUpApi(
        api_name="shared-name", resource_id="model-1", publication_id="publication-1",
        kind="model", display_name="Old standalone", model_endpoint_id="endpoint-1",
        deployment_name="old-model", cost_telemetry="aggregateOnly", first_seen_at=NOW,
    )
    fresh = remembered.model_copy(update={**replacement, "cost_telemetry": fresh_marker})
    [merged] = merge_apis([remembered], [fresh], NOW)
    assert merged.cost_telemetry == fresh_marker
    assert merged.kind == fresh.kind
    assert merged.resource_id == fresh.resource_id
    assert merged.publication_id == fresh.publication_id


@pytest.mark.parametrize("subtotal", [1000.0, 1100.0])
def test_known_lower_bound_can_block_but_cannot_claim_a_complete_forecast(subtotal: float) -> None:
    budget = Budget(
        id="b", tenant_id="test", cost_center_id="c", amount=1000, action="block",
    )
    state = BudgetState(id="s", tenant_id="test", budget_id="b", month="2026-03", amount=1000)
    result = evaluate(
        budget, state, spent=subtotal, forecast=99999, through=NOW, unpriced_tokens=0,
        now=NOW, spend_is_lower_bound=True, error="Spend is an incomplete lower bound.",
    )
    assert result.state.blocked
    assert 100 in result.state.crossed
    assert result.state.forecast is None
    assert "incomplete" in result.state.error


def test_below_cap_incomplete_subtotal_cannot_lift_or_rearm_a_raised_budget() -> None:
    budget = Budget(
        id="b", tenant_id="test", cost_center_id="c", amount=2000, action="block",
    )
    state = BudgetState(
        id="s", tenant_id="test", budget_id="b", month="2026-03", amount=1000,
        blocked=True, blocked_at=NOW, crossed=[50, 80, 100],
    )
    result = evaluate(
        budget, state, spent=1100, forecast=None, through=NOW, unpriced_tokens=0,
        now=NOW, spend_is_lower_bound=True,
    )
    assert result.state.blocked and result.unblocked is None
    assert result.state.crossed == [50, 80, 100]
    assert result.state.amount == 1000


def test_incomplete_subtotal_below_cap_cannot_prove_a_block() -> None:
    budget = Budget(id="b", tenant_id="test", cost_center_id="c", amount=1000, action="block")
    state = BudgetState(id="s", tenant_id="test", budget_id="b", month="2026-03", amount=1000)
    result = evaluate(
        budget, state, spent=900, forecast=2000, through=NOW, unpriced_tokens=0,
        now=NOW, spend_is_lower_bound=True,
    )
    assert not result.state.blocked and result.blocked is False
    assert 100 not in result.state.crossed and result.state.forecast is None
