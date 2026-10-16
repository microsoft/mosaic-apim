# Notification preferences and recipient routing

The preferences service is separate from inbox storage and transport delivery. Saved opt-ins
express intent, not proof of provider acceptance or actual delivery. In-app notifications are
on by default; email, personal Teams and recurring summaries are off until explicitly selected.
Administrator and portal preferences are separate even when the same person holds both roles.

## API

All paths below are relative to `/api/v1`. Personal paths infer the tenant, user object ID and
persona from authenticated dependencies; these values are not accepted in update payloads.

| Method | Path | Access |
| --- | --- | --- |
| GET, PUT | `/notifications/me/preferences` | Administrator, own admin persona |
| GET, PUT | `/me/notifications/preferences` | Portal, own portal persona |
| GET | Either personal path followed by `/preview` | Same personal scope |
| GET, PUT | `/settings/notifications` | Administrator |
| GET, POST | `/notification-routes` | Administrator |
| GET, PUT, DELETE | `/notification-routes/{id}` | Administrator |
| GET, POST | `/notification-routes/{id}/preview` | Administrator |
| GET | `/notification-routes/administrator-choices` | Administrator |

Preferences and settings GET/PUT return the saved contract record. Updates accept the
corresponding input contract, not a complete persisted entity. PUT and DELETE optionally accept
`If-Match: "<revision>"`; stale supplied revisions return 409. Repositories additionally enforce
ETag compare-and-swap and record settings mutations and their audits together. Unsaved defaults
have revision 1; their first persisted replacement has revision 2.

Personal previews return `preferences`, `channels` and `checks`. Each channel entry identifies
its category, channel, requested mode, digest periods, availability, suppression reasons and
next scheduled delivery. Requested cadence does not imply a matching tenant route or an
operational transport. Route previews return `routeId`, `recipients`, `unavailable` and
`checkedAt`. GET uses a safe synthetic sample; POST accepts a tenant-matching `NotificationEvent`
for subject-specific revalidation. Neither sends a message.

## Actual administrator discovery

`GraphNotificationDirectory` adapts an existing `GraphDirectoryLookup`; existing directory
protocols and their transitive **grant** group lookups are unchanged.

1. Resolve the API's enterprise application using its configured API client ID.
2. Find the enabled app role whose value is the configured administrator role.
3. Enumerate that API's role assignments, following every page.
4. Include assigned users and **direct user members** of assigned groups. Nested groups do not
   inherit enterprise-app assignments, so notification discovery never uses transitive members.

The adapter requires application permissions `Application.Read.All`, `User.ReadBasic.All` and
`GroupMember.Read.All`, or their supported broader read permissions. Hidden-membership groups may
require additional consent. Permission failures, malformed responses, paging failures and bounded
discovery limits are explicitly unavailable, never a complete partial roster. Graph assignment
replication is eventually consistent; two-minute freshness evidence is not an access token.

For an explicitly selected audience the adapter can verify direct memberships individually,
without enumerating every user in a large assigned group. This is still actual role verification,
not a list of onboarded principals or people who signed in. Full administrator choices and
all-admin routes require complete discovery. With no personal administrator routes, admin event
in-app delivery uses the actual full roster. Once matching selected/all-admin routes exist, enabled
routes define that event's administrator audience; disabled routes do not silently become a
default all-admin audience.

Email addresses come only from a usable directory `mail` value. A display name is never converted
into an address, and a guest's sign-in UPN is never assumed deliverable. A missing mailbox is an
explicit unavailable channel. A failed contact read is discovery unavailability, not an opt-out.
No roster or mailbox lookup falls back to MOSAIC's principal inventory.

## Worker composition and safety

Construct `NotificationPreferenceService(repository, directory=...,
authorize_recipient=..., expand_recipients=..., budget_recipients=..., clock=...)`.
It implements the worker's
`resolve(event)` audience port and `authorize(delivery, events)` send-time authorization port;
`mailbox_resolver(delivery)` supplies a single directory-backed ACS recipient. The
`authorize_recipient(event, object_id, persona)` callback must resolve **current** subject
visibility from durable source records. Portal delivery fails closed when this callback is
absent. Administrator recipients additionally require fresh actual-role verification.
The optional `expand_recipients(event)` callback resolves group grant audiences from durable
former-holder provenance and fresh membership. It runs at fan-out and again before sending,
never replaces recipient authorization, rejects incomplete discovery, and is bounded to 10,000
users. It also runs for provenance-bearing `access.ready` and `access.applyFailed` events;
group object IDs are never personal recipient IDs. Grant group membership may be transitive;
this does not change app-role admin discovery.

Install `notification_preferences_router` and `portal_notification_preferences_router` from
`notifications_preferences_api`, and set `app.state.notification_preference_service`. The
`readiness_checks(tenant_id, object_id=None)` hook composes administrator discovery readiness or
one person's contact readiness with the separate health routers. This module does not register
or shadow `/notifications/health` or `/me/notifications/health`.

Resolution and send authorization both re-read tenant channel settings, event audience, route
filters, personal category/channel/cadence preferences, subject authorization and applicable
Teams/contact readiness. Private events are never redirected into a shared Teams destination.
Shared Teams routes have route-level preferences; one administrator's personal opt-out does not
disable an independently configured shared route. Candidate external delivery is deduplicated
across routes and admin/portal personas for the same person/channel; in-app personas stay distinct.
The engine persists deterministic IDs and merged route eligibility, so replay does not resend.

Independent audiences fail independently. Administrator discovery, group expansion or mailbox
discovery failures raise `NotificationPartialAudienceError` carrying already-resolved safe
targets, rather than discarding the requester's default in-app notification. The engine persists
those targets without completing source fan-out; unresolved audiences remain explicitly
retryable and can be requeued after exhaustion. A failed lookup is never a saved opt-out or
evidence that somebody lost their administrator role. Send-time checks allow a proven recipient
despite unrelated discovery failures, but defer when that recipient's eligibility is unknown.
The digest wrapper registers known-safe digest references before propagating partial failure.

Quiet hours produce a durable next-attempt time and are rechecked at delivery. A changed quiet
window raises `NotificationDeferredError`; the engine delays without consuming provider retry
attempts. Local times use IANA zones, advance through spring-forward gaps, and choose one
fall-back occurrence.

`digest_candidates(event)` exposes opted-in recipients, effective periods, schedules, time zones
and next due times. `NotificationDigestCoordinator` durably registers those candidates before
source fan-out completes, then publishes actual digests for completed local calendar days,
Monday-based weeks and months. A missed run coalesces retained completed periods rather than
replaying empty intervals or sending incomplete periods. Multiple periods deliver only when
explicitly opted in. Digests are separated by recipient, persona, category, channel and schedule;
several matching routes do not send the same person/channel/period a duplicate message.

The coordinator uses a focused `NotificationDigestRepository` facade over existing protocol
operations and the same desired-state tenant partition. It needs no new container or private
backend API. A bounded recipient/schedule stream contains pending original event references
and hashed replay receipts; a prepared window is immutable. Publishing a deterministic
`notificationDigest` source event and removing its stream references share one
`commit_source_event(..., checkpoint=...)` transaction. An interrupted or conflicting publish
does not lose references or create a second delivery. The normal engine still owns delivery
claims, provider idempotency, unknown outcomes and retries.

An unregistered, mismatched, unpublished or early `digest_window_id` never authorizes sending.
The supported async validator is `coordinator.validate_window(delivery, events) -> str | None`;
the coordinator constructor connects it to `preferences.digest_window_validator`. It validates
the durable window, exact recipient/channel/persona/routes, completed calendar interval and due
time, then re-resolves **every original event** through current preferences, authorization and
membership. Removed access or opt-outs filter content instead of leaking old facts. Missing
retained source events defer with an explicit error; expired sources never become invented
summaries. A schedule edit rehomes pending references only to the same target/channel/period,
never to a shared route or different audience.

`DigestNotificationDispatcher` expands the synthetic event into the bounded, newly authorized
original events immediately before the existing email or Teams transport. It preserves the
fallback transport's idempotency support. Quiet hours and future due times defer durably without
consuming provider attempts, including a changed preference between authorization and dispatch.

Completed usage summaries are already durable producer events:
`origin=usageProducer:admin|portal` fixes their persona and
`data.reasonCode=digest:daily|weekly|monthly` selects exactly one opted-in period. Those prepared
`usage.summary` events are not aggregated a second time. Non-summary usage threshold events may
use the generic coordinator when their channel cadence is digest.

### Generic digest composition

```python
from mosaic_api.services.notification_digests import (
    DigestNotificationDispatcher,
    NotificationDigestCoordinator,
)

digests = NotificationDigestCoordinator(
    notification_repository,
    preferences_service,
    tenant_id=tenant_id,
    hosting_verified=hosting_verified,
)

# NotificationService composition:
# audience_resolver=digests
# delivery_authorizer=preferences_service
# dispatchers["email"]=DigestNotificationDispatcher(digests, budget_bridge)
# dispatchers["teamsPersonal"]=DigestNotificationDispatcher(digests, personal_teams)
# dispatchers["teamsChannel"]=DigestNotificationDispatcher(digests, shared_teams)
# legacy_budget_authorizer=budget_bridge
#
# Register digests.reconcile alongside the budget/usage source reconciliation callbacks.
# Compose await digests.readiness_checks() into administrator capability readiness.
```

The `clock` and `owner` constructor overrides support deterministic tests and lease ownership;
`stream_batch_size` defaults to 100 and accepts 1–1000. Persisted round-robin progress prevents
old empty streams starving later recipients. Reconciliation needs an awake host and reports
its own dated heartbeat, hosting verification and errors.

Bounds are explicit backpressure, not silent truncation: 256 pending references and 4096 hashed
retention receipts per recipient/category/channel/period/schedule stream, 16 route references
per original event, 100 originals per published window, and 64 index buckets of at most 512
streams per tenant. Full registration raises a conflict so the original outbox remains retryable.
Evidence checkpoints and receipts follow source retention, with at least 90 days of window
evidence. Index pointers have explicit expiration and are pruned during registration; they
cannot remove a concurrently refreshed recipient stream.

Existing explicit budget recipients and budget-owner addresses remain supported by the
compatibility budget bridge. New routes accept only a reference to that existing budget, never
arbitrary forwarding addresses. `budget_delivery_migrated` defaults false to prevent parallel
general email sends; the bridge's durable source ownership does not require enabling it.
Enable generic budget routing only after a separate coordinated ownership transfer, and supply
the fresh budget-recipient callback. Opaque recipient references contain a budget ID and address
hash, not the external mailbox. The email dispatcher order is ordinary ACS, then
`BudgetNotificationBridge(fallback=ordinary_acs)`, then
`DigestNotificationDispatcher(digests, budget_bridge)`.

Events with `origin=budgetNotificationBridge` always suppress **generic** email candidates,
including opted-in personal/admin routes and general explicit-budget routes. Send-time
authorization enforces the same rule even when directory discovery is unavailable. The migration
bridge's separate compatibility job and legacy-budget authorizer retain exclusive email
ownership and the existing ACS operation identity; in-app and opted-in Teams channels remain
independent. This guard does not require enabling `budget_delivery_migrated` globally.
