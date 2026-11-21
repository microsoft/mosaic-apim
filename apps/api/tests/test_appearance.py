from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from mosaic_api.appearance import contrast
from mosaic_api.config import Settings
from mosaic_api.main import create_app

LIGHT = {
    "accent": "#0f9d8a",
    "backdrop": "#eef6f4",
    "panel": "#ffffff",
    "border": "#c9dcd8",
    "text": "#10231f",
    "textMuted": "#45605a",
}
DARK = {
    "accent": "#0f9d8a",
    "backdrop": "#07120f",
    "panel": "#15241f",
    "border": "#2f4a43",
    "text": "#eef7f4",
    "textMuted": "#a9c2bb",
}


@pytest.fixture
def portal_client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings.model_copy(update={"local_roles": ["User"]}))) as client:
        yield client


def test_contrast_matches_the_wcag_extremes() -> None:
    assert contrast("#000000", "#ffffff") == pytest.approx(21)
    assert contrast("#3566e8", "#3566e8") == pytest.approx(1)
    assert contrast("#ffffff", "#000000") == contrast("#000000", "#ffffff")


def test_appearance_defaults_to_the_built_in_colors(client: TestClient) -> None:
    response = client.get("/api/v1/appearance")

    assert response.status_code == 200
    body = response.json()
    assert body["scheme"] is None
    assert body["allowCustomThemes"] is True
    assert client.get("/api/v1/portal/appearance").json() == {
        "scheme": None,
        "allowCustomThemes": True,
    }


def test_an_administrator_saves_a_scheme_everyone_then_reads(client: TestClient) -> None:
    saved = client.put(
        "/api/v1/appearance",
        json={"scheme": {"light": LIGHT, "dark": DARK}, "allowCustomThemes": False},
    )

    assert saved.status_code == 200
    assert saved.json()["scheme"] == {"light": LIGHT, "dark": DARK}
    assert client.get("/api/v1/portal/appearance").json() == {
        "scheme": {"light": LIGHT, "dark": DARK},
        "allowCustomThemes": False,
    }
    events = client.app.state.appearance_repository.audit_events.values()  # type: ignore[attr-defined]
    assert [(event.action, event.details) for event in events] == [
        ("appearance.settingsUpdated", {"customScheme": True, "allowCustomThemes": False})
    ]

    # A second save replaces the first, and `scheme: null` returns to the built-in colors.
    reset = client.put("/api/v1/appearance", json={"scheme": None, "allowCustomThemes": True})
    assert reset.status_code == 200
    assert client.get("/api/v1/portal/appearance").json() == {
        "scheme": None,
        "allowCustomThemes": True,
    }


def test_hex_colors_are_normalized_to_lower_case(client: TestClient) -> None:
    light = {**LIGHT, "accent": " #0F9D8A "}

    response = client.put("/api/v1/appearance", json={"scheme": {"light": light, "dark": DARK}})

    assert response.status_code == 200
    assert response.json()["scheme"]["light"]["accent"] == "#0f9d8a"


@pytest.mark.parametrize(
    "accent",
    ["#abc", "0f9d8a", "#0f9d8g", "rgb(15, 157, 138)", "var(--mosaic-accent)", "#0f9d8a80", ""],
)
def test_only_six_digit_hex_colors_are_accepted(client: TestClient, accent: str) -> None:
    light = {**LIGHT, "accent": accent}

    response = client.put("/api/v1/appearance", json={"scheme": {"light": light, "dark": DARK}})

    assert response.status_code == 422
    assert client.get("/api/v1/portal/appearance").json()["scheme"] is None


def test_a_palette_people_could_not_read_is_refused(client: TestClient) -> None:
    light = {**LIGHT, "textMuted": "#9fb5af"}

    response = client.put("/api/v1/appearance", json={"scheme": {"light": light, "dark": DARK}})

    assert response.status_code == 422
    assert "Secondary text on panels has a contrast of" in response.text
    assert "It needs at least 4.5:1." in response.text


def test_a_scheme_needs_both_modes_and_no_extra_colors(client: TestClient) -> None:
    assert client.put("/api/v1/appearance", json={"scheme": {"light": LIGHT}}).status_code == 422
    extra = {**LIGHT, "banner": "#ff0000"}
    assert (
        client.put(
            "/api/v1/appearance", json={"scheme": {"light": extra, "dark": DARK}}
        ).status_code
        == 422
    )


def test_a_portal_user_reads_the_colors_but_cannot_change_them(portal_client: TestClient) -> None:
    assert portal_client.get("/api/v1/portal/appearance").status_code == 200
    assert portal_client.get("/api/v1/appearance").status_code == 403
    response = portal_client.put(
        "/api/v1/appearance", json={"scheme": {"light": LIGHT, "dark": DARK}}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "The Admin app role is required"
