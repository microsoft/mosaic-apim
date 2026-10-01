# ADR 0023: Budgets and notifications

**Status:** Accepted

**Builds on:** [ADR 0019](0019-usage-telemetry.md), which rolls gateway logs up into Cosmos,
[ADR 0020](0020-price-list.md), which prices them, and [ADR 0022](0022-cost-centers.md), which
charges every grant and call to a cost center.

## Context

MOSAIC knows what each cost center has spent this month: ADR 0022 charges every call to one, and
ADR 0020 prices it at list price. Administrators asked for budgets that act on that:
- a monthly budget per cost center that warns at 80% and 100%;
- warnings by email to any address, including people who don't use MOSAIC;
- a choice, per cost center, to block its calls at 100% or let them continue;
- an organization-wide budget that only warns.

Several things shape how MOSAIC can do that:
- The gateway decides everything from a compiled policy. It can't call MOSAIC or Cosmos during a
  call, and recompiling every policy to block one cost center would be slow and risky.
- Usage reaches MOSAIC through the rollup job, every 15 minutes, a few minutes behind the
  gateway's logs. A block lands after the spend that triggers it, so a small overshoot is
  accepted.
- Several API instances can run at once. Layer 1's rollup state lost updates until it moved to
  conditional writes, and budget state must not repeat that.
- Prices can be corrected after the fact, so a month's spend can fall as well as rise.
- MOSAIC sends no email today, and keeps no secrets of its own outside Key Vault.
- MOSAIC is a pre-release proof of concept, so no data migration is needed.

## Decision

**A budget is administrator-authored desired state.**
- A cost center has at most one budget, and the organization one. Each is a monthly amount in US
  dollars, at least one cent, for the UTC calendar month, across every gateway, at list price.
- Thresholds are one to five whole percentages from 1 to 1,000, 80 and 100 by default.
- It emails up to 20 addresses, which needn't belong to anyone with a MOSAIC account, and the cost
  center's owners when **Email the owners** is on, which it is by default. With at most 20 owners,
  every email fits one Communication Services message, which takes 50 recipients.
- At 100%, a cost center's budget either lets calls continue, and emails, or blocks them. The
  organization's budget only warns: it covers every cost center, and blocking all of them at
  once is a decision for each cost center's own budget.
- It's a `budget` item in `desired-state`, saved with its audit event and its etag like other
  desired state. It's its own item rather than a field of the `costCenter` item, because
  ADR 0022's recheck bookkeeping rewrites that item, and budget edits shouldn't race it. A cost
  center's budget is removed at the next check after the cost center is.

**Spend is the rolled-up cost of the cost center's grants, as the Cost tab prices it.**
- `AnalyticsService.budget_spend` reads this month's `grant` summaries once for every budget being
  judged, and gives each cost center exactly what `cost_center_spend` gives it: its grants' calls,
  priced by deployment and day, with provisioned throughput shared by each caller's share of its
  tokens. Tests hold the two equal.
- Reserved capacity nobody called, and calls MOSAIC couldn't link to a grant, count only toward
  the organization's budget. Idle capacity isn't any cost center's call, and charging it to one
  could block a team for capacity it never used. The organization's budget counts all of it, so
  no spend escapes every budget.
- Tokens MOSAIC can't price don't count, and the budget says how many there were. When MOSAIC can't
  price the month at all, a check changes nothing on its account.
- The forecast is ADR 0020's, labelled projected: spend so far, times the days in the month, over
  the days elapsed. It appears once a day of figures exists.

**A background check judges budgets.**
- It runs in the API process, like the rollup job, and only where usage comes from rollups. A
  full check runs every 15 minutes (`MOSAIC_BUDGET_INTERVAL_SECONDS`), after every rollup cycle, at
  the start of each UTC month, and for one budget right after an administrator saves it. Budgets
  that have spent 90% or more, with a threshold or a block still to come, are checked every 5
  minutes (`MOSAIC_BUDGET_FAST_INTERVAL_SECONDS`).
- One check judges budgets at a time in a tenant, under the gateway repository's `budgets` scope
  lease. The lease covers only the judging, which reads and writes MOSAIC's own records, as the
  lease's contract asks. The gateway writes and the emails follow it: each email was claimed while
  judging, and each gateway write works the list out again once it has written, writing again if a
  block started or lifted meanwhile, so whichever write lands last leaves what the budgets say.
- Each budget's state is a `budgetState` item in `usage-rollups`: the month, the spend and
  forecast it was judged on, the thresholds reached, each email and how it went, and whether the
  cost center is blocked. It's written only through `update_budget_state(tenant, budget, change)`,
  which reads the item, applies `change`, and writes it back only if its etag still matches,
  reading again on a conflict, as ADR 0019's `update_rollup_state` does. `change` is the pure
  `evaluate()`, so a retried write judges again against what's stored.
- A threshold is reached once a month, even if the budget stops warning at it and starts again.
  Thresholds reached by one check, such as by a budget set below what's already spent, send one
  email, for the highest. A new month starts every budget afresh and lifts every block, and a
  check that runs across the month's end is followed at once by one for the new month.
- Only a change of amount re-arms the thresholds the spend no longer reaches. Spend that falls,
  such as after a price correction, re-arms nothing, so no threshold is emailed twice. An amount
  changed while MOSAIC can't price the month waits for the next check that can.
- With `MOSAIC_BUDGETS_ENABLED` off, budgets are kept but nothing judges them. Saving one doesn't
  judge it, an on-demand check is refused, and a list written meanwhile, such as by a new
  publication, blocks no one.

**Each email goes at most once.**
- A check claims an email, as `sending`, in the same conditional write that reaches its threshold
  or blocks the cost center, and only then sends it. Two instances can't both claim it.
- It goes with an `Operation-Id` derived from the notification's ID, so Communication Services
  takes a retry as the same operation. A 409 for an operation it already accepted counts as sent.
- A refused email is tried again at the next checks, three times in all. A claim more than an
  hour old whose outcome MOSAIC never recorded, because a check stopped, is marked failed and never
  sent again: a missed email is better than one sent twice.
- A cost center's budget emails at each threshold, once a month, and when the cost center is
  blocked or unblocked. Each email names the cost center, the month, its spend, the amount, and
  what happens next. None says who spent it.

**MOSAIC blocks a cost center at the gateway with a named value it owns.**
- Every gateway MOSAIC manages has a plain named value, `mosaic-blocked-cost-centers`. It lists the
  blocked cost centers' keys, comma-separated, oldest block first. A key is the first 12 hex digits
  of a SHA-256 of the cost center's ID, so it doesn't change when the cost center is renamed or
  recoded. When nothing is blocked the value is `-`, because API Management refuses an empty one.
- It holds at most 4,096 characters, 315 keys. A list that doesn't fit leaves the newest blocks out
  and says so in the overview and the log. It doesn't refuse everyone: a long list is the result of
  many blocks, not of a fault.
- After each check that blocks or unblocks anything, and every full check, MOSAIC reads each
  managed gateway's value and writes it only if it differs, auditing
  `gateway.blockedCostCentersUpdated`. A gateway it can't write keeps its old list and is tried
  again at the next check, and the budget shows which gateways enforce the block.
- The governed policy, for models and MCP servers, checks the list once the grant and its cost
  center are known: after every refusal for an unknown caller, grant, or cost center, and before
  the operation guard, the attribution trace, and any limit counts the call. The policy compiles
  each of its cost centers' codes to their keys, and compares the key between commas, so one key
  can't match inside another. A blocked cost center's call gets 403, with a message that names the
  cost center and says its budget is used up, and the gateway's trace records
  `mosaic-deny v=1 r=budget`. Other cost centers' calls, from the same person, still work.
- A value that isn't one MOSAIC wrote refuses every call with 503 and `r=budget-list`: a damaged
  list must never let a blocked cost center's calls through. Both reasons are in ADR 0019's
  denial analytics.
- The block lifts when the UTC month ends, when an administrator raises the amount above the
  spend, turns blocking off, or removes the budget, and the unblock is emailed. A block never
  revokes a grant: it refuses calls at the gateway, and nothing else.
- A block lands about 15 to 30 minutes after the spend: the logs' delay, the rollup's interval,
  and the time API Management takes to apply a named value. That overshoot is accepted.

**The named value exists before any policy refers to it.**
- API Management refuses a policy that refers to a named value that doesn't exist, so every
  governed plan, for models and MCP servers, starts with a step for the list: **create** when the
  gateway lacks it, **no change** when it has it. Applying it creates the list only if it's
  missing, with what's blocked now, and never overwrites one.
- The list isn't a publication's resource. It's the gateway's, shared by every publication on it,
  so unpublish, rollback, and recovery never delete it, and a failed apply makes sure it exists
  before it restores the safe policy that also refers to it. API Management refuses to delete a
  named value a policy still refers to in any case.
- Publications applied before this change don't check budgets until they're applied again.

**Email goes through Azure Communication Services, as MOSAIC's managed identity.**
- MOSAIC calls the Email REST API (`2023-03-31`) with a token for
  `https://communication.azure.com/.default`, or `https://communication.azure.us/.default` for an
  endpoint in Azure Government. It keeps no connection string or key, and never shows one.
- Email is off until an administrator saves an endpoint and a sender address in **Settings**, and
  turns it on. Endpoints must be HTTPS on a Communication Services host. **Send test email** sends
  from the saved settings, even while email is off, at most once every 30 seconds.
- `azd` can deploy Communication Services with an Azure-managed email domain when
  `MOSAIC_DEPLOY_EMAIL` is `true`. It's off by default. The resource refuses access keys, and the
  API's identity gets **Communication and Email Service Owner** on it. Settings offers the deployed
  endpoint and sender, but email stays off until an administrator saves them.

**The apps show budgets as totals.**
- The console's Dashboard shows each budget's progress and forecast, and which cost centers are
  blocked. The Cost centers page badges cost centers near or past their budgets and sets the
  organization's. A cost center's page shows its budget's progress, forecast, emails, and the
  gateways that enforce its block, and edits it.
- The portal shows a banner on My access and Usage & cost when a cost center the person holds an
  enabled grant under is at 80%, past 100%, or blocked. It shows the cost center's share of its
  budget, never anyone's own use.
- Budgets and email settings are administrator routes. The portal's route returns only the
  caller's own cost centers' alerts.

## Consequences

- Administrators can cap a team's monthly spend without revoking anything, and lift the cap by
  raising it. People see why their calls are refused, in the response and in the portal.
- A block needs no publish: one named value write per gateway. Every governed policy reads the
  list, so a gateway without it refuses every governed call. The plan step keeps it there, and
  the denial analytics show `budget-list` if it's ever damaged.
- Budgets are as current as the rollups. Spend lands within about 15 minutes of the logs, and a
  block within about 30 minutes of the spend.
- Owners and administrators get at most one email per threshold per month, and one per block and
  unblock. A failure of Communication Services delays an email, and after three tries, or a check
  that stopped mid-send, drops it rather than risk a duplicate.
- Communication Services is one more resource to run, and the managed identity's role on it is
  broader than sending. A custom role with `Microsoft.Communication/CommunicationServices/Read`,
  `Microsoft.Communication/CommunicationServices/Write`, and
  `Microsoft.Communication/EmailServices/Write` is the least-privilege alternative.

## Alternatives considered

- **Recompiling policies to block.** Exact, but a block would mean a publish on every gateway, with
  its reviews and its risk, at the moment spend runs out. A named value changes one string.
- **A policy fragment per blocked cost center.** Every policy would have to include each new
  fragment, so a block would still mean a publish.
- **Blocking by revoking or disabling grants.** It would lose the grants' keys and limits, need a
  publish to undo, and say nothing at the gateway about why. A budget block must only refuse calls.
- **Calling MOSAIC from the policy.** It would put MOSAIC in the traffic path, which ADR 0019 rules
  out.
- **Counting idle provisioned throughput against cost centers.** Teams would be blocked for
  capacity they didn't use. The organization's budget counts it instead.
- **Email through SMTP or a third-party service.** Either needs a credential MOSAIC would have to
  keep. Communication Services takes the managed identity.
- **Budget fields on the cost center.** The cost center's item is rewritten by recheck bookkeeping,
  and budget edits would have to share its etag path.

## Live verification still required

- How long API Management takes to apply a changed named value at the gateway, on the classic and
  v2 tiers, and that its ARM `GET` returns a plain named value's `value`.
- Whether Communication Services Email, its Azure-managed domain, and Microsoft Entra
  authentication work in Azure Government, with the `.azure.us` endpoint and scope.
- That a repeated `Operation-Id` doesn't send a second email, and what status it returns.
- The round trip on a real gateway: spend past a blocking budget, a 403 with `r=budget` within
  about 30 minutes, the emails once each, and the block lifting when the budget is raised.

[docs/e2e/roadmap.md](../e2e/roadmap.md) tracks these as R12 to R14.
