"""Account-scoped management checks; these grant neither Azure roles nor inference access."""

from mosaic_api.integrations.aoai.client import CognitiveServicesClient
from mosaic_api.integrations.rbac import permits
from mosaic_api.model_deployments import DEPLOYMENT_ACTIONS, DeploymentAccess


async def deployment_access(client: CognitiveServicesClient) -> DeploymentAccess:
    permissions = await client.effective_permissions(at_account=True)
    scope = client.resource.account_scope
    if permissions is None:
        return DeploymentAccess(
            scope=scope,
            message=(
                "MOSAIC could not evaluate the management identity's permissions at this Azure "
                "account. Deployment is blocked. Allow the identity to read its effective "
                "permissions at this scope, then check again."
            ),
        )
    missing = [action for action in DEPLOYMENT_ACTIONS if not permits(permissions, action)]
    return DeploymentAccess(
        scope=scope,
        can_deploy=not missing,
        evaluation="denied" if missing else "allowed",
        missing_actions=missing,
        message=(
            "Grant the listed control-plane actions to the endpoint's management identity at "
            "this account or an ancestor scope, then check again. A project-scoped grant does "
            "not cover its parent account. MOSAIC does not assign roles."
            if missing
            else "The management identity has the required account-scoped deployment actions. "
            "Quota and model eligibility are checked separately before deployment."
        ),
    )
