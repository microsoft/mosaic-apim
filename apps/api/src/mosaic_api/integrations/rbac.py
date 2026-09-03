"""Shared Azure RBAC evaluation.

Both gateway and model-endpoint preflight ask the same question of an effective-permissions
response: does this identity hold a given action at this scope? Keeping one implementation means a
subtle change in how ``notActions`` is interpreted cannot silently diverge between the two.
"""

import re

from mosaic_api.integrations.apim.client import JsonObject


def action_matches(pattern: str, action: str) -> bool:
    """Match an RBAC action against a permission pattern, where ``*`` spans any characters."""

    regex = re.escape(pattern).replace(r"\*", ".*")
    return re.fullmatch(regex, action, re.IGNORECASE) is not None


def permits(permissions: list[JsonObject], action: str) -> bool:
    """Evaluate RBAC per assignment: ``notActions`` only subtract from their own ``actions``.

    Evaluating the union of every assignment's ``notActions`` against the union of its ``actions``
    would let an unrelated restrictive assignment mask a grant that genuinely applies.
    """

    for permission in permissions:
        actions = permission.get("actions")
        granted = isinstance(actions, list) and any(
            isinstance(pattern, str) and action_matches(pattern, action) for pattern in actions
        )
        if not granted:
            continue
        not_actions = permission.get("notActions")
        excluded = isinstance(not_actions, list) and any(
            isinstance(pattern, str) and action_matches(pattern, action) for pattern in not_actions
        )
        if not excluded:
            return True
    return False
