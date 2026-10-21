"""The Azure AI write boundary: one reviewed deployment, never account keys or inference."""

from urllib.parse import quote

from mosaic_api.domain import COGNITIVE_SERVICES_API_VERSION, CognitiveServicesResourceId
from mosaic_api.integrations.apim.client import ArmClient
from mosaic_api.model_deployments import ModelDeploymentCreate


class ModelDeploymentWriter:
    def __init__(self, arm: ArmClient, resource: CognitiveServicesResourceId) -> None:
        arm.require_cloud(resource.azure_cloud)
        self._arm = arm
        self._account = resource.account_scope

    async def create(self, configuration: ModelDeploymentCreate) -> None:
        # Azure documents this as an upsert, without a conditional-create contract. The service
        # checks live name availability under MOSAIC's account lock immediately before this call.
        await self._arm.put(
            f"{self._account}/deployments/{quote(configuration.deployment_name, safe='')}",
            configuration.azure_payload(),
            params={"api-version": COGNITIVE_SERVICES_API_VERSION},
            retry=False,
        )
