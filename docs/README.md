# MOSAIC documentation

[MOSAIC overview and selected screenshots](../README.md) | [Full screenshot gallery](gallery.md)

MOSAIC is the control plane; APIM is the runtime gateway. Start with the architecture and workflows
to understand the boundaries, then follow the guide for the task you need to perform.

## Understand MOSAIC

| Guide | What it answers |
| --- | --- |
| [Architecture](architecture.md) | What runs where, who owns state, which identity needs permissions, and where trust boundaries sit |
| [Workflows](workflows.md) | How center setup, Automatic inheritance, request/approval/apply, removal, publishing, and budget feedback fit together |
| [FAQ](faq.md) | Common questions about APIM, Entra groups, limits, costs, availability, and support |
| [Capabilities and roadmap](overview.md) | Detailed implemented capabilities, deployment caveats, and the development sequence |
| [Screenshot gallery](gallery.md) | The complete console and portal walkthrough using the fictional demo estate |

## Install and operate

| Task | Guide |
| --- | --- |
| Run locally, contribute, and run quality checks | [Development](development.md) |
| Provision the reference Azure deployment | [Deployment](deployment.md) |
| Register gateways, inspect APIs, import resources, or register an MCP server | [Gateway onboarding and inventory](gateways.md) |
| Register endpoints, check runtime readiness, or explicitly create an Azure model deployment | [Model endpoints](model-endpoints.md) |
| Publish or unpublish an individual model through a reviewed plan | [Model publishing](model-publishing.md) |
| Route compatible models across multiple deployments and regions | [Model pools](model-pools.md) |
| Classify resources and enforce publication compatibility | [Environments](environments.md) |
| Manage Government resources from commercial MOSAIC | [Azure Government](azure-government.md) |
| Prove runtime access or recover an interrupted operation | [Verification](verification.md) and [access recovery](access.md#recovering-an-interrupted-operation) |
| Run the persona-based browser harness | [E2E runbook](e2e/runbook.md) and [harness reference](../e2e/README.md) |

## Grant and use access

| Task | Guide |
| --- | --- |
| Understand direct/group grants and their applied gateway state | [Grants and gateway access](access.md) |
| Configure an Entra-backed center, member access modes, or a request for a new center | [Cost centers](cost-centers.md) and [setup workflow](workflows.md#set-up-an-entra-backed-cost-center) |
| Call a model as a person or application | [Entra model calls](call-models-with-entra-tokens.md) |
| Call as an agent identity or agent user | [Agent identity calls](call-models-with-agent-identities.md) |
| Publish an MCP server | [MCP publishing](publish-mcp-servers.md) |
| Connect an MCP client | [MCP connections](connect-to-mcp-servers.md) |
| Give an MCP server its own governed model access | [MCP servers that call models](mcp-servers-that-call-models.md) |
| Configure request approvals and notification preferences | [Approvals and notifications](approvals-and-notifications.md) |

Portal grouping is presentation only: a grant and connection always name an exact offering, cloud,
environment, and cost center. An MCP grant does not substitute for a model grant.

## Measure and control spending

| Task | Guide |
| --- | --- |
| Set up gateway telemetry and understand rollups, freshness, and retention | [Usage analytics](usage-analytics.md) |
| Find console/portal views and their API routes | [Analytics reference](analytics-reference.md) |
| Understand price sources, effective dates, overrides, and missing prices | [Pricing](pricing.md) |
| Import or retrieve USD cloud billing, configure credentials and draft schedules, then review and publish rates | [Billing-derived pricing](pricing.md#billing-data-import-review-publish) and [ADR 0031](adr/0031-billing-derived-pricing.md) |
| Configure Entra membership, charging, resource modes, individual keys, and pooled quotas | [Cost centers](cost-centers.md) |
| Configure monthly budgets, alerts, and delayed cost-center blocking | [Budgets and alerts](budgets-and-alerts.md) |

## Configure notifications and presentation

| Task | Guide |
| --- | --- |
| Configure Graph email, shared Teams Workflows, or private Teams messages | [Notification setup](notification-setup.md) |
| Understand recipient discovery, worker timing, and delivery evidence | [Notification routing](notification-routing.md) and [engine](notification-engine.md) |
| Set up the Teams companion application | [Teams application](../apps/teams/README.md) |
| Enable optional Entra person thumbnails | [Identity photos](identity-photos.md) |
| Regenerate and redact documentation images | [Screenshot maintenance](screenshots.md) |

## Design and evidence

[PRODUCT.md](../PRODUCT.md) defines product goals and non-goals;
[DESIGN.md](../DESIGN.md) defines interaction principles. The
[architecture decision records](adr/) explain individual design choices. Current operator guides
take precedence over superseded implementation-phase details in an ADR. The
[E2E roadmap](e2e/roadmap.md) records journey evidence; a fictional screenshot is not live proof.

[ADR 0030](adr/0030-entra-backed-cost-centers.md) supersedes the local managed-center experiment.
The [APIM-local membership/inherited-key feasibility proposal](cost-center-access-feasibility.md)
is historical and unimplemented, not the current Entra-backed workflow or live gateway evidence.
