"""The API Management double refuses the policy expressions API Management refuses to parse."""

import pytest
from apim_double import expression_error, policy_expression_error


def _block(code: str) -> str:
    return "@{\n" + code + "\n}"


def _single_statement(found: str) -> str:
    return (
        f'Expected a "{{" but found a "{found}". '
        'Block statements must be enclosed in "{" and "}".'
    )


@pytest.mark.parametrize(
    ("code", "found"),
    [
        # The key lookup MOSAIC generated before governed access could be applied.
        ('if (context.Subscription == null) return "";\nreturn context.Subscription.Id;', "return"),
        ('if (x) { return "a"; }\nelse return "b";', "return"),
        ('if (x) { return "a"; }\nelse if (y) return "b";\nreturn "c";', "return"),
        ('if (x) { if (y) return "a"; }\nreturn "b";', "return"),
        ("while (x < 3) x++;\nreturn x;", "x"),
        ("for (var i = 0; i < 3; i++) total += i;\nreturn total;", "total"),
        ("foreach (var item in items) total += item;\nreturn total;", "total"),
        ("do x++; while (x < 3);\nreturn x;", "x"),
        ("using (var reader = Open()) return reader.Read();", "return"),
        ("lock (gate) count++;\nreturn count;", "count"),
        ('switch (x) { case 1: if (y) return "a"; break; }\nreturn "b";', "return"),
        ("try return A(); catch { return B(); }", "return"),
        ("try { return A(); } catch (Exception e) return B();", "return"),
        ("try { return A(); } finally Done();", "Done"),
        # A bracket or keyword in a literal or comment doesn't end or start a statement.
        ('if (name == ")") return true;\nreturn false;', "return"),
        ("if (open == '(') return true;\nreturn false;", "return"),
        ("if (x) // then\nreturn 1;\nreturn 0;", "return"),
        ("if (x) /* { */ return 1;\nreturn 0;", "return"),
    ],
)
def test_a_control_flow_body_that_is_not_a_block_is_refused(code: str, found: str) -> None:
    error = expression_error(_block(code))

    assert error is not None
    assert error.startswith(_single_statement(found))


@pytest.mark.parametrize(
    "code",
    [
        'if (context.Subscription == null) { return ""; }\nreturn context.Subscription.Id;',
        'if (x) { return "a"; } else if (y) { return "b"; } else { return "c"; }',
        'if (x) { if (y) { return "a"; } }\nreturn "b";',
        "while (x < 3) { x++; }\nreturn x;",
        "for (var i = 0; i < 3; i++) { total += i; }\nreturn total;",
        "foreach (var item in items) { total += item; }\nreturn total;",
        "do { x++; } while (x < 3);\nreturn x;",
        "using (var reader = Open()) { return reader.Read(); }",
        "lock (gate) { count++; }\nreturn count;",
        'switch (x) { case 1: return "a"; default: return "b"; }',
        "try { return A(); } catch { return B(); }",
        "try { return A(); } catch (FormatException e) { return B(); } catch { return C(); }",
        "try { return A(); } finally { Done(); }",
        # Code in a literal or a comment isn't code.
        'var text = "if (x) return y;";\nreturn text;',
        'var text = "a \\" if (x) return y; \\\\";\nreturn text;',
        'var text = @"verbatim "" if (x) return y; \\";\nreturn text;',
        "var open = '(';\nvar quote = '\\'';\nif (open == quote) { return true; }\nreturn false;",
        "// if (x) return y;\nreturn z;",
        "/* if (x) return y; */\nreturn z;",
        "return items.Where(item => item != null).Count();",
    ],
)
def test_braced_bodies_and_look_alikes_are_accepted(code: str) -> None:
    assert expression_error(_block(code)) is None


@pytest.mark.parametrize(
    "value", ['@(x ? "a" : "b")', "if (x) return y;", "", '@("if (x) return y;")']
)
def test_only_a_multi_statement_expression_is_parsed_as_a_code_block(value: str) -> None:
    assert expression_error(value) is None


def test_the_first_refused_expression_is_located_by_the_element_that_holds_it() -> None:
    policy = (
        "<policies>\n"
        "  <inbound>\n"
        '    <set-variable name="ok" value="@{ if (x) { return 1; } return 0; }" />\n'
        "    <choose>\n"
        "      <when condition='@{ if (x) return true; return false; }'>\n"
        '        <set-body>@{ if (y) return "a"; return "b"; }</set-body>\n'
        "      </when>\n"
        "    </choose>\n"
        "  </inbound>\n"
        "</policies>"
    )

    refusal = policy_expression_error(policy)

    assert refusal is not None
    element, line, column, reason = refusal
    # The line and column are where the element's name starts, as API Management reports them.
    assert (element, line, column) == ("when", 5, 8)
    assert reason.startswith(_single_statement("return"))


def test_an_expression_in_element_text_is_parsed_like_one_in_an_attribute() -> None:
    policy = '<fragment>\n  <set-body>@{ if (y) return "a"; return "b"; }</set-body>\n</fragment>'

    refusal = policy_expression_error(policy)

    assert refusal is not None
    assert refusal[:3] == ("set-body", 2, 4)


@pytest.mark.parametrize(
    "policy",
    [
        "<fragment><set-variable name='a' value='@(x ? 1 : 0)' /></fragment>",
        "<fragment><set-body>if (x) return y;</set-body></fragment>",
        "<fragment><set-variable name='a' "
        "value='@{&#10;if (x) { return 1; }&#10;return 0;&#10;}' /></fragment>",
    ],
)
def test_a_policy_without_a_refused_expression_is_accepted(policy: str) -> None:
    assert policy_expression_error(policy) is None
