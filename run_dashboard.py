from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
DASHBOARD = ROOT / "dashboard"
load_dotenv(ROOT / ".env")
os.chdir(DASHBOARD)
sys.path.insert(0, str(DASHBOARD))
runpy.run_path(str(DASHBOARD / "app.py"), run_name="__main__")
