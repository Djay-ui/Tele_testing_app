"""Shared detection for a PROCEDURE/FUNCTION declared *inside* another
routine's own DECLARE section (a "nested subprogram") -- used by all three
Oracle-PL/SQL-sourced converters (plsql_converter.py, tsql_converter.py,
db2_converter.py) so a nested subprogram gets excised and flagged for
manual conversion as one intact unit, instead of being silently shredded
by each converter's ordinary declaration parser.

The concrete failure mode this replaces: `convert_declare_block` in every
one of the three converters splits the DECLARE section's text on every
top-level ';' (via `split_top_level`) and runs each resulting chunk
through a generic `^name\\s+(.+)$` fallback regex meant for plain variable
declarations. A nested subprogram like

    PROCEDURE inner_proc IS
    BEGIN
      v_x := 1;
      v_y := 2;
    END inner_proc;

has *its own* internal semicolons, so naive top-level splitting shreds it
into "PROCEDURE inner_proc IS BEGIN v_x := 1", "v_y := 2", and
"END inner_proc" -- each then misparsed by the generic fallback as if it
were a `<name> <type>` variable declaration, producing garbage output
(e.g. a bogus `PROCEDURE <mapped-garbage-type>;` line) instead of a
clearly-flagged manual-conversion block. None of the three converters
attempt to actually *convert* a nested subprogram's body -- only to find
its true boundaries so it can be cut out whole and flagged, which is why
this module only needs to track BEGIN/END depth (see _find_matching_end),
not do any real parsing of what's inside it.

Both public functions here try a real grammar parse first (via
tgdatabridge.core.plsql_ast, an ANTLR-generated parser for the actual Oracle
PL/SQL grammar) and only fall back to the regex/depth-counting
implementation below when that doesn't apply -- the grammar parser isn't
available in this environment, or `text` uses a construct outside what
this grammar snapshot covers, or it's a genuine syntax error. The grammar
path never raises out to callers for any of those cases; it's strictly an
accuracy upgrade over the regex path when it works, not a hard
dependency. See plsql_ast.find_declare_and_body_structure's own docstring
for the two source shapes it recognizes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from tgdatabridge.core import plsql_ast as _plsql_ast

_NESTED_SUBPROGRAM_START_RE = re.compile(
    r"\b(PROCEDURE|FUNCTION)\s+([A-Za-z_][\w$#]*)\b", re.IGNORECASE
)

_BEGIN_RE = re.compile(r"\bBEGIN\b", re.IGNORECASE)

# A block-opener that closes with a *two-token* END (END IF/END LOOP/
# END CASE) rather than a bare END -- see _find_matching_end's own
# docstring for why these never need to be tracked separately.
_BARE_END_RE = re.compile(r"\bBEGIN\b|\bEND\b(?!\s+(IF|LOOP|CASE)\b)", re.IGNORECASE)

_IS_AS_RE = re.compile(r"\b(IS|AS)\b", re.IGNORECASE)


def _is_forward_declaration(header_gap: str) -> bool:
    """`header_gap` is the text between a PROCEDURE/FUNCTION header's name
    and the next BEGIN found anywhere after it. A real definition always
    has an IS/AS keyword between its header and its own BEGIN (with any
    local declarations, and their own semicolons, in between); a forward
    declaration ("PROCEDURE foo(p IN NUMBER);") has neither IS/AS nor a
    body -- it terminates with its own ';' and nothing else. So: if a ';'
    appears before any IS/AS keyword (or there's no IS/AS in the gap at
    all), that BEGIN cannot belong to this header -- it's a forward
    declaration, and the BEGIN belongs to something else entirely (most
    often the enclosing routine's own body, or a later nested subprogram)."""
    semi_idx = header_gap.find(";")
    if semi_idx == -1:
        return False
    is_as_m = _IS_AS_RE.search(header_gap)
    if is_as_m is None:
        return True
    return semi_idx < is_as_m.start()


@dataclass
class NestedSubprogram:
    kind: str    # "PROCEDURE" | "FUNCTION"
    name: str
    source: str  # the full, unmodified text of the nested declaration (header through its closing END)


def _blank_strings(text: str) -> str:
    """Replace the contents of every '...' string literal with spaces (same
    length, so positions/spans into the original text stay valid) -- keeps
    a keyword that happens to appear inside a string literal (e.g. a log
    message containing the word "BEGIN") from being mistaken for real
    PL/SQL syntax by the depth-tracking scan below. Oracle's doubled-quote
    ('') escape needs no special handling: blanking everything between the
    first and second quote on each side of a doubled quote just blanks one
    extra, already-safe, non-keyword character."""
    out = []
    in_string = False
    for ch in text:
        if ch == "'":
            in_string = not in_string
            out.append(ch)
        elif in_string:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _find_matching_end(blanked: str, begin_pos: int) -> int:
    """Given the index of the 'B' in the 'BEGIN' keyword that opens a
    nested subprogram's body (within `blanked`, already string-blanked so
    keyword matches never land inside a literal), returns the index just
    past the bare 'END' that closes it -- tracking BEGIN/END depth only.

    IF/LOOP/CASE blocks nested inside never need to be tracked separately:
    they always close with a *two-token* END (END IF / END LOOP /
    END CASE), which `_BARE_END_RE`'s negative lookahead simply never
    matches as a depth-closer, so they pass through this scan invisibly --
    only a bare BEGIN...END pair (the subprogram's own body, or any
    anonymous block nested inside it) changes the depth."""
    depth = 1
    pos = begin_pos + len("BEGIN")
    for m in _BARE_END_RE.finditer(blanked, pos):
        if m.group(0).upper() == "BEGIN":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                end = m.end()
                # Oracle allows (but doesn't require) repeating the
                # subprogram's own name right after its closing END, and
                # the whole declaration is itself terminated by a ';' --
                # consume both if present so the returned span (and the
                # NestedSubprogram.source built from it) includes the
                # complete "END [name];" rather than stopping at the bare
                # keyword and leaving a dangling "name;" fragment behind
                # in what's supposed to be the *remaining* declare text.
                tail_m = re.match(r"[ \t]*[A-Za-z_][\w$#]*", blanked[end:])
                if tail_m:
                    end += tail_m.end()
                semi_m = re.match(r"\s*;", blanked[end:])
                if semi_m:
                    end += semi_m.end()
                return end
    return len(blanked)  # malformed/unbalanced input -- consume to end rather than loop forever


def find_top_level_begin(text: str) -> Optional[int]:
    """Finds the index of the 'BEGIN' keyword that starts *this* routine's
    (or trigger's) own executable section within `text` -- i.e. the first
    BEGIN that does NOT belong to a nested PROCEDURE/FUNCTION declared
    earlier in the same DECLARE section.

    This matters even before extract_nested_subprograms gets a chance to
    run: every converter's header parser originally located the
    declare/body boundary with a naive "first BEGIN anywhere" search,
    which finds a *nested* subprogram's own BEGIN instead whenever one is
    declared before the enclosing routine's real body -- silently
    misidentifying the declare/body split for the *outer* routine, well
    before convert_declare_block ever runs (and long before it could flag
    the nested subprogram itself). Returns None if no BEGIN is found at
    all (malformed/unparseable input, handled the same way the caller's
    original bare `re.search` miss was).

    Tries a real grammar parse of `text` first (see this module's own
    docstring); falls back to the regex/depth-counting implementation
    below whenever that doesn't apply."""
    grammar_offset = _grammar_find_top_level_begin(text)
    if grammar_offset is not None:
        return grammar_offset
    return _regex_find_top_level_begin(text)


def _grammar_find_top_level_begin(text: str) -> Optional[int]:
    try:
        structure = _plsql_ast.find_declare_and_body_structure(text)
    except Exception:
        return None
    if structure is None:
        return None
    if not (0 <= structure.begin_offset <= len(text)):
        return None
    return structure.begin_offset


def _regex_find_top_level_begin(text: str) -> Optional[int]:
    """Regex/BEGIN-END-depth-counting fallback for find_top_level_begin --
    see that function's own docstring for what it's used for and when the
    grammar-based path above defers to it."""
    blanked = _blank_strings(text)
    pos = 0
    while True:
        begin_m = _BEGIN_RE.search(blanked, pos)
        if begin_m is None:
            return None
        nested_m = _NESTED_SUBPROGRAM_START_RE.search(blanked, pos)
        if nested_m is not None and nested_m.start() < begin_m.start():
            # begin_m is, by construction, the earliest BEGIN at or after
            # nested_m.end() (nothing before it starting from `pos`, and
            # nested_m.start() < begin_m.start()) -- but that doesn't mean
            # it belongs to *this* nested subprogram. A mere forward
            # declaration ("PROCEDURE foo(p IN NUMBER);") terminates with a
            # ';' of its own before any BEGIN of its own; if that ';' comes
            # before begin_m, this BEGIN belongs to something later (most
            # often the enclosing routine's own body), not to this
            # declaration -- skip past just the forward declaration and
            # keep looking from there rather than misattributing begin_m.
            header_gap = blanked[nested_m.end():begin_m.start()]
            if _is_forward_declaration(header_gap):
                pos = nested_m.end() + header_gap.find(";") + 1
                continue
            # No terminating ';' before begin_m -- it's this subprogram's
            # own body. Skip that subprogram's whole span (header through
            # its own closing END) and keep looking from there.
            pos = _find_matching_end(blanked, begin_m.start())
            continue
        return begin_m.start()


def extract_nested_subprograms(declare_text: str) -> Tuple[str, List[NestedSubprogram]]:
    """Scans `declare_text` (the raw text of a DECLARE section) for nested
    PROCEDURE/FUNCTION declarations and cuts each one out whole, matching
    its true end via `_find_matching_end` rather than the naive
    split-on-next-top-level-semicolon every converter's ordinary
    declaration parser uses (see this module's own docstring for the
    concrete corruption that naive splitting causes).

    Returns `(remaining_text, nested)` where `nested` is every extracted
    NestedSubprogram found (empty if none), and `remaining_text` is
    `declare_text` with every nested subprogram's span removed -- safe to
    hand to the caller's normal declaration splitter for everything else
    still in the DECLARE section.

    Tries a real grammar parse of `declare_text` first (see this module's
    own docstring); falls back to the regex/depth-counting implementation
    below whenever that doesn't apply."""
    grammar_result = _grammar_extract_nested_subprograms(declare_text)
    if grammar_result is not None:
        return grammar_result
    return _regex_extract_nested_subprograms(declare_text)


# `declare_text` (as every caller passes it) never has its own real BEGIN
# ... END -- it's just the declare section, always cut off before the
# routine's own body starts (see find_top_level_begin). The grammar has
# no rule for a bare declare-section-with-no-body at all (every real
# grammar rule that accepts declare_specs also requires a body right
# after them), so this appends a throwaway body before handing it to
# find_declare_and_body_structure, and only trusts spans that land fully
# inside `declare_text` itself -- the appended tail can never legitimately
# contain a nested subprogram, so any span reaching into it means the
# grammar path didn't apply cleanly here (caught below, falls back to regex).
_DUMMY_BODY_SUFFIX = "\nBEGIN\n  NULL;\nEND;\n"


def _grammar_extract_nested_subprograms(
    declare_text: str,
) -> Optional[Tuple[str, List[NestedSubprogram]]]:
    try:
        structure = _plsql_ast.find_declare_and_body_structure(
            declare_text + _DUMMY_BODY_SUFFIX)
    except Exception:
        return None
    if structure is None:
        return None
    spans = structure.nested
    if not all(0 <= s.start and s.end <= len(declare_text) for s in spans):
        return None
    if not spans:
        return declare_text, []

    nested = [
        NestedSubprogram(kind=s.kind, name=s.name, source=declare_text[s.start:s.end])
        for s in spans
    ]
    parts: List[str] = []
    last = 0
    for s in spans:
        parts.append(declare_text[last:s.start])
        last = s.end
    parts.append(declare_text[last:])
    return "".join(parts), nested


def _regex_extract_nested_subprograms(declare_text: str) -> Tuple[str, List[NestedSubprogram]]:
    """Regex/BEGIN-END-depth-counting fallback for extract_nested_subprograms
    -- see that function's own docstring for what it's used for and when
    the grammar-based path above defers to it."""
    blanked = _blank_strings(declare_text)
    cuts: List[Tuple[int, int]] = []
    nested: List[NestedSubprogram] = []
    pos = 0
    while True:
        m = _NESTED_SUBPROGRAM_START_RE.search(blanked, pos)
        if not m:
            break
        start = m.start()
        kind, name = m.group(1).upper(), m.group(2)
        begin_m = re.search(r"\bBEGIN\b", blanked[m.end():], re.IGNORECASE)
        if not begin_m:
            # No BEGIN anywhere after this header at all -- a forward
            # declaration with no body ("PROCEDURE foo(...);" -- legal in a
            # package spec/body's own DECLARE-section-like area, rare in a
            # plain procedure's DECLARE section) -- not a nested subprogram
            # *definition*, so there's no body to shred and nothing to
            # extract here; let the ordinary declaration parser handle it.
            pos = m.end()
            continue
        header_gap = blanked[m.end():m.end() + begin_m.start()]
        if _is_forward_declaration(header_gap):
            # The header terminates with its own ';' before reaching this
            # BEGIN -- same forward-declaration shape as above, just with
            # a later BEGIN in the text (typically the enclosing routine's
            # own, or a subsequent nested subprogram's) that must not be
            # misattributed to this bodyless declaration.
            pos = m.end() + header_gap.find(";") + 1
            continue
        begin_pos = m.end() + begin_m.start()
        end_pos = _find_matching_end(blanked, begin_pos)
        nested.append(NestedSubprogram(kind=kind, name=name, source=declare_text[start:end_pos]))
        cuts.append((start, end_pos))
        pos = end_pos

    if not cuts:
        return declare_text, []

    parts: List[str] = []
    last = 0
    for start, end in cuts:
        parts.append(declare_text[last:start])
        last = end
    parts.append(declare_text[last:])
    return "".join(parts), nested
