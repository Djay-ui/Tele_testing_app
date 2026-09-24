"""The orphan-rows control, and the guarantee that it defaults to safety.

Two things matter here and nothing else does. The default must be the
policy that changes no data, because it is applied without asking. And
reading it must never be able to break an apply -- it is consulted at the
moment a migration has already gone wrong, which is the worst possible
time to raise a second exception.
"""
import pytest

pytest.importorskip("PySide6", reason="GUI tests need PySide6 installed")

from PySide6.QtWidgets import QApplication          # noqa: E402

from tgdatabridge.core import fk_recovery as R             # noqa: E402
from tgdatabridge.gui.main_window import MainWindow        # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def window(qt_app):
    return MainWindow()


def test_the_control_exists_and_offers_every_policy(window):
    combo = window.fk_policy_combo
    offered = [combo.itemData(i) for i in range(combo.count())]
    assert set(offered) == set(R.POLICIES)


def test_the_default_is_the_one_that_changes_no_data(window):
    """It is applied without asking, so it must not delete or blank
    anything."""
    assert window._fk_policy() == R.POLICY_NOT_VALID


def test_neither_destructive_policy_is_the_default(window):
    assert window.fk_policy_combo.currentData() not in (
        R.POLICY_DELETE_ORPHANS, R.POLICY_NULL_ORPHANS)


def test_choosing_a_policy_is_what_the_apply_loop_reads(window):
    combo = window.fk_policy_combo
    for index in range(combo.count()):
        combo.setCurrentIndex(index)
        assert window._fk_policy() == combo.itemData(index)
    combo.setCurrentIndex(0)


def test_a_missing_or_broken_combo_falls_back_to_the_safe_default(window):
    """Reading the policy happens on the failure path. It must not be able
    to turn one failed statement into a crashed apply."""
    saved = window.fk_policy_combo

    class Exploding:
        def currentData(self):
            raise RuntimeError("boom")

    try:
        window.fk_policy_combo = Exploding()
        assert window._fk_policy() == R.POLICY_NOT_VALID
        del window.fk_policy_combo
        assert window._fk_policy() == R.POLICY_NOT_VALID
    finally:
        window.fk_policy_combo = saved


def test_an_unrecognised_value_falls_back_rather_than_being_passed_through(window):
    saved = window.fk_policy_combo

    class Nonsense:
        def currentData(self):
            return "drop_the_database"

    try:
        window.fk_policy_combo = Nonsense()
        assert window._fk_policy() == R.POLICY_NOT_VALID
    finally:
        window.fk_policy_combo = saved


def test_the_tooltip_says_what_each_choice_does(window):
    tip = window.fk_policy_combo.toolTip()
    for phrase in ("NOT VALID", "NOVALIDATE", "WITH NOCHECK",
                   "FOREIGN_KEY_CHECKS", "Destructive"):
        assert phrase in tip


def test_the_window_still_fits_a_laptop_screen(window):
    """The toolbar gained a label and a combo; the fix for "the connect
    button is below the taskbar" must survive that."""
    assert window.minimumSizeHint().height() <= 768
