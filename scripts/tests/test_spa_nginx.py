"""Checks the nginx configuration that serves the console (apps/web) and the portal (apps/portal).

Each image copies apps/<app>/nginx.conf to /etc/nginx/conf.d/default.conf, inside nginx's http
block.
"""

from __future__ import annotations

import re
import unittest
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APPS = ("web", "portal")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
}
IMMUTABLE = "public, max-age=31536000, immutable"
HASHED_OUTPUT_OPTIONS = (
    "outDir",
    "assetsDir",
    "entryFileNames",
    "chunkFileNames",
    "assetFileNames",
)
TOKEN = re.compile(
    r"""(?P<space>\s+)|(?P<comment>\#[^\n]*)|(?P<punct>[{};])"""
    r"""|"(?P<double>(?:\\.|[^"\\])*)"|'(?P<single>(?:\\.|[^'\\])*)'|(?P<word>[^\s{};"']+)"""
)


@dataclass
class Directive:
    name: str
    args: list[str]
    block: list[Directive] | None = None

    def children(self, name: str, *args: str) -> list[Directive]:
        """Direct children with this name whose arguments start with args."""
        return [
            child
            for child in self.block or []
            if child.name == name and child.args[: len(args)] == list(args)
        ]


def parse(text: str) -> Directive:
    """Parse nginx configuration into nested directives. Quoted arguments keep their raw text."""
    top: list[Directive] = []
    stack = [top]
    words: list[str] = []
    position = 0
    while position < len(text):
        token = TOKEN.match(text, position)
        if token is None:
            raise ValueError(f"Unparseable nginx configuration at offset {position}")
        position = token.end()
        kind = token.lastgroup or ""
        if kind in ("space", "comment"):
            continue
        if kind != "punct":
            words.append(token.group(kind))
            continue
        if token.group(kind) == "}":
            stack.pop()
            continue
        block: list[Directive] = []
        opens = token.group(kind) == "{"
        stack[-1].append(Directive(words[0], words[1:], block if opens else None))
        if opens:
            stack.append(block)
        words = []
    return Directive("", [], top)


def walk(directive: Directive) -> Iterator[Directive]:
    for child in directive.block or []:
        yield child
        yield from walk(child)


def map_value(map_block: Directive, value: str) -> str:
    """Resolve a map as nginx does: an exact string, then regexes in order, then the default."""
    entries = {entry.name: entry.args[0] for entry in map_block.block or []}
    if value in entries:
        return entries[value]
    for key, result in entries.items():
        if key.startswith("~*") and re.search(key[2:], value, re.IGNORECASE):
            return result
        if key.startswith("~") and not key.startswith("~*") and re.search(key[1:], value):
            return result
    return entries.get("default", "")


def load(app: str) -> Directive:
    return parse(ROOT.joinpath("apps", app, "nginx.conf").read_text())


class SpaNginxConfigTests(unittest.TestCase):
    def one(self, directives: list[Directive], description: str) -> Directive:
        self.assertEqual(len(directives), 1, f"Expected exactly one {description}")
        return directives[0]

    def test_console_and_portal_serve_the_same_config(self) -> None:
        web, portal = (ROOT.joinpath("apps", app, "nginx.conf").read_text() for app in APPS)
        self.assertEqual(web, portal)
        for app in APPS:
            with self.subTest(app=app):
                dockerfile = ROOT.joinpath("apps", app, "Dockerfile").read_text().splitlines()
                # conf.d files are included inside the http block, where map is allowed.
                copy = f"COPY apps/{app}/nginx.conf /etc/nginx/conf.d/default.conf"
                self.assertIn(copy, dockerfile)

    def test_security_headers_are_added_once_at_server_level(self) -> None:
        # An add_header inside a location stops every server-level add_header applying there.
        for app in APPS:
            with self.subTest(app=app):
                config = load(app)
                server = self.one(config.children("server"), "server")
                added = server.children("add_header")
                self.assertEqual([d for d in walk(config) if d.name == "add_header"], added)
                headers = {header.args[0]: header.args[1:] for header in added}
                self.assertEqual(len(headers), len(added), "A header is added more than once")
                for name, value in SECURITY_HEADERS.items():
                    self.assertEqual(headers.get(name), [value, "always"], name)

    def test_only_content_hashed_assets_are_cached(self) -> None:
        for app in APPS:
            with self.subTest(app=app):
                config = load(app)
                server = self.one(config.children("server"), "server")
                header = self.one(
                    server.children("add_header", "Cache-Control"), "Cache-Control add_header"
                )
                # Not "always": a 404 under /assets/ must not carry the year-long lifetime.
                self.assertNotIn("always", header.args)
                variable = header.args[1]
                policy = self.one(config.children("map", "$uri", variable), f"map $uri {variable}")
                # $uri is read after try_files, so every SPA route resolves to /index.html.
                fallback = self.one(server.children("location", "/"), "location /")
                try_files = self.one(fallback.children("try_files"), "try_files for /")
                self.assertEqual(try_files.args[-1], "/index.html")
                expected = {
                    "/index.html": "no-cache",
                    "/favicon.svg": "no-cache",
                    "/config.js": "no-store",
                    "/assets/index-CnAY3vWS.js": IMMUTABLE,
                    "/assets/index-BSboVMAS.css": IMMUTABLE,
                }
                for uri, value in expected.items():
                    self.assertEqual(map_value(policy, uri), value, uri)
                # Vite's default output names put a content hash in every file under assets/, but
                # it copies public/ into the build without hashing file names.
                vite_config = ROOT.joinpath("apps", app, "vite.config.ts").read_text()
                for option in HASHED_OUTPUT_OPTIONS:
                    self.assertNotIn(option, vite_config)
                self.assertFalse(ROOT.joinpath("apps", app, "public", "assets").exists())

    def test_a_missing_asset_is_a_plain_404(self) -> None:
        for app in APPS:
            with self.subTest(app=app):
                server = self.one(load(app).children("server"), "server")
                assets = self.one(
                    server.children("location", "^~", "/assets/"), "location ^~ /assets/"
                )
                try_files = self.one(assets.children("try_files"), "try_files for /assets/")
                self.assertEqual(try_files.args[:-1], ["$uri"])
                name = try_files.args[-1]
                missing = self.one(server.children("location", name), f"location {name}")
                self.assertEqual(self.one(missing.children("return"), "return").args[0], "404")
                # An empty types block makes nginx use default_type instead of the .js or .css type.
                self.assertEqual(self.one(missing.children("types"), "types").block, [])
                self.assertEqual(
                    self.one(missing.children("default_type"), "default_type").args, ["text/plain"]
                )


if __name__ == "__main__":
    unittest.main()
