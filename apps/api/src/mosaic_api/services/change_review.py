"""Redacted intent comparisons. Never serialize policy XML or credential-bearing expressions."""

from hashlib import sha256
from typing import Literal

from mosaic_api.domain import (
    McpAccessSnapshot,
    McpPublication,
    ModelAccessSnapshot,
    PlanReview,
    PoolAccessSnapshot,
    Publication,
    PublishPlan,
    ReviewChange,
    ReviewField,
    ReviewSnapshot,
)
from mosaic_api.model_pools import ModelPool

Target = Publication | McpPublication | ModelPool
Access = ModelAccessSnapshot | McpAccessSnapshot | PoolAccessSnapshot


def review_snapshot(
    target: Target, access: Access | None, *, keyed_backends: frozenset[str] = frozenset()
) -> ReviewSnapshot:
    fields: list[ReviewField] = []

    def add(key: str, category: str, label: str, value: str | int | bool | None) -> None:
        if value is not None:
            text = ("Enabled" if value else "Disabled") if isinstance(value, bool) else str(value)
            fields.append(ReviewField(key=key, category=category, label=label, value=text))

    def limits(key: str, label: str, tokens: object, requests: object = None) -> None:
        # Explicit allowlist: counter expressions can contain credentials and are never review data.
        for source, names in (
            (
                tokens,
                (
                    ("tokens_per_minute", "tokensPerMinute", "tokens/minute"),
                    ("token_quota", "tokenQuota", "token quota"),
                    ("token_quota_period", "tokenQuotaPeriod", "token quota period"),
                    ("estimate_prompt_tokens", "estimatePromptTokens", "estimate prompt tokens"),
                ),
            ),
            (
                requests,
                (
                    ("calls", "calls", "calls/window"),
                    ("renewal_period_seconds", "renewalPeriodSeconds", "window (seconds)"),
                    ("call_quota", "callQuota", "call quota"),
                    ("call_quota_period", "callQuotaPeriod", "call quota period"),
                ),
            ),
        ):
            for attr, path, title in names:
                value = getattr(source, attr, None)
                if isinstance(value, (str, int, bool)):
                    add(f"{key}/{path}", "Limits", f"{label} - {title}", value)
            expression = getattr(source, "counter_key_expression", None)
            if expression:
                suffix = "tokenCounter" if source is tokens else "callCounter"
                add(
                    f"{key}/{suffix}",
                    "Limits",
                    f"{label} - counter expression fingerprint (not its contents)",
                    sha256(expression.encode()).hexdigest(),
                )

    add("publication/displayName", "Configuration", "Display name", target.display_name)
    add("publication/apiPath", "Routing", "Gateway API path", target.api_path)
    add("publication/apiShape", "Routing", "API shape", getattr(target, "api_shape", None))
    if isinstance(target, McpPublication):
        add("mcp/endpoint", "Routing", "MCP endpoint record", target.mcp_endpoint_id)
    if isinstance(target, Publication):
        add("publication/deploymentName", "Routing", "Model deployment", target.deployment_name)
        add(
            "publication/modelEndpointId",
            "Routing",
            "Model endpoint record",
            target.model_endpoint_id,
        )
        limits("publication", "All callers (publication safeguard)", target.enforcement)
        if access is None:
            add(
                "authentication/subscriptionRequired",
                "Authentication",
                "Require subscription",
                target.subscription_required,
            )
    if isinstance(target, ModelPool):
        add(
            "pool/description",
            "Configuration",
            "Pool API description",
            target.description
            or f"The models in the {target.display_name} pool, served by MOSAIC.",
        )
        add("pool/poolType", "Routing", "Pool routing", target.pool_type)
        add("pool/breakerPreset", "Routing", "Circuit breaker preset", target.breaker_preset)
        add("pool/maxRetries", "Routing", "Maximum retries", target.max_retries)
        limits("pool/safeguard", "All callers per pool model (safeguard)", target.safeguard)
        for model in target.models:
            prefix = f"models/{model.id}"
            add(
                f"{prefix}/publicName",
                "Routing",
                f"{model.display_name} - public name",
                model.public_name,
            )
            add(
                f"{prefix}/expectedVersion",
                "Routing",
                f"{model.display_name} - expected version",
                model.expected_version,
            )
            add(
                f"{prefix}/allowMixedVersions",
                "Routing",
                f"{model.display_name} - accept mixed versions",
                model.allow_mixed_versions,
            )
            order = 0
            for member in model.members:
                key = f"{prefix}/members/{member.backend_name}"
                label = (
                    f"{model.display_name} / {member.deployment_name} ({member.model_endpoint_id})"
                )
                add(
                    f"{key}/endpoint",
                    "Routing",
                    f"{label} - endpoint record",
                    member.model_endpoint_id,
                )
                add(f"{key}/deployment", "Routing", f"{label} - deployment", member.deployment_name)
                add(f"{key}/weight", "Routing", f"{label} - weight", member.weight)
                add(f"{key}/drained", "Routing", f"{label} - drained", member.drained)
                if not member.drained and (
                    target.pool_type == "linear" or member.backend_name in keyed_backends
                ):
                    order += 1
                    add(f"{key}/order", "Routing", f"{label} - attempt order", order)
    if access is not None:
        add("authentication/audience", "Authentication", "Entra token audience", access.audience)
        add(
            "authentication/keysEnabled",
            "Authentication",
            "Subscription-key access",
            access.settings.keys_enabled,
        )
        add(
            "authentication/entraEnabled",
            "Authentication",
            "Entra access",
            access.settings.entra_enabled,
        )
        for grant in access.grants:
            key = f"grants/{grant.entitlement_id}"
            model_label = ""
            if isinstance(target, ModelPool) and hasattr(grant, "pool_model_id"):
                model_label = next(
                    (f" / {m.display_name}" for m in target.models if m.id == grant.pool_model_id),
                    "",
                )
            label = f"{grant.display_name}{model_label} / {grant.cost_center_code}"
            add(f"{key}/enabled", "Access", f"{label} - access", grant.enabled)
            add(f"{key}/subject", "Access", f"{label} - caller object", grant.object_id)
            add(f"{key}/costCenter", "Access", f"{label} - cost center", grant.cost_center_code)
            add(
                f"{key}/limitsSource",
                "Limits",
                f"{label} - per-person limit source",
                {"grant": "Grant override", "costCenter": "Cost-center defaults"}.get(
                    grant.limits_source or "", "Not recorded"
                ),
            )
            add(f"{key}/keysAllowed", "Access", f"{label} - key permission", grant.keys_allowed)
            add(
                f"{key}/defaultCostCenter",
                "Access",
                f"{label} - default cost center",
                grant.default_cost_center,
            )
            limits(
                key,
                label,
                grant.enforcement.tokens if grant.enforcement else None,
                grant.enforcement.requests if grant.enforcement else None,
            )
        quotas = access.quotas if isinstance(access, PoolAccessSnapshot) else access.pools
        for quota in quotas:
            model_id = quota.pool_model_id if hasattr(quota, "pool_model_id") else ""
            key = f"quotas/{quota.cost_center_id}/{model_id}"
            label = f"{quota.cost_center_code} - shared by all grants"
            if model_id and isinstance(target, ModelPool):
                label += next(
                    (f" / {m.display_name}" for m in target.models if m.id == model_id),
                    f" / {model_id}",
                )
            add(f"{key}/monthlyTokens", "Limits", f"{label} - tokens/month", quota.monthly_tokens)
            add(f"{key}/monthlyCalls", "Limits", f"{label} - calls/month", quota.monthly_calls)
        if isinstance(access, McpAccessSnapshot) and access.model_caller:
            add(
                "mcp/modelCaller",
                "Access",
                "MCP model-calling application",
                access.model_caller.display_name,
            )
            add(
                "mcp/modelCallerObject",
                "Access",
                "MCP model-calling application object",
                access.model_caller.object_id,
            )
        if isinstance(access, McpAccessSnapshot):
            add(
                "authentication/delegatedScope",
                "Authentication",
                "MCP delegated scope",
                access.delegated_scope,
            )
            add(
                "authentication/applicationRole",
                "Authentication",
                "MCP application role",
                access.application_role,
            )
    return ReviewSnapshot(fields=sorted(fields, key=lambda field: field.key))


def build_review(
    target: Target,
    plan: PublishPlan,
    access: Access | None,
    *,
    keyed_backends: frozenset[str] = frozenset(),
) -> PlanReview:
    proposed = review_snapshot(target, access, keyed_backends=keyed_backends)
    previous = target.applied_review
    new = (
        (target.last_applied_at is None or target.unpublished_at is not None)
        and not target.resources
        and not any(step.existed and step.action != "noChange" for step in plan.steps)
    )
    baseline: Literal["applied", "new", "partial", "unavailable"] = (
        "applied" if previous else "new" if new else "unavailable"
    )
    if target.access_state in {"failed", "unknown"} or (
        target.last_applied_at and target.status in {"failed", "rolledBack"}
    ):
        previous = None
        new = False
        baseline = "unavailable"
    old = {field.key: field for field in previous.fields} if previous else {}
    # Older records can still truthfully compare recorded access, but not routing intent.
    known_prefixes: tuple[str, ...] = ()
    if previous is None and not new and target.applied_access is not None:
        observed_access = review_snapshot(target, target.applied_access)
        known_prefixes = ("grants/", "quotas/", "authentication/", "mcp/modelCaller")
        old = {
            field.key: field
            for field in observed_access.fields
            if field.key.startswith(known_prefixes)
        }
        baseline = "partial"
    desired = {field.key: field for field in proposed.fields}
    removing = plan.operation == "unpublish"
    if removing:
        desired = {}
        # Unpublish always removes the currently recorded access, not unapplied desired grants.
        if previous is None and target.applied_access is not None:
            old = {
                field.key: field
                for field in review_snapshot(target, target.applied_access).fields
                if field.key.startswith(known_prefixes)
            }
        baseline = "partial" if previous is None else baseline
    changes: list[ReviewChange] = []
    for key in sorted(old.keys() | desired.keys()):
        before = old.get(key)
        after = desired.get(key)
        known = previous is not None or new or key.startswith(known_prefixes)
        if before and after and before.value == after.value:
            continue
        field = after or before
        if field is None:
            continue
        changes.append(
            ReviewChange(
                key=key,
                category=field.category,
                label=field.label,
                before=before.value if before else None,
                after=after.value if after else None,
                previous_known=known,
            )
        )
    impact = []
    if removing:
        impact.append("The gateway stops serving this offering. Its applied callers lose access.")
        impact.append(
            "Underlying model deployments and MCP servers are retained; only the listed "
            "APIM resources are removed."
        )
    elif any(step.stage == "prepare" and step.kind == "apiPolicy" for step in plan.steps):
        impact.append(
            "This target temporarily blocks calls during apply, including callers whose "
            "permissions are unchanged. Permitted access resumes after policy installation."
        )
    if any(
        step.subscription_state == "suspended" and step.action != "noChange" for step in plan.steps
    ):
        impact.append(
            "Existing keys are suspended during the batch; only permitted keys reactivate."
        )
    if access is not None and not removing and target.applied_access is None:
        if isinstance(target, (Publication, ModelPool)):
            impact.append(
                "The first governed apply retires the shared bootstrap subscription. Existing "
                "clients must use permitted grants; governed access cannot be turned off afterward."
            )
    if access is not None and not removing and not any(grant.enabled for grant in access.grants):
        impact.append(
            "The proposed access has no enabled grants; the gateway refuses every caller."
        )
    if (
        access is not None
        and not removing
        and not (access.settings.keys_enabled or access.settings.entra_enabled)
    ):
        impact.append("Both sign-in methods are disabled; the gateway refuses every caller.")
    deleted = len({step.resource_id for step in plan.steps if step.action == "delete"})
    if deleted:
        impact.append(f"This plan deletes {deleted} APIM resources. Inspect Technical execution.")
    if any(
        step.kind in {"policyFragment", "apiPolicy"} and step.existed and step.action == "update"
        for step in plan.steps
    ):
        impact.append(
            "Generated policy documents are replaced, not merged. Manual edits inside them "
            "can be overwritten; inherited external policy can still affect calls."
        )
    if any(
        step.kind == "namedValue" and step.name == "mosaic-blocked-cost-centers"
        for step in plan.steps
    ):
        impact.append("This target uses the gateway's shared cost-center budget blocking list.")
    if plan.target == "mcp":
        impact.append("MCP discovery can use a shared metadata API on this gateway.")
    if not removing:
        impact.append(
            "Apply includes all saved pending changes for this target. APIM configuration may "
            "be written again even where intended behavior is unchanged."
        )
    return PlanReview(
        baseline=baseline,
        applied_at=target.last_applied_at,
        changes=changes,
        target=ReviewSnapshot() if removing else proposed,
        impact=impact,
    )
