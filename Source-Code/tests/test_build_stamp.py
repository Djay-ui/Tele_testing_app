"""The build stamp, and the one-DROP rule.

Both exist because of the same waste of time: a fix ships, the tool still
behaves like the old build, and nothing inside the running program says
which build it is. The stamp answers that from the first screenshot.
"""
import re
import sys

import pytest

from tgdatabridge import version


def test_the_title_names_the_product_and_the_build():
    title = version.title()
    assert "TG DataBridge" in title
    assert version.TAGLINE in title
    assert version.BUILD in title
    assert version.BUILD_DATE in title


def test_the_log_banner_names_the_build_and_what_changed():
    banner = version.banner()
    assert version.PRODUCT in banner
    assert version.BUILD in banner
    assert version.BUILD_SUMMARY in banner


def test_the_banner_is_ascii_only():
    """It is written to plain-text log files and echoed by the headless
    CLI. A Windows console in a legacy code page turns a stray em-dash or
    a ™ into a UnicodeEncodeError at exactly the moment somebody is
    reading a log to find out what went wrong."""
    version.banner().encode("ascii")


def test_no_module_hard_codes_the_product_name_in_a_displayed_string():
    """The whole point of the rebrand cleanup: renaming the product again
    should be an edit to version.py, not a hunt through forty modules.

    Only *displayed* strings are checked -- string literals that are not
    docstrings. A docstring naming the product is documentation for
    whoever is reading the file, costs nothing at a rename, and including
    them would make this test so noisy it would be deleted rather than
    obeyed. The vendor name is not checked at all: that is the company,
    not the product, and it is what the dashboard watermark says.
    """
    import ast
    import pathlib

    root = pathlib.Path(version.__file__).parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if "plsql_grammar" in path.parts or path.name == "version.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings
                    and ("TG DataBridge" in node.value
                         or version.LEGACY_PRODUCT in node.value)):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == [], f"product name hard-coded outside version.py: {offenders}"


def test_the_build_is_a_round_number_matching_whats_fixed():
    assert re.fullmatch(r"R\d+", version.BUILD)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", version.BUILD_DATE)


def test_the_window_title_carries_it():
    pytest.importorskip("PySide6", reason="GUI test needs PySide6")
    from PySide6.QtWidgets import QApplication

    from tgdatabridge.gui.main_window import MainWindow

    QApplication.instance() or QApplication([])
    assert version.BUILD in MainWindow().windowTitle()


# ----------------------------------------------------------- one DROP

def test_a_converter_that_emits_its_own_drop_does_not_get_a_second_one():
    """Several converters must emit their own DROP -- a multi-event
    trigger becomes N objects under names _replace_prefix cannot know.
    A second DROP on top is harmless but reads as a bug in a script a DBA
    reviews before running."""
    from tgdatabridge.core import ddl_generator
    from tgdatabridge.core.schema_model import Routine

    routine = Routine(
        name="trg_x", schema="app", kind="TRIGGER", source_engine="MySQL",
        source="BEGIN\n  SET NEW.a = 1;\nEND",
        table_name="t", timing="BEFORE", events=["INSERT"], row_level=True)
    from tgdatabridge.core.plsql_converter import convert_routine
    convert_routine(routine, "MySQL")
    prefix = ddl_generator._replace_prefix(routine, "MySQL")
    assert prefix == ""
    assert routine.converted_source.count("DROP TRIGGER IF EXISTS") == 1


def test_a_converter_with_no_drop_of_its_own_still_gets_one():
    from tgdatabridge.core import ddl_generator
    from tgdatabridge.core.schema_model import Routine

    routine = Routine(name="p", schema="app", kind="PROCEDURE",
                      source="PROCEDURE p IS\nBEGIN\n  NULL;\nEND;")
    from tgdatabridge.core.plsql_converter import convert_routine
    convert_routine(routine, "MySQL")
    assert "DROP PROCEDURE IF EXISTS" in ddl_generator._replace_prefix(routine, "MySQL")


# --------------------------------------------- finding the app's own assets

def test_the_assets_are_found_in_a_pre_rebrand_frozen_layout(tmp_path, monkeypatch):
    """_internal is 208 MB and is not re-sent with every build, so the copy
    on the user's machine still has its data files under the package's OLD
    directory name. Both call sites tolerate a missing file, so getting
    this wrong costs the window icon, the dashboard watermark and the
    entire stylesheet -- and says nothing at all about it."""
    from tgdatabridge.utils import resources

    internal = tmp_path / "_internal"
    (internal / resources._LEGACY_PACKAGE_DIR_NAME / "assets").mkdir(parents=True)
    (internal / resources._LEGACY_PACKAGE_DIR_NAME / "gui").mkdir()
    (internal / resources._LEGACY_PACKAGE_DIR_NAME / "gui" / "style.qss").write_text("QWidget{}", encoding="utf-8")

    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    resources.reset_cache()
    try:
        assert resources.package_root() == internal / resources._LEGACY_PACKAGE_DIR_NAME
        assert resources.stylesheet_path().read_text(encoding="utf-8") == "QWidget{}"
    finally:
        resources.reset_cache()


def test_a_build_whose_internal_matches_the_new_name_is_preferred(tmp_path, monkeypatch):
    from tgdatabridge.utils import resources

    internal = tmp_path / "_internal"
    for name in (resources._PACKAGE_DIR_NAME, resources._LEGACY_PACKAGE_DIR_NAME):
        (internal / name / "assets").mkdir(parents=True)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    resources.reset_cache()
    try:
        assert resources.package_root() == internal / resources._PACKAGE_DIR_NAME
    finally:
        resources.reset_cache()


def test_running_from_source_finds_the_real_assets_directory():
    from tgdatabridge.utils import resources

    resources.reset_cache()
    assert resources.assets_dir().is_dir()
    assert (resources.assets_dir() / "logo_256.png").exists()
    assert resources.stylesheet_path().exists()
    assert "NOT FOUND" not in resources.describe()


def test_nothing_found_still_returns_a_path_rather_than_raising(tmp_path, monkeypatch):
    """Startup must not die because an asset folder is missing."""
    from tgdatabridge.utils import resources

    monkeypatch.setattr(resources, "_candidates", lambda: [tmp_path / "nope"])
    resources.reset_cache()
    try:
        assert resources.package_root() == tmp_path / "nope"
        assert "NOT FOUND" in resources.describe()
    finally:
        resources.reset_cache()
