"""The organization's colour scheme for the console and the portal.

A palette is six colours. Both apps derive every other colour from them, including readable text
on the accent, so only the pairs a person chooses directly need checking here.
"""

import re
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from mosaic_api.domain import Entity, MosaicModel, deterministic_id

_HEX_COLOR = re.compile(r"#[0-9a-f]{6}")

# WCAG AA for body text. The apps refuse a palette below it, so the API must too.
MIN_TEXT_CONTRAST = 4.5


def _hex_color(value: str) -> str:
    color = value.strip().lower()
    if not _HEX_COLOR.fullmatch(color):
        raise ValueError("Use a six-digit hex colour such as #3566e8")
    return color


HexColor = Annotated[str, AfterValidator(_hex_color)]


def _luminance(color: str) -> float:
    def channel(index: int) -> float:
        value = int(color[index : index + 2], 16) / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5)


def contrast(first: str, second: str) -> float:
    """The WCAG contrast ratio of two opaque colours, from 1 to 21."""

    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


class Palette(MosaicModel):
    accent: HexColor
    backdrop: HexColor
    panel: HexColor
    border: HexColor
    text: HexColor
    text_muted: HexColor

    @model_validator(mode="after")
    def _readable(self) -> Self:
        pairs = (
            ("Text on panels", self.text, self.panel),
            ("Secondary text on panels", self.text_muted, self.panel),
            ("Text on the page background", self.text, self.backdrop),
            ("Secondary text on the page background", self.text_muted, self.backdrop),
        )
        for label, foreground, background in pairs:
            # Judge the figure the editor shows, which is rounded to one decimal place.
            ratio = round(contrast(foreground, background), 1)
            if ratio < MIN_TEXT_CONTRAST:
                raise ValueError(
                    f"{label} has a contrast of {ratio}:1. It needs at least {MIN_TEXT_CONTRAST}:1."
                )
        return self


class ColorScheme(MosaicModel):
    """The colours everyone sees in light mode and in dark mode."""

    light: Palette
    dark: Palette


class AppearanceSettings(Entity):
    """What an administrator has chosen for everyone. No scheme means MOSAIC's own colours."""

    entity_type: Literal["appearanceSettings"] = "appearanceSettings"
    scheme: ColorScheme | None = None
    allow_custom_themes: bool = True


class AppearanceUpdate(MosaicModel):
    """A full replacement: send `scheme: null` to return to MOSAIC's own colours."""

    scheme: ColorScheme | None = Field(default=None)
    allow_custom_themes: bool = True


class AppearanceView(MosaicModel):
    """What every signed-in person's app needs in order to paint itself."""

    scheme: ColorScheme | None = None
    allow_custom_themes: bool = True


def appearance_settings_id(tenant_id: str) -> str:
    return deterministic_id("appearanceSettings", tenant_id)


def default_appearance(tenant_id: str) -> AppearanceSettings:
    return AppearanceSettings(id=appearance_settings_id(tenant_id), tenant_id=tenant_id)
