"""Production bundles and API documentation must work without third-party asset servers."""

import contextlib
import json
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Route, expect, sync_playwright

from scripts.screenshots import capture

TELEMETRY_ORIGIN = "https://telemetry.example.test"
API_ORIGIN = "https://api.example.test"


@pytest.fixture(scope="module")
def fictional_apps() -> Iterator[dict[str, str]]:
    api_port, console_port, portal_port = 18069, 15169, 15269
    urls = {
        "api": f"http://127.0.0.1:{api_port}",
        "console": f"http://127.0.0.1:{console_port}",
        "portal": f"http://127.0.0.1:{portal_port}",
    }
    with tempfile.TemporaryDirectory(prefix="mosaic-local-assets-") as directory:
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
                    f"{urls['api']}/healthz",
                    Path(directory),
                )
            )
            for name, app, port in (
                ("console", "web", console_port),
                ("portal", "portal", portal_port),
            ):
                app_dir = capture.REPO_ROOT / "apps" / app
                servers.enter_context(
                    capture._running(
                        name,
                        capture._vite(app_dir, port, preview=True),
                        app_dir,
                        urls[name],
                        Path(directory),
                    )
                )
            yield urls


@pytest.mark.parametrize(
    ("target", "path", "ready"),
    [
        ("console", "/dashboard", "main h1"),
        ("portal", "/catalog", "main h1"),
        ("api", "/docs", ".swagger-ui .info .title"),
        ("api", "/redoc", ".redoc-wrap h1"),
    ],
)
def test_shipped_pages_use_only_local_assets(
    fictional_apps: dict[str, str], target: str, path: str, ready: str
) -> None:
    urls = fictional_apps
    local_origins = set(urls.values())
    outside: list[str] = []
    errors: list[str] = []
    local_assets: list[str] = []
    telemetry_uploads: list[str] = []

    def network(route: Route) -> None:
        request = route.request
        address = urlsplit(request.url)
        origin = f"{address.scheme}://{address.netloc}"
        if origin in local_origins:
            if request.resource_type in {"script", "stylesheet", "font", "image", "worker"}:
                local_assets.append(request.url)
            route.continue_()
        elif (
            origin == TELEMETRY_ORIGIN
            and address.path in {"/v2/track", "/v2.1/track"}
            and request.method in {"POST", "OPTIONS"}
        ):
            if request.method == "POST":
                telemetry_uploads.append(request.url)
            route.fulfill(
                status=200,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Headers": "*",
                    "Access-Control-Allow-Methods": "POST, OPTIONS",
                },
                json={"itemsReceived": 1, "itemsAccepted": 1, "errors": []},
            )
        elif origin == API_ORIGIN and address.path == "/probe":
            route.fulfill(
                headers={"Access-Control-Allow-Origin": "*"},
                json={"externalApi": "available"},
            )
        else:
            outside.append(request.url)
            route.abort()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(service_workers="block")
        try:
            context.route("**/*", network)
            config = json.loads(
                capture._config_script(
                    int(urlsplit(urls["api"]).port), portal_url=urls["portal"]
                ).removeprefix("window.__MOSAIC_CONFIG__ = ").removesuffix(";")
            )
            config["applicationInsightsConnectionString"] = (
                "InstrumentationKey=00000000-0000-0000-0000-000000000001;"
                f"IngestionEndpoint={TELEMETRY_ORIGIN}/"
            )
            context.route(
                "**/config.js",
                capture._config_route(f"window.__MOSAIC_CONFIG__ = {json.dumps(config)};"),
            )
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("localStorage.setItem('mosaic-theme', 'dark')")
            page.set_default_timeout(90_000)
            if target != "api":
                with page.expect_response(
                    lambda response: response.url.startswith(TELEMETRY_ORIGIN)
                    and response.request.method == "POST",
                    timeout=45_000,
                ):
                    page.goto(f"{urls[target]}{path}")
                    expect(page.locator(ready).first).to_be_visible()
                assert telemetry_uploads, "Local bundling must not disable telemetry uploads"
                assert page.locator("html").get_attribute("data-theme") == "dark"
            else:
                page.goto(f"{urls[target]}{path}")
                expect(page.locator(ready).first).to_contain_text("MOSAIC API", timeout=90_000)
                if path == "/redoc":
                    assert page.evaluate("""
                        async () => {
                            const fonts = await Promise.all(['Roboto', 'Montserrat'].map(
                                name => document.fonts.load(`400 16px ${name}`)
                            ));
                            return fonts.every(faces => faces.length > 0 &&
                                faces.every(face => face.status === 'loaded'));
                        }
                    """)
                    expect(page.get_by_alt_text("redocly logo")).to_be_visible(timeout=30_000)
                    assert page.get_by_alt_text("redocly logo").evaluate(
                        "(img) => img.complete && img.naturalWidth > 0"
                    )
                    # The search index runs in a locally bundled blob worker.
                    search = page.get_by_placeholder("Search...")
                    search.fill("healthz")
                    expect(
                        page.locator('[data-role="search:results"]').get_by_role("menuitem").first
                    ).to_be_visible(timeout=30_000)

            assert local_assets
            assert not outside, f"Remote runtime assets/configuration were requested: {outside}"
            assert not errors, errors
            assert page.evaluate(
                f"fetch('{API_ORIGIN}/probe').then(response => response.json())"
            ) == {"externalApi": "available"}

            page.evaluate("""
                () => {
                    window.assetViolations = [];
                    document.addEventListener('securitypolicyviolation', event => {
                        if (event.blockedURI.startsWith('https://assets.example.test/')) {
                            window.assetViolations.push(event.effectiveDirective);
                        }
                    });
                    const script = document.createElement('script');
                    script.src = 'https://assets.example.test/remote.js';
                    document.head.append(script);
                    const style = document.createElement('link');
                    style.rel = 'stylesheet';
                    style.href = 'https://assets.example.test/remote.css';
                    document.head.append(style);
                    new FontFace('remote', 'url(https://assets.example.test/font.woff2)')
                        .load().catch(() => {});
                    const image = new Image();
                    image.src = 'https://assets.example.test/image.svg';
                }
            """)
            page.wait_for_function("""
                ['script-src-elem', 'style-src-elem', 'font-src', 'img-src']
                    .every(directive => window.assetViolations.includes(directive))
            """)
            assert not outside, "The browser must block remote assets before sending requests"
        finally:
            context.close()
            browser.close()
