# Notification engine composition

The notification settings and delivery protocols are both implemented by one
`NotificationPersistence` repository. Share that instance between the notification service,
preferences, source producers, digest coordinator, and budget bridge.

## Storage

```python
from mosaic_api.repositories.cosmos_notifications import CosmosNotificationRepository
from mosaic_api.repositories.memory_notifications import InMemoryNotificationRepository

# Local memory: approval transactions and the worker share the same event dictionary.
repository = InMemoryNotificationRepository(
    events=entitlement_repository.notification_events,
    audit_events=directory_repository.audit_events,
)

# Azure: source events committed by approval transactions live in desired-state.
repository = CosmosNotificationRepository(
    cosmos_client,
    database_name,
    desired_state_container,
    audit_events_container,
    owns_client=False,
)
```

Select the database/container variables from the existing application's settings.
The concrete constructor takes those four positional arguments. Do not create a second
notification repository for delivery or copy the memory source dictionary.

Enable document-level Cosmos TTL on `desired-state` with `defaultTtl=-1`: settings, leases,
and ordinary checkpoints without a TTL must not expire. Events, inbox items, deliveries,
attempts, and explicitly retained source-evidence checkpoints carry their own TTL. Budget
compatibility outboxes live in `usage-rollups`, whose TTL must also be enabled; preserve its
existing default retention if TTL is already enabled.

## Email and the budget migration

```python
from mosaic_api.services.budget_notification_bridge import BudgetNotificationBridge
from mosaic_api.services.notification_email import AcsNotificationDispatcher
from mosaic_api.services.notifications import NotificationService

acs = AcsNotificationDispatcher(
    email_sender,
    budget_repository,
    mailbox_resolver=preferences.mailbox_resolver,
    frontend_url=settings.frontend_url,
    portal_url=settings.portal_url,
)
budget_bridge = BudgetNotificationBridge(
    budget_repository,
    repository,
    cost_center_repository=cost_center_repository,
    sender=email_sender,
    fallback=acs,
    tenant_id=tenant_id,
    enabled=settings.budgets_enabled,
)
notification_service = NotificationService(
    repository,
    audience_resolver=preferences,
    delivery_authorizer=preferences,
    legacy_budget_authorizer=budget_bridge,
    dispatchers={"email": budget_bridge},
    hosting_verified=settings.scheduled_workers_always_on,
)
```

Add `notification_bridge=budget_bridge` to the existing `BudgetService` construction, preserving
its other arguments. With the bridge injected, that service never also invokes its inline email
sender for migrated notices. Register `budget_bridge.reconcile` in the source service's
`additional_sources`; the budget worker releases pending block/unblock notices only after a
gateway-sync attempt. The deletion-intent journal also recovers a crash between deleting the
desired budget and deleting its monthly state. It cannot announce an unblock while the desired
budget still exists.

Pass the same `settings.budgets_enabled` switch used by `BudgetService` to the bridge.
When false, it does not publish new budget source events, and existing owned legacy jobs defer
without consuming provider attempts or becoming permanent opt-outs. Their ownership and receipts
remain durable for resume. Receipt repair for already projected records may continue while off;
no compatibility email is sent. Re-enabling resumes after the durable next-attempt time.

Keep the preference resolver's `origin="budgetNotificationBridge"` generic-email guard:
the compatibility delivery owns the existing explicit owner/recipient email exclusively. In-app
and authorized Teams targets remain independent. The bridge preserves legacy message content,
ACS operation IDs, and the three-attempt budget, including already consumed attempts. It rechecks
the legacy budget-email switch and audience immediately before sending. A legacy in-flight claim
is unknown, not queued; durable acceptance receipts can repair either projection after a crash.

When installing `NotificationDigestCoordinator`, use it as `audience_resolver`, retain preferences
as `delivery_authorizer`, and wrap the bridge with
`DigestNotificationDispatcher(digests, budget_bridge)` for the email dispatcher. Its fallback chain
is digest wrapper → budget bridge → ordinary ACS adapter. Register `digests.reconcile` as another
source. Teams dispatchers can be wrapped independently; no provider is enabled just by adding it
to the dispatcher map.

## Routers and lifecycle

```python
from mosaic_api.notification_delivery_api import notification_delivery_router
from mosaic_api.notification_inbox_api import notifications_router, portal_notifications_router

app.state.notification_service = notification_service
app.include_router(notifications_router)
app.include_router(portal_notifications_router)
app.include_router(notification_delivery_router)
```

Both inbox families support collection GET with `category`, `unread_only`, `limit`, and
`continuation_token`; exact-item GET; and PATCH of an item with `{"read": true}` or
`{"read": false}`:

- Admin: `/api/v1/notifications/me/inbox`
- Portal: `/api/v1/me/notifications/inbox`

An item's path is the collection path followed by `/{item_id}`. Identity and persona come from
existing authentication, never query parameters. Both GET and PATCH sanitize no-longer-authorized
resource references. The admin delivery router exposes delivery/attempt history, audited retries,
outbox dead letters, and channel health.

After repository readiness and source composition, host
`notification_service.run(tenant_id, interval_seconds=settings.notification_interval_seconds)`
as a retained asyncio task when notifications are enabled. Cancel and await that task during
application shutdown. Register the existing domain-error handler for domain errors.
The worker state key is `notifications`; `hosting_verified` must reflect actual always-on worker
hosting, not merely a successful HTTP startup.

External channels remain opt-in. Compatibility budget email is the deliberate exception only
for its already-explicit legacy switch and recipient audience. A provider acceptance receipt is
not confirmation of delivery. Unknown outcomes are never automatically resent; an audited manual
retry needs explicit acknowledgement and an idempotent provider.
An imported pre-migration unknown claim still owned by the legacy sender cannot be reclaimed
by that retry endpoint; it reports a conflict instead of queuing a send it cannot authorize.

## Independent audience failures

Resolvers that know some safe targets but cannot resolve another optional audience raise
`NotificationPartialAudienceError(targets, reason)` from `services.notifications`. It is a
`DirectoryError` with an immutable `targets` tuple. The engine commits those targets in the same
bounded atomic chunks as ordinary fan-out, without marking the event complete, then checkpoints
the unresolved audience for retry. Narrow per-target Graph/contact authorization failures have
the same effect: other independently authorized targets still materialize.

This typed partial failure is local to its source, not a failed whole worker tick. After its
retry/dead-letter checkpoint succeeds, the worker logs the failure, continues the remaining
bounded fan-out claims, and processes queued private deliveries in the same tick. Worker
`last_error` reports degradation, and `last_succeeded_at` is not advanced as if the tick were
fully successful. Known degradation remains while retry backlog is waiting for its due time.
Storage/checkpoint failures and other non-partial errors still propagate.

Replay deduplicates delivery and inbox IDs and preserves existing read state. Repeated unresolved
failures eventually create a visible outbox dead letter rather than pretending all audiences
were notified. Resolver wrappers, including digest registration, must propagate the partial
targets after processing their own safe candidates; returning them as an ordinary complete list
would lose the unresolved audience.

## Verification boundary

The memory and conditional Cosmos-batch doubles exercise tenant isolation, atomic fan-out,
fenced renewable leases, crash replay, retries, quiet-hour deferrals, exact inbox reads, legacy
budget ownership transfer, and receipt repair. Live ACS acceptance, actual recipient receipt,
Cosmos deployment settings, and continuously running worker hosting require deployment checks;
offline tests do not assert those external facts.
