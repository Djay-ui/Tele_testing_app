"""Thin wrapper over the grammar-based Oracle PL/SQL parser -- an ANTLR4
lexer/parser generated from the real Oracle grammar published at
https://github.com/antlr/grammars-v4 (sql/plsql), vendored/generated under
tgdatabridge/plsql_grammar/ (see that directory's own notes for provenance).

This module exists to answer narrow, purely-structural questions with a
real parser instead of a regex heuristic -- starting with "where does this
routine's own top-level BEGIN actually start" (see nested_subprogram.py's
docstring for why that specific question is hard to answer reliably with
regex alone). It is deliberately NOT a replacement for the regex/marker-
based conversion rules in plsql_converter.py/tsql_converter.py/
db2_converter.py -- those still do all the real syntax translation work.
Every caller of this module is expected to treat a non-ok ParseResult as
ordinary, common signal (not an error) and fall back to its existing
regex-based logic unchanged.

Two entry points are used, matching the two raw source shapes this tool's
introspectors actually hand it (see plsql_converter.py's own note on the
same distinction):

  * PROCEDURE / FUNCTION / PACKAGE / PACKAGE BODY -- sourced from
    ALL_SOURCE, which omits the leading ``CREATE [OR REPLACE]``. We
    prepend ``CREATE OR REPLACE`` and parse via the grammar's top-level
    ``sql_script`` rule.
  * TRIGGER -- sourced from ALL_TRIGGERS.TRIGGER_BODY, which omits the
    *entire* ``CREATE TRIGGER ... ON table [FOR EACH ROW]`` header and
    starts straight at DECLARE/BEGIN. This shape matches the grammar's
    ``trigger_block : (DECLARE declare_spec*)? body ;`` rule directly, so
    we parse via that rule with no prefix.
"""
from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass, field
from typing import List, Optional

_GENERATED_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "plsql_grammar", "generated")
)
_VENDOR_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "plsql_grammar", "vendor")
)

_lock = threading.Lock()
_loaded = False
_antlr4 = None
_PlSqlLexer = None
_PlSqlParser = None


class GrammarUnavailableError(RuntimeError):
    """Raised when the ANTLR runtime and/or generated parser modules can't
    be imported at all (neither a real pip-installed antlr4-python3-runtime
    nor the vendored fallback is usable). Distinct from a syntax error in
    some PL/SQL source -- this means the parser itself couldn't load."""


def _ensure_importable() -> None:
    """Makes the `antlr4`, `PlSqlLexer`, and `PlSqlParser` modules
    importable, caching them at this module's own level so repeated calls
    don't re-import -- re-parsing the generated PlSqlParser.py's large
    serialized ATN table on every call would be needlessly slow; this
    happens at most once per process.

    Prefers a real pip-installed antlr4-python3-runtime (the normal path
    for a real deployment -- see requirements.txt); falls back to the
    vendored copy under tgdatabridge/plsql_grammar/vendor/ if that import fails
    (e.g. a sandboxed/offline environment where pip couldn't reach PyPI)."""
    global _loaded, _antlr4, _PlSqlLexer, _PlSqlParser
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        try:
            import antlr4  # type: ignore  # real pip-installed package, if present
        except ImportError:
            if _VENDOR_DIR not in sys.path:
                sys.path.insert(0, _VENDOR_DIR)
            try:
                import antlr4  # type: ignore  # vendored fallback
            except ImportError as exc:
                raise GrammarUnavailableError(
                    "Could not import antlr4 (neither a pip-installed "
                    "antlr4-python3-runtime nor the vendored fallback at "
                    f"{_VENDOR_DIR} worked)."
                ) from exc
        if _GENERATED_DIR not in sys.path:
            sys.path.insert(0, _GENERATED_DIR)
        try:
            from PlSqlLexer import PlSqlLexer  # type: ignore
            from PlSqlParser import PlSqlParser  # type: ignore
        except ImportError as exc:
            raise GrammarUnavailableError(
                "Could not import the generated PlSqlLexer/PlSqlParser "
                f"modules from {_GENERATED_DIR}."
            ) from exc
        _antlr4, _PlSqlLexer, _PlSqlParser = antlr4, PlSqlLexer, PlSqlParser
        _loaded = True


def grammar_available() -> bool:
    """True if the grammar parser can be loaded in this environment. Lets
    callers check once (e.g. at converter start-up) rather than relying on
    catching GrammarUnavailableError from every individual parse call."""
    try:
        _ensure_importable()
        return True
    except GrammarUnavailableError:
        return False


@dataclass
class PlSqlSyntaxError:
    line: int
    column: int
    message: str


class _CollectingErrorListener:
    """Replaces ANTLR's default ConsoleErrorListener (which prints every
    syntax error straight to stderr) with one that just collects them, so a
    parse failure becomes data callers can inspect or ignore instead of
    log noise."""

    def __init__(self) -> None:
        self.errors: List[PlSqlSyntaxError] = []

    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):  # noqa: N802 (ANTLR's interface name)
        self.errors.append(PlSqlSyntaxError(line=line, column=column, message=msg))

    # ANTLR's ErrorListener interface also expects these three. They're
    # no-ops here: this module only cares about outright syntax errors, not
    # ambiguity/context-sensitivity diagnostics (ANTLR's adaptive LL
    # parsing can report those even for unambiguous, perfectly valid input
    # in some grammars, so treating them as failures would be too strict).
    def reportAmbiguity(self, *args, **kwargs):
        pass

    def reportAttemptingFullContext(self, *args, **kwargs):
        pass

    def reportContextSensitivity(self, *args, **kwargs):
        pass


def _make_parser(text: str):
    """Returns a ready-to-use PlSqlParser for `text`, with a
    _CollectingErrorListener installed on both the lexer and the parser in
    place of ANTLR's noisy stderr-printing default. The listener is
    stashed on the parser instance so callers can read `.errors` back
    after driving whichever grammar rule they need."""
    _ensure_importable()
    listener = _CollectingErrorListener()
    input_stream = _antlr4.InputStream(text)
    lexer = _PlSqlLexer(input_stream)
    lexer.removeErrorListeners()
    lexer.addErrorListener(listener)
    stream = _antlr4.CommonTokenStream(lexer)
    parser = _PlSqlParser(stream)
    parser.removeErrorListeners()
    parser.addErrorListener(listener)
    parser._collecting_error_listener = listener  # noqa: SLF001 (our own attribute, not ANTLR's)
    return parser


@dataclass
class ParseResult:
    tree: Optional[object]
    errors: List[PlSqlSyntaxError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.tree is not None and not self.errors


# Routine kinds sourced from ALL_SOURCE (no leading CREATE [OR REPLACE] --
# see this module's docstring); parsed via the grammar's sql_script rule
# after that prefix is restored.
_SQL_SCRIPT_KINDS = {"PROCEDURE", "FUNCTION", "PACKAGE", "PACKAGE BODY"}


def parse_routine_source(kind: str, source: str) -> ParseResult:
    """Parses `source` -- a Routine.source string in exactly the raw shape
    this tool's introspectors read it in -- using the grammar rule that
    matches that raw shape. Never raises for ordinary syntax errors or
    unsupported constructs; those come back as a non-ok ParseResult with
    `.errors` populated. Raises GrammarUnavailableError only if the parser
    itself couldn't be loaded at all (see grammar_available()).

    A non-ok result is meaningful signal, not necessarily a bug in the
    routine -- this grammar snapshot may not cover every construct Oracle
    accepts. Callers should treat it as "fall back to the regex-based
    approach", not as proof the input is invalid PL/SQL."""
    kind_upper = (kind or "").strip().upper()
    try:
        if kind_upper == "TRIGGER":
            parser = _make_parser(source)
            tree = parser.trigger_block()
        else:
            parser = _make_parser("CREATE OR REPLACE\n" + source)
            tree = parser.sql_script()
    except GrammarUnavailableError:
        raise
    except Exception as exc:  # pragma: no cover -- ANTLR itself can raise on catastrophic input (e.g. a lexer-exhausting unterminated literal) rather than routing through the error listener
        return ParseResult(tree=None, errors=[PlSqlSyntaxError(line=0, column=0, message=str(exc))])

    errors = parser._collecting_error_listener.errors  # noqa: SLF001
    return ParseResult(tree=tree if not errors else None, errors=errors)


def can_parse(kind: str, source: str) -> "tuple[bool, List[str]]":
    """Convenience smoke-test wrapper: returns (ok, [error messages]).
    Never raises for parse failures; does raise GrammarUnavailableError if
    the parser can't be loaded at all, same as parse_routine_source."""
    result = parse_routine_source(kind, source)
    return result.ok, [f"line {e.line}:{e.column}: {e.message}" for e in result.errors]


# --------------------------------------------------------------------------
# Declare-section / body structure -- used by tgdatabridge.core.nested_subprogram
# to locate a routine's own top-level BEGIN and any nested PROCEDURE/
# FUNCTION declared inside its DECLARE section, with a real parse tree
# instead of BEGIN/END depth-counting over a string-blanked copy of the
# text. See nested_subprogram.py's own module docstring for why that
# structural question matters and what naive regex-based splitting gets
# wrong; this section exists to answer it more reliably wherever the
# grammar accepts the input, with nested_subprogram.py falling back to its
# own regex implementation unchanged whenever it doesn't (unsupported
# syntax, a genuine syntax error, or the grammar parser being unavailable
# at all -- never raised as an exception to that caller).

SYNTHETIC_PROCEDURE_HEADER = "CREATE OR REPLACE PROCEDURE tgdatabridge_synthetic_wrapper IS\n"
"""Prepended to text that has the shape found right after a PROCEDURE/
FUNCTION header's own IS/AS keyword: zero or more declare_specs with NO
leading DECLARE keyword (Oracle's procedure/function body syntax never
uses one there, unlike a trigger's DECLARE section or an anonymous PL/SQL
block), followed by a body (BEGIN ... END). There is no single named
grammar rule for exactly that shape by itself -- it's inlined as an
alternative inside create_procedure_body/procedure_body/etc in the .g4
source, always preceded by a real CREATE/PROCEDURE header -- so
find_declare_and_body_structure() parses `SYNTHETIC_PROCEDURE_HEADER +
text` through the ordinary sql_script entry point instead, then maps
every resulting offset back into `text`'s own coordinates by subtracting
len(SYNTHETIC_PROCEDURE_HEADER)."""


@dataclass
class NestedSubprogramSpan:
    kind: str    # "PROCEDURE" | "FUNCTION"
    name: str
    start: int   # char offset into the *original* text passed to find_declare_and_body_structure
    end: int     # one past the last char (Python-slice-style)


@dataclass
class DeclareAndBodyStructure:
    begin_offset: int  # char offset of the routine's own top-level BEGIN keyword
    nested: List[NestedSubprogramSpan]


def _find_context_of_type(node, class_name: str):
    """Depth-first search for the first descendant of `node` (or `node`
    itself) whose class name is `class_name`. Does not look inside a
    match for further occurrences -- callers want the outermost one (e.g.
    the top-level create_procedure_body, not a nested procedure_body
    declared inside its own DECLARE section)."""
    if type(node).__name__ == class_name:
        return node
    get_child_count = getattr(node, "getChildCount", None)
    if get_child_count is None:
        return None
    for i in range(get_child_count()):
        found = _find_context_of_type(node.getChild(i), class_name)
        if found is not None:
            return found
    return None


def _span(ctx, prefix_len: int) -> "tuple[int, int]":
    return ctx.start.start - prefix_len, ctx.stop.stop + 1 - prefix_len


def _structure_from_declare_specs(declare_specs, body_ctx, prefix_len: int) -> DeclareAndBodyStructure:
    """`declare_specs` is a list of Declare_specContext (as returned by
    either Trigger_blockContext.declare_spec() or
    Seq_of_declare_specsContext.declare_spec()); only the ones that are a
    real nested PROCEDURE/FUNCTION *definition* (declare_spec ->
    procedure_body | function_body -- i.e. it has its own body, not just a
    forward-declaring procedure_spec/function_spec) count as a nested
    subprogram to extract."""
    nested: List[NestedSubprogramSpan] = []
    for spec in declare_specs:
        proc = spec.procedure_body()
        func = spec.function_body() if proc is None else None
        inner = proc or func
        if inner is None:
            continue
        kind = "PROCEDURE" if proc is not None else "FUNCTION"
        name = inner.identifier().getText()
        start, end = _span(inner, prefix_len)
        nested.append(NestedSubprogramSpan(kind=kind, name=name, start=start, end=end))
    begin_offset = body_ctx.start.start - prefix_len
    return DeclareAndBodyStructure(begin_offset=begin_offset, nested=nested)


def _try_trigger_block_shape(text: str) -> Optional[DeclareAndBodyStructure]:
    """Covers two shapes directly, with no wrapping needed: a trigger's
    raw source (optional literal DECLARE keyword + declare_specs, then
    body), and the common case of no declare section at all (bare
    "BEGIN ... END")."""
    try:
        parser = _make_parser(text)
        tree = parser.trigger_block()
    except GrammarUnavailableError:
        raise
    except Exception:
        return None
    if parser._collecting_error_listener.errors:  # noqa: SLF001
        return None
    body_ctx = tree.body()
    if body_ctx is None:
        return None
    return _structure_from_declare_specs(tree.declare_spec(), body_ctx, prefix_len=0)


def _try_synthetic_procedure_shape(text: str) -> Optional[DeclareAndBodyStructure]:
    """Covers the remaining shape: bare declare_specs (no DECLARE keyword)
    then body -- see SYNTHETIC_PROCEDURE_HEADER."""
    synthetic = SYNTHETIC_PROCEDURE_HEADER + text
    try:
        parser = _make_parser(synthetic)
        tree = parser.sql_script()
    except GrammarUnavailableError:
        raise
    except Exception:
        return None
    if parser._collecting_error_listener.errors:  # noqa: SLF001
        return None
    proc_ctx = _find_context_of_type(tree, "Create_procedure_bodyContext")
    if proc_ctx is None:
        return None
    body_ctx = proc_ctx.body()
    if body_ctx is None:
        return None
    seq_ctx = proc_ctx.seq_of_declare_specs()
    declare_specs = seq_ctx.declare_spec() if seq_ctx is not None else []
    return _structure_from_declare_specs(
        declare_specs, body_ctx, prefix_len=len(SYNTHETIC_PROCEDURE_HEADER))


def find_declare_and_body_structure(text: str) -> Optional[DeclareAndBodyStructure]:
    """Parses `text` -- either a trigger's raw DECLARE/BEGIN...END source,
    or the bare-declare_specs-then-body shape found after a PROCEDURE/
    FUNCTION header's IS/AS keyword -- and returns the offset of its own
    top-level BEGIN plus the span of every nested PROCEDURE/FUNCTION
    *definition* declared directly in its declare section (not a mere
    forward-declaring spec, and not one declared inside a *further*
    nested subprogram's own declare section).

    Returns None if the grammar parser isn't available at all, or if
    neither of the two shapes it tries parses `text` cleanly (a genuine
    syntax error, or a construct this grammar snapshot doesn't cover) --
    callers should treat that the same as "grammar path doesn't apply
    here", not as proof `text` is invalid PL/SQL, and fall back to their
    own regex-based logic unchanged."""
    if not grammar_available():
        return None
    return _try_trigger_block_shape(text) or _try_synthetic_procedure_shape(text)
