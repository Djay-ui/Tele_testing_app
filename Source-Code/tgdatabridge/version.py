"""The product name and the build stamp -- the two things the tool says
about itself.

**This is the only file that spells the product name.** Everything that
shows it to a user -- the window title, the log banner, the crash dialog,
the HTML report headers, the CLI ``--help``, the PyInstaller spec -- imports
it from here. That is deliberate: the previous rebrand meant hunting the
old name through forty modules, and the point of doing it once properly is
that the next one is an edit to four strings below.

The build stamp exists because of a specific, repeated waste of everybody's
time: a fix would ship, the tool would still show the old behaviour, and
there was no way to tell from inside the running program whether it was the
new build or the old one still sitting in the folder. The answer was always
"check the file size in Explorer", which is a poor way to find out. The
title bar now says which build is running, so the first screenshot of any
problem answers the question by itself.
"""
from __future__ import annotations

# --------------------------------------------------------------- the brand

#: The product, as it is written everywhere a person sees it.
PRODUCT = "TG DataBridge"

#: With the trademark mark, for the places that are formally "the product
#: name" rather than running text -- title bar, report headers, About. Kept
#: separate because a log file, a filename and a thread name all want the
#: plain form.
PRODUCT_TM = "TG DataBridge™"

#: Goes under the name wherever there is room for it.
TAGLINE = "Connecting Legacy Data to the Future"

#: Who makes it. Unchanged by the rebrand -- "TG" in the product name is
#: this company, and the watermark on the dashboard is its wordmark.
VENDOR = "Teleglobal International Pvt Ltd"

#: The name used for files and folders the tool creates: the Windows
#: %APPDATA% directory, the packaged .exe, the PyInstaller build. No
#: spaces, no punctuation -- it ends up in paths and in Prometheus metric
#: names.
SLUG = "TGDataBridge"

#: What this product was called before the rebrand. Still needed in exactly
#: one place: app_storage looks for the old %APPDATA% folder so an existing
#: install's saved connections, run history and in-flight migration
#: checkpoints carry over instead of silently starting empty.
LEGACY_APP_DIR_NAME = "TeleglobalTDMT"
LEGACY_PRODUCT = "Teleglobal Database Migration Tool"

# --------------------------------------------------------------- the build

#: Bumped by hand with each delivered build. Kept deliberately simple --
#: a date and a round number, both of which appear in WHATS-FIXED.md, so a
#: screenshot can be matched to a changelog entry without asking.
BUILD = "R36"
BUILD_DATE = "2026-09-17"

#: Every headline change in this build, for the About/startup log.
BUILD_SUMMARY = (
    "Fixed the rest of the 'Apply DDL to Target' failures from the same "
    "PL/SQL package Round 35 started on, plus one new class of failure "
    "found in the data underneath it. PostgreSQL PL/pgSQL conversion: "
    "(1) an Oracle collection/array element assignment like "
    "'v_values(1):=100;' is now caught and flagged 'Requires manual "
    "conversion' instead of reaching the target as broken SQL; (2) a "
    "parameterized CURSOR's own parameter types (e.g. VARCHAR2) are now "
    "mapped to PostgreSQL types the same way a routine's own parameters "
    "already were; (3) a bare procedure-call statement with no leading "
    "keyword now gets Postgres's required CALL keyword added "
    "automatically; (4) SYS_REFCURSOR now maps to PostgreSQL's REFCURSOR "
    "type instead of silently defaulting to TEXT; (5) PIPE ROW (Oracle "
    "pipelined table functions) is now flagged 'Requires manual "
    "conversion' rather than reaching the target verbatim, since "
    "PostgreSQL has no equivalent statement to rewrite it to. Separately: "
    "'Apply DDL to Target' now checks, before running a single statement, "
    "whether a RANGE/LIST-partitioned source table already exists on the "
    "target as a plain table left over from an earlier run -- previously "
    "'CREATE TABLE IF NOT EXISTS' silently kept the stale plain table, "
    "and every 'CREATE TABLE ... PARTITION OF' statement for it then "
    "failed with '\"<table>\" is not partitioned', once per partition, "
    "with nothing naming the actual cause. This is now caught up front "
    "and offered through the same 'Leave it out' / 'Replace it...' "
    "recovery dialog already used for an ordinary column-shape mismatch."
)


def name() -> str:
    """The product name on its own, with the trademark mark."""
    return PRODUCT_TM


def title() -> str:
    """What goes in the window title bar.

    Name, then tagline, then the build -- in that order, because the name
    is what a person is looking for in the taskbar and the build is what a
    support engineer is looking for in a screenshot.
    """
    return f"{PRODUCT_TM}  —  {TAGLINE}  —  build {BUILD} ({BUILD_DATE})"


def short_title() -> str:
    """Name and build, for dialogs and message boxes where the tagline is
    just noise."""
    return f"{PRODUCT_TM}  —  build {BUILD} ({BUILD_DATE})"


def banner() -> str:
    """The first line in the log, so a saved log identifies its build.

    ASCII only: this string is also written to plain-text log files and
    echoed by the headless CLI, and a Windows console in a legacy code page
    turns a stray em-dash into a UnicodeEncodeError at the exact moment
    somebody is trying to read a log to find out what went wrong.
    """
    return (f"{PRODUCT} ({TAGLINE}) -- build {BUILD} ({BUILD_DATE}). "
            f"{BUILD_SUMMARY}")
