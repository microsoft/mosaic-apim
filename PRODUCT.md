# MOSAIC

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users and purpose

Administrators operate an APIM-backed AI gateway estate: discover resources, publish models and
MCP servers, govern access, allocate budgets, and understand telemetry. End users discover
published resources, request access, connect applications, and understand their own usage.
Administrators can also deploy a new model onto an existing Azure AI account, after explicitly
enabling deployment management, reviewing configuration, and verifying permissions and quota.

MOSAIC stores desired governance state, produces reviewable plans for API Management, and reads
gateway telemetry. It does not proxy model traffic or replace API Management.

## Operating principles

- Observed inventory, saved intent, reviewed changes, and applied gateway state are distinct.
- Approval creates a grant; it does not promise that gateway access is ready.
- Imports and record removal must not be described as changes to the underlying cloud resource.
- Azure model creation is a distinct, billable, reviewed operation; endpoints are read-only by
  default. Deployment does not publish through APIM, grant access, or add a pool member.
- An unconfirmed Azure write must be reconciled, never automatically resubmitted. Successful
  provisioning and a failed inventory refresh are distinct outcomes.
- Organization budgets warn; cost-center budgets may block.
- Credentials appear only in explicitly authorized, ephemeral reveal workflows.

## Brand and constraints

Preserve MOSAIC blue, Fluent UI controls, system typography, and light/dark themes. The tile-based
M symbol identifies both apps. Adapt SimpleChat's layered navigation ideas, not its services or
visual theme. Keep portal workflows user-scoped and separate from administrator operations.

## Evidence and documentation

README images come exclusively from the fictional estate seeded by `scripts/screenshots`.
Regenerate affected images through its capture script, review redaction, and synchronize the
README, screenshot inventory, and capture definitions whenever a pictured view changes.
