"""Tests for tgdatabridge.core.nested_subprogram -- the shared nested
PROCEDURE/FUNCTION detection module used by all three PL/SQL-sourced
converters. See that module's own docstring for the concrete silent-
corruption bug this exists to prevent.

Every test above the "grammar path" section below exercises the public
find_top_level_begin/extract_nested_subprograms API -- which now tries a
real grammar parse first and falls back to the regex/depth-counting
implementation only when that doesn't apply (see this module's own
docstring) -- so they already cover the grammar path wherever it's
available in the environment these tests run in. The dedicated section
below adds tests that pin down the two paths agreeing with each other,
and that the regex fallback still engages correctly for input the
grammar snapshot doesn't cover."""
from tgdatabridge.core import plsql_ast as _plsql_ast
from tgdatabridge.core.nested_subprogram import (
    _grammar_extract_nested_subprograms, _grammar_find_top_level_begin,
    _regex_extract_nested_subprograms, _regex_find_top_level_begin,
    extract_nested_subprograms, find_top_level_begin,
)

# ------------------------------------------------------- extract_nested_subprograms


def test_no_nested_subprogram_returns_text_unchanged():
    text = "\n  v_count NUMBER := 0;\n  v_total NUMBER;\n"
    remaining, nested = extract_nested_subprograms(text)
    assert remaining == text
    assert nested == []


def test_single_nested_procedure_extracted_and_flagged():
    text = (
        "\n  v_count NUMBER := 0;\n"
        "  PROCEDURE inner_proc IS\n"
        "  BEGIN\n"
        "    v_count := v_count + 1;\n"
        "  END inner_proc;\n"
        "  v_total NUMBER;\n"
    )
    remaining, nested = extract_nested_subprograms(text)
    assert len(nested) == 1
    assert nested[0].kind == "PROCEDURE"
    assert nested[0].name == "inner_proc"
    assert "BEGIN" in nested[0].source and "END inner_proc" in nested[0].source
    # The extracted span is gone from what's left for the ordinary parser,
    # and no dangling fragment (e.g. a bare "inner_proc;") remains behind.
    assert "PROCEDURE inner_proc" not in remaining
    assert "inner_proc" not in remaining
    assert "v_count NUMBER := 0;" in remaining
    assert "v_total NUMBER;" in remaining


def test_nested_function_with_anonymous_block_inside_tracked_by_depth():
    text = (
        "  FUNCTION inner_fn RETURN NUMBER IS\n"
        "    v_x NUMBER;\n"
        "  BEGIN\n"
        "    BEGIN\n"
        "      v_x := 1;\n"
        "    END;\n"
        "    RETURN v_x;\n"
        "  END inner_fn;\n"
        "  v_after NUMBER;\n"
    )
    remaining, nested = extract_nested_subprograms(text)
    assert len(nested) == 1
    assert nested[0].kind == "FUNCTION"
    assert nested[0].name == "inner_fn"
    assert "RETURN v_x;" in nested[0].source
    assert "v_after NUMBER;" in remaining


def test_two_sequential_nested_subprograms_both_extracted():
    text = (
        "  PROCEDURE p1 IS BEGIN NULL; END p1;\n"
        "  FUNCTION f1 RETURN NUMBER IS BEGIN RETURN 1; END f1;\n"
        "  v_final NUMBER;\n"
    )
    remaining, nested = extract_nested_subprograms(text)
    assert [n.name for n in nested] == ["p1", "f1"]
    assert "v_final NUMBER;" in remaining
    assert "p1" not in remaining and "f1" not in remaining


def test_keyword_inside_string_literal_does_not_confuse_depth_tracking():
    text = (
        "  PROCEDURE inner_proc IS\n"
        "  BEGIN\n"
        "    v_msg := 'this looks like BEGIN and END but is just text';\n"
        "  END inner_proc;\n"
        "  v_after NUMBER;\n"
    )
    remaining, nested = extract_nested_subprograms(text)
    assert len(nested) == 1
    assert "looks like BEGIN and END" in nested[0].source
    assert "v_after NUMBER;" in remaining


def test_forward_declaration_with_no_body_is_left_alone():
    text = "  PROCEDURE fwd_decl(p_x IN NUMBER);\n  v_after NUMBER;\n"
    remaining, nested = extract_nested_subprograms(text)
    assert nested == []
    assert remaining == text


def test_forward_declaration_does_not_swallow_a_later_real_nested_body():
    # The forward declaration's own ';' terminates it with no body of its
    # own; the BEGIN that follows belongs to the *next* nested subprogram
    # (which does have a real body), not to the forward declaration.
    text = (
        "  PROCEDURE fwd_decl(p_x IN NUMBER);\n"
        "  PROCEDURE real_one IS\n"
        "  BEGIN\n"
        "    NULL;\n"
        "  END real_one;\n"
        "  v_after NUMBER;\n"
    )
    remaining, nested = extract_nested_subprograms(text)
    assert len(nested) == 1
    assert nested[0].name == "real_one"
    assert "END real_one" in nested[0].source
    assert "fwd_decl" in remaining  # forward declaration left for the ordinary parser
    assert "v_after NUMBER;" in remaining


# ----------------------------------------------------------- find_top_level_begin


def test_find_top_level_begin_no_nested_subprogram():
    text = "v_x NUMBER;\nBEGIN\n  v_x := 1;\nEND;"
    pos = find_top_level_begin(text)
    assert text[pos:pos + 5] == "BEGIN"
    # It's the only BEGIN, so this is trivially the outer routine's own.
    assert text.index("BEGIN") == pos


def test_find_top_level_begin_skips_nested_subprogram_begin():
    text = (
        "v_count NUMBER;\n"
        "PROCEDURE inner_proc IS\n"
        "BEGIN\n"
        "  v_count := v_count + 1;\n"
        "END inner_proc;\n"
        "v_total NUMBER;\n"
        "BEGIN\n"
        "  inner_proc;\n"
        "  v_total := v_count;\n"
        "END outer_proc;"
    )
    pos = find_top_level_begin(text)
    # Must land on the *second* BEGIN (the outer routine's own), not the
    # nested subprogram's own BEGIN a few lines earlier.
    assert pos == text.index("BEGIN\n  inner_proc;")


def test_find_top_level_begin_skips_two_nested_subprograms():
    text = (
        "PROCEDURE p1 IS BEGIN NULL; END p1;\n"
        "FUNCTION f1 RETURN NUMBER IS BEGIN RETURN 1; END f1;\n"
        "BEGIN\n"
        "  p1;\n"
        "END outer;"
    )
    pos = find_top_level_begin(text)
    assert text[pos:].startswith("BEGIN\n  p1;")


def test_find_top_level_begin_returns_none_when_absent():
    assert find_top_level_begin("v_x NUMBER;") is None


def test_find_top_level_begin_forward_declaration_no_body_still_finds_real_begin():
    text = (
        "PROCEDURE fwd_decl(p_x IN NUMBER);\n"
        "BEGIN\n"
        "  NULL;\n"
        "END outer;"
    )
    pos = find_top_level_begin(text)
    assert text[pos:].startswith("BEGIN\n  NULL;")


def test_find_top_level_begin_forward_declaration_then_real_nested_body():
    text = (
        "PROCEDURE fwd_decl(p_x IN NUMBER);\n"
        "PROCEDURE real_one IS\n"
        "BEGIN\n"
        "  NULL;\n"
        "END real_one;\n"
        "BEGIN\n"
        "  real_one;\n"
        "END outer;"
    )
    pos = find_top_level_begin(text)
    assert text[pos:].startswith("BEGIN\n  real_one;")


# ------------------------------------------------------- grammar path vs. regex fallback


def _skip_if_grammar_unavailable():
    if not _plsql_ast.grammar_available():
        print("grammar parser not available in this environment; skipping")
        return True
    return False


def test_grammar_and_regex_agree_on_find_top_level_begin():
    if _skip_if_grammar_unavailable():
        return
    text = (
        "v_count NUMBER;\n"
        "PROCEDURE inner_proc IS\n"
        "BEGIN\n"
        "  v_count := v_count + 1;\n"
        "END inner_proc;\n"
        "FUNCTION fwd_fn(p NUMBER) RETURN NUMBER;\n"
        "v_total NUMBER;\n"
        "BEGIN\n"
        "  inner_proc;\n"
        "  v_total := fwd_fn(1);\n"
        "END outer_proc;"
    )
    grammar_pos = _grammar_find_top_level_begin(text)
    regex_pos = _regex_find_top_level_begin(text)
    assert grammar_pos is not None
    assert grammar_pos == regex_pos
    assert find_top_level_begin(text) == grammar_pos


def test_grammar_and_regex_agree_on_extract_nested_subprograms():
    if _skip_if_grammar_unavailable():
        return
    text = (
        "  v_count NUMBER := 0;\n"
        "  PROCEDURE inner_proc IS\n"
        "  BEGIN\n"
        "    v_count := v_count + 1;\n"
        "  END inner_proc;\n"
        "  FUNCTION fwd_fn(p NUMBER) RETURN NUMBER;\n"
        "  v_total NUMBER;\n"
    )
    grammar_result = _grammar_extract_nested_subprograms(text)
    regex_result = _regex_extract_nested_subprograms(text)
    assert grammar_result is not None
    assert grammar_result[0] == regex_result[0]
    assert [(n.kind, n.name, n.source) for n in grammar_result[1]] == [
        (n.kind, n.name, n.source) for n in regex_result[1]
    ]
    assert extract_nested_subprograms(text) == grammar_result


def test_grammar_path_used_for_trigger_shape_with_declare_keyword():
    # A trigger's raw source keeps the literal DECLARE keyword (unlike a
    # procedure/function's declare section) -- confirms the grammar path
    # handles that shape too, not just the bare-declare_specs one.
    if _skip_if_grammar_unavailable():
        return
    text = (
        "DECLARE\n"
        "  v_x NUMBER;\n"
        "  PROCEDURE helper IS\n"
        "  BEGIN\n"
        "    NULL;\n"
        "  END helper;\n"
        "BEGIN\n"
        "  helper;\n"
        "END;"
    )
    grammar_pos = _grammar_find_top_level_begin(text)
    assert grammar_pos is not None
    assert text[grammar_pos:].startswith("BEGIN\n  helper;")


def test_grammar_path_defers_to_regex_for_unsupported_construct():
    # A construct the grammar snapshot doesn't parse cleanly -- the
    # grammar helper must return None (not raise), so the public API
    # falls through to the regex implementation and still gets a correct
    # answer.
    if _skip_if_grammar_unavailable():
        return
    text = "v_x NUMBER := ???totally not valid PL/SQL syntax(((;\nBEGIN\n  NULL;\nEND;"
    assert _grammar_find_top_level_begin(text) is None
    pos = find_top_level_begin(text)
    assert text[pos:].startswith("BEGIN\n  NULL;")
