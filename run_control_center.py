from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from control_center.app import run  # noqa: E402

if __name__ == "__main__":
    run()
