#!/usr/bin/env python3
import sys
import os

# Ensure the launcher root is on sys.path so imports work whether
# this file is executed directly or via a symlink (e.g. from Flatpak /app/bin/).
_script_dir = os.path.dirname(os.path.realpath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from ui.main_window import main

if __name__ == "__main__":
    main()
