"""Single-folder launcher for the Portuguese/English server monitor."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import app_en as _english
import app_pt as _portuguese

_portuguese_main = _portuguese.main


def _saved_language() -> str:
    settings_path = (
        Path(os.environ.get("APPDATA", Path.home()))
        / "LibbysServerMonitor"
        / "settings.json"
    )
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return "pt"
    if isinstance(settings, dict) and settings.get("language") == "en":
        return "en"
    return "pt"


def main() -> None:
    if _saved_language() == "en":
        _english.main()
    else:
        _portuguese_main()


if __name__ == "__main__":
    main()
else:
    # Preserve the original ``import app`` API. Re-exporting names alone would
    # leave function globals bound to app_pt, so existing patches and scripts
    # would no longer affect the functions they import from app.
    _portuguese.main = main
    sys.modules[__name__] = _portuguese
