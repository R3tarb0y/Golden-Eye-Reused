from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _configure_utf8_stdio() -> None:
    # Windows PowerShell/Scheduled Task may default to cp1252. Golden Eye logs
    # contain Unicode labels, so make CLI output deterministic and non-fatal.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


_configure_utf8_stdio()
sys.path.insert(0, str(ROOT / "src"))

from golden_eye.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())

