"""Keyboard and announcement-delivery proof using only the fictional local estate."""

import contextlib
import re
import socket
import sys
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, expect, sync_playwright

from scripts.screenshots import capture

_SYSTEM_TEMP = tempfile.gettempdir()
_PORTS = (18049, 15149, 15249)


@pytest.fixture(scope="module")
def fictional_apps() -> Iterator[tuple[dict[str, str], int]]:
    api_port, console_port, portal_port = _PORTS
    for port in _PORTS:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))
    urls = {
        "console": f"http://127.0.0.1:{console_port}",
        "portal": f"http://127.0.0.1:{portal_port}",
    }
    logs = Path(__file__).parent / "__pycache__" / f"dialog-accessibility-{uuid.uuid4().hex}"
    logs.mkdir(parents=True)
    with contextlib.ExitStack() as servers:
        servers.enter_context(
            capture._running(
                "api",
                [
                    sys.executable,
                    "-m",
                    "scripts.screenshots.demo_api",
                    "--port",
                    str(api_port),
                    "--console-port",
                    str(console_port),
                    "--portal-port",
                    str(portal_port),
                ],
                capture.REPO_ROOT,
                f"http://127.0.0.1:{api_port}/healthz",
                logs,
            )
        )
        for app in ("console", "portal"):
            directory = capture.REPO_ROOT / "apps" / ("web" if app == "console" else "portal")
            port = console_port if app == "console" else portal_port
            servers.enter_context(
                capture._running(
                    app,
                    capture._vite(directory, port),
                    directory,
                    urls[app],
                    logs,
                )
            )
        yield urls, api_port


@pytest.fixture
def proof_page(
    request: pytest.FixtureRequest,
    fictional_apps: tuple[dict[str, str], int],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Page]:
    urls, api_port = fictional_apps
    mode = getattr(request, "param", "fallback")
    with tempfile.TemporaryDirectory(prefix="mosaic-a11y-", dir=_SYSTEM_TEMP) as browser_temp:
        for variable in ("TEMP", "TMP", "TMPDIR"):
            monkeypatch.setenv(variable, browser_temp)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                reduced_motion="reduce",
                locale="en-US",
                timezone_id="UTC",
            )
            outside: list[str] = []

            def local_only(route: Route) -> None:
                address = urlparse(route.request.url)
                if address.scheme in {"http", "https"} and address.hostname not in {
                    "127.0.0.1",
                    "localhost",
                }:
                    outside.append(address.hostname or "unknown host")
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", local_only)
            context.route(
                "**/config.js",
                capture._config_route(capture._config_script(api_port, portal_url=urls["portal"])),
            )
            context.add_init_script(f"""
                window.__mosaic_announcements = [];
                const original = document.ariaNotify;
                if ({str(mode == "native").lower()} && typeof original === 'function') {{
                  document.ariaNotify = function(message, options) {{
                    window.__mosaic_announcements.push(message);
                    return original.call(this, message, options);
                  }};
                }} else {{
                  Object.defineProperty(document, 'ariaNotify', {{
                    configurable: true, value: undefined
                  }});
                }}
                let lastSpan;
                new MutationObserver(() => {{
                  const region = document.querySelector('[aria-live][data-tabster-never-hide]');
                  const span = region?.firstElementChild;
                  if (span && span !== lastSpan && span.textContent.trim()) {{
                    lastSpan = span;
                    window.__mosaic_announcements.push(span.textContent);
                  }}
                }}).observe(document, {{ childList: true, subtree: true, characterData: true }});
            """)
            page = context.new_page()
            page.set_default_timeout(45_000)
            page.set_default_navigation_timeout(60_000)
            try:
                if mode == "native" and page.evaluate("typeof document.ariaNotify") != "function":
                    pytest.skip("This Chromium does not expose the native ariaNotify API.")
                yield page
                assert not outside, (
                    f"The fictional fixture attempted external browser requests: {outside}"
                )
            finally:
                context.close()
                browser.close()


def test_environment_submission_keeps_focus_and_escape(
    proof_page: Page,
    fictional_apps: tuple[dict[str, str], int],
) -> None:
    page = proof_page
    urls, _ = fictional_apps
    applied = False
    held: list[Route] = []
    suggestion = {
        "resourceKind": "gateway",
        "resourceId": "fixture_gateway",
        "resourceName": "Demo gateway",
        "suggestedEnvironment": "production",
        "source": None,
        "evidence": None,
    }
    page.route(
        "**/api/v1/environment-suggestions",
        lambda route: route.fulfill(
            json={"items": [] if applied else [suggestion]},
        ),
    )
    page.route("**/api/v1/environment-assignments", lambda route: held.append(route))
    page.goto(urls["console"] + "/settings?tab=environments")
    opener = page.get_by_role("button", name="Review suggestions", exact=True)
    opener.press("Enter")
    dialog = page.locator('[role="dialog"]').filter(
        has=page.get_by_text("Review environment suggestions", exact=True)
    )
    submit = dialog.locator("button").filter(has_text=re.compile("^Submit"))
    expect(submit).to_be_enabled()
    submit.press("Enter")
    try:
        page.wait_for_function(
            "document.querySelector('[role=dialog] button[disabled]') !== null"
            " || document.querySelector('[role=dialog] button[aria-disabled=true]') !== null"
        )
        expect(submit).to_be_focused()
        submit.press("Enter")
        assert len(held) == 1, "The pending selection was submitted twice."
        applied = True
        held.pop().fulfill(
            json={
                "results": [
                    {
                        "resourceKind": "gateway",
                        "resourceId": "fixture_gateway",
                        "resourceName": "Demo gateway",
                        "environment": "production",
                        "previousEnvironment": None,
                        "status": "applied",
                        "message": None,
                    }
                ],
                "grantsCarried": 0,
                "warnings": [],
            }
        )
        expect(
            dialog.get_by_text("No unclassified resources need review.", exact=True)
        ).to_be_visible()
        page.wait_for_function("""() => {
          const element = document.activeElement;
          return element?.textContent.includes('Demo gateway: applied')
            || element?.textContent === 'Close';
        }""")
        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        expect(opener).to_be_focused()
    finally:
        for route in held:
            route.abort()


def test_connection_returns_to_its_primary_opener(
    proof_page: Page,
    fictional_apps: tuple[dict[str, str], int],
) -> None:
    page = proof_page
    urls, _ = fictional_apps
    page.emulate_media(reduced_motion="no-preference")
    page.goto(urls["console"] + "/entitlements")
    page.get_by_role("list", name="Grants", exact=True).get_by_role("button").first.press("Enter")
    detail = page.get_by_role("region", name="Selected grant", exact=True)
    opener = detail.get_by_role("button", name="Connection info", exact=True)
    expect(opener).to_be_visible()
    opener.press("Enter")
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()


def test_identity_workspace_capture_focus_contract(
    proof_page: Page,
    fictional_apps: tuple[dict[str, str], int],
) -> None:
    page = proof_page
    urls, _ = fictional_apps
    page.set_viewport_size({"width": 390, "height": 900})
    page.goto(urls["console"] + "/identity?tab=users")
    capture._check_identity_workspace(page)


def test_reset_dialog_restores_the_opener_then_its_disabled_fallback(
    proof_page: Page,
    fictional_apps: tuple[dict[str, str], int],
) -> None:
    page = proof_page
    urls, _ = fictional_apps
    page.goto(urls["portal"] + "/notification-preferences")
    timezone = page.get_by_role("textbox", name="Time zone", exact=True)
    saved_zone = timezone.input_value()
    edited_zone = "Pacific/Honolulu" if saved_zone != "Pacific/Honolulu" else "UTC"
    timezone.fill(edited_zone)
    opener = page.get_by_role("button", name="Reset changes", exact=True)
    opener.press("Enter")
    dialog = page.get_by_role("dialog", name="Reset unsaved changes?", exact=True)
    dialog.get_by_role("button", name="Keep editing", exact=True).press("Enter")
    expect(dialog).to_have_count(0)
    expect(opener).to_be_focused()
    expect(timezone).to_have_value(edited_zone)
    opener.press("Enter")
    dialog.get_by_role("button", name="Reset changes", exact=True).press("Enter")
    expect(dialog).to_have_count(0)
    expect(timezone).to_have_value(saved_zone)
    expect(opener).to_be_disabled()
    expect(page.get_by_role("heading", name="Categories & channels", exact=True)).to_be_focused()


def test_environment_failure_focus_and_removed_opener_fallback(
    proof_page: Page,
    fictional_apps: tuple[dict[str, str], int],
) -> None:
    page = proof_page
    urls, _ = fictional_apps
    catalog: dict[str, Any] | None = None
    deleted = False
    held: list[Route] = []

    def fictional_catalog(route: Route) -> None:
        nonlocal catalog
        response = route.fetch()
        assert response.ok, "The fictional environment catalog did not load."
        catalog = response.json()
        if not deleted:
            catalog["environments"].append(
                {
                    "key": "focus-proof",
                    "displayName": "Focus proof",
                    "description": None,
                    "color": "brand",
                    "production": False,
                    "aliases": [],
                    "acceptsEndpointsFrom": [],
                    "order": 90,
                    "builtIn": False,
                    "usage": {"gateways": 0, "modelEndpoints": 0, "mcpEndpoints": 0},
                }
            )
        route.fulfill(response=response, json=catalog)

    page.route("**/api/v1/environment-catalog", fictional_catalog)
    page.route(
        "**/api/v1/environment-catalog/environments/focus-proof",
        lambda route: held.append(route),
    )
    page.emulate_media(reduced_motion="no-preference")
    page.goto(urls["console"] + "/settings?tab=environments")
    row = (
        page.get_by_role("table", name="Environments", exact=True)
        .get_by_role("row")
        .filter(
            has=page.get_by_text("Focus proof", exact=True),
        )
    )
    opener = row.get_by_role("button", name="Delete", exact=True)
    fallback = page.get_by_role("heading", name="Environments", exact=True)
    opener.press("Enter")
    dialog = page.get_by_role("dialog", name="Delete environment", exact=True)
    submit = dialog.locator("button").filter(has_text=re.compile("^Delet"))
    submit.press("Enter")
    try:
        expect(submit).to_have_attribute("aria-disabled", "true")
        expect(submit).to_be_focused()
        submit.press("Enter")
        assert len(held) == 1, "The pending deletion was submitted twice."
        message = "The fictional environment was not deleted. Retry when the API is available."
        held.pop().fulfill(status=503, json={"code": "unavailable", "message": message})
        expect(dialog.get_by_text(message, exact=True)).to_be_visible()
        expect(dialog.locator("[tabindex='-1']").filter(has_text=message)).to_be_focused()
        submit.press("Enter")
        expect(submit).to_have_attribute("aria-disabled", "true")
        assert len(held) == 1
        assert catalog is not None
        deleted = True
        updated = {
            **catalog,
            "environments": [
                item for item in catalog["environments"] if item["key"] != "focus-proof"
            ],
        }
        held.pop().fulfill(json=updated)
        expect(dialog).to_be_hidden()
        expect(opener).to_have_count(0)
        expect(fallback).to_be_focused()
    finally:
        for route in held:
            route.abort()


@pytest.mark.parametrize("kind", ["rotate", "delete"])
def test_key_confirmation_keeps_pending_focus_and_returns_to_opener(
    proof_page: Page,
    fictional_apps: tuple[dict[str, str], int],
    kind: str,
) -> None:
    page = proof_page
    urls, _ = fictional_apps
    held: list[Route] = []
    request_method = "POST" if kind == "rotate" else "DELETE"
    route_pattern = "**/keys/rotate" if kind == "rotate" else "**/keys"

    def hold_write(route: Route) -> None:
        if route.request.method == request_method:
            held.append(route)
        else:
            route.continue_()

    page.route(route_pattern, hold_write)
    page.goto(urls["portal"] + "/access")
    capture.open_grant_connection("gpt-4o", "Customer Insights")(page)
    drawer = page.get_by_role("dialog")
    label = "Rotate primary key" if kind == "rotate" else "Delete key"
    opener = drawer.get_by_role("button", name=label, exact=True)
    opener.press("Enter")
    confirmation = drawer.get_by_role(
        "group",
        name="Confirm rotate primary key" if kind == "rotate" else "Confirm delete key",
        exact=True,
    )
    submit = confirmation.get_by_role("button", name=label, exact=True)
    submit.press("Enter")
    try:
        expect(submit).to_have_attribute("aria-disabled", "true")
        expect(submit).to_be_focused()
        submit.press("Enter")
        assert len(held) == 1, "The pending key action was submitted twice."
        held.pop().fulfill(
            json={
                "entitlementId": "fictional-grant",
                "subscriptionName": "fictional-subscription",
                "exists": kind == "rotate",
                "costCenter": None,
                "rotated": "primary" if kind == "rotate" else None,
            }
        )
        expect(confirmation).to_have_count(0)
        expect(opener).to_be_focused()
    finally:
        for route in held:
            route.abort()


@pytest.mark.parametrize("proof_page", ["native", "fallback"], indirect=True)
def test_repeated_clipboard_failure_delivery_and_drawer_teardown(
    proof_page: Page,
    fictional_apps: tuple[dict[str, str], int],
) -> None:
    page = proof_page
    urls, _ = fictional_apps
    page.add_init_script(
        "Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined });"
    )
    page.goto(urls["portal"] + "/access")
    capture.open_grant_connection("gpt-4o", "Customer Insights")(page)
    drawer = page.get_by_role("dialog")
    show = drawer.get_by_role("button", name="Show primary key", exact=True)
    show.press("Enter")
    secret = drawer.locator("[data-secret]").inner_text()
    copy = drawer.get_by_role("button", name="Copy key", exact=True)
    copy.press("Enter")
    page.wait_for_function(
        "window.__mosaic_announcements"
        ".filter(message => message.includes('Could not copy the key')).length >= 1"
    )
    assert (
        page.evaluate(
            "window.__mosaic_announcements"
            ".filter(message => message.includes('Could not copy the key')).length"
        )
        == 1
    )
    expect(copy).to_be_focused()
    copy.press("Enter")
    page.wait_for_function(
        "window.__mosaic_announcements"
        ".filter(message => message.includes('Could not copy the key')).length >= 2"
    )
    assert (
        page.evaluate(
            "window.__mosaic_announcements"
            ".filter(message => message.includes('Could not copy the key')).length"
        )
        == 2
    )
    expect(copy).to_be_focused()
    assert secret not in page.evaluate("window.__mosaic_announcements.join(' ')")
    page.keyboard.press("Escape")
    expect(drawer).to_be_hidden()
    expect(page.locator("[data-secret]")).to_have_count(0)
    capture.open_grant_connection("gpt-4o", "Customer Insights")(page)
    expect(drawer.get_by_role("button", name="Show primary key", exact=True)).to_be_enabled()
    expect(drawer.locator("[data-secret]")).to_have_count(0)
    expect(drawer.get_by_text(re.compile("Could not copy the key"))).to_have_count(0)


def test_credential_eligibility_tears_down_failures_and_late_results(
    proof_page: Page,
    fictional_apps: tuple[dict[str, str], int],
) -> None:
    page = proof_page
    urls, _ = fictional_apps
    keys_allowed = True
    connection_pattern = "**/api/v1/me/entitlements/*/connection"

    def connection_eligibility(route: Route) -> None:
        response = route.fetch()
        assert response.ok, "The fictional direct-grant connection did not load."
        info = response.json()
        info["keysAllowedByCostCenter"] = keys_allowed
        route.fulfill(response=response, json=info)

    page.route(connection_pattern, connection_eligibility)
    page.add_init_script("""
        Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined });
        const originalFetch = window.fetch;
        window.fetch = async function(...args) {
          const response = await originalFetch.apply(this, args);
          const url = typeof args[0] === 'string' ? args[0] : args[0].url;
          if (window.__holdFixtureReveal && url.endsWith('/keys/reveal')) {
            return new Promise(resolve => {
              window.__releaseFixtureReveal = () => resolve(response);
            });
          }
          return response;
        };
    """)
    page.clock.install()
    page.goto(urls["portal"] + "/access")
    capture.open_grant_connection("gpt-4o", "Customer Insights")(page)
    drawer = page.get_by_role("dialog")
    show = drawer.get_by_role("button", name="Show primary key", exact=True)
    copy = drawer.get_by_role("button", name="Copy key", exact=True)
    reason = drawer.get_by_text(
        "Subscription keys are turned off for this cost center. "
        "Use a Microsoft Entra ID token instead.",
        exact=True,
    )
    heading = drawer.get_by_role("heading", name="Subscription key", exact=True)

    def refetch(allowed: bool) -> None:
        nonlocal keys_allowed
        keys_allowed = allowed
        page.clock.fast_forward(16_000)
        with page.expect_response(re.compile(r"/api/v1/me/entitlements/[^/]+/connection$")):
            page.evaluate("window.dispatchEvent(new Event('visibilitychange'))")
        if allowed:
            expect(show).to_be_enabled()
        else:
            expect(reason).to_be_visible()
        expect(drawer.locator("[data-secret]")).to_have_count(0)
        expect(drawer.get_by_text(re.compile("Could not copy the key"))).to_have_count(0)

    def settle_delivery() -> None:
        page.evaluate("""async () => {
          await Promise.resolve();
          await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        }""")

    show.press("Enter")
    secret = drawer.locator("[data-secret]").inner_text()
    copy.press("Enter")
    expect(drawer.get_by_text(re.compile("Could not copy the key"))).to_be_visible()
    expect(copy).to_be_focused()
    page.wait_for_function(
        "window.__mosaic_announcements"
        ".filter(message => message.includes('Could not copy the key')).length >= 1"
    )
    page.clock.fast_forward(16_000)
    with page.expect_response(re.compile(r"/api/v1/me/entitlements/[^/]+/connection$")):
        page.evaluate("window.dispatchEvent(new Event('visibilitychange'))")
    settle_delivery()
    expect(copy).to_be_focused()
    expect(drawer.locator("[data-secret]")).to_have_text(secret)
    assert (
        page.evaluate(
            "window.__mosaic_announcements"
            ".filter(message => message.includes('Could not copy the key')).length"
        )
        == 1
    )
    refetch(False)
    expect(reason).to_be_focused()
    refetch(True)
    expect(heading).to_be_focused()

    show.press("Enter")
    expect(drawer.locator("[data-secret]")).to_be_visible()
    page.evaluate("""
        Object.defineProperty(navigator, 'clipboard', {
          configurable: true,
          value: {
            writeText: () => new Promise(resolve => { window.__resolveFixtureCopy = resolve; }),
          },
        });
    """)
    copy.press("Enter")
    page.wait_for_function("typeof window.__resolveFixtureCopy === 'function'")
    refetch(False)
    expect(reason).to_be_focused()
    refetch(True)
    page.evaluate("window.__resolveFixtureCopy()")
    settle_delivery()
    expect(drawer.locator("[data-secret]")).to_have_count(0)
    expect(drawer.get_by_text(re.compile("^Key copied\\."))).to_have_count(0)

    shown_before = page.evaluate(
        "window.__mosaic_announcements"
        ".filter(message => message.includes('Primary key shown')).length"
    )
    page.evaluate("window.__holdFixtureReveal = true")
    show.press("Enter")
    page.wait_for_function("typeof window.__releaseFixtureReveal === 'function'")
    expect(show).to_be_focused()
    refetch(False)
    refetch(True)
    page.evaluate("window.__releaseFixtureReveal()")
    settle_delivery()
    expect(drawer.locator("[data-secret]")).to_have_count(0)
    assert (
        page.evaluate(
            "window.__mosaic_announcements"
            ".filter(message => message.includes('Primary key shown')).length"
        )
        == shown_before
    )
    assert secret not in page.evaluate("window.__mosaic_announcements.join(' ')")
