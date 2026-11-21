"""Checks the appearance files the console (apps/web) and the portal (apps/portal) share.

The two apps are separate packages, so each carries its own copy of the palette arithmetic, the
theme provider, the shell stylesheet, the colour editors, and the fonts. A change made to one copy
and not the other would give the two apps different colours for the same saved palette.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHARED = (
    "src/appearance.ts",
    "src/appearance.test.ts",
    "src/prism.css",
    "src/theme.tsx",
    "src/theme-context.ts",
    "src/test/node-fs.d.ts",
    "src/components/AppearanceControl.tsx",
    "src/components/appearance/CustomThemeEditor.tsx",
    "src/components/appearance/OrganizationAppearanceLoader.tsx",
    "src/components/appearance/PaletteEditor.tsx",
    "src/components/appearance/appearance.module.css",
    "src/components/appearance/appearance.test.tsx",
    "src/fonts/fonts.css",
)
FONT_FOLDERS = ("src/fonts/outfit-5.3.0", "src/fonts/jetbrains-mono-5.3.0")


def read(app: str, relative: str) -> bytes:
    return ROOT.joinpath("apps", app, relative).read_bytes()


class SharedAppearanceTests(unittest.TestCase):
    def test_console_and_portal_carry_the_same_appearance_files(self) -> None:
        for relative in SHARED:
            with self.subTest(file=relative):
                self.assertEqual(read("web", relative), read("portal", relative))

    def test_each_app_ships_the_same_fonts_with_their_licences(self) -> None:
        for folder in FONT_FOLDERS:
            web = sorted(path.name for path in ROOT.joinpath("apps", "web", folder).iterdir())
            portal = sorted(path.name for path in ROOT.joinpath("apps", "portal", folder).iterdir())
            with self.subTest(folder=folder):
                self.assertEqual(web, portal)
                self.assertIn("LICENSE", web)
                self.assertTrue(any(name.endswith(".woff2") for name in web))
                for name in web:
                    path = f"{folder}/{name}"
                    self.assertEqual(read("web", path), read("portal", path))
                self.assertIn(b"SIL Open Font License", read("web", f"{folder}/LICENSE"))

    def test_every_font_file_the_stylesheet_names_is_vendored(self) -> None:
        stylesheet = read("web", "src/fonts/fonts.css").decode()
        names = [part.split('"')[0] for part in stylesheet.split('url("./')[1:]]
        self.assertEqual(len(names), 4)
        for name in names:
            with self.subTest(font=name):
                self.assertTrue(ROOT.joinpath("apps", "web", "src", "fonts", name).is_file())

    def test_both_pages_paint_the_saved_theme_before_the_app_loads(self) -> None:
        def bootstrap(app: str) -> str:
            html = read(app, "index.html").decode()
            return html[html.index("<script>") : html.index("</script>")]

        self.assertEqual(bootstrap("web"), bootstrap("portal"))
        # Only MOSAIC's own tokens are ever applied from the cache.
        self.assertIn("name.startsWith('--mosaic-')", bootstrap("web"))


if __name__ == "__main__":
    unittest.main()
