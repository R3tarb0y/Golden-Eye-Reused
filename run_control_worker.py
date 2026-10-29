from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


_configure_utf8_stdio()
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from control_center.worker import run_worker  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run_worker())
