"""Headless/CLI mode for TG DataBridge
(ENTERPRISE_READINESS.md section 5, "Operability & automation"). Lets a
script or CI/CD pipeline drive Load Schema -> Convert -> (optionally)
Apply DDL / Migrate Data using a config file instead of GUI clicks --
built entirely on the same tgdatabridge.core.* modules the GUI uses (see
tgdatabridge/gui/main_window.py), which were already GUI-independent.

Nothing under tgdatabridge/cli/ may import PySide6 or anything from tgdatabridge.gui --
the entire point of a headless mode is that it works on a machine with no
Qt installed at all (a CI runner, a bare Linux server)."""
