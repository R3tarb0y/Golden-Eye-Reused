from __future__ import annotations
import sys
from pathlib import Path
from dotenv import load_dotenv
ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from control_center.db import bootstrap, pause_active_jobs
if __name__ == "__main__":
    bootstrap()
    n = pause_active_jobs()
    print(f"[CONTROL] jobs parked PAUSED={n}; checkpoints preserved")
