# ruff: noqa: E501
"""Real Azure built-in role definitions, as ``az role definition list --name <id>`` returns them.

The runtime check is only as good as its reading of real definitions, so tests evaluate these
rather than hand-written approximations: the fallback list and the recommend-then-accept
invariant are both pinned against them. Only ``roleName`` and ``permissions`` are kept, and
built-in definitions are the same in every tenant. Refresh by re-running the command for each ID
below and replacing its permission blocks verbatim.
"""

from typing import Any

AZURE_OPENAI_USER_ROLE_ID = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
AZURE_OPENAI_CONTRIBUTOR_ROLE_ID = "a001fd3d-188f-4b5d-821b-7da978bf7442"
COGNITIVE_SERVICES_USER_ROLE_ID = "a97b65f3-24c7-4388-baec-2e87135dc908"
FOUNDRY_USER_ROLE_ID = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
AZURE_AI_DEVELOPER_ROLE_ID = "64702f94-c441-49e6-a78b-ef80e0188fee"
COGNITIVE_SERVICES_DATA_CONTRIBUTOR_ROLE_ID = "19c28022-e58e-450d-a464-0b2a53034789"
FOUNDRY_OWNER_ROLE_ID = "c883944f-8b7b-4483-af10-35834be79c4a"
FOUNDRY_PROJECT_MANAGER_ROLE_ID = "eadc314b-1a2d-4efa-be10-5d325db5065e"
READER_ROLE_ID = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
COGNITIVE_SERVICES_DATA_READER_ROLE_ID = "b59867f0-fa02-499b-be73-45a86b5b3e1c"
COGNITIVE_SERVICES_CONTRIBUTOR_ROLE_ID = "25fbc0a9-bd7c-42a3-aa1a-3b75d497ee68"

BUILT_IN_ROLE_DEFINITIONS: dict[str, tuple[str, list[dict[str, Any]]]] = {
    AZURE_OPENAI_USER_ROLE_ID: (
        "Cognitive Services OpenAI User",
        [
            {
                "actions": [
                    "Microsoft.CognitiveServices/*/read",
                    "Microsoft.Authorization/roleAssignments/read",
                    "Microsoft.Authorization/roleDefinitions/read",
                ],
                "notActions": [],
                "dataActions": [
                    "Microsoft.CognitiveServices/accounts/OpenAI/*/read",
                    "Microsoft.CognitiveServices/accounts/OpenAI/engines/completions/action",
                    "Microsoft.CognitiveServices/accounts/OpenAI/engines/search/action",
                    "Microsoft.CognitiveServices/accounts/OpenAI/engines/generate/action",
                    "Microsoft.CognitiveServices/accounts/OpenAI/deployments/audio/action",
                    "Microsoft.CognitiveServices/accounts/OpenAI/deployments/search/action",
                    "Microsoft.CognitiveServices/accounts/OpenAI/deployments/completions/action",
                    "Microsoft.CognitiveServices/accounts/OpenAI/deployments/chat/completions/action",
                    "Microsoft.CognitiveServices/accounts/OpenAI/deployments/realtime/action",
                    "Microsoft.CognitiveServices/accounts/OpenAI/deployments/extensions/chat/completions/action",
                    "Microsoft.CognitiveServices/accounts/OpenAI/deployments/embeddings/action",
                    "Microsoft.CognitiveServices/accounts/OpenAI/images/generations/action",
                    "Microsoft.CognitiveServices/accounts/OpenAI/video/generations/*/action",
                    "Microsoft.CognitiveServices/accounts/OpenAI/video/generations/*/delete",
                    "Microsoft.CognitiveServices/accounts/OpenAI/assistants/*",
                    "Microsoft.CognitiveServices/accounts/OpenAI/responses/*",
                ],
                "notDataActions": [
                    "Microsoft.CognitiveServices/accounts/OpenAI/stored-completions/read"
                ],
                "condition": None,
                "conditionVersion": None,
            }
        ],
    ),
    AZURE_OPENAI_CONTRIBUTOR_ROLE_ID: (
        "Cognitive Services OpenAI Contributor",
        [
            {
                "actions": [
                    "Microsoft.CognitiveServices/*/read",
                    "Microsoft.CognitiveServices/accounts/deployments/write",
                    "Microsoft.CognitiveServices/accounts/deployments/delete",
                    "Microsoft.CognitiveServices/accounts/raiPolicies/read",
                    "Microsoft.CognitiveServices/accounts/raiPolicies/write",
                    "Microsoft.CognitiveServices/accounts/raiPolicies/delete",
                    "Microsoft.CognitiveServices/accounts/commitmentplans/read",
                    "Microsoft.CognitiveServices/accounts/commitmentplans/write",
                    "Microsoft.CognitiveServices/accounts/commitmentplans/delete",
                    "Microsoft.Authorization/roleAssignments/read",
                    "Microsoft.Authorization/roleDefinitions/read",
                ],
                "notActions": [],
                "dataActions": ["Microsoft.CognitiveServices/accounts/OpenAI/*"],
                "notDataActions": [],
                "condition": None,
                "conditionVersion": None,
            }
        ],
    ),
    COGNITIVE_SERVICES_USER_ROLE_ID: (
        "Cognitive Services User",
        [
            {
                "actions": [
                    "Microsoft.CognitiveServices/*/read",
                    "Microsoft.CognitiveServices/accounts/listkeys/action",
                    "Microsoft.CognitiveServices/accounts/projects/connections/listsecrets/action",
                    "Microsoft.Insights/alertRules/read",
                    "Microsoft.Insights/diagnosticSettings/read",
                    "Microsoft.Insights/logDefinitions/read",
                    "Microsoft.Insights/metricdefinitions/read",
                    "Microsoft.Insights/metrics/read",
                    "Microsoft.ResourceHealth/availabilityStatuses/read",
                    "Microsoft.Resources/deployments/operations/read",
                    "Microsoft.Resources/subscriptions/operationresults/read",
                    "Microsoft.Resources/subscriptions/read",
                    "Microsoft.Resources/subscriptions/resourceGroups/read",
                    "Microsoft.Support/*",
                ],
                "notActions": [],
                "dataActions": ["Microsoft.CognitiveServices/*"],
                "notDataActions": [
                    "Microsoft.CognitiveServices/accounts/AIServices/agents/endpoints/UserIdentityImpersonation/action",
                    "Microsoft.CognitiveServices/accounts/OpenAI/fine-tunes-deployments/write",
                    "Microsoft.CognitiveServices/accounts/AIServices/fine_tuning_deployments/write",
                ],
                "condition": None,
                "conditionVersion": None,
            }
        ],
    ),
    FOUNDRY_USER_ROLE_ID: (
        "Foundry User",
        [
            {
                "actions": [
                    "Microsoft.Authorization/*/read",
                    "Microsoft.CognitiveServices/*/read",
                    "Microsoft.CognitiveServices/accounts/listkeys/action",
                    "Microsoft.CognitiveServices/accounts/projects/connections/listsecrets/action",
                    "Microsoft.Insights/alertRules/read",
                    "Microsoft.Insights/diagnosticSettings/read",
                    "Microsoft.Insights/logDefinitions/read",
                    "Microsoft.Insights/metricdefinitions/read",
                    "Microsoft.Insights/metrics/read",
                    "Microsoft.ResourceHealth/availabilityStatuses/read",
                    "Microsoft.Resources/deployments/*",
                    "Microsoft.Resources/subscriptions/operationresults/read",
                    "Microsoft.Resources/subscriptions/read",
                    "Microsoft.Resources/subscriptions/resourceGroups/read",
                    "Microsoft.Support/*",
                ],
                "notActions": [],
                "dataActions": ["Microsoft.CognitiveServices/*"],
                "notDataActions": [
                    "Microsoft.CognitiveServices/accounts/AIServices/agents/endpoints/UserIdentityImpersonation/action",
                    "Microsoft.CognitiveServices/accounts/OpenAI/fine-tunes-deployments/write",
                    "Microsoft.CognitiveServices/accounts/AIServices/fine_tuning_deployments/write",
                ],
                "condition": None,
                "conditionVersion": None,
            }
        ],
    ),
    AZURE_AI_DEVELOPER_ROLE_ID: (
        "Azure AI Developer",
        [
            {
                "actions": [
                    "Microsoft.MachineLearningServices/workspaces/*/read",
                    "Microsoft.MachineLearningServices/workspaces/*/action",
                    "Microsoft.MachineLearningServices/workspaces/*/delete",
                    "Microsoft.MachineLearningServices/workspaces/*/write",
                    "Microsoft.MachineLearningServices/locations/*/read",
                    "Microsoft.Authorization/*/read",
                    "Microsoft.Resources/deployments/*",
                ],
                "notActions": [
                    "Microsoft.MachineLearningServices/workspaces/delete",
                    "Microsoft.MachineLearningServices/workspaces/write",
                    "Microsoft.MachineLearningServices/workspaces/listKeys/action",
                    "Microsoft.MachineLearningServices/workspaces/hubs/write",
                    "Microsoft.MachineLearningServices/workspaces/hubs/delete",
                    "Microsoft.MachineLearningServices/workspaces/featurestores/write",
                    "Microsoft.MachineLearningServices/workspaces/featurestores/delete",
                    "Microsoft.MachineLearningServices/workspaces/evaluations/results/labels/read",
                    "Microsoft.MachineLearningServices/workspaces/evaluations/results/reasonings/read",
                    "Microsoft.MachineLearningServices/workspaces/simulations/results/images/read",
                ],
                "dataActions": [
                    "Microsoft.CognitiveServices/accounts/OpenAI/*",
                    "Microsoft.CognitiveServices/accounts/SpeechServices/*",
                    "Microsoft.CognitiveServices/accounts/ContentSafety/*",
                    "Microsoft.CognitiveServices/accounts/MaaS/*",
                ],
                "notDataActions": [
                    "Microsoft.CognitiveServices/accounts/OpenAI/fine-tunes-deployments/write"
                ],
                "condition": None,
                "conditionVersion": None,
            }
        ],
    ),
    COGNITIVE_SERVICES_DATA_CONTRIBUTOR_ROLE_ID: (
        "Cognitive Services Data Contributor (Preview)",
        [
            {
                "actions": [],
                "notActions": [],
                "dataActions": ["Microsoft.CognitiveServices/*"],
                "notDataActions": [
                    "Microsoft.CognitiveServices/accounts/OpenAI/fine-tunes-deployments/write",
                    "Microsoft.CognitiveServices/accounts/AIServices/fine_tuning_deployments/write",
                ],
                "condition": None,
                "conditionVersion": None,
            }
        ],
    ),
    FOUNDRY_OWNER_ROLE_ID: (
        "Foundry Owner",
        [
            {
                "actions": [
                    "Microsoft.AlertsManagement/actionRules/*",
                    "Microsoft.AlertsManagement/alerts/*",
                    "Microsoft.AlertsManagement/issues/*",
                    "Microsoft.AlertsManagement/prometheusRuleGroups/*",
                    "Microsoft.AlertsManagement/smartDetectorAlertRules/*",
                    "Microsoft.Authorization/*/read",
                    "Microsoft.Authorization/roleAssignments/write",
                    "Microsoft.Authorization/roleAssignments/delete",
                    "Microsoft.CognitiveServices/*",
                    "Microsoft.Insights/activityLogAlerts/*",
                    "Microsoft.Insights/metricalerts/*",
                    "Microsoft.Insights/scheduledqueryrules/*",
                    "Microsoft.ResourceHealth/availabilityStatuses/read",
                    "Microsoft.Resources/deployments/*",
                    "Microsoft.Resources/deployments/operations/read",
                    "Microsoft.Resources/subscriptions/operationresults/read",
                    "Microsoft.Resources/subscriptions/read",
                    "Microsoft.Resources/subscriptions/resourcegroups/deployments/*",
                    "Microsoft.Resources/subscriptions/resourceGroups/read",
                    "Microsoft.Support/*",
                ],
                "notActions": [],
                "dataActions": ["Microsoft.CognitiveServices/*"],
                "notDataActions": [
                    "Microsoft.CognitiveServices/accounts/AIServices/agents/endpoints/UserIdentityImpersonation/action"
                ],
                "condition": "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR "
                "(@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] "
                "ForAnyOfAnyValues:GuidEquals{eed3b665-ab3a-47b6-8f48-c9382fb1dad6,53ca6127-db72-4b80-b1b0-d745d6d5456d,3bc748fc-213d-45c1-8d91-9da5725539b9,2a1e307c-b015-4ebd-883e-5b7698a07328,73c42c96-874c-492b-b04d-ab87d138a893})) "
                "AND ((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) "
                "OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] "
                "ForAnyOfAnyValues:GuidEquals{eed3b665-ab3a-47b6-8f48-c9382fb1dad6,53ca6127-db72-4b80-b1b0-d745d6d5456d,3bc748fc-213d-45c1-8d91-9da5725539b9,2a1e307c-b015-4ebd-883e-5b7698a07328,73c42c96-874c-492b-b04d-ab87d138a893}))",
                "conditionVersion": "2.0",
            }
        ],
    ),
    FOUNDRY_PROJECT_MANAGER_ROLE_ID: (
        "Foundry Project Manager",
        [
            {
                "actions": [
                    "Microsoft.Authorization/roleAssignments/write",
                    "Microsoft.Authorization/roleAssignments/delete",
                    "Microsoft.CognitiveServices/accounts/*/read",
                    "Microsoft.CognitiveServices/accounts/projects/*",
                    "Microsoft.CognitiveServices/locations/*/read",
                    "Microsoft.Authorization/*/read",
                    "Microsoft.Insights/alertRules/*",
                    "Microsoft.Resources/deployments/*",
                    "Microsoft.Resources/subscriptions/resourceGroups/read",
                ],
                "notActions": [],
                "dataActions": ["Microsoft.CognitiveServices/*"],
                "notDataActions": [
                    "Microsoft.CognitiveServices/accounts/AIServices/agents/endpoints/UserIdentityImpersonation/action",
                    "Microsoft.CognitiveServices/accounts/OpenAI/fine-tunes-deployments/write",
                    "Microsoft.CognitiveServices/accounts/AIServices/fine_tuning_deployments/write",
                ],
                "condition": "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR "
                "(@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] "
                "ForAnyOfAnyValues:GuidEquals{eed3b665-ab3a-47b6-8f48-c9382fb1dad6,53ca6127-db72-4b80-b1b0-d745d6d5456d})) "
                "AND ((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) "
                "OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] "
                "ForAnyOfAnyValues:GuidEquals{eed3b665-ab3a-47b6-8f48-c9382fb1dad6,53ca6127-db72-4b80-b1b0-d745d6d5456d}))",
                "conditionVersion": "2.0",
            }
        ],
    ),
    READER_ROLE_ID: (
        "Reader",
        [
            {
                "actions": ["*/read"],
                "notActions": [],
                "dataActions": [],
                "notDataActions": [],
                "condition": None,
                "conditionVersion": None,
            }
        ],
    ),
    COGNITIVE_SERVICES_DATA_READER_ROLE_ID: (
        "Cognitive Services Data Reader",
        [
            {
                "actions": [],
                "notActions": [],
                "dataActions": ["Microsoft.CognitiveServices/*/read"],
                "notDataActions": [],
                "condition": None,
                "conditionVersion": None,
            }
        ],
    ),
    COGNITIVE_SERVICES_CONTRIBUTOR_ROLE_ID: (
        "Cognitive Services Contributor",
        [
            {
                "actions": [
                    "Microsoft.Authorization/*/read",
                    "Microsoft.CognitiveServices/*",
                    "Microsoft.Features/features/read",
                    "Microsoft.Features/providers/features/read",
                    "Microsoft.Features/providers/features/register/action",
                    "Microsoft.Insights/alertRules/*",
                    "Microsoft.Insights/diagnosticSettings/*",
                    "Microsoft.Insights/logDefinitions/read",
                    "Microsoft.Insights/metricdefinitions/read",
                    "Microsoft.Insights/metrics/read",
                    "Microsoft.ResourceHealth/availabilityStatuses/read",
                    "Microsoft.Resources/deployments/*",
                    "Microsoft.Resources/deployments/operations/read",
                    "Microsoft.Resources/subscriptions/operationresults/read",
                    "Microsoft.Resources/subscriptions/read",
                    "Microsoft.Resources/subscriptions/resourcegroups/deployments/*",
                    "Microsoft.Resources/subscriptions/resourceGroups/read",
                    "Microsoft.Support/*",
                ],
                "notActions": [],
                "dataActions": [],
                "notDataActions": [],
                "condition": None,
                "conditionVersion": None,
            }
        ],
    ),
}
