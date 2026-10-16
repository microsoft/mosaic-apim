"""Leased usage producers. Source evidence and durable notification intent commit together.

Register ``reconcile`` as a NotificationSourceService additional source. Delivery still goes
through current preference/authorization checks; this service never sends to an external address.
"""

import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from mosaic_api.domain import PrincipalKind, utc_now
from mosaic_api.errors import ConflictError
from mosaic_api.integrations.graph import DirectoryLookup
from mosaic_api.notifications import (
    NOTIFICATION_RETENTION_DAYS,
    CapabilityCheck,
    ChannelCadence,
    DigestSchedule,
    DigestWatermark,
    DigestWindow,
    NotificationAlertIncident,
    NotificationAudience,
    NotificationCategory,
    NotificationEvent,
    NotificationEventType,
    NotificationPersona,
    NotificationPreferences,
    NotificationSubject,
    NotificationTemplateData,
    NotificationWorkerState,
)
from mosaic_api.repositories import DirectoryRepository
from mosaic_api.repositories.notifications import NotificationPersistence
from mosaic_api.services.directory import Actor
from mosaic_api.services.notification_events import (
    build_notification_event,
    notification_event_id,
    notification_preferences_id,
    notification_record_id,
)
from mosaic_api.services.usage_notification_readiness import usage_readiness_checks
from mosaic_api.services.usage_notification_schedule import (
    completed_window,
    scheduled_delivery,
)
from mosaic_api.services.usage_notification_source import (
    UsageCounterObservation,
    UsageNotificationSnapshot,
    UsageNotificationSource,
)

UsageRecipientAuthorization = Callable[
    [NotificationEvent, str, NotificationPersona], Awaitable[bool]
]
MAX_USAGE_SUBJECTS_PER_CYCLE = 100


class UsageNotificationIdentities(Protocol):
    async def subjects(
        self, tenant_id: str, preferences: Sequence[NotificationPreferences]
    ) -> Sequence[tuple[str, NotificationPersona]]: ...

    async def actor(self, tenant_id: str, object_id: str) -> Actor | None: ...


class DirectoryUsageNotificationIdentities:
    """Known users plus explicit preference owners, not an invented administrator roster.

    Graph group checks are fresh on each resolution. With Graph unavailable, direct local users
    can be resolved from the local repository, but no security-group claims are manufactured.
    Application principals are deliberately not treated as people.
    """

    def __init__(
        self, repository: DirectoryRepository, *, directory_lookup: DirectoryLookup | None = None
    ) -> None:
        self._repository = repository
        self._lookup = directory_lookup

    async def subjects(
        self, tenant_id: str, preferences: Sequence[NotificationPreferences]
    ) -> Sequence[tuple[str, NotificationPersona]]:
        subjects: set[tuple[str, NotificationPersona]] = {
            (item.user_object_id.casefold(), item.persona)
            for item in preferences
            if item.tenant_id == tenant_id
        }
        subjects.update(
            (principal.object_id.casefold(), "portal")
            for principal in await self._repository.list_principals(tenant_id)
            if principal.kind in {PrincipalKind.USER, PrincipalKind.AGENT_USER}
        )
        return sorted(subjects)

    async def actor(self, tenant_id: str, object_id: str) -> Actor | None:
        principals = await self._repository.list_principals(tenant_id)
        if self._lookup is None:
            principal = next(
                (item for item in principals if item.object_id.casefold() == object_id.casefold()),
                None,
            )
            if principal is None or principal.kind not in {
                PrincipalKind.USER,
                PrincipalKind.AGENT_USER,
            }:
                return None
            return Actor(object_id=object_id, tenant_id=tenant_id)
        person = await self._lookup.get_object(object_id)
        if person is None or person.kind != "user":
            return None
        groups = [
            item.object_id for item in principals if item.kind == PrincipalKind.SECURITY_GROUP
        ]
        membership = await self._lookup.member_groups(object_id, groups) if groups else set()
        return Actor(
            object_id=object_id,
            tenant_id=tenant_id,
            group_ids=frozenset(group.casefold() for group in membership),
        )


def _usage_enabled(preferences: NotificationPreferences) -> bool:
    category = next(
        (
            item
            for item in preferences.categories
            if item.category == NotificationCategory.USAGE_AND_BUDGETS
        ),
        None,
    )
    # Omitted categories have the same safe defaults as the preference resolver.
    return (
        category is None
        or category.in_app
        or any(channel.mode != "off" for channel in (category.email, category.teams))
    )


def _summary_channel(
    preferences: NotificationPreferences, schedule: DigestSchedule
) -> Literal["email", "teamsPersonal", "inApp"] | None:
    category = next(
        (
            item
            for item in preferences.categories
            if item.category == NotificationCategory.USAGE_AND_BUDGETS
        ),
        None,
    )
    if category is None:
        return "inApp"
    channels: tuple[tuple[Literal["email", "teamsPersonal"], ChannelCadence], ...] = (
        ("email", category.email),
        ("teamsPersonal", category.teams),
    )
    for name, channel in channels:
        if channel.mode == "digest" and schedule.period in channel.digest_periods:
            return name
    return "inApp" if category.in_app else None


def _schedule_identity(preferences: NotificationPreferences, schedule: DigestSchedule) -> str:
    return notification_record_id(
        "usageSchedule",
        preferences.tenant_id,
        preferences.user_object_id.casefold(),
        preferences.persona,
        preferences.time_zone,
        schedule.model_dump_json(),
    )


def _summary_text(window: DigestWindow, snapshot: UsageNotificationSnapshot) -> str:
    zone = ZoneInfo(window.time_zone)
    cost = (
        "Cost is unknown or unavailable."
        if snapshot.cost_usd is None
        else f"Estimated cost: USD {snapshot.cost_usd:.4f}."
    )
    parts = [
        f"{'Catch-up: completed' if window.catch_up else 'Completed'} "
        f"{window.period} local-calendar usage ({window.time_zone}), "
        f"{window.start_at.astimezone(zone):%Y-%m-%d} to "
        f"{window.end_at.astimezone(zone):%Y-%m-%d} (end exclusive).",
        f"Calls: {snapshot.calls if snapshot.calls is not None else 'unknown'}; "
        f"tokens: {snapshot.tokens if snapshot.tokens is not None else 'unknown/not applicable'}.",
        cost,
        f"Coverage: {snapshot.coverage}.",
    ]
    if snapshot.through:
        parts.append(f"Measured through {snapshot.through.isoformat()}.")
    for counter in snapshot.counters[:2]:
        evidence = counter.evidence
        utilization = (
            f"{evidence.used / evidence.limit * 100:.1f}%"
            if evidence.limit and evidence.used is not None and evidence.coverage == "complete"
            else "unknown"
        )
        parts.append(
            f"Configured {counter.metric} quota: {evidence.limit:g}/{counter.period} UTC; "
            f"observed utilization {utilization}."
            if evidence.limit
            else f"{counter.metric} quota unavailable."
        )
    for resource in snapshot.resources[:2]:
        parts.append(
            f"{(resource.resource_name or 'Resource')[:60]}: "
            f"{resource.calls if resource.calls is not None else 'unknown'} calls, "
            f"{resource.tokens if resource.tokens is not None else 'unknown/not applicable'} "
            "tokens."
        )
    for shared in snapshot.shared_monthly[:1]:
        parts.append(
            f"Shared cost center {shared.cost_center.name[:40]}, {shared.month_start} UTC month "
            f"(everyone, not personal; coverage unverified): observed {shared.requests} calls, "
            f"{shared.total_tokens} tokens."
        )
        for pooled in shared.resources[:1]:
            if pooled.utilization is not None:
                parts.append(
                    f"Observed shared monthly pool utilization: {pooled.utilization * 100:.1f}% "
                    "(coverage unverified)."
                )
    parts.append("Rollups are delayed; exact near-real-time RPM/TPM warnings are unsupported.")
    result = ""
    for part in parts:
        if len(result) + len(part) + 1 > 970:
            return result + " More detail in MOSAIC."
        result += (" " if result else "") + part
    return result


class UsageNotificationProducer:
    def __init__(
        self,
        repository: NotificationPersistence,
        source: UsageNotificationSource,
        identities: UsageNotificationIdentities,
        *,
        tenant_id: str,
        authorize_recipient: UsageRecipientAuthorization | None = None,
        owner: str | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._source = source
        self._identities = identities
        self._tenant = tenant_id
        self._authorize = authorize_recipient
        self._owner = owner or f"usage-{uuid4().hex}"
        self._clock = clock

    async def _preferences(
        self, object_id: str, persona: NotificationPersona
    ) -> NotificationPreferences:
        stored = await self._repository.get_preferences(self._tenant, object_id, persona)
        return stored or NotificationPreferences(
            id=notification_preferences_id(self._tenant, object_id, persona),
            tenant_id=self._tenant,
            user_object_id=object_id,
            persona=persona,
        )

    async def readiness_checks(
        self, tenant_id: str, object_id: str, persona: NotificationPersona
    ) -> list[CapabilityCheck]:
        """The host supplies its authenticated self scope, never a submitted recipient ID.

        Reads only this owner's persona-specific checkpoints and opted-in schedule watermarks.
        Current role, identity and source visibility must pass before any saved figures surface.
        Never rolls up telemetry, publishes events, sends messages or changes settings.
        """

        now = self._clock().astimezone(UTC)
        preferences = NotificationPreferences(
            id=notification_preferences_id(tenant_id, object_id, persona),
            tenant_id=tenant_id,
            user_object_id=object_id,
            persona=persona,
        )
        snapshot: UsageNotificationSnapshot | None = None
        summaries: list[tuple[DigestWindow, UsageNotificationSnapshot]] = []
        reason: str | None = None
        if tenant_id.casefold() != self._tenant.casefold():
            reason = "Usage production is not configured for this authenticated tenant scope."
        else:
            try:
                preferences = await self._preferences(object_id, persona)
                record = await self._repository.get_worker_state(
                    self._tenant, self._evidence_worker(object_id, persona, "latest")
                )
                if record and record.checkpoint:
                    snapshot = UsageNotificationSnapshot.model_validate_json(record.checkpoint)
                    actor = await self._identities.actor(self._tenant, object_id)
                    probe = build_notification_event(
                        tenant_id=self._tenant,
                        event_type=NotificationEventType.USAGE_SUMMARY,
                        source_id=record.id,
                        source_revision=str(preferences.revision),
                        occurred_at=now,
                        origin=f"usageProducer:{persona}",
                        subject=NotificationSubject(),
                        audience=NotificationAudience(user_object_ids=[object_id]),
                    )
                    if self._authorize is None:
                        reason = (
                            "Fresh API app-role verification is not configured for usage health."
                        )
                    elif (
                        actor is None
                        or actor.tenant_id.casefold() != tenant_id.casefold()
                        or actor.object_id.casefold() != object_id.casefold()
                    ):
                        reason = (
                            "Current directory identity "
                            "could not be verified. Saved figures are withheld."
                        )
                    elif not await self._source.eligible(actor, snapshot):
                        reason = (
                            "Current original usage-scope visibility could not be verified. "
                            "Saved figures are withheld; review your current grants."
                        )
                    elif not await self._authorize(probe, object_id, persona):
                        reason = (
                            "Current API app-role assignment could not be verified. "
                            "Saved figures are withheld; verify app-role assignments and Graph "
                            "Application.Read.All, User.ReadBasic.All and GroupMember.Read.All."
                        )
                    if reason:
                        snapshot = None
                    else:
                        for schedule in preferences.digest_schedules:
                            watermark = await self._repository.get_digest_watermark(
                                self._tenant, _schedule_identity(preferences, schedule)
                            )
                            if watermark is None or watermark.last_completed_end_at is None:
                                continue
                            event_id = notification_event_id(
                                self._tenant,
                                NotificationEventType.USAGE_SUMMARY,
                                watermark.id,
                                watermark.last_completed_end_at.isoformat(),
                            )
                            checkpoint = await self._repository.get_worker_state(
                                self._tenant, self._evidence_worker(object_id, persona, event_id)
                            )
                            if checkpoint is None or not checkpoint.checkpoint:
                                continue
                            payload = json.loads(checkpoint.checkpoint)
                            if payload.get("eventId") != event_id:
                                continue
                            completed = UsageNotificationSnapshot.model_validate(
                                payload["snapshot"]
                            )
                            window = DigestWindow.model_validate(payload["window"])
                            if actor is not None and await self._source.eligible(actor, completed):
                                summaries.append((window, completed))
            except Exception:
                # Health remains actionable during storage/directory outages; raw errors may
                # contain tenant/resource identifiers and are never personal readiness evidence.
                snapshot, summaries = None, []
                reason = (
                    "Your saved usage evidence or current directory/source authorization is "
                    "unavailable or invalid. Check producer storage and directory permissions."
                )
        return usage_readiness_checks(
            preferences,
            now=now,
            snapshot=snapshot,
            summaries=summaries,
            unavailable_reason=reason,
        )

    async def reconcile(self) -> None:
        state = await self._repository.get_worker_state(self._tenant, "usageNotifications")
        if state and state.paused:
            return
        preferences = await self._repository.list_preferences(self._tenant)
        subjects = sorted(
            await self._identities.subjects(self._tenant, preferences),
            key=lambda subject: json.dumps(subject),
        )
        remaining = [
            subject
            for subject in subjects
            if state is None or state.checkpoint is None or json.dumps(subject) > state.checkpoint
        ] or subjects
        batch = remaining[:MAX_USAGE_SUBJECTS_PER_CYCLE]
        errors = 0
        for object_id, persona in batch:
            scope = notification_record_id("usageSubject", object_id.casefold(), persona)
            lease = await self._repository.claim_lease(
                self._tenant, scope, self._owner, now=self._clock(), lease_seconds=300
            )
            if lease is None:
                continue
            try:
                actor = await self._identities.actor(self._tenant, object_id)
                if actor is None or actor.tenant_id != self._tenant:
                    continue
                current = await self._preferences(object_id, persona)
                if not _usage_enabled(current):
                    continue
                snapshot = await self._source.observe(actor, now=self._clock())
                fresh_actor = await self._identities.actor(self._tenant, object_id)
                if fresh_actor is None or not await self._source.eligible(fresh_actor, snapshot):
                    continue
                if self._clock() >= lease.expires_at:
                    errors += 1
                    continue
                await self._save_evidence(object_id, persona, "latest", snapshot)
                for counter in snapshot.counters:
                    for threshold in sorted(set(current.thresholds)):
                        await self._counter(
                            actor,
                            current,
                            snapshot,
                            counter,
                            threshold,
                            lease_expires_at=lease.expires_at,
                        )
                for schedule in current.digest_schedules:
                    if _summary_channel(current, schedule) is not None:
                        await self._summary(
                            actor, current, schedule, lease_expires_at=lease.expires_at
                        )
            except ConflictError:
                # A competing CAS/lease winner or a just-edited setting is retried next cycle.
                continue
            except Exception:
                errors += 1
            finally:
                try:
                    await self._repository.release_lease(lease)
                except ConflictError:
                    pass
        now = self._clock()
        await self._repository.update_worker_state(
            self._tenant,
            "usageNotifications",
            lambda current: current.model_copy(
                update={
                    "heartbeat_at": now,
                    "last_succeeded_at": current.last_succeeded_at if errors else now,
                    "last_error": "Usage source or eligibility checks unavailable."
                    if errors
                    else None,
                    "backlog": errors + max(0, len(remaining) - len(batch)),
                    "checkpoint": json.dumps(batch[-1]) if len(remaining) > len(batch) else None,
                }
            ),
        )

    async def _allowed(
        self,
        actor: Actor,
        preferences: NotificationPreferences,
        snapshot: UsageNotificationSnapshot,
        event: NotificationEvent,
    ) -> bool:
        latest = await self._preferences(actor.object_id, preferences.persona)
        if latest.revision != preferences.revision or not _usage_enabled(latest):
            return False
        fresh_actor = await self._identities.actor(self._tenant, actor.object_id)
        if fresh_actor is None or not await self._source.eligible(fresh_actor, snapshot):
            return False
        if self._authorize is not None:
            return await self._authorize(event, actor.object_id, preferences.persona)
        # Without actual role discovery an administrative persona cannot be verified.
        return preferences.persona == "portal"

    def _evidence_worker(self, object_id: str, persona: str, key: str) -> str:
        return notification_record_id("usageEvidence", object_id.casefold(), persona, key)

    async def _save_evidence(
        self, object_id: str, persona: str, key: str, snapshot: UsageNotificationSnapshot
    ) -> None:
        await self._repository.update_worker_state(
            self._tenant,
            self._evidence_worker(object_id, persona, key),
            lambda current: current.model_copy(
                update={
                    "heartbeat_at": snapshot.observed_at,
                    "checkpoint": snapshot.model_dump_json(),
                    "ttl": NOTIFICATION_RETENTION_DAYS * 86400,
                }
            ),
        )

    async def _event_evidence(
        self,
        event: NotificationEvent,
        actor: Actor,
        persona: NotificationPersona,
        snapshot: UsageNotificationSnapshot,
        *,
        window: DigestWindow | None = None,
    ) -> NotificationWorkerState:
        worker = self._evidence_worker(actor.object_id, persona, event.id)
        checkpoint = await self._repository.get_worker_state(self._tenant, worker)
        checkpoint = checkpoint or NotificationWorkerState(
            id=notification_record_id("notificationWorker", self._tenant, worker),
            tenant_id=self._tenant,
            worker=worker,
        )
        return checkpoint.model_copy(
            update={
                "heartbeat_at": snapshot.observed_at,
                "checkpoint": json.dumps(
                    {
                        "window": window.model_dump(mode="json") if window else None,
                        "snapshot": snapshot.model_dump(mode="json"),
                        "eventId": event.id,
                    },
                    separators=(",", ":"),
                ),
                "ttl": NOTIFICATION_RETENTION_DAYS * 86400,
            }
        )

    async def authorize_delivery(
        self, event: NotificationEvent, object_id: str, persona: NotificationPersona
    ) -> bool:
        """Compose into the delivery recipient-authorizer, in addition to current role checks.

        Historical usage remains private after partial grant removal, not merely after the user
        loses their final grant. Missing/expired evidence is fail-closed.
        """

        if (
            event.tenant_id != self._tenant
            or event.origin != f"usageProducer:{persona}"
            or object_id.casefold()
            not in {candidate.casefold() for candidate in event.audience.user_object_ids}
        ):
            return False
        checkpoint = await self._repository.get_worker_state(
            self._tenant, self._evidence_worker(object_id, persona, event.id)
        )
        if checkpoint is None or not checkpoint.checkpoint:
            return False
        try:
            payload = json.loads(checkpoint.checkpoint)
            if payload["eventId"] != event.id:
                return False
            snapshot = UsageNotificationSnapshot.model_validate(payload["snapshot"])
        except (ValueError, KeyError, TypeError):
            return False
        actor = await self._identities.actor(self._tenant, object_id)
        return actor is not None and await self._source.eligible(actor, snapshot)

    async def _counter(
        self,
        actor: Actor,
        preferences: NotificationPreferences,
        snapshot: UsageNotificationSnapshot,
        counter: UsageCounterObservation,
        threshold: int,
        *,
        lease_expires_at: datetime,
    ) -> None:
        latest = await self._preferences(actor.object_id, preferences.persona)
        if (
            latest.revision != preferences.revision
            or not _usage_enabled(latest)
            or self._clock() >= lease_expires_at
        ):
            return
        now = snapshot.observed_at
        evidence = counter.evidence
        identity = notification_record_id(
            "usageIncident",
            self._tenant,
            actor.object_id.casefold(),
            preferences.persona,
            counter.scope_id,
            counter.scope_revision,
            counter.period_start_at.isoformat(),
            counter.period_end_at.isoformat(),
            str(threshold),
        )
        current = await self._repository.get_alert_incident(self._tenant, identity)
        incident = current or NotificationAlertIncident(
            id=identity,
            tenant_id=self._tenant,
            scope_id=counter.scope_id,
            scope_revision=counter.scope_revision,
            period_start_at=counter.period_start_at,
            period_end_at=counter.period_end_at,
            threshold_percent=threshold,
            first_observed_at=now,
            last_observed_at=now,
            evidence=evidence,
        )
        incident = incident.model_copy(update={"last_observed_at": now, "evidence": evidence})
        if (
            counter.unsupported_reason
            or evidence.coverage != "complete"
            or not evidence.priced
            or evidence.limit is None
            or evidence.used is None
        ):
            # Keep a prior open incident open: insufficient evidence is never a recovery.
            await self._repository.save_alert_incident(incident)
            return
        crossed = evidence.used / evidence.limit * 100 >= threshold
        if not crossed:
            await self._repository.save_alert_incident(
                incident.model_copy(
                    update={
                        "state": "recovered",
                        "recovered_at": now if incident.state == "open" else incident.recovered_at,
                    }
                )
            )
            return
        repeat = incident.last_notified_at is None or now - incident.last_notified_at >= timedelta(
            seconds=preferences.repeat_cooldown_seconds
        )
        if incident.state == "open" and not repeat:
            await self._repository.save_alert_incident(incident)
            return
        event = build_notification_event(
            tenant_id=self._tenant,
            event_type=(
                NotificationEventType.QUOTA_REACHED
                if threshold >= 100
                else NotificationEventType.QUOTA_APPROACHING
            ),
            source_id=identity,
            source_revision=now.isoformat(),
            occurred_at=now,
            origin=f"usageProducer:{preferences.persona}",
            subject=counter.subject.model_copy(update={"incident_id": identity}),
            audience=NotificationAudience(user_object_ids=[actor.object_id]),
            data=NotificationTemplateData(
                threshold_percent=threshold,
                period_label=f"{counter.period} UTC",
                calls=int(evidence.used) if counter.metric == "requests" else None,
                tokens=int(evidence.used) if counter.metric == "tokens" else None,
                coverage=evidence.coverage,
                unpriced_tokens=evidence.unpriced_tokens,
                summary=(
                    f"Observed {counter.metric} usage: {evidence.used:g}/{evidence.limit:g} "
                    f"({evidence.used / evidence.limit * 100:.1f}%) in the "
                    f"{counter.period.lower()} UTC period; threshold {threshold}%. "
                    "Rollups are delayed, not a live balance."
                ),
            ),
            severity="warning",
        )
        if not await self._allowed(actor, preferences, snapshot, event):
            return
        if self._clock() >= lease_expires_at:
            return
        await self._repository.commit_source_event(
            event,
            incident=incident.model_copy(
                update={"state": "open", "last_notified_at": now, "recovered_at": None}
            ),
            checkpoint=await self._event_evidence(event, actor, preferences.persona, snapshot),
        )

    async def _summary(
        self,
        actor: Actor,
        preferences: NotificationPreferences,
        schedule: DigestSchedule,
        *,
        lease_expires_at: datetime,
    ) -> None:
        now = self._clock().astimezone(UTC)
        identity = _schedule_identity(preferences, schedule)
        watermark = await self._repository.get_digest_watermark(self._tenant, identity)
        channel = _summary_channel(preferences, schedule)
        if channel is None:
            return
        if watermark is None:
            activated_at = max(preferences.created_at, preferences.updated_at)
            previous_due = scheduled_delivery(
                activated_at, schedule, preferences.time_zone, following=False
            )
            previous = completed_window(previous_due, schedule, preferences.time_zone)
            watermark = await self._repository.save_digest_watermark(
                DigestWatermark(
                    id=identity,
                    tenant_id=self._tenant,
                    recipient_reference=actor.object_id,
                    persona=preferences.persona,
                    channel=channel,
                    schedule_revision=preferences.revision,
                    period=schedule.period,
                    time_zone=preferences.time_zone,
                    last_completed_end_at=previous.end_at,
                    next_due_at=scheduled_delivery(
                        activated_at, schedule, preferences.time_zone, following=True
                    ),
                )
            )
        if watermark.next_due_at is None or watermark.next_due_at > now:
            return
        due = scheduled_delivery(now, schedule, preferences.time_zone, following=False)
        latest = completed_window(due, schedule, preferences.time_zone)
        start = watermark.last_completed_end_at or latest.start_at
        if start >= latest.end_at:
            await self._repository.save_digest_watermark(
                watermark.model_copy(
                    update={
                        "next_due_at": scheduled_delivery(
                            now, schedule, preferences.time_zone, following=True
                        )
                    }
                )
            )
            return
        window = DigestWindow(
            period=schedule.period,
            time_zone=preferences.time_zone,
            start_at=start,
            end_at=latest.end_at,
            due_at=due,
            catch_up=start < latest.start_at,
        )
        # Persist due intent before source reads. A restart coalesces from this same watermark.
        watermark = await self._repository.save_digest_watermark(
            watermark.model_copy(update={"pending_window": window})
        )
        snapshot = await self._source.observe(actor, now=now, window=window)
        zone = ZoneInfo(window.time_zone)
        label = (
            f"{window.start_at.astimezone(zone):%Y-%m-%d} to "
            f"{window.end_at.astimezone(zone):%Y-%m-%d} ({window.time_zone})"
        )
        event = build_notification_event(
            tenant_id=self._tenant,
            event_type=NotificationEventType.USAGE_SUMMARY,
            source_id=identity,
            source_revision=window.end_at.isoformat(),
            occurred_at=window.due_at,
            origin=f"usageProducer:{preferences.persona}",
            subject=NotificationSubject(),
            audience=NotificationAudience(user_object_ids=[actor.object_id]),
            data=NotificationTemplateData(
                reason_code=f"digest:{schedule.period}",
                period_label=label,
                calls=snapshot.calls,
                tokens=snapshot.tokens,
                cost_usd=snapshot.cost_usd,
                coverage=snapshot.coverage,
                unpriced_tokens=snapshot.unpriced_tokens,
                summary=_summary_text(window, snapshot),
            ),
        )
        if not await self._allowed(actor, preferences, snapshot, event):
            return
        if self._clock() >= lease_expires_at:
            return
        checkpoint = await self._event_evidence(
            event, actor, preferences.persona, snapshot, window=window
        )
        await self._repository.commit_source_event(
            event,
            watermark=watermark.model_copy(
                update={
                    "last_completed_end_at": window.end_at,
                    "pending_window": None,
                    "next_due_at": scheduled_delivery(
                        now, schedule, preferences.time_zone, following=True
                    ),
                }
            ),
            checkpoint=checkpoint,
        )
