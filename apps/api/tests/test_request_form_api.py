import json
from unittest.mock import patch

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from mosaic_api.auth import AuthContext
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.domain import CatalogVisibility
from mosaic_api.main import create_app
from mosaic_api.services.portal import PortalService
from test_approval_workflows import RESOURCE, TENANT, Harness, audit
from test_request_forms import definition, flexible_definition


class Auth:
    def __init__(self) -> None:
        self.context = AuthContext(
            object_id="local-admin",
            tenant_id=TENANT,
            roles=frozenset({"Admin"}),
        )

    async def authenticate(self, _request: Request) -> AuthContext:
        return self.context

    async def close(self) -> None:
        pass


@pytest.mark.parametrize("flexible", [False, True])
async def test_form_http_authorization_submission_and_explicit_safe_views(flexible: bool) -> None:
    config = flexible_definition() if flexible else definition()
    write_in = "Compare with fictional reference notes."
    multiple_answers = (
        [
            {
                "questionId": "uses",
                "value": {"optionIds": ["synthetic", "unsure"], "customText": write_in},
            },
        ]
        if flexible
        else []
    )
    h = Harness()
    await h.seed()
    model = await h.gateways.get_model_api(TENANT, RESOURCE.id)
    assert model is not None
    await h.gateways.save_model_api(
        model.model_copy(update={"visibility": CatalogVisibility.CATALOG}), audit()
    )
    settings = Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=TENANT,
    )
    with TestClient(create_app(settings)) as client:
        app = client.app
        app.state.entitlement_service = h.entitlements
        app.state.portal_service = PortalService(
            h.entitlements,
            directory_repository=h.directory,
            gateway_repository=h.gateways,
            endpoint_repository=h.endpoints,
        )
        auth = Auth()
        app.state.authenticator = auth
        published = client.put(
            "/api/v1/request-form",
            json={
                "expectedRevision": 0,
                "definition": config.model_dump(mode="json"),
                "enableConfirmed": True,
            },
        )
        assert published.status_code == 200, published.text
        auth.context = AuthContext(
            object_id="local-admin",
            tenant_id=TENANT,
            roles=frozenset({"User"}),
        )
        assert client.get("/api/v1/request-form").status_code == 403
        assert client.get("/api/v1/request-form/revisions/1").status_code == 403
        assert (
            client.put(
                "/api/v1/request-form",
                json={
                    "expectedRevision": 1,
                    "definition": config.model_dump(mode="json"),
                },
            ).status_code
            == 403
        )
        target = {"resource": RESOURCE.model_dump(mode="json")}
        assert client.post("/api/v1/portal/access-requests", json=target).status_code == 409
        resolved = client.post("/api/v1/portal/access-requests/form", json=target)
        assert resolved.status_code == 200, resolved.text
        form = resolved.json()
        assert form["status"] == "configured"
        assert form["environment"] == "development"
        assert all("scope" not in question for question in form["questions"])
        assert form["formatVersion"] == (2 if flexible else 1)
        payload = {
            **target,
            "requestForm": {
                "revision": form["revision"],
                "contextRevision": form["contextRevision"],
                "answers": [
                    {"questionId": "purpose", "value": "Fictional request evidence."},
                    {
                        "questionId": "data",
                        "value": {"optionIds": ["synthetic"]} if flexible else "synthetic",
                    },
                    {"questionId": "charge", "value": "LUNA-042"},
                    {"questionId": "ack", "value": True},
                    *multiple_answers,
                ],
            },
        }
        created = client.post("/api/v1/portal/access-requests", json=payload)
        assert created.status_code == 201, created.text
        request_id = created.json()["id"]
        listed = client.get("/api/v1/portal/access-requests")
        for response in (created, listed):
            assert "LUNA-042" not in response.text
            assert write_in not in response.text
            assert "requestFormSummary" in response.text
            assert "requesterName" not in response.text
            assert "approvalProvenance" not in response.text
        with patch.object(
            h.repository, "list_access_requests", wraps=h.repository.list_access_requests
        ) as history:
            detail = client.get(f"/api/v1/portal/access-requests/{request_id}")
            assert history.call_count == 0
        assert detail.status_code == 200
        assert "LUNA-042" in detail.text
        if flexible:
            assert detail.json()["recordedForm"]["answers"][-1] == multiple_answers[0]
        assert set(detail.json()["recordedForm"]) == {
            "formatVersion",
            "revision",
            "questions",
            "answers",
        }
        assert "schemaHash" not in detail.text
        assert "costCenterGroupIds" not in detail.text
        auth.context = AuthContext(
            object_id="someone-else",
            tenant_id=TENANT,
            roles=frozenset({"User"}),
        )
        assert client.get(f"/api/v1/portal/access-requests/{request_id}").status_code == 404
        auth.context = AuthContext(
            object_id="local-admin",
            tenant_id="another-tenant",
            roles=frozenset({"User"}),
        )
        assert client.get(f"/api/v1/portal/access-requests/{request_id}").status_code == 404
        auth.context = AuthContext(
            object_id="local-admin",
            tenant_id=TENANT,
            roles=frozenset({"Admin"}),
        )
        admin_list = client.get("/api/v1/access-requests")
        admin_detail = client.get(f"/api/v1/access-requests/{request_id}")
        for sensitive in ["LUNA-042", *([write_in] if flexible else [])]:
            assert sensitive not in admin_list.text
            assert sensitive in admin_detail.text
            assert sensitive not in json.dumps(
                [event.model_dump(mode="json") for event in h.repository.audit_events.values()]
            )
            assert sensitive not in json.dumps(
                [
                    event.model_dump(mode="json")
                    for event in h.repository.notification_events.values()
                ]
            )
