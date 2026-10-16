"""Build a tenant-customized personal-only Teams ZIP locally, without any deployment."""

import argparse
import io
import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image, ImageDraw


def _url(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise ValueError("Use HTTPS operator-published website, privacy and terms URLs.")
    return value


def _icon(size: int, *, outline: bool) -> bytes:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0) if outline else "#176B67")
    draw = ImageDraw.Draw(image)
    cell = size // 8
    for x, y in ((2, 2), (4, 2), (2, 4), (4, 4)):
        draw.rounded_rectangle(
            (x * cell, y * cell, (x + 1.5) * cell, (y + 1.5) * cell),
            radius=max(size // 64, 1),
            fill="#FFFFFF",
        )
    stream = io.BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


def build_package(
    output: Path, *, app_id: str, bot_app_id: str, website: str, privacy: str, terms: str
) -> dict[str, object]:
    template = Path(__file__).with_name("manifest.template.json").read_text(encoding="utf-8")
    values = {
        "APP_ID": str(UUID(app_id)),
        "BOT_APP_ID": str(UUID(bot_app_id)),
        "WEBSITE_URL": _url(website),
        "PRIVACY_URL": _url(privacy),
        "TERMS_URL": _url(terms),
    }
    # Replace JSON string values, not raw template fragments supplied by the operator.
    for key, value in values.items():
        template = template.replace(json.dumps("${" + key + "}"), json.dumps(value))
    manifest: dict[str, object] = json.loads(template)
    if "${" in template:
        raise ValueError("The Teams manifest has unresolved placeholders.")
    if output.is_absolute() or ".." in output.parts:
        raise ValueError("Write the package to a project-relative path.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        archive.writestr("color.png", _icon(192, outline=False))
        archive.writestr("outline.png", _icon(32, outline=True))
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-id", required=True, help="Teams catalog app UUID")
    parser.add_argument("--bot-app-id", required=True, help="Azure Bot application/client UUID")
    parser.add_argument("--website", required=True)
    parser.add_argument("--privacy", required=True)
    parser.add_argument("--terms", required=True)
    parser.add_argument(
        "--output", type=Path, default=Path("apps") / "teams" / "dist" / "mosaic.zip"
    )
    args = parser.parse_args()
    build_package(
        args.output,
        app_id=args.app_id,
        bot_app_id=args.bot_app_id,
        website=args.website,
        privacy=args.privacy,
        terms=args.terms,
    )
    print(f"Built {args.output}. No registration, consent, upload or deployment was performed.")


if __name__ == "__main__":
    main()
