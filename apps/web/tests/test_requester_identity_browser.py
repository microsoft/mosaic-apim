"""Requester provenance and long-value reflow against the fictional capture estate."""

import re
import tempfile
from pathlib import Path

import pytest
from playwright.sync_api import Page, Route, expect
from test_cost_center_offerings_browser import fictional_console as fictional_console

from scripts.screenshots import capture
from scripts.screenshots.demo_api import JORDAN

_SYSTEM_TEMP = tempfile.gettempdir()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1440, 320])
def test_requester_snapshot_reflows_and_remains_inspectable(
    fictional_console: tuple[str, int, Path],
    monkeypatch: pytest.MonkeyPatch,
    theme: capture.Theme,
    width: int,
) -> None:
    console_url, api_port, logs = fictional_console
    name, username = "N" * 200, "U" * 200
    monkeypatch.setattr(
        capture, "SENSITIVE_LITERALS", [*capture.SENSITIVE_LITERALS, name, username]
    )

    def check(page: Page) -> None:
        def long_snapshot(route: Route) -> None:
            response = route.fetch()
            requests = response.json()
            for request in requests:
                if request["requesterObjectId"] == JORDAN.object_id:
                    request["requesterName"] = name
                    request["requesterUsername"] = username
            route.fulfill(response=response, json=requests)

        page.route("**/api/v1/access-requests?*", long_snapshot)
        page.reload()
        capture._settle(page)
        row = page.get_by_role("list", name="Requests", exact=True).get_by_role(
            "button", name=re.compile(rf"^{name}")
        )
        row.focus()
        row.press("Enter")
        detail = page.get_by_role("region", name="Selected access request", exact=True)
        expect(detail).to_be_visible()
        snapshot = detail.get_by_role("region", name="Sign-in identity at request time")
        expect(snapshot.get_by_text(name, exact=True)).to_be_visible()
        expect(snapshot.get_by_text(username, exact=True)).to_be_visible()
        expect(snapshot.get_by_text(JORDAN.object_id, exact=True)).to_be_visible()
        capture._assert_no_document_overflow(
            page, f"requester detail {theme}", expected_width=width
        )

        approve = detail.get_by_role("button", name="Approve", exact=True)
        expect(approve).to_be_enabled()
        approve.focus()
        approve.press("Enter")
        editor = page.get_by_role("region", name="Approve access request", exact=True)
        expect(editor).to_be_visible()
        snapshot = editor.get_by_role("region", name="Sign-in identity at request time")
        expect(snapshot.get_by_text(name, exact=True)).to_be_visible()
        expect(snapshot.get_by_text(username, exact=True)).to_be_visible()
        expect(editor.get_by_text(JORDAN.object_id, exact=True)).to_have_count(1)
        expect(editor.get_by_role("button", name="Approve and create grant")).to_be_enabled()
        expect(editor.get_by_role("textbox", name=re.compile("note", re.I))).to_be_editable()
        capture._assert_no_document_overflow(
            page, f"requester approval {theme}", expected_width=width
        )
        page.emulate_media(forced_colors="active")
        capture._assert_no_document_overflow(page, "requester forced colors", expected_width=width)
        page.emulate_media(forced_colors="none")

    # Chromium's nested profile paths exceed Windows' limit under a long worktree cache path.
    with tempfile.TemporaryDirectory(prefix="mosaic-requester-", dir=_SYSTEM_TEMP) as browser_temp:
        for variable in ("TEMP", "TMP", "TMPDIR"):
            monkeypatch.setenv(variable, browser_temp)
        capture.capture(
            [
                capture.Shot(
                    f"requester-identity-{theme}-{width}",
                    "console",
                    "/entitlements?tab=requests",
                    theme,
                    "Requests",
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
