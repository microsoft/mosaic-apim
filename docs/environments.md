# Environments

[Documentation](README.md) | [MOSAIC](../README.md)


Every gateway, model endpoint, and registered MCP server belongs to one environment, or is
**Unclassified**. Administrators manage environments in **Settings → Environments**. MOSAIC
seeds Development, Test, QC, Staging, Production, and Sandbox. Administrators can rename or
recolor them and add their own.

Each environment has:
- a key that never changes;
- optionally, the **production-class** flag;
- optionally, a list of other environments whose endpoints its gateways may also front.

A production-class environment may list only other production-class environments.

Whenever a model or MCP server is published, MOSAIC judges the pairing of the gateway with the
model endpoint or MCP server:
- **Allowed:** both are in the same environment, or the gateway's environment lists the
  endpoint's as an exception.
- **Blocked:** two different classified environments without an exception.
- **Blocked:** a production-class resource paired with an unclassified one, in either direction.
- **Warning:** any other pairing that involves an unclassified resource. Once **Require
  classification** is on, these are blocked too.

The publish dialogs disable blocked deployments and MCP servers and say why. Creating, planning,
and applying a publication each check again. A plan also records the verdict it was reviewed
under. If either environment, a production-class flag, the exception, or Require classification
changes before apply, apply asks for a fresh plan.

**Classifying resources**
- Registration asks for an environment.
- To change it later, use **Change environment** on the resource's page. To classify many
  resources at once, use the **Unclassified** card in Settings. Both go through
  `POST /environment-assignments`. The gateway, model endpoint, and MCP server update routes don't
  accept an environment, so every change passes the same checks.
- MOSAIC suggests an environment, but never applies one without confirmation. It suggests from
  either:
  - an Azure `environment` or `env` tag it read during preflight or the subscription scan; or
  - the legacy environment label.
- MCP servers are registered by URL, so they have no Azure tag.

**Changes that are refused**

MOSAIC refuses any change that would leave an applied model or MCP publication blocked:
- re-classifying a gateway, model endpoint, or MCP server;
- editing or deleting an environment;
- turning on Require classification.

The refusal names the publications. Sometimes a gateway must move together with its endpoints or
MCP servers, for example to classify an unclassified pair as Production. The console then submits
them as one batch, which MOSAIC validates as a whole and writes atomically.

**Grants follow their resource.** Moving a resource with enabled grants into or out of a
production-class environment lists the people and applications affected, and requires
confirmation. The audit event records the grants.

**In the portal**
- Every catalog entry and grant shows its environment, and the catalog can be filtered by it.
- People request each environment separately, so development access doesn't imply production
  access.
- A request records the environment it was made for. If the resource has moved since, approval
  asks the administrator to confirm the new environment.

**Findings** point out blocked pairings that MOSAIC's rules didn't stop. They cover:
- an applied model or MCP publication whose pairing is blocked, which only a change made outside
  MOSAIC can cause;
- a gateway backend or API that calls a registered model endpoint in an incompatible environment;
- a gateway MCP server whose URL is a registered MCP endpoint's URL, unless MOSAIC published it.

Findings are advisory, and each shows its evidence and confidence. They appear on the gateway, in
Settings, and in the import dialog. MOSAIC doesn't inspect backends referenced only from policy.

| Method | Route under `/api/v1` | Result |
| --- | --- | --- |
| GET | `/environment-catalog` | Environments with usage counts, the compatibility matrix, and Require classification |
| POST | `/environment-catalog/environments` | Add an environment |
| PATCH, DELETE | `/environment-catalog/environments/{key}` | Edit or delete an environment |
| PATCH | `/environment-catalog/settings` | Turn Require classification on or off |
| GET | `/environment-suggestions` | Unclassified resources and the environment suggested for each |
| POST | `/environment-assignments` | Classify or re-classify resources as one validated batch |
| GET | `/environment-findings?gatewayId=` | Advisory findings, optionally for one gateway |
| GET | `/portal/environments` | The environments, for portal users |

[ADR 0014](adr/0014-environments.md) records these rules.
