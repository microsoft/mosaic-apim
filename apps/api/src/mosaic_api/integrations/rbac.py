"""Shared Azure RBAC evaluation.

Both gateway and model-endpoint preflight ask the same question of an effective-permissions
response: does this identity hold a given action at this scope? The gateway runtime check asks the
same question of a role definition's data actions. Keeping one implementation means a subtle change
in how ``notActions`` or ``notDataActions`` is interpreted cannot silently diverge between them.
"""

import re

from mosaic_api.integrations.apim.client import JsonObject


def action_matches(pattern: str, action: str) -> bool:
    """Match an RBAC action against a permission pattern, where ``*`` spans any characters."""

    regex = re.escape(pattern).replace(r"\*", ".*")
    return re.fullmatch(regex, action, re.IGNORECASE) is not None


def _matches_any(patterns: object, action: str) -> bool:
    return isinstance(patterns, list) and any(
        isinstance(pattern, str) and action_matches(pattern, action) for pattern in patterns
    )


def _block_grants(permission: JsonObject, action: str, *, granted: str, excluded: str) -> bool:
    return _matches_any(permission.get(granted), action) and not _matches_any(
        permission.get(excluded), action
    )


def permits(permissions: list[JsonObject], action: str) -> bool:
    """Evaluate RBAC per assignment: ``notActions`` only subtract from their own ``actions``.

    Evaluating the union of every assignment's ``notActions`` against the union of its ``actions``
    would let an unrelated restrictive assignment mask a grant that genuinely applies.
    """

    return any(
        _block_grants(permission, action, granted="actions", excluded="notActions")
        for permission in permissions
    )


def grants_data_action(permission: JsonObject, data_action: str) -> bool:
    """Does one permission block grant a data action?

    Azure's semantics, the same ones :func:`permits` applies to actions: matching is
    case-insensitive with ``*`` wildcards, and ``notDataActions`` subtract only from the
    ``dataActions`` of the block they appear in.
    """

    return _block_grants(permission, data_action, granted="dataActions", excluded="notDataActions")


class _UnrecognizedCondition(ValueError):
    pass


# Token kinds are "(", ")", "not", "and", "or", and "term".
_Token = tuple[str, str]
_ACTION_MATCHES = re.compile(r"^ActionMatches\s*\{\s*'([^']*)'\s*\}$", re.IGNORECASE)
_CLOSERS = {"{": "}", "[": "]"}


def _tokenize(condition: str) -> list[_Token]:
    tokens: list[_Token] = []
    word: list[str] = []

    def flush() -> None:
        if word:
            text = "".join(word)
            word.clear()
            keyword = text.casefold()
            tokens.append((keyword, text) if keyword in {"and", "or", "not"} else ("term", text))

    index = 0
    while index < len(condition):
        char = condition[index]
        if char == "'":
            end = condition.find("'", index + 1)
            if end < 0:
                raise _UnrecognizedCondition("unterminated string")
            word.append(condition[index : end + 1])
            index = end + 1
            continue
        if char in _CLOSERS:
            end = index + 1
            quoted = False
            while end < len(condition) and (quoted or condition[end] != _CLOSERS[char]):
                if condition[end] == "'":
                    quoted = not quoted
                end += 1
            if end >= len(condition):
                raise _UnrecognizedCondition("unterminated bracket")
            word.append(condition[index : end + 1])
            index = end + 1
            continue
        if condition.startswith(("&&", "||"), index):
            flush()
            tokens.append(("and" if char == "&" else "or", condition[index : index + 2]))
            index += 2
            continue
        if char.isspace():
            flush()
        elif char in "()":
            flush()
            tokens.append((char, char))
        elif char == "!":
            flush()
            tokens.append(("not", char))
        else:
            word.append(char)
        index += 1
    flush()
    return tokens


class _ConditionEvaluator:
    """Kleene three-valued evaluation of an ABAC condition for one action.

    ``ActionMatches`` is the only term whose value MOSAIC knows, because the action is the one
    thing it knows about the request. Every attribute test is unknown, and ``AND`` mixed with
    ``OR`` at one level without parentheses is refused rather than guessed at.
    """

    def __init__(self, tokens: list[_Token], action: str) -> None:
        self._tokens = tokens
        self._position = 0
        self._action = action

    def evaluate(self) -> bool | None:
        value = self._expression()
        if self._position != len(self._tokens):
            raise _UnrecognizedCondition("unexpected trailing tokens")
        return value

    def _peek(self) -> str | None:
        return self._tokens[self._position][0] if self._position < len(self._tokens) else None

    def _expression(self) -> bool | None:
        operands = [self._unary()]
        operator = self._peek()
        if operator not in {"and", "or"}:
            return operands[0]
        while self._peek() in {"and", "or"}:
            if self._peek() != operator:
                raise _UnrecognizedCondition("AND and OR mixed without parentheses")
            self._position += 1
            operands.append(self._unary())
        decisive = operator == "or"
        if any(value is decisive for value in operands):
            return decisive
        if all(value is (not decisive) for value in operands):
            return not decisive
        return None

    def _unary(self) -> bool | None:
        token = self._peek()
        if token == "not":
            self._position += 1
            value = self._unary()
            return None if value is None else not value
        if token == "(":
            self._position += 1
            value = self._expression()
            if self._peek() != ")":
                raise _UnrecognizedCondition("unbalanced parentheses")
            self._position += 1
            return value
        if token != "term":
            raise _UnrecognizedCondition("expected a term")
        words: list[str] = []
        while self._peek() == "term":
            words.append(self._tokens[self._position][1])
            self._position += 1
        match = _ACTION_MATCHES.match(" ".join(words))
        return action_matches(match.group(1), self._action) if match else None


def _evaluate_condition(condition: object, action: str) -> bool | None:
    if condition is None or (isinstance(condition, str) and not condition.strip()):
        return True
    if not isinstance(condition, str):
        return None
    try:
        return _ConditionEvaluator(_tokenize(condition), action).evaluate()
    except _UnrecognizedCondition:
        return None


def condition_permits(condition: object, action: str) -> bool:
    """Whether an ABAC condition is provably true for ``action``, whatever else the request says.

    Azure evaluates a condition per request, and a clause of the form
    ``(!(ActionMatches{'X'})) OR (<attribute test>)`` is true for every action other than ``X``.
    Foundry Owner's condition, for example, constrains only role-assignment writes and deletes, so
    it leaves inference untouched. MOSAIC recognises that shape and combinations of it. A condition
    that depends on an attribute MOSAIC cannot know, or syntax it does not parse, is not provably
    true: claiming "can invoke" on the strength of it would be exactly the false positive the
    runtime check exists to prevent. A missing condition is trivially true.
    """

    return _evaluate_condition(condition, action) is True


def condition_may_hold(condition: object, action: str) -> bool:
    """Whether a condition could be true for ``action``: only a provable ``False`` rules it out.

    A deny assignment's condition decides whether the deny applies, so the safe reading there is
    the opposite of :func:`condition_permits`.
    """

    return _evaluate_condition(condition, action) is not False
