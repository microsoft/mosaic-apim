"""Self-scoped producer capability evidence, never a tenant-wide usage or delivery claim."""

from datetime import UTC, datetime, timedelta

from mosaic_api.notifications import (
    CapabilityCheck,
    CapabilityRemediation,
    CapabilityState,
    DigestSchedule,
    DigestWindow,
    NotificationCategory,
    NotificationPreferences,
)
from mosaic_api.services.notification_events import notification_record_id
from mosaic_api.services.usage_notification_schedule import completed_window, scheduled_delivery
from mosaic_api.services.usage_notification_source import UsageNotificationSnapshot
from mosaic_api.usage_telemetry import rollup_stale_after


def usage_readiness_checks(
    preferences: NotificationPreferences,
    *,
    now: datetime,
    snapshot: UsageNotificationSnapshot | None,
    summaries: list[tuple[DigestWindow, UsageNotificationSnapshot]],
    unavailable_reason: str | None = None,
) -> list[CapabilityCheck]:
    scope = notification_record_id(
        "personalUsageReadiness",
        preferences.tenant_id,
        preferences.user_object_id.casefold(),
        preferences.persona,
    )
    portal = preferences.persona == "portal"
    usage_path = "/usage" if portal else "/analytics"
    preferences_path = "/notification-preferences" if portal else "/profile"
    telemetry_path = "/usage" if portal else "/gateways"
    checks: list[CapabilityCheck] = []

    def check(
        feature: str,
        state: CapabilityState,
        reason: str,
        action: str,
        *,
        evidence: list[str] | None = None,
        path: str = usage_path,
        required: bool = True,
        expires: datetime | None = None,
        counter_scope: str | None = None,
    ) -> None:
        checks.append(
            CapabilityCheck(
                feature=feature,
                scope=counter_scope or scope,
                required=required,
                state=state,
                reason=reason,
                evidence=evidence or [],
                checked_at=now,
                fresh_until=expires if expires is not None and expires > now else None,
                configuration_revision=str(preferences.revision),
                remediation=CapabilityRemediation(
                    action=action,
                    path=path,
                    owner="user" if path == preferences_path else "mosaicAdmin",
                ),
            )
        )

    check(
        "usageLiveRateWarnings",
        "needsAction",
        "Exact near-real-time RPM/TPM warnings are unsupported: retained rollups are delayed "
        "and hourly; observed peaks are not a live remaining balance.",
        "Use gateway rate enforcement and observed throttling. A reliable authorized live counter "
        "source is required before enabling precise near-real-time warnings.",
        required=False,
    )
    check(
        "usageSharedPoolAlerts",
        "needsAction",
        "Shared-pool threshold alerts are unsupported without authoritative aggregate counter "
        "scope and coverage. Shared UTC-month totals are not a personal limit.",
        "Review shared monthly usage and configured pools. Ask an administrator to establish "
        "authoritative aggregate enforcement coverage before enabling shared-pool alerts.",
        required=False,
    )
    check(
        "usageOnBehalfLocalSummaries",
        "needsAction",
        "Local-calendar summaries of model calls made through MCP applications are unsupported: "
        "their caller attribution has daily, not local-hour, detail.",
        "Use the separate on-behalf breakdown in My usage. Hour-resolved caller attribution is "
        "required before including it in local-calendar summaries.",
        required=False,
    )

    schedules = preferences.digest_schedules or [DigestSchedule(period="daily")]
    windows = [
        completed_window(
            scheduled_delivery(now, schedule, preferences.time_zone, following=True),
            schedule,
            preferences.time_zone,
        )
        for schedule in schedules
    ]
    subhour = any(
        instant.minute or instant.second or instant.microsecond
        for window in windows
        for instant in (window.start_at, window.end_at)
    )
    partial_utc_day = any(
        instant.astimezone(UTC).hour or instant.minute or instant.second or instant.microsecond
        for window in windows
        for instant in (window.start_at, window.end_at)
    )
    check(
        "usageCalendarSummaryCounts",
        "needsAction" if subhour else "unknown",
        (
            "This IANA schedule has subhour UTC boundaries; hourly rollups cannot split exact "
            "local-calendar usage and the producer never prorates counts."
            if subhour
            else "This schedule has UTC-hour-aligned boundaries, but a saved schedule alone "
            "does not verify measured local-calendar summary production."
        ),
        "Choose a UTC-hour-aligned time zone such as UTC for exact retained counts, or provide "
        "finer-grained authorized telemetry. Wait for an opted-in completed summary to verify it.",
        path=preferences_path,
        required=False,
    )
    calendar_check = checks[-1]
    check(
        "usageSummaryPricingGrain",
        "needsAction" if partial_utc_day else "unknown",
        (
            "Local-calendar boundaries split UTC days. Retained hourly facts do not retain the "
            "input/output token and deployment pricing detail needed for exact local-period cost."
            if partial_utc_day
            else "UTC-day pricing is supported by the source, but no successful priced summary is "
            "established by saved settings alone."
        ),
        "Choose UTC for daily-grain cost summaries, configure prices for every served model, "
        "and verify a measured completed summary; unknown cost is never reported as zero.",
        path=preferences_path,
        required=False,
    )
    grain_check = checks[-1]
    category = next(
        (
            item
            for item in preferences.categories
            if item.category == NotificationCategory.USAGE_AND_BUDGETS
        ),
        None,
    )
    enabled = bool(preferences.digest_schedules) and (
        category is None
        or category.in_app
        or any(
            channel.mode == "digest" and channel.digest_periods
            for channel in (category.email, category.teams)
        )
    )
    check(
        "usageSummaries",
        "unknown" if enabled else "disabled",
        (
            "Recurring usage summaries are explicitly selected, but production is not yet verified."
            if enabled
            else "Recurring usage summaries are off; in-app defaults do not opt you into summaries "
            "or external delivery."
        ),
        "Select the desired completed daily/weekly/monthly schedules and time zone in your "
        "preferences, then verify the next producer run. External channels require separate "
        "opt-in.",
        path=preferences_path,
        required=False,
    )
    summary_check = checks[-1]

    if snapshot is None:
        reason = unavailable_reason or (
            "No persisted usage observation exists for this tenant, user and persona. A saved "
            "preference is not evidence that telemetry or the producer is functional."
        )
        for feature in ("usageFreshness", "usageCoverage", "usagePricing", "usageQuotaCounters"):
            check(
                feature,
                "unknown",
                reason,
                "Verify the current API app-role assignment and producer registration; ask an "
                "administrator to check gateway/LLM diagnostics, Log Analytics access, rollup "
                "backfill and worker health. Configure fresh Graph role verification with "
                "Application.Read.All, User.ReadBasic.All and GroupMember.Read.All, then "
                "run usage reconciliation again.",
                path=telemetry_path,
            )
        return checks

    max_age = rollup_stale_after(timedelta(minutes=snapshot.freshness_interval_minutes or 15))
    timestamps = [snapshot.observed_at, snapshot.freshness_updated_at, snapshot.through]
    dated = all(instant is not None for instant in timestamps)
    future = any(
        instant is not None and instant > now + timedelta(seconds=30) for instant in timestamps
    )
    expires = min(instant + max_age for instant in timestamps if instant is not None)
    fresh = dated and not future and snapshot.freshness_status == "current" and expires > now
    freshness_state: CapabilityState = (
        "verified"
        if fresh
        else "unknown"
        if not dated or future or snapshot.freshness_status is None
        else "degraded"
    )
    observation = [
        f"Your observation: {snapshot.observed_at.isoformat()}.",
        f"Your source coverage: {snapshot.coverage}.",
    ]
    if snapshot.through:
        observation.append(f"Your source rolled through: {snapshot.through.isoformat()}.")
    check(
        "usageFreshness",
        freshness_state,
        "Current dated rollup evidence exists for your eligible scope; this is not a live balance."
        if fresh
        else "Your saved rollup evidence is stale, incomplete, failing, undated or future-dated; "
        "new crossings and recovery claims must not be inferred from it.",
        "Check API diagnostics and Log Analytics access, complete rollup backfill, and refresh "
        "usage reconciliation. Ask an administrator if these controls are unavailable.",
        evidence=observation,
        path=telemetry_path,
        expires=expires,
    )
    complete = fresh and snapshot.coverage == "complete"
    check(
        "usageCoverage",
        "verified" if complete else "needsAction",
        "Your eligible measured source has complete coverage through its recorded UTC instant."
        if complete
        else "Your saved usage observation does not establish fresh complete coverage. "
        "Missing data must not be treated as healthy zero usage.",
        "Verify every relevant gateway/API is instrumented, complete missing rollup/backfill "
        "windows, and refresh your usage. No enforcement setting is changed by this check.",
        evidence=observation,
        path=telemetry_path,
        expires=expires,
    )

    model_counters = [
        counter
        for counter in snapshot.counters
        if counter.subject.resource is None or counter.subject.resource.kind != "mcpServer"
    ]
    latest_summary = max(summaries, key=lambda item: item[1].observed_at) if summaries else None
    pricing_snapshot = latest_summary[1] if latest_summary else snapshot
    priced = (
        pricing_snapshot.cost_usd is not None and pricing_snapshot.coverage == "complete"
        if latest_summary
        else bool(model_counters) and all(counter.evidence.priced for counter in model_counters)
    )
    check(
        "usagePricing",
        "verified" if fresh and priced else "needsAction",
        "Your saved measured pricing evidence covers the evaluated scope."
        if fresh and priced
        else "Your cost is unknown, unpriced, incomplete, stale or cannot be allocated at "
        "the retained grain. A missing price is not a zero-dollar cost.",
        "Configure prices for every served deployment and verify token metering. For a local "
        "period that splits UTC days, use UTC summaries or provide hourly pricing detail.",
        evidence=[
            f"Your completed summary cost: USD {pricing_snapshot.cost_usd:.6f}."
            if latest_summary and pricing_snapshot.cost_usd is not None
            else f"Your model quota observations with pricing: "
            f"{sum(counter.evidence.priced for counter in model_counters)}/{len(model_counters)}.",
            f"Your observed unpriced tokens: {pricing_snapshot.unpriced_tokens}."
            if pricing_snapshot.unpriced_tokens
            else "Missing pricing or token detail is not proof of zero unpriced usage.",
        ],
        path=usage_path if portal else "/pricing",
        expires=expires,
    )

    for counter in snapshot.counters[:20]:
        evidence = counter.evidence
        usable = (
            fresh
            and evidence.coverage == "complete"
            and evidence.priced
            and evidence.limit is not None
            and evidence.used is not None
            and counter.unsupported_reason is None
        )
        facts = [
            f"Scope: {evidence.scope_kind}; metric: {counter.metric}; "
            f"period: {counter.period} UTC.",
            f"Accounting window: {counter.period_start_at.isoformat()} to "
            f"{counter.period_end_at.isoformat()}.",
            f"Coverage: {evidence.coverage}; pricing sufficient: {evidence.priced}.",
            f"Your notification thresholds: {preferences.thresholds}; "
            f"repeat cooldown: {preferences.repeat_cooldown_seconds} seconds.",
        ]
        if evidence.limit is not None:
            facts.append(f"Configured limit: {evidence.limit:g}.")
        if evidence.used is not None:
            facts.append(f"Last observed usage: {evidence.used:g}; not a live remaining balance.")
        check(
            "usageQuotaCounter",
            "verified" if usable else "needsAction",
            "This finite counter has fresh, priced evidence in its authoritative accounting scope."
            if usable
            else "This counter cannot currently produce trustworthy crossings/recovery: scope, "
            "coverage, pricing or freshness is insufficient.",
            "Verify the applied grant and per-member attribution, complete the UTC accounting "
            "window's rollups and pricing, then reconcile again. Notification thresholds never "
            "change the grant's enforced limit.",
            evidence=facts,
            expires=expires,
            counter_scope=notification_record_id(
                "personalUsageCounterReadiness", scope, counter.scope_id, counter.scope_revision
            ),
        )
    if not snapshot.counters:
        check(
            "usageQuotaCounters",
            "disabled" if fresh else "unknown",
            "No finite period call/token counter was observed in your eligible scope; unbounded "
            "resources have no fabricated utilization percentage.",
            "Review your grants in My usage. Only an administrator can configure/apply actual "
            "limits; configure personal notification thresholds separately.",
            required=False,
        )
    if enabled and latest_summary:
        window, completed = latest_summary
        summary_complete = fresh and completed.coverage == "complete"
        summary_check.state = "verified" if summary_complete else "needsAction"
        summary_check.reason = (
            "The producer persisted a measured completed local-calendar summary for you; "
            "this does not prove provider acceptance or recipient delivery."
            if summary_complete
            else "Your last persisted summary has incomplete/delayed coverage or the current "
            "source is no longer fresh. Saved schedules are not proof of functional production."
        )
        summary_check.evidence = [
            f"Completed {window.period} window in {window.time_zone}: "
            f"{window.start_at.isoformat()} to {window.end_at.isoformat()}.",
            f"Your summary coverage: {completed.coverage}; catch-up: {window.catch_up}.",
        ]
        if summary_complete and not subhour:
            calendar_check.state = "verified"
            calendar_check.evidence = summary_check.evidence
            calendar_check.reason = (
                "Measured completed local-calendar counts were persisted for you."
            )
        if summary_complete and completed.cost_usd is not None and not partial_utc_day:
            grain_check.state = "verified"
            grain_check.evidence = [
                f"Your measured completed UTC-day summary cost: USD {completed.cost_usd:.6f}."
            ]
            grain_check.reason = "The producer persisted a fully priced completed UTC-day summary."
    return checks
