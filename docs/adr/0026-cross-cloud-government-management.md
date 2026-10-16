# ADR 0026: Government resources managed by commercial MOSAIC

## Status

Accepted.

## Context

An organization wants one commercial console, portal, identity directory, cost-center catalog,
and usage report for Commercial and Government resources. A commercial managed identity is not
a Government management identity. A commercial MOSAIC sign-in is not proof of Government account
ownership, and an entered Government UPN is not an authenticated account link.

The existing backend-secret vault is readable by APIM. Reusing it for a Government management
credential would expose management permissions to a runtime gateway.

## Decision

Persist explicit `azureCloud` and `azureConnectionId` on resources, independent of environment
classification. Legacy records keep commercial defaults and existing IDs. Resource identity
distinguishes clouds, not credential rotations or connection names; switching credentials must
not manufacture a second owner of the same Azure resource.

Use Government-tenant service principals with write-only client-secret inputs. Store credentials
only in a separate commercial management vault to which only the API identity has data access.
Persist vault references and safe connection metadata, never secret values. Keep the host
identity for Cosmos, Graph, commercial authentication, email, and instrumentation.

Route ARM, identity/permission checks, telemetry, publishing, subscriptions, and backend
credential operations by explicit connection/cloud context. Bind authorities, origins, scopes,
and audiences to closed cloud profiles; reject cross-origin authenticated continuations and
redirects. Never infer cloud from an environment tag or pricing override.

Government gateway runtime access is key-only. The key represents an exact applied direct grant
and cost center, not a Government user account. Commercial bearer tokens and group membership do
not become a Government runtime fallback. Provision the initial eligible subscription only after
successful reviewed apply, with durable retry and explicit-deletion intent. Reveal remains a
separate authorized, audited operation; plaintext stays in APIM.

Government gateways use their own Government identities for compatible same-cloud backends.
Unavailable or unknown tier/API-version/region features remain explicit rather than weakening
policy. Preserve commercial behavior and existing review/manage gates.

Use shared commercial cost-center IDs, retaining cloud/resource provenance in rollups, prices,
filters, exports, and enforcement. Price the served backend's actual cloud and deployment;
unpriced or unavailable data stays visible rather than falling back to commercial prices or zero.

## Consequences

Government metadata, telemetry, and management secrets are handled by a commercial deployment.
Operators must approve that cross-cloud data boundary. This is not a sovereign MOSAIC deployment
or a compliance certification. Keys provide no private-network connectivity and cannot identify
which human physically used a copied credential.

Government user sign-in/account linking, Government Graph expansion, runtime Government Entra
tokens, federation/certificates, and other sovereign clouds are deferred. Live non-sensitive
Government smoke tests are still required to establish actual service/capability/network support;
offline doubles are not proof of regional availability.

See [the operator guide](../azure-government.md) for configuration and recovery.
