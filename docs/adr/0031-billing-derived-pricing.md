# ADR 0031: Reviewed billing-derived model pricing

**Status:** Accepted

**Extends:** [ADR 0020](0020-price-list.md); preserves its dated price book and shared pricer.

## Context

Public prices and manually entered rates can differ materially from negotiated cloud consumption
costs. Administrators need reproducible effective rates from detailed billing evidence without
turning gateway telemetry into an invoice or letting an export silently change budgets.

The original ADR's list-price-only description remains a historical record. This decision adds
another sourced price origin; it does not replace provider billing or change runtime model access.

## Decision

- Accept supported uploads and read-only connections for Azure amortized Cost Details/exports,
  AWS CUR/CUR 2.0 in S3, and GCP detailed BigQuery billing exports. Both acquisition paths use one
  bounded normalization/derivation pipeline. Customers provision exports, IAM/trust and datasets.
- Accept USD source currency only. Derive quantity-weighted contract-discounted consumption rates,
  including attributable amortized commitments but excluding tax and one-off credits. Missing
  prepaid allocation, unknown units, ambiguous dimensions and zero denominators are review issues,
  never invented free or blended input/output rates.
- Keep account/resource, model/version, region, tier, routing and usage category distinctions.
  Publish only supported token, PTU-hour, or substantiated deployment-month rates. Do not extend
  telemetry for caches, modality, provider capacity units or other unmeasured dimensions.
- Default the observation to the previous complete calendar month and activation to the UTC
  approval date. Store source month independently of effective date and invoice month. Explicit
  backdating is an acknowledged correction that can revise reports and budget status.
- Require administrator evidence/mapping review and **Publish rates**. Ambiguous/cross-account
  bindings and replacement of a manual override require explicit decisions. A partial candidate
  must not displace a usable complete rate.
- Persist immutable prepared price versions and reveal them through one audited, concurrency-
  protected publication marker. Readers cannot observe a partly prepared publication. Repeated
  approval is idempotent; stale previews must be reloaded.
- Preserve tenant-scoped normalized evidence in bounded chunks, source fingerprints/manifests,
  exclusions, mappings and derivation version. Do not archive raw invoices or upload bodies.
  Reference evidence with internal `mosaic://` identifiers. Portal users see cost basis, not
  account-level evidence.
- Use the isolated API-only management vault for stored billing credentials, never a gateway
  runtime key. Federation is preferred. Provider adapters implement bounded HTTP/token exchange;
  they do not supply a general AWS/GCP SDK credential-discovery mechanism.
- Enable monthly schedules only per explicit connection opt-in. The background worker polls and
  prepares drafts under leases; it never publishes. Durable storage and an always-on API host are
  operational prerequisites. Changed/late data produces another draft, not rewritten approvals.
- Keep every cost consumer on the shared pricer: Analytics, forecasts, chargeback, portal and
  budgets. Carry public/manual/billing-derived/mixed provenance. Existing budget checks, not
  billing connectors, reconcile blocking after rates change.

## Consequences and boundaries

Estimates better reflect observed contracted rates but are not invoice reconciliation. Source
month, incomplete coverage, cache overestimation, reserved idle cost and unpriced usage remain
important qualifications. A completed calendar month is not proof that all adjustments arrived.

Read-only queries/object reads can cost money. Permission checks, limits, expired credentials and
failed/empty acquisitions must remain visible and cannot remove approved prices. Live provider
validation needs authorized real billing datasets; mocked tests and fictional screenshots prove
only their local paths.

There is no FX, automatic publication, IAM/export provisioning, new AWS/GCP public seed, runtime
deployment capability, or administrator percentage/fixed-dollar markup feature. This work relates
to issue #165's actual-cost direction but does **not** close its additional markup requirements.

See [Pricing](../pricing.md) for supported schemas, credential forms, exact deployment settings,
least-privilege guidance, source/query limits and recovery.
