"""AWS Bedrock endpoints, and the pools that serve their Anthropic models (ADR 0024, phase 3).

An AWS Bedrock host serves the Anthropic Messages API with a Bedrock API key. MOSAIC registers the
host with the Key Vault secret that holds the key, reads the key only to confirm it's there, and
never sends it to AWS. It serves the model IDs an administrator declares only as members of a
model pool, which the gateway reaches with the key, read from Key Vault through a named value.

These drive the real endpoint, pool and publishing services against the Key Vault, Cognitive
Services and API Management doubles.
"""

import re

import pytest
from aoai_double import AI_RESOURCE_ID
from key_vault_double import SECRET_URI, VAULT_NAME, FakeKeyStore
from mosaic_api.deployment_capacity import CapacityType, ProcessingScope, classify_bedrock_model
from mosaic_api.domain import (
    AccessEvaluation,
    ApiShape,
    BedrockEndpointUrl,
    DeclaredDeployment,
    DeclaredDeploymentCreate,
    EndpointAuthMode,
    GatewayRuntimeAccess,
    ModelEndpoint,
    ModelEndpointCapabilities,
    ModelEndpointCreate,
    ModelEndpointStatus,
    ModelProvider,
    PublishRunStatus,
    RuntimeAccessEvaluation,
    RuntimeAccessReason,
    deterministic_id,
    is_bedrock_host,
)
from mosaic_api.errors import (
    ConflictError,
    UpstreamAuthorizationError,
    UpstreamError,
    ValidationError,
)
from mosaic_api.integrations.backend_keys import backend_key_name
from mosaic_api.integrations.key_vault import (
    MANAGED_BY,
    MANAGED_BY_TAG,
    MODEL_ENDPOINT_TAG,
    STORED_KEY_PREFIX,
)
from mosaic_api.services.model_endpoints import BEDROCK_ENDPOINT_NOTES
from pydantic import ValidationError as SchemaError
from test_key_endpoints import ACTOR, KEY, PROJECT_URL, KeyWorld, keyed
from test_key_publishing import KeyHarness
from test_model_pools import ACTOR as POOL_ACTOR
from test_model_pools import (
    TENANT,
    Estate,
    _candidate,
    _endpoint_id,
    _granted,
    _group,
    _member,
    _model,
    _steps,
    _xml,
)
from test_model_pools import _audit as _pool_audit
from test_pool_backend_keys import (
    ATTEMPT,
    BEARER,
    _api_policy,
    _fragment,
    _key_secret,
    _placements,
    _route,
)

BEDROCK_HOST = "bedrock-runtime.us-east-1.amazonaws.com"
BEDROCK_URL = f"https://{BEDROCK_HOST}"
MANTLE_URL = "https://bedrock-mantle.us-east-1.api.aws"
# A US cross-region inference profile, a global one, and a model served in its own region.
BEDROCK_ID = "us.anthropic.claude-opus-4-5-20251101-v1:0"
GLOBAL_ID = "global.anthropic.claude-opus-4-5-20251101-v1:0"
REGIONAL_ID = "anthropic.claude-opus-4-5-20251101-v1:0"
BEDROCK_DECL = DeclaredDeploymentCreate(
    deployment_name=BEDROCK_ID,
    model_name="claude-opus-4-5",
    model_version="1",
    api_shape=ApiShape.ANTHROPIC_MESSAGES,
)
LABEL = f"{BEDROCK_ID} on bedrock-east"
NOT_CONFIRMED = (
    "The gateway can read this endpoint's API key from Key Vault, but MOSAIC doesn't send keys to "
    "AWS, so it can't confirm that AWS accepts it. The first request through the pool shows "
    "whether it does."
)
OUTSIDE_AZURE = "outside Azure, so AWS processes the requests it serves, prompts included."
CROSS_REGION = (
    " Its model ID is a cross-region inference profile, so AWS may process them in other regions "
    "too."
)
NO_TOKEN_COUNT = (
    " AWS documents counting tokens with the Anthropic API only on bedrock-mantle hosts, so a "
    "token count the gateway sends it may fail."
)


def bedrock(**overrides: object) -> ModelEndpointCreate:
    return keyed(
        **{
            "endpoint": BEDROCK_URL,
            "deployments": [BEDROCK_DECL],
            "name": "Bedrock US East",
            **overrides,
        }
    )


def _create(**payload: object) -> ModelEndpointCreate:
    return ModelEndpointCreate.model_validate(payload)


@pytest.fixture
async def world() -> KeyWorld:
    built = KeyWorld()
    await built.add_gateway()
    return built


async def _add_bedrock(
    estate: Estate,
    name: str = "bedrock-east",
    *,
    url: str = BEDROCK_URL,
    model_id: str = BEDROCK_ID,
    location: str = "us-east-1",
) -> None:
    """An AWS Bedrock endpoint beside the pool estate, with its key in Key Vault."""

    endpoint = ModelEndpoint(
        id=_endpoint_id(name),
        tenant_id=TENANT,
        name=name,
        provider=ModelProvider.AWS_BEDROCK,
        endpoint=url,
        environment="development",
        auth_mode=EndpointAuthMode.API_KEY,
        runtime_access=[_granted(estate.gateway_id)],
        capabilities=ModelEndpointCapabilities(location=location),
        declared_deployments=[
            DeclaredDeployment(
                deployment_name=model_id,
                model_name="claude-opus-4-5",
                model_version="1",
                api_shape=ApiShape.ANTHROPIC_MESSAGES,
            )
        ],
    )
    await estate.endpoint_repository.save_endpoint(endpoint, _pool_audit())
    await estate.observe(name, [])
    await _key_secret(estate, name, SECRET_URI)


@pytest.fixture
async def estate() -> Estate:
    """The pool estate, with bedrock-east serving Claude Opus 4.5 through a US profile."""

    built = Estate()
    await built.setup()
    await _add_bedrock(built)
    return built


# -- the endpoint URL ----------------------------------------------------------------------------


class TestEndpointUrl:
    @pytest.mark.parametrize(
        ("url", "host", "region", "slug"),
        [
            (BEDROCK_URL, BEDROCK_HOST, "us-east-1", "bedrock-runtime-us-east-1"),
            (f"{BEDROCK_URL}/", BEDROCK_HOST, "us-east-1", "bedrock-runtime-us-east-1"),
            (f"{BEDROCK_URL}/anthropic", BEDROCK_HOST, "us-east-1", "bedrock-runtime-us-east-1"),
            (
                f"  {BEDROCK_URL}/Anthropic/V1/  ",
                BEDROCK_HOST,
                "us-east-1",
                "bedrock-runtime-us-east-1",
            ),
            (
                "HTTPS://Bedrock-Runtime.AP-Southeast-2.amazonaws.com",
                "bedrock-runtime.ap-southeast-2.amazonaws.com",
                "ap-southeast-2",
                "bedrock-runtime-ap-southeast-2",
            ),
            (
                "https://bedrock-runtime.us-gov-west-1.amazonaws.com",
                "bedrock-runtime.us-gov-west-1.amazonaws.com",
                "us-gov-west-1",
                "bedrock-runtime-us-gov-west-1",
            ),
            (
                f"{MANTLE_URL}/anthropic/v1",
                "bedrock-mantle.us-east-1.api.aws",
                "us-east-1",
                "bedrock-mantle-us-east-1",
            ),
        ],
    )
    def test_accepts_the_regional_hosts_that_serve_the_anthropic_api(
        self, url: str, host: str, region: str, slug: str
    ) -> None:
        parsed = BedrockEndpointUrl.parse(url)
        assert (parsed.host, parsed.region, parsed.slug) == (host, region, slug)
        assert parsed.origin == f"https://{host}"

    @pytest.mark.parametrize(
        ("url", "message"),
        [
            ("http://bedrock-runtime.us-east-1.amazonaws.com", "over https"),
            (f"{BEDROCK_URL}:443", "over https"),
            (f"{BEDROCK_URL}:abc", "Expected https://bedrock-runtime"),
            (f"{BEDROCK_URL}/?region=us-east-1", "over https"),
            (f"{BEDROCK_URL}/#messages", "over https"),
            (f"https://admin@{BEDROCK_HOST}", "over https"),
            ("https://bedrock-runtime-fips.us-east-1.amazonaws.com", "regional runtime endpoint"),
            ("https://bedrock-runtime.cn-north-1.amazonaws.com.cn", "regional runtime endpoint"),
            ("https://bedrock.us-east-1.amazonaws.com", "regional runtime endpoint"),
            ("https://models.example.com", "regional runtime endpoint"),
            (f"{BEDROCK_URL}/anthropic/v1/messages", "without an operation path"),
            (f"{BEDROCK_URL}/openai/v1", "without an operation path"),
        ],
    )
    def test_refuses_anything_else(self, url: str, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            BedrockEndpointUrl.parse(url)

    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            (BEDROCK_HOST, True),
            ("BEDROCK-RUNTIME.US-EAST-1.AMAZONAWS.COM.", True),
            ("bedrock-runtime-fips.us-east-1.amazonaws.com", True),
            ("bedrock.us-east-1.amazonaws.com", True),
            ("bedrock-runtime.cn-north-1.amazonaws.com.cn", True),
            ("bedrock-mantle.us-east-1.api.aws", True),
            ("s3.us-east-1.amazonaws.com", False),
            ("bedrock.example.com", False),
            ("fabrikam-foundry.services.ai.azure.com", False),
            (None, False),
        ],
    )
    def test_knows_bedrock_hosts_it_cant_reach_too(self, host: str | None, expected: bool) -> None:
        assert is_bedrock_host(host) is expected


@pytest.mark.parametrize(
    ("model_id", "capacity", "scope"),
    [
        (REGIONAL_ID, CapacityType.PAY_AS_YOU_GO, ProcessingScope.REGIONAL),
        (BEDROCK_ID, CapacityType.PAY_AS_YOU_GO, ProcessingScope.DATA_ZONE),
        ("eu.anthropic.claude-sonnet-4-5-20250929-v1:0", CapacityType.PAY_AS_YOU_GO,
         ProcessingScope.DATA_ZONE),
        ("apac.anthropic.claude-3-haiku-20240307-v1:0", CapacityType.PAY_AS_YOU_GO,
         ProcessingScope.DATA_ZONE),
        ("us-gov.anthropic.claude-3-haiku-20240307-v1:0", CapacityType.PAY_AS_YOU_GO,
         ProcessingScope.DATA_ZONE),
        (GLOBAL_ID, CapacityType.PAY_AS_YOU_GO, ProcessingScope.GLOBAL),
        (" Global.Anthropic.Claude-Opus-4-5 ", CapacityType.PAY_AS_YOU_GO, ProcessingScope.GLOBAL),
        ("meta.llama3-70b-instruct-v1:0", CapacityType.PAY_AS_YOU_GO, ProcessingScope.UNKNOWN),
    ],
)
def test_a_bedrock_model_id_says_how_it_is_served(
    model_id: str, capacity: CapacityType, scope: ProcessingScope
) -> None:
    assert classify_bedrock_model(model_id) == (capacity, scope)


# -- registering one -------------------------------------------------------------------------------


class TestRequest:
    @pytest.mark.parametrize(
        "payload",
        [
            {"endpoint": BEDROCK_URL, "api_key": KEY},
            {"endpoint": BEDROCK_URL, "api_key": KEY, "deployments": [BEDROCK_DECL]},
            {"endpoint": BEDROCK_URL, "credential_secret_uri": SECRET_URI,
             "deployments": [BEDROCK_DECL]},
            {"endpoint": BEDROCK_URL, "credential_secret_uri": SECRET_URI,
             "provider": "awsBedrock"},
        ],
    )
    def test_a_key_or_declared_models_make_a_bedrock_host_bedrock(
        self, payload: dict[str, object]
    ) -> None:
        assert _create(**payload).provider == ModelProvider.AWS_BEDROCK

    def test_a_bedrock_host_with_only_a_secret_is_openai_compatible(self) -> None:
        # Bedrock also serves OpenAI-compatible routes, which take neither a key nor model IDs.
        request = _create(endpoint=f"{BEDROCK_URL}/openai/v1", credential_secret_uri=SECRET_URI)
        assert request.provider == ModelProvider.OPENAI_COMPATIBLE

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            (
                {"endpoint": BEDROCK_URL, "provider": "awsBedrock"},
                "Give the Bedrock API key, or the Key Vault secret URI that holds it",
            ),
            (
                {"endpoint": BEDROCK_URL, "api_key": KEY, "credential_secret_uri": SECRET_URI},
                "not both",
            ),
            (
                {"azure_resource_id": AI_RESOURCE_ID, "provider": "awsBedrock"},
                "Register an AWS Bedrock endpoint by its URL",
            ),
            (
                {"endpoint": "https://models.example.com", "provider": "awsBedrock",
                 "api_key": KEY},
                "regional runtime endpoint",
            ),
            (
                {"endpoint": "https://bedrock-runtime-fips.us-east-1.amazonaws.com",
                 "api_key": KEY},
                "regional runtime endpoint",
            ),
            (
                {"endpoint": f"{BEDROCK_URL}/anthropic/v1/messages", "api_key": KEY},
                "without an operation path",
            ),
            (
                {
                    "endpoint": BEDROCK_URL,
                    "api_key": KEY,
                    "deployments": [
                        DeclaredDeploymentCreate(
                            deployment_name="gpt-4o",
                            model_name="gpt-4o",
                            api_shape=ApiShape.AZURE_OPENAI,
                        )
                    ],
                },
                "only through its Anthropic Messages API, so gpt-4o has to be declared",
            ),
        ],
    )
    def test_refuses_what_mosaic_cant_reach(
        self, payload: dict[str, object], message: str
    ) -> None:
        with pytest.raises(SchemaError, match=message) as refused:
            _create(**payload)
        # A refused request never repeats the key it carried.
        assert KEY not in str(refused.value)

    def test_a_model_id_may_have_a_colon_but_an_azure_deployment_name_may_not(self) -> None:
        assert [item.deployment_name for item in bedrock().deployments or []] == [BEDROCK_ID]
        with pytest.raises(SchemaError, match="isn't an Azure deployment name"):
            keyed(endpoint=PROJECT_URL, deployments=[BEDROCK_DECL])

    def test_a_provisioned_throughput_arn_is_refused(self) -> None:
        # Provisioned throughput is called by its ARN, which MOSAIC doesn't serve.
        with pytest.raises(SchemaError, match="A Bedrock model ID can also have colons"):
            DeclaredDeploymentCreate(
                deployment_name="provisioned-model/abc123def456",
                model_name="claude-opus-4-5",
                api_shape=ApiShape.ANTHROPIC_MESSAGES,
            )


class TestRegistration:
    async def test_registers_the_host_without_sending_the_key_to_aws(self, world: KeyWorld) -> None:
        endpoint = await world.register(**_bedrock_overrides())

        assert endpoint.id == deterministic_id("endpoint", ACTOR.tenant_id, "bedrock", BEDROCK_HOST)
        assert endpoint.provider == ModelProvider.AWS_BEDROCK
        assert endpoint.auth_mode == EndpointAuthMode.API_KEY
        assert str(endpoint.endpoint).rstrip("/") == BEDROCK_URL
        assert endpoint.name == "Bedrock US East"
        assert endpoint.azure_resource_id is None
        assert endpoint.key_stored_by_mosaic is False
        assert endpoint.capabilities.location == "us-east-1"
        assert endpoint.capabilities.notes == list(BEDROCK_ENDPOINT_NOTES)
        [declared] = endpoint.declared_deployments
        assert (declared.deployment_name, declared.api_shape) == (
            BEDROCK_ID,
            ApiShape.ANTHROPIC_MESSAGES,
        )
        assert declared.declared_by == ACTOR.object_id
        credential = world.endpoint_repository.credentials[endpoint.credential_reference_id or ""]
        assert credential.id == deterministic_id(
            "credential", ACTOR.tenant_id, "bedrock", BEDROCK_URL
        )
        assert str(credential.secret_uri) == SECRET_URI
        # MOSAIC reads the key to confirm it's there, and never sends it anywhere.
        assert world.secret_reads == [SECRET_URI]
        assert world.probes == []
        assert endpoint.status == ModelEndpointStatus.PENDING
        assert endpoint.access.can_read is False
        assert endpoint.access.evaluation == AccessEvaluation.NOT_EVALUATED
        assert endpoint.access.message == (
            "MOSAIC read the Bedrock API key from Key Vault. It doesn't send keys to AWS to check "
            "them, so the first request through a pool is what tells whether AWS accepts it."
        )

    async def test_a_host_is_registered_once(self, world: KeyWorld) -> None:
        first = await world.register(**_bedrock_overrides())

        with pytest.raises(
            ConflictError,
            match=re.escape(f"{BEDROCK_HOST} is already registered, as Bedrock US East."),
        ) as refused:
            await world.register(
                **_bedrock_overrides(endpoint=f"{BEDROCK_URL}/anthropic/v1", name="Again")
            )
        assert refused.value.details == {"id": first.id, "name": "Bedrock US East"}

        # The region's other host is another endpoint.
        mantle = await world.register(**_bedrock_overrides(endpoint=MANTLE_URL, name="Mantle"))
        assert mantle.id != first.id
        assert str(mantle.endpoint).rstrip("/") == MANTLE_URL
        assert mantle.capabilities.location == "us-east-1"

    async def test_an_openai_compatible_endpoint_at_the_host_blocks_it(
        self, world: KeyWorld
    ) -> None:
        compatible = await world.service.register(
            ACTOR, _create(endpoint=BEDROCK_URL, credential_secret_uri=SECRET_URI)
        )
        assert compatible.provider == ModelProvider.OPENAI_COMPATIBLE

        with pytest.raises(ConflictError, match="already registered with MOSAIC") as refused:
            await world.register(**_bedrock_overrides())
        assert refused.value.details["id"] == compatible.id

    async def test_it_blocks_an_openai_compatible_endpoint_at_the_host(
        self, world: KeyWorld
    ) -> None:
        endpoint = await world.register(**_bedrock_overrides())

        with pytest.raises(ConflictError, match="already registered with MOSAIC") as refused:
            await world.service.register(
                ACTOR, _create(endpoint=f"{BEDROCK_URL}/", credential_secret_uri=SECRET_URI)
            )
        assert refused.value.details["id"] == endpoint.id
        # Bedrock's OpenAI-compatible route is another URL, so it can be registered beside it.
        compatible = await world.service.register(
            ACTOR,
            _create(endpoint=f"{BEDROCK_URL}/openai/v1", credential_secret_uri=SECRET_URI),
        )
        assert compatible.provider == ModelProvider.OPENAI_COMPATIBLE

    async def test_a_pasted_key_is_kept_in_mosaics_key_vault(self) -> None:
        world = KeyWorld(key_store=FakeKeyStore())
        await world.add_gateway()

        endpoint = await world.register(
            **_bedrock_overrides(credential_secret_uri=None, api_key=KEY)
        )

        assert world.key_store is not None
        [name] = world.key_store.versions
        assert re.fullmatch(rf"{STORED_KEY_PREFIX}bedrock-runtime-us-east-1-[0-9a-f]{{8}}", name)
        assert world.key_store.versions[name] == [KEY]
        assert world.key_store.tags[name] == {
            MANAGED_BY_TAG: MANAGED_BY,
            MODEL_ENDPOINT_TAG: endpoint.id,
        }
        assert endpoint.key_stored_by_mosaic is True
        credential = world.endpoint_repository.credentials[endpoint.credential_reference_id or ""]
        assert str(credential.secret_uri) == f"https://{VAULT_NAME}.vault.azure.net/secrets/{name}"
        assert world.probes == []
        assert endpoint.status == ModelEndpointStatus.PENDING
        assert KEY not in endpoint.model_dump_json()

    async def test_a_pasted_key_needs_a_key_vault_to_keep_it_in(self, world: KeyWorld) -> None:
        with pytest.raises(ConflictError, match="has no Key Vault to keep an API key in") as error:
            await world.register(**_bedrock_overrides(credential_secret_uri=None, api_key=KEY))
        assert error.value.details == {"reason": "keyStoreUnavailable"}
        assert await world.endpoint_repository.list_endpoints(ACTOR.tenant_id) == []

    @pytest.mark.parametrize(
        ("error", "status", "message"),
        [
            (
                UpstreamAuthorizationError("denied"),
                ModelEndpointStatus.UNAUTHORIZED,
                "isn't allowed",
            ),
            (ValidationError("missing"), ModelEndpointStatus.DEGRADED, "has no such secret"),
            (UpstreamError("unreachable"), ModelEndpointStatus.UNREACHABLE, "couldn't reach"),
        ],
    )
    async def test_a_key_mosaic_cant_read_says_why(
        self, world: KeyWorld, error: Exception, status: ModelEndpointStatus, message: str
    ) -> None:
        world.secret_error = error

        endpoint = await world.register(**_bedrock_overrides())

        assert endpoint.status == status
        assert endpoint.access.can_read is False
        assert message in (endpoint.access.message or "")
        assert world.probes == []

    async def test_a_key_stored_with_a_line_break_is_caught(self, world: KeyWorld) -> None:
        world.secret_value = f"{KEY}\n"

        endpoint = await world.register(**_bedrock_overrides())

        assert endpoint.status == ModelEndpointStatus.DEGRADED
        assert "line break" in (endpoint.access.message or "")
        assert KEY not in endpoint.model_dump_json()

    async def test_a_bedrock_api_key_cant_list_models(self, world: KeyWorld) -> None:
        endpoint = await world.register(**_bedrock_overrides())

        with pytest.raises(ConflictError, match="Declare the model IDs to pool instead"):
            await world.service.start_sync(ACTOR, endpoint.id)

    async def test_more_model_ids_are_declared_with_the_anthropic_api(
        self, world: KeyWorld
    ) -> None:
        endpoint = await world.register(**_bedrock_overrides())

        updated = await world.service.declare_deployment(
            ACTOR,
            endpoint.id,
            DeclaredDeploymentCreate(
                deployment_name=GLOBAL_ID,
                model_name="claude-opus-4-5",
                api_shape=ApiShape.ANTHROPIC_MESSAGES,
            ),
        )
        assert [item.deployment_name for item in updated.declared_deployments] == [
            BEDROCK_ID,
            GLOBAL_ID,
        ]

        with pytest.raises(ValidationError, match="only through its Anthropic Messages API"):
            await world.service.declare_deployment(
                ACTOR,
                endpoint.id,
                DeclaredDeploymentCreate(
                    deployment_name="meta.llama3-70b-instruct-v1:0",
                    model_name="llama3-70b-instruct",
                    api_shape=ApiShape.FOUNDRY_MODELS,
                ),
            )
        with pytest.raises(ConflictError, match="is already declared on Bedrock US East"):
            await world.service.declare_deployment(ACTOR, endpoint.id, BEDROCK_DECL)


def _bedrock_overrides(**overrides: object) -> dict[str, object]:
    """What ``KeyWorld.register`` needs to register bedrock-runtime in us-east-1."""

    return {
        "endpoint": BEDROCK_URL,
        "deployments": [BEDROCK_DECL],
        "name": "Bedrock US East",
        **overrides,
    }


# -- serving it --------------------------------------------------------------------------------


async def test_a_bedrock_model_is_served_only_through_a_pool() -> None:
    harness = KeyHarness()
    await harness.setup(**_bedrock_overrides())

    offered = await harness.service.publishable_models(ACTOR, harness.gateway_id)
    assert all(item.model_endpoint_id != harness.endpoint_id for item in offered)
    with pytest.raises(ValidationError, match="only through a model pool"):
        await harness.publish(BEDROCK_ID)


async def test_a_bedrock_model_joins_the_models_it_shares_a_name_with(estate: Estate) -> None:
    candidates = await estate.service.candidates(POOL_ACTOR, estate.gateway_id)

    claude = _group(candidates, "claude-opus-4-5", ApiShape.ANTHROPIC_MESSAGES)
    assert claude.model_format == "Anthropic"
    bedrock_candidate = _candidate(claude, "bedrock-east", BEDROCK_ID)
    assert bedrock_candidate.eligible is True, bedrock_candidate.reason
    # MOSAIC never sends the key to AWS, so a Bedrock deployment is never shown as ready.
    assert bedrock_candidate.readiness == "notConfirmed"
    assert (bedrock_candidate.capacity_type, bedrock_candidate.processing_scope) == (
        CapacityType.PAY_AS_YOU_GO,
        ProcessingScope.DATA_ZONE,
    )
    assert bedrock_candidate.region == "us-east-1"
    assert (bedrock_candidate.declared, bedrock_candidate.api_key) == (True, True)
    assert bedrock_candidate.provider == ModelProvider.AWS_BEDROCK
    assert _candidate(claude, "foundry-east", "claude-opus-4-5").provider == (
        ModelProvider.AZURE_AI_FOUNDRY
    )


async def test_a_model_id_callers_cant_send_needs_a_public_name(estate: Estate) -> None:
    with pytest.raises(ValidationError, match="Give this model a public name"):
        await estate.create("Claude", _model(_member("bedrock-east", BEDROCK_ID)))


async def test_a_pool_of_one_bedrock_model_tries_it_once_with_its_key(estate: Estate) -> None:
    pool = await estate.create(
        "Claude",
        _model(_member("bedrock-east", BEDROCK_ID), public_name="claude-opus-4-5"),
    )
    [model] = pool.models
    [member] = model.members
    key_name = backend_key_name(member.backend_name)

    plan = await estate.service.plan(POOL_ACTOR, pool.id)

    assert "backendPool" not in {kind for kind, _, _ in _steps(plan)}
    assert ("namedValue", key_name, "create") in _steps(plan)
    assert (
        "claude-opus-4-5's only active deployment is reached with an API key, which an API "
        "Management backend pool can't hold. The gateway tries it once, with no circuit breaker."
    ) in plan.warnings
    run = await estate.apply(pool.id, plan)
    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    assert estate.apim.dangling_references == []

    written = estate.apim.written
    backend = written[f"backends/{member.backend_name}"]["properties"]
    # Bedrock serves the Anthropic API at its host, as Foundry does.
    assert backend["url"] == BEDROCK_URL
    assert "circuitBreaker" not in backend
    named_value = written[f"namedValues/{key_name}"]["properties"]
    assert named_value["keyVault"]["secretIdentifier"] == SECRET_URI
    assert "value" not in named_value
    # No member is reached with the gateway's identity, so nothing asks for a token.
    assert list(_xml(_fragment(estate, pool)).iter("authentication-managed-identity")) == []

    policy = _api_policy(estate, pool)
    assert policy.find("backend/retry") is None
    route = _route(policy, model.id)
    assert _placements(route) == [
        (None, member.backend_name, [("x-api-key", "override", f"{{{{{key_name}}}}}")])
    ]
    # Callers send the public name, and Bedrock gets the model ID it serves the model under.
    [body] = route.findall("set-body")
    assert f'body["model"] = "{BEDROCK_ID}"' in (body.text or "")


async def test_a_bedrock_model_is_tried_after_the_azure_deployments_of_its_model(
    estate: Estate,
) -> None:
    pool = await estate.create(
        "Claude",
        _model(
            _member("foundry-east", "claude-opus-4-5"),
            _member("foundry-west", "claude-opus-4-5"),
            _member("bedrock-east", BEDROCK_ID),
            public_name="claude-opus-4-5",
        ),
    )
    model = pool.models[0]
    east, west, bedrock_member = model.members
    key_name = backend_key_name(bedrock_member.backend_name)

    plan = await estate.service.plan(POOL_ACTOR, pool.id)

    disclosure = f"{LABEL} is on AWS Bedrock in us-east-1, {OUTSIDE_AZURE}{CROSS_REGION}"
    for warning in (
        "1 of claude-opus-4-5's 3 active deployments is reached with an API key, which an API "
        "Management backend pool can't hold. The gateway tries it once, after the backend pool's "
        "attempts, with no circuit breaker.",
        f"{disclosure}{NO_TOKEN_COUNT}",
        f"{LABEL}: {NOT_CONFIRMED}",
    ):
        assert warning in plan.warnings
    assert any(
        "process data in different scopes (dataZone, global)" in item for item in plan.warnings
    )

    run = await estate.apply(pool.id, plan)
    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    assert estate.apim.dangling_references == []
    services = estate.apim.written[f"backends/{model.backend_pool_name}"]["properties"]["pool"][
        "services"
    ]
    assert [item["id"].rsplit("/", 1)[-1] for item in services] == [
        east.backend_name,
        west.backend_name,
    ]

    route = _route(_api_policy(estate, pool), model.id)
    assert _placements(route) == [
        (
            f"@({ATTEMPT} <= 2)",
            model.backend_pool_name,
            [("Authorization", "override", BEARER), ("x-api-key", "delete", None)],
        ),
        (
            f"@({ATTEMPT} == 3)",
            bedrock_member.backend_name,
            [("Authorization", "delete", None), ("x-api-key", "override", f"{{{{{key_name}}}}}")],
        ),
    ]
    bodies = [when.findtext("set-body") or "" for when in route.findall("choose/when")]
    assert '"claude-opus-4-5"' in bodies[0]
    assert f'"{BEDROCK_ID}"' in bodies[1]

    detail = await estate.service.detail(POOL_ACTOR, pool.id)
    assert detail.problems == []
    views = {member.endpoint_name: member for member in detail.models[0].members}
    view = views["bedrock-east"]
    assert (view.order, view.priority) == (1, None)
    assert (view.region, view.provider) == ("us-east-1", ModelProvider.AWS_BEDROCK)
    assert (view.capacity_type, view.processing_scope) == (
        CapacityType.PAY_AS_YOU_GO,
        ProcessingScope.DATA_ZONE,
    )
    assert (view.readiness, view.readiness_message) == ("notConfirmed", NOT_CONFIRMED)
    assert (view.api_key, view.declared, view.observed) == (True, True, False)
    assert views["foundry-east"].provider == ModelProvider.AZURE_AI_FOUNDRY


async def test_a_gateway_that_cant_read_the_bedrock_key_is_a_problem(estate: Estate) -> None:
    pool = await estate.create(
        "Claude",
        _model(_member("bedrock-east", BEDROCK_ID), public_name="claude-opus-4-5"),
    )
    await estate.update_endpoint(
        "bedrock-east",
        runtime_access=[
            GatewayRuntimeAccess(
                gateway_id=estate.gateway_id,
                gateway_name="apim-contoso-dev",
                can_invoke=False,
                evaluation=RuntimeAccessEvaluation.ROLE_ASSIGNMENTS,
                reason=RuntimeAccessReason.MISSING_ROLE,
            )
        ],
    )

    detail = await estate.service.detail(POOL_ACTOR, pool.id)

    assert detail.models[0].members[0].readiness == "cannotInvoke"
    assert (
        f"{LABEL}: The gateway's managed identity can't read this endpoint's API key from Key "
        "Vault. Grant the role shown on the endpoint."
    ) in detail.problems
    with pytest.raises(ConflictError, match="can't be published until its problems are fixed"):
        await estate.service.plan(POOL_ACTOR, pool.id)


async def test_the_disclosure_says_only_what_applies_to_the_model_and_host(
    estate: Estate,
) -> None:
    await _add_bedrock(
        estate,
        "bedrock-west",
        url="https://bedrock-runtime.us-west-2.amazonaws.com",
        model_id=REGIONAL_ID,
        location="us-west-2",
    )
    await _add_bedrock(estate, "bedrock-mantle", url=MANTLE_URL, model_id=GLOBAL_ID)
    pool = await estate.create(
        "Claude",
        _model(
            _member("bedrock-west", REGIONAL_ID),
            _member("bedrock-mantle", GLOBAL_ID),
            public_name="claude-opus-4-5",
        ),
    )

    detail = await estate.service.detail(POOL_ACTOR, pool.id)

    # A model served in its own region stays there, and only a bedrock-runtime host may fail a
    # token count.
    assert (
        f"{REGIONAL_ID} on bedrock-west is on AWS Bedrock in us-west-2, {OUTSIDE_AZURE}"
        f"{NO_TOKEN_COUNT}"
    ) in detail.warnings
    assert (
        f"{GLOBAL_ID} on bedrock-mantle is on AWS Bedrock in us-east-1, {OUTSIDE_AZURE}"
        f"{CROSS_REGION}"
    ) in detail.warnings
    scopes = {member.endpoint_name: member.processing_scope for member in detail.models[0].members}
    assert scopes == {
        "bedrock-west": ProcessingScope.REGIONAL,
        "bedrock-mantle": ProcessingScope.GLOBAL,
    }
