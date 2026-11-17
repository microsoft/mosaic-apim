"""Real capability refusal, retained drafts and field recovery on the fictional estate."""

import json
import tempfile
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect
from test_cost_center_offerings_browser import fictional_console as fictional_console

from scripts.screenshots import capture

_SYSTEM_TEMP = tempfile.gettempdir()


@pytest.mark.parametrize(
    "fictional_console", [(18043, 15143, 15243)], indirect=True, scope="module"
)
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1440, 320])
def test_real_capability_refusal_keeps_draft_and_recovers_field(
    fictional_console: tuple[str, int, Path],
    monkeypatch: pytest.MonkeyPatch,
    theme: capture.Theme,
    width: int,
) -> None:
    console_url, api_port, logs = fictional_console
    api_url = f"http://127.0.0.1:{api_port}/api/v1/entitlements"

    def check(page: Page) -> None:
        before = page.request.get(api_url)
        assert before.status == 200
        previous = before.json()
        with page.expect_response(
            lambda response: response.url == api_url and response.request.method == "POST"
        ) as submitted:
            capture.preview_gateway_limit_refusal(page)
        response = submitted.value
        assert response.status == 422
        body = response.json()
        assert body["code"] == "validation_error"
        details = body["details"]
        assert details["reason"] == "gatewayPolicyLimitsRejected"
        assert details["boundary"] == "save"
        assert {issue["field"] for issue in details["issues"]} == {
            "enforcement.tokens.tokensPerMinute", "publication.enforcement.tokensPerMinute"
        }
        requested = response.request.post_data_json
        assert requested["enforcement"]["tokens"]["tokensPerMinute"] == 1000
        expected_resource = {
            **requested["resource"], "scopeId": requested["resource"].get("scopeId")
        }
        for issue in details["issues"]:
            assert issue["resource"] == expected_resource
            assert issue["policy"] == "llm-token-limit"
            assert issue["support"] == "unavailable"
            assert issue["reason"] == "unsupportedApiShape"
            assert issue["nextAction"] == "reviewResource"
            assert issue["documentationUrl"] == (
                "https://learn.microsoft.com/en-us/azure/api-management/llm-token-limit-policy"
            )
        assert "counterKeyExpression" not in json.dumps(details)
        assert page.request.get(api_url).json() == previous

        editor = page.get_by_role("region", name="New grant", exact=True)
        outcome = editor.locator("[tabindex='-1']").filter(has_text="Unable to save grant")
        expect(outcome).to_be_focused()
        expect(
            editor.get_by_text("Publication safeguard: Tokens per minute", exact=True)
        ).to_be_visible()
        field = editor.get_by_role("spinbutton", name="Tokens per minute", exact=True)
        expect(field).to_have_value("1000")
        editor.get_by_role("button", name="Review tokens per minute", exact=True).click()
        expect(field).to_be_focused()
        field.fill("1200")
        expect(field).to_be_focused()
        expect(field).to_have_value("1200")
        expect(editor.get_by_text("Unable to save grant", exact=True)).to_be_visible()
        assert page.request.get(api_url).json() == previous
        capture._assert_no_document_overflow(page, f"capability {theme}", expected_width=width)
        page.emulate_media(forced_colors="active")
        capture._assert_no_document_overflow(page, "capability forced colors", expected_width=width)
        page.emulate_media(forced_colors="none")

    with tempfile.TemporaryDirectory(prefix="mosaic-capability-", dir=_SYSTEM_TEMP) as browser_temp:
        for variable in ("TEMP", "TMP", "TMPDIR"):
            monkeypatch.setenv(variable, browser_temp)
        capture.capture(
            [
                capture.Shot(
                    f"capability-refusal-{theme}-{width}", "console", "/entitlements",
                    theme, "Grants", width=width, height=1900, actions=(check,),
                )
            ],
            output=logs / "redacted",
            urls={"console": console_url, "portal": "http://127.0.0.1:15243"},
            api_port=api_port,
            logs=logs,
        )
