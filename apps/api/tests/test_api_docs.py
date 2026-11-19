import hashlib
import json
import re
from html.parser import HTMLParser
from importlib.resources import files
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from mosaic_api.config import Settings
from mosaic_api.main import create_app


class AssetReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "link", "redoc"}:
            for name, value in attrs:
                if name in {"src", "href", "spec-url"} and value:
                    self.urls.append(value)


@pytest.mark.parametrize("root_path", ["", "/mosaic"])
def test_documentation_uses_served_local_assets(settings: Settings, root_path: str) -> None:
    with TestClient(create_app(settings), root_path=root_path) as client:
        for route in ("/docs", "/redoc"):
            response = client.get(f"{root_path}{route}")
            assert response.status_code == 200
            assert "script-src 'self'" in response.headers["content-security-policy"]
            parser = AssetReferences()
            parser.feed(response.text)
            assert parser.urls
            for url in parser.urls:
                assert not urlsplit(url).netloc, url
                assert url.startswith(f"{root_path}/"), url
                asset = client.get(url)
                assert asset.status_code == 200, url
                if url.endswith(".js"):
                    assert "javascript" in asset.headers["content-type"]
                elif url.endswith(".css"):
                    assert asset.headers["content-type"].startswith("text/css")
                elif url.endswith(".svg"):
                    assert asset.headers["content-type"].startswith("image/svg+xml")
            assert "fonts.googleapis.com" not in response.text

        swagger = client.get(f"{root_path}/docs").text
        assert f"url: '{root_path}/openapi.json'" in swagger
        assert f"window.location.origin + '{root_path}/docs/oauth2-redirect'" in swagger
        assert re.search(r'"validatorUrl":\s*null', swagger)
        redirect = client.get(f"{root_path}/docs/oauth2-redirect")
        assert redirect.status_code == 200
        assert "swaggerUIRedirectOauth2" in redirect.text
        parser = AssetReferences()
        parser.feed(redirect.text)
        assert not parser.urls


def test_documentation_assets_fail_closed_and_stay_out_of_openapi(client: TestClient) -> None:
    for path in ("/docs-assets/missing.js", "/docs-assets/../main.py"):
        response = client.get(path)
        assert response.status_code == 404
        assert not response.headers["content-type"].startswith("text/html")
    paths = client.get("/openapi.json").json()["paths"]
    assert not any(path.startswith(("/docs", "/redoc")) for path in paths)


def test_vendored_assets_match_provenance_manifest() -> None:
    static = files("mosaic_api").joinpath("static")
    manifest = json.loads(static.joinpath("vendor-manifest.json").read_text())
    for name, digest in manifest["files"].items():
        assert hashlib.sha256(static.joinpath(name).read_bytes()).hexdigest() == digest, name
    redoc = static.joinpath("redoc-2.5.4/redoc.standalone.js").read_text(encoding="utf-8")
    assert "https://cdn.redoc.ly/redoc/logo-mini.svg" not in redoc
    assert "./docs-assets/redoc-2.5.4/logo-mini.svg" in redoc
