"""Quota authoring, draft safety and reflow against the fictional estate only."""

import tempfile
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect
from test_cost_center_offerings_browser import fictional_console as fictional_console

from scripts.screenshots import capture

_SYSTEM_TEMP = tempfile.gettempdir()


@pytest.mark.parametrize(
    "fictional_console", [(18052, 15152, 15252)], indirect=True, scope="function"
)
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1440, 320])
def test_call_quota_draft_focus_and_reflow(
    fictional_console: tuple[str, int, Path],
    monkeypatch: pytest.MonkeyPatch,
    theme: capture.Theme,
    width: int,
) -> None:
    console_url, api_port, logs = fictional_console

    def check_native_input(page: Page) -> None:
        approvals: list[str] = []
        page.on("request", lambda request: approvals.append(request.url)
                if request.method == "POST" and request.url.endswith("/approve") else None)
        capture.select_resource_request(page)
        page.get_by_role("button", name="Approve", exact=True).click()
        editor = page.get_by_role("region", name="Approve access request", exact=True)
        approve = editor.get_by_role("button", name="Approve and create grant", exact=True)
        expect(approve).to_be_enabled()
        for label in ("Call quota", "Calls"):
            field = editor.get_by_role("spinbutton", name=label, exact=True)
            for symbol in ("-", "e"):
                field.fill("")
                field.press_sequentially(symbol)
                state = field.evaluate("e => ({value: e.value, badInput: e.validity.badInput})")
                assert state == {"value": "", "badInput": True}
                expect(field).to_be_focused()
                expect(approve).to_be_disabled()
                field.press("Enter")
                assert approvals == []
                field.press("ControlOrMeta+A")
                field.press("Backspace")
                expect(approve).to_be_enabled()
        assert approvals == []

    def check(page: Page) -> None:
        writes: list[dict[str, Any]] = []

        def refuse_save(route: Route) -> None:
            if route.request.method == "PATCH":
                writes.append(route.request.post_data_json)
                route.fulfill(
                    status=422,
                    json={
                        "code": "validation_error",
                        "message": "Quota draft was not saved. Your values are retained.",
                    },
                )
            else:
                route.continue_()

        page.route("**/api/v1/entitlements/*", refuse_save)
        row = page.get_by_role("list", name="Grants", exact=True).get_by_role("button").first
        row.focus()
        row.press("Enter")
        page.get_by_role("button", name="Edit grant", exact=True).click()
        editor = page.get_by_role("region", name="Edit grant", exact=True)
        expect(editor).to_be_visible()
        quota = editor.get_by_role("spinbutton", name="Call quota", exact=True)
        period = editor.get_by_role("combobox", name="Call quota period", exact=True)
        save = editor.get_by_role("button", name="Save grant", exact=True)
        summary = editor.get_by_role("complementary", name="Decision summary", exact=True)
        calls = editor.get_by_role("region", name="Call limits", exact=True)
        expect(period).to_be_disabled()
        assert writes == []

        quota.fill("1.5")
        expect(quota).to_be_focused()
        expect(save).to_be_disabled()
        expect(
            editor.get_by_text("Limits must be positive whole numbers, or left empty.", exact=True)
        ).to_be_visible()
        quota.fill("1000")
        period.select_option("Weekly")
        expect(save).to_be_enabled()
        expect(summary.get_by_text("After: Explicit grant limits", exact=True)).to_be_visible()
        capture._assert_no_document_overflow(page, f"quota {theme}", expected_width=width)
        calls_box, summary_box = calls.bounding_box(), summary.bounding_box()
        assert calls_box is not None and summary_box is not None
        if width == 1440:
            assert summary_box["x"] > calls_box["x"]
        else:
            assert summary_box["y"] >= calls_box["y"] + calls_box["height"]
        page.emulate_media(forced_colors="active")
        capture._assert_no_document_overflow(page, "quota forced colors", expected_width=width)
        page.emulate_media(forced_colors="none")
        assert writes == []

        save.focus()
        save.press("Enter")
        message = editor.get_by_text(
            "Quota draft was not saved. Your values are retained.", exact=False
        )
        expect(message).to_be_visible()
        outcome = editor.locator("[tabindex='-1']").filter(
            has_text="Quota draft was not saved. Your values are retained."
        )
        expect(outcome).to_be_focused()
        expect(quota).to_have_value("1000")
        expect(period).to_have_value("Weekly")
        assert len(writes) == 1
        requests = writes[0]["enforcement"]["requests"]
        assert requests["callQuota"] == 1000
        assert requests["callQuotaPeriod"] == "Weekly"
        assert requests["calls"] > 0 and requests["renewalPeriodSeconds"] > 0

        quota.fill("1200")
        expect(quota).to_be_focused()
        expect(quota).to_have_value("1200")
        cancel = editor.get_by_role("button", name="Cancel", exact=True)
        cancel.focus()
        cancel.press("Enter")
        discard = page.get_by_role("dialog", name="Discard unsaved changes?", exact=True)
        expect(discard).to_be_visible()
        discard.get_by_role("button", name="Stay", exact=True).press("Enter")
        expect(discard).to_be_hidden()
        expect(quota).to_be_focused()
        expect(quota).to_have_value("1200")
        expect(period).to_have_value("Weekly")
        cancel.focus()
        cancel.press("Enter")
        page.get_by_role("button", name="Discard changes", exact=True).click()
        expect(editor).to_be_hidden()
        assert len(writes) == 1
        capture._assert_no_document_overflow(page, "returned grant", expected_width=width)

    with tempfile.TemporaryDirectory(prefix="mosaic-quota-", dir=_SYSTEM_TEMP) as browser_temp:
        for variable in ("TEMP", "TMP", "TMPDIR"):
            monkeypatch.setenv(variable, browser_temp)
        capture.capture(
            [
                capture.Shot(
                    f"quota-native-input-{theme}-{width}",
                    "console",
                    "/entitlements?tab=requests",
                    theme,
                    "Requests",
                    width=width,
                    height=1000,
                    actions=(check_native_input,),
                ),
                capture.Shot(
                    f"quota-authoring-{theme}-{width}",
                    "console",
                    "/entitlements",
                    theme,
                    "Grants",
                    width=width,
                    height=1000,
                    actions=(check,),
                )
            ],
            output=logs / "redacted",
            urls={"console": console_url, "portal": "http://127.0.0.1:1"},
            api_port=api_port,
            logs=logs,
        )
