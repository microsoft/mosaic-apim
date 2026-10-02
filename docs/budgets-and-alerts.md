# Budgets and alerts

A budget caps what a cost center spends on AI each month. MOSAIC emails its owners and anyone else
at 80% and 100% of it, and, if the cost center chooses, refuses its calls at the gateway once it's
used up. An organization budget covers everything and only warns.
[ADR 0023](adr/0023-budgets-and-notifications.md) records the design.

- [What a budget holds](#what-a-budget-holds)
- [Set up email](#set-up-email)
- [Set a budget](#set-a-budget)
- [What counts against a budget](#what-counts-against-a-budget)
- [When MOSAIC checks budgets](#when-mosaic-checks-budgets)
- [Emails](#emails)
- [Blocking](#blocking)
- [What people see](#what-people-see)
- [Settings](#settings)
- [API](#api)
- [Limits of the design](#limits-of-the-design)

## What a budget holds

| Field | What it is |
| --- | --- |
| Amount | US dollars a month, at list price, across every gateway. At least $0.01. |
| Warn at | One to five whole percentages from 1 to 1,000. 80 and 100 by default. |
| Email the owners | Whether the cost center's owners get its emails. On by default. |
| Also email | Up to 20 more email addresses. They needn't belong to anyone who uses MOSAIC. |
| At 100% | **Let calls continue, and email**, or **Block calls at the gateway** until the budget is raised or the month ends. |

The month is the UTC calendar month. The organization's budget has an amount, thresholds, and
addresses, and always lets calls continue.

## Set up email

MOSAIC sends email through [Azure Communication Services
Email](https://learn.microsoft.com/azure/communication-services/concepts/email/email-overview),
signed in as its own managed identity, so it keeps no key. Email is off until an administrator
turns it on.

1. Create a Communication Services resource, an Email Communication Services resource with a
   domain, and connect the domain to the first resource. An Azure-managed domain sends from
   `DoNotReply@<generated>.azurecomm.net` with no DNS to set up. `azd` can create all three: set
   `MOSAIC_DEPLOY_EMAIL` to `true` and run `azd provision`. `MOSAIC_EMAIL_DATA_LOCATION` chooses
   where they keep data, **United States** by default.
2. Give MOSAIC's API identity **Communication and Email Service Owner** on the Communication
   Services resource. `azd` does it when it creates the resource. A custom role with
   `Microsoft.Communication/CommunicationServices/Read`,
   `Microsoft.Communication/CommunicationServices/Write`, and
   `Microsoft.Communication/EmailServices/Write` is enough, if you'd rather not grant the
   built-in one.
3. In the console, open **Settings > Email**. Enter the resource's endpoint, such as
   `https://contoso-mosaic.unitedstates.communication.azure.com`, and a sender address on the
   connected domain. When `azd` created the resource, **Use them** fills both in.
4. Save, then use **Send test email** to send one to yourself. It sends from the saved settings even
   while email is off, so you can check them first.
5. Turn on **Send budget email** and save.

With email off, budgets still reach their thresholds, show on the Dashboard, and block, but the
emails are recorded as not sent.

## Set a budget

- **A cost center's:** open it under **Cost centers** and fill in **Budget**. The card shows the
  month's spend against the amount, the month-end forecast, the thresholds reached, the emails
  sent, and, for a budget that blocks, whether each gateway is refusing its calls.
- **The organization's:** under **Cost centers > Organization budget**.

Saving judges the budget at once, so a budget set below what's already spent emails and, if it
blocks, blocks within seconds, and a raised budget lifts its block in the same request.
**Remove budget** lifts any block too. Budgets are audited as `budget.created`, `budget.updated`,
and `budget.deleted`.

## What counts against a budget

- A cost center's spend is exactly what **Analytics > Cost** shows for it: the calls of its
  grants, priced by deployment and day at list price. Provisioned throughput is shared among its
  callers by their share of its tokens. See [Pricing](pricing.md). A grant on a
  [model pool](../README.md#model-pools)'s model counts the same way, each call priced at the
  deployment that served it, whichever region that was.
- Reserved capacity nobody called, and calls MOSAIC couldn't link to a grant, belong to no cost
  center. They count only toward the organization's budget, which counts every priced call.
- Tokens MOSAIC can't price don't count, and the budget says how many there were. A budget can't be
  judged until usage is priced: without prices, no threshold is reached and no block lifts, except
  when the month ends or an administrator changes the budget.
- The forecast is this month's spend so far, times the days in the month, over the days elapsed,
  labelled projected. It shows once a day of figures exists.

## When MOSAIC checks budgets

A background job in the API judges every budget:
- every 15 minutes, and after each rollup of the gateways' logs;
- every 5 minutes for a budget that has spent 90% or more and still has a threshold or a block to
  come;
- at the start of each UTC month, which starts every budget afresh;
- when an administrator saves a budget, or calls `POST /budgets/check`, at most once a minute.

It runs only where usage comes from the gateways' logs (`MOSAIC_USAGE_SOURCE` `rollups`) and
`MOSAIC_BUDGETS_ENABLED` is on, one check at a time across the API's instances. A threshold the
budget stops warning at and starts again isn't emailed twice in a month, and an amount changed
while MOSAIC can't price the month waits for the next check that can.

## Emails

| Email | When |
| --- | --- |
| *Research (RES) has reached 80% of its March 2026 budget* | The spend reaches a threshold, once a month |
| *Calls charged to Research (RES) are now blocked* | A budget that blocks is used up |
| *Calls charged to Research (RES) are allowed again* | The block lifts, and why |

Each email gives the month's spend and the amount, and a threshold's email the forecast and what
happens at 100%. A block or unblock email also says how many of the gateways MOSAIC manages already
refuse, or allow, the cost center's calls, so a gateway MOSAIC couldn't write yet isn't hidden. None
says who spent it. Thresholds reached by one check send one email, for the highest.

Each email goes at most once. A check records an email as being sent before it sends it, so two
API instances can't both send it, and Communication Services is told each try is the same
operation. A refused email is tried again at the next checks, three times in all. If MOSAIC stops
while sending one, it doesn't send it again: a missed email is better than a duplicate. Each
budget's card lists its emails this month and how each went.

## Blocking

A cost center whose budget blocks is refused at every gateway MOSAIC manages, about 15 to 30
minutes after its spend passes the amount. That's the time the gateway's logs take to arrive,
the rollup's 15 minutes, and the time API Management takes to apply a change, so a block lets a
little more through than the amount. A block refuses calls and nothing else: grants, keys, and
limits stay as they are.

- Each managed gateway holds the named value `mosaic-blocked-cost-centers`, a comma-separated list
  of the blocked cost centers' keys, or `-` when none is. A cost center's key is the first 12 hex
  digits of a SHA-256 of its ID.
- MOSAIC creates it with the first plan that publishes to the gateway, before any policy refers to
  it, and writes it when a block starts or lifts. It's audited as
  `gateway.blockedCostCentersUpdated`. Don't edit or delete it: unpublishing leaves it, and API
  Management won't delete it while a policy refers to it.
- The governed policy checks it after it has matched the caller's grant and cost center, a
  governed model pool's included. A call charged to a blocked cost center gets `403 Forbidden`
  with this body, which names the cost center by its code, in lowercase:

  ```text
  Access denied. Cost center ci-204 has used its monthly budget, so its calls are refused until an administrator raises the budget or the month ends.
  ```

  The gateway's trace records `mosaic-deny v=1 r=budget`, which **Analytics > Reliability** counts
  as a denial reason. The same person's calls under another cost center still work: with a
  Microsoft Entra token, `x-mosaic-cost-center` can name another cost center they hold.
- If the list isn't one MOSAIC wrote, every governed call on that gateway is refused with `503`
  and `r=budget-list`, rather than risk letting a blocked cost center through. The next check
  writes it again.
- The block lifts when the month ends, or when an administrator raises the amount above the spend,
  turns blocking off, or removes the budget. Removing it deletes only the state it found, so a
  budget set again for the same cost center meanwhile keeps its own block.

Publications applied before budgets existed don't check the list until they're applied again.

## What people see

- **Dashboard:** a **Budgets** card with the organization's budget and each cost center's
  progress, forecast, and level: **On track**, **Near its limit**, **Over budget**, or
  **Blocked**.
- **Cost centers:** a badge on each cost center near or past its budget.
- **Portal:** when a cost center someone holds an enabled grant under has reached 80%, passed
  100%, or is blocked, **My access** and **Usage & cost** show a banner with its share of its budget
  and what happens at 100%. It never shows anyone's own use.

## Settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `MOSAIC_BUDGETS_ENABLED` | `true` | Runs the background check. Off, budgets are kept but never judged: saving one doesn't judge it, `POST /budgets/check` is refused, and no one is emailed or blocked. A block already on a gateway stays until checks are back on or its budget is removed. |
| `MOSAIC_BUDGET_INTERVAL_SECONDS` | `900` | How often every budget is checked, from 60 to 86,400 seconds. |
| `MOSAIC_BUDGET_FAST_INTERVAL_SECONDS` | `300` | How often a budget at 90% or more, with a threshold or block to come, is checked, from 60 to 3,600 seconds. |
| `MOSAIC_EMAIL_SUGGESTED_ENDPOINT` | None | The endpoint of a Communication Services resource `azd` created. It only fills in **Settings > Email**. |
| `MOSAIC_EMAIL_SUGGESTED_SENDER` | None | The sender address of the domain `azd` created. It only fills in **Settings > Email**. |

## API

Administrators (`Admin`):

| Method | Route under `/api/v1` | Result |
| --- | --- | --- |
| GET | `/budgets` | Every budget, worst first, with how many cost centers have none and whether email is ready |
| POST | `/budgets/check` | Check every budget now; 429 within a minute of the last check, 409 while another runs |
| GET, PUT, DELETE | `/budgets/organization` | The organization's budget; 422 for `"action": "block"` |
| GET, PUT, DELETE | `/cost-centers/{id}/budget` | A cost center's budget: `amount`, `thresholds`, `recipients`, `notifyOwners`, `action` |
| GET, PUT | `/settings/email` | Email settings: `enabled`, `endpoint`, `sender`, with the last test and the deployment's suggestions |
| POST | `/settings/email/test` | Send a test email: `{"to": "..."}`; 409 `notConfigured` without an endpoint and sender, 429 within 30 seconds of the last |

People (`User`), for themselves only:

| Method | Route under `/api/v1` | Result |
| --- | --- | --- |
| GET | `/me/budgets` | Their cost centers whose budgets are at 80%, past 100%, or blocked, with each one's share of its budget |

## Limits of the design

- Budgets are as current as the rollups, and a block lands after the spend that triggers it.
- The list holds 315 blocked cost centers. Past that, the newest blocks don't fit, the gateways
  still allow their calls, and the Dashboard's budget overview says how many.
- Budgets are in US dollars at list price, like the Cost tab. They aren't a bill: discounts,
  credits, and other currencies aren't modelled.
- A blocked cost center's calls are refused on every gateway MOSAIC manages. A gateway MOSAIC only
  observes doesn't check the list.
- Communication Services in Azure Government, and how long API Management takes to apply a named
  value, still need checking on a live deployment. See [the end-to-end roadmap](e2e/roadmap.md).
