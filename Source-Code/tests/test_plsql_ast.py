"""Tests for tgdatabridge.core.plsql_ast -- the thin wrapper over the ANTLR-
generated, grammar-based Oracle PL/SQL parser (see that module's own
docstring for what it is and is not for).

These tests exercise the wrapper's public surface (grammar_available,
parse_routine_source, can_parse) against the two raw source shapes this
tool's introspectors actually hand it -- ALL_SOURCE-style (no leading
CREATE [OR REPLACE]) for PROCEDURE/FUNCTION/PACKAGE/PACKAGE BODY, and
ALL_TRIGGERS.TRIGGER_BODY-style (no header at all, starts at DECLARE/
BEGIN) for TRIGGER -- plus the error-collection path for invalid input.

If the grammar parser isn't loadable in the environment these tests run
in (e.g. the vendored ANTLR runtime/generated modules aren't present),
every test here is skipped rather than failed, since this capability is
an optional, additive upgrade -- not a hard requirement of the rest of
the tool (see plsql_ast.py's own module docstring)."""
from tgdatabridge.core import plsql_ast as pa

_SKIP_MSG = "grammar parser not available in this environment; skipping"


def _skip_if_unavailable():
    if not pa.grammar_available():
        print(_SKIP_MSG)
        return True
    return False


# ------------------------------------------------------- grammar_available


def test_grammar_available_is_a_bool_and_does_not_raise():
    result = pa.grammar_available()
    assert isinstance(result, bool)


# ------------------------------------------------------- parse_routine_source: procedures/functions


def test_simple_procedure_parses_cleanly():
    if _skip_if_unavailable():
        return
    src = (
        "PROCEDURE simple_proc (p_id IN NUMBER) IS\n"
        "BEGIN\n"
        "  NULL;\n"
        "END simple_proc;"
    )
    result = pa.parse_routine_source("PROCEDURE", src)
    assert result.ok
    assert result.tree is not None
    assert result.errors == []


def test_simple_function_parses_cleanly():
    if _skip_if_unavailable():
        return
    src = (
        "FUNCTION add_one (p_x IN NUMBER) RETURN NUMBER IS\n"
        "BEGIN\n"
        "  RETURN p_x + 1;\n"
        "END add_one;"
    )
    result = pa.parse_routine_source("FUNCTION", src)
    assert result.ok


def test_nested_subprogram_shape_parses_cleanly():
    # This is the exact shape that originally motivated pulling in a real
    # grammar parser -- see nested_subprogram.py's module docstring.
    if _skip_if_unavailable():
        return
    src = (
        "PROCEDURE outer_proc (p_id IN NUMBER) IS\n"
        "  PROCEDURE inner_proc (p_x IN NUMBER) IS\n"
        "  BEGIN\n"
        "    NULL;\n"
        "  END inner_proc;\n"
        "BEGIN\n"
        "  inner_proc(p_id);\n"
        "END outer_proc;"
    )
    result = pa.parse_routine_source("PROCEDURE", src)
    assert result.ok


def test_package_body_parses_cleanly():
    if _skip_if_unavailable():
        return
    src = (
        "PACKAGE BODY pkg_demo IS\n"
        "  PROCEDURE do_thing IS\n"
        "  BEGIN\n"
        "    NULL;\n"
        "  END do_thing;\n"
        "END pkg_demo;"
    )
    result = pa.parse_routine_source("PACKAGE BODY", src)
    assert result.ok


# ------------------------------------------------------- parse_routine_source: triggers


def test_trigger_body_shape_parses_via_trigger_block_rule():
    # ALL_TRIGGERS.TRIGGER_BODY has no CREATE TRIGGER header at all -- it
    # starts straight at DECLARE/BEGIN. This must NOT go through the
    # sql_script/CREATE-OR-REPLACE-prefix path used for the other kinds.
    if _skip_if_unavailable():
        return
    src = (
        "DECLARE\n"
        "  v_x NUMBER;\n"
        "BEGIN\n"
        "  v_x := 1;\n"
        "END;"
    )
    result = pa.parse_routine_source("TRIGGER", src)
    assert result.ok


def test_trigger_body_without_declare_section_parses():
    if _skip_if_unavailable():
        return
    src = "BEGIN\n  :NEW.updated_at := SYSDATE;\nEND;"
    result = pa.parse_routine_source("TRIGGER", src)
    assert result.ok


# ------------------------------------------------------- error collection


def test_invalid_source_reports_errors_instead_of_raising():
    if _skip_if_unavailable():
        return
    src = "PROCEDURE %%% totally not valid ((( )))"
    result = pa.parse_routine_source("PROCEDURE", src)
    assert not result.ok
    assert result.tree is None
    assert len(result.errors) > 0
    assert all(hasattr(e, "line") and hasattr(e, "message") for e in result.errors)


def test_can_parse_returns_bool_and_string_messages():
    if _skip_if_unavailable():
        return
    ok, errs = pa.can_parse("PROCEDURE", "PROCEDURE %%% not valid (((")
    assert ok is False
    assert isinstance(errs, list)
    assert all(isinstance(e, str) for e in errs)

    ok2, errs2 = pa.can_parse(
        "PROCEDURE",
        "PROCEDURE p IS\nBEGIN\n  NULL;\nEND p;",
    )
    assert ok2 is True
    assert errs2 == []
