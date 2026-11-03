"""Real-layout regressions using the capture tool's fictional API and redacted captures."""

import contextlib
import re
import socket
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, expect

from scripts.screenshots import capture


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.fixture(scope="module")
def fictional_console() -> Iterator[tuple[str, int, Path]]:
    api_port, console_port, portal_port = (_free_port() for _ in range(3))
    console_url = f"http://127.0.0.1:{console_port}"
    logs = Path(__file__).parent / "__pycache__" / f"cost-center-offerings-browser-{time.time_ns()}"
    logs.mkdir(parents=True)
    with pytest.MonkeyPatch.context() as environment, contextlib.ExitStack() as servers:
        environment.setenv("TEMP", str(logs.resolve()))
        environment.setenv("TMP", str(logs.resolve()))
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
        servers.enter_context(
            capture._running(
                "console",
                capture._vite(capture.REPO_ROOT / "apps" / "web", console_port),
                capture.REPO_ROOT / "apps" / "web",
                console_url,
                logs,
            )
        )
        yield console_url, api_port, logs


def _offerings(page: Page) -> None:
    offerings = page.get_by_role("list", name="Exact resource offerings", exact=True)
    expect(offerings).to_be_visible()
    expect(page.get_by_text("Loading exact resource offerings", exact=True)).to_be_hidden()
    assert offerings.get_by_role("button").count() >= 17
    pane = page.get_by_role("region", name="Exact resource offerings", exact=True)
    dimensions = pane.evaluate(
        """element => ({
          height: element.clientHeight,
          content: element.scrollHeight,
          overflow: getComputedStyle(element).overflowY,
          documentHeight: document.documentElement.scrollHeight,
        })"""
    )
    assert dimensions["overflow"] == "auto"
    assert 0 < dimensions["height"] <= min(560, page.viewport_size["height"] * 0.6) + 1
    assert dimensions["content"] > dimensions["height"]
    last = offerings.get_by_role("button").last
    last.focus()
    assert pane.evaluate("element => element.scrollTop") > 0
    print(f"bounded offerings at {page.viewport_size}: {dimensions}")


def _nested_read_only(page: Page) -> None:
    centers = page.get_by_role("list", name="Cost centers", exact=True)
    centers.get_by_role("button", name=re.compile(r"^Customer Insights")).click()
    selected_center = parse_qs(urlparse(page.url).query)["selected"]
    section = page.get_by_role("navigation", name="Cost center sections", exact=True)
    button = section.get_by_role("button", name="Resource access", exact=True)
    compact = page.get_by_label("Cost center section", exact=True)
    button.or_(compact).filter(visible=True).first.wait_for()
    if button.is_visible():
        button.click()
    else:
        compact.select_option("access")
    _offerings(page)
    offerings = page.get_by_role("list", name="Exact resource offerings", exact=True)
    row = offerings.get_by_role("button", name=re.compile(r"^Text embeddings \(large\)"))
    row.focus()
    row.press("Enter")
    detail = page.get_by_role("region", name="Selected resource access", exact=True)
    expect(detail).to_be_focused()
    mode = detail.get_by_role("combobox", name=re.compile(r"^Access mode for"))
    expect(mode).to_have_value("automatic")
    expect(mode).to_be_disabled()
    expect(offerings).to_be_hidden()
    detail.get_by_role("button", name="Back to list", exact=True).click()
    expect(offerings).to_be_visible()
    expect(row).to_be_focused()
    assert parse_qs(urlparse(page.url).query)["selected"] == selected_center
    pool = offerings.get_by_role("button", name=re.compile(r"Pool model")).first
    pool.press("Enter")
    expect(detail.get_by_role("combobox")).to_have_value("requestOnly")
    expect(detail.get_by_role("combobox")).to_be_disabled()
    expect(detail.locator("dt", has_text=re.compile(r"^Pool scope ID$"))).to_be_visible()
    page.get_by_role("heading", name="Exact offerings", exact=True).scroll_into_view_if_needed()


def _editor(page: Page, *, existing: bool) -> None:
    if existing:
        page.get_by_role("list", name="Cost centers", exact=True).get_by_role(
            "button", name=re.compile(r"^Knowledge Engineering")
        ).click()
        page.get_by_role("button", name="Finish group setup", exact=True).click()
        expect(page.get_by_role("heading", name="Edit Knowledge Engineering")).to_be_visible()
    else:
        page.get_by_role("button", name="New cost center", exact=True).click()
        expect(page.get_by_role("heading", name="New cost center", exact=True)).to_be_visible()
    _offerings(page)
    offerings = page.get_by_role("list", name="Exact resource offerings", exact=True)
    field = page.get_by_label("Filter resource offerings", exact=True)
    field.fill("Text embeddings (large)")
    expect(offerings.get_by_role("button")).to_have_count(1)
    field.fill("")
    offerings.get_by_role("button").last.press("Enter")
    detail = page.get_by_role("region", name="Selected resource access", exact=True)
    expect(detail).to_be_focused()
    expect(detail.get_by_role("combobox", name=re.compile(r"^Access mode for"))).to_be_enabled()
    back = detail.get_by_role("button", name="Back to list", exact=True)
    if back.is_visible():
        expect(offerings).to_be_hidden()
        back.click()
        expect(offerings).to_be_visible()
    page.get_by_role("heading", name="Exact offerings", exact=True).scroll_into_view_if_needed()


@pytest.mark.parametrize("width,height", [(1440, 1000), (390, 844)])
def test_cost_center_offerings_layout(
    fictional_console: tuple[str, int, Path], width: int, height: int
) -> None:
    console_url, api_port, logs = fictional_console
    capture.capture(
        [
            capture.Shot(
                f"cost-center-{name}-{width}",
                "console",
                "/cost-centers",
                "light",
                "Cost centers",
                width=width,
                height=height,
                actions=(action,),
            )
            for name, action in [
                ("read-only", _nested_read_only),
                ("new", lambda page: _editor(page, existing=False)),
                ("setup", lambda page: _editor(page, existing=True)),
            ]
        ],
        output=logs / "redacted",
        urls={"console": console_url},
        api_port=api_port,
        logs=logs,
    )
