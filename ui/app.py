# FIX: This file previously duplicated the App/MainWindow class that already
# lives in ui/main_window.py, creating two conflicting entry-points with
# different application_id init paths.
# Now it simply re-exports from the canonical location so any old import
# (e.g. `from ui.app import LauncherApp`) still works without double-init.

from ui.main_window import App as LauncherApp, main  # noqa: F401
