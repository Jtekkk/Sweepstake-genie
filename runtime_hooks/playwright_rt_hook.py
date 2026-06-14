"""
PyInstaller runtime hook for playwright-python.

When frozen, playwright looks for its Node.js driver relative to its
package __file__. PyInstaller places package files under sys._MEIPASS,
so the driver ends up at sys._MEIPASS/playwright/driver/ — which is
exactly where playwright expects it. This hook also suppresses Python
runtime warnings that would otherwise appear as Windows dialogs.
"""
import os
import sys
import warnings

# Silence all Python runtime warnings — they have no useful outlet in a
# windowed (no-console) exe and can appear as confusing Windows dialogs.
warnings.filterwarnings("ignore")

if getattr(sys, "frozen", False):
    # Playwright's _driver.py resolves the driver path relative to its own
    # __file__.  When frozen that resolves correctly under _MEIPASS, but we
    # also set PLAYWRIGHT_BROWSERS_PATH so Playwright finds the user's
    # already-installed browser cache (same location as normal installs).
    if sys.platform == "win32":
        browsers_path = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "ms-playwright",
        )
    elif sys.platform == "darwin":
        browsers_path = os.path.join(
            os.path.expanduser("~"), "Library", "Caches", "ms-playwright"
        )
    else:
        browsers_path = os.path.join(
            os.path.expanduser("~"), ".cache", "ms-playwright"
        )

    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", browsers_path)
