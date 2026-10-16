"""Text matching for the fictional screenshot estate's exact grant selections."""


def grant_option_matches(text: str, cost_center: str, cloud: str | None) -> bool:
    return (
        text.startswith(cost_center + " ·")
        and " · Granted directly to you · " in text
        and text.endswith((" · Applied to APIM", " · Ready"))
        and (cloud is None or f" · {cloud} · " in text)
    )
