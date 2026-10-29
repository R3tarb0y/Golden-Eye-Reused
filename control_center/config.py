from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _csv(name: str, default: str = "") -> list[str]:
    return [v.strip().upper() for v in os.getenv(name, default).split(",") if v.strip()]


@dataclass(frozen=True)
class ControlConfig:
    host: str = os.getenv("CONTROL_CENTER_HOST", "127.0.0.1")
    port: int = int(os.getenv("CONTROL_CENTER_PORT", "8060"))
    worker_poll_seconds: float = float(os.getenv("CONTROL_WORKER_POLL_SECONDS", "3"))
    worker_heartbeat_seconds: float = float(os.getenv("CONTROL_WORKER_HEARTBEAT_SECONDS", "5"))
    resume_stale_seconds: int = int(os.getenv("CONTROL_RESUME_STALE_SECONDS", "90"))
    python_exe: str = (os.getenv("CONTROL_PYTHON_EXE", "").strip() or sys.executable or "py")
    log_dir: Path = Path(os.getenv("CONTROL_LOG_DIR", str(ROOT / "control_logs"))).resolve()
    report_dir: Path = Path(os.getenv("CONTROL_REPORT_OUTPUT_DIR", str(ROOT / "control_reports"))).resolve()
    default_companies: tuple[str, ...] = tuple(_csv("CONTROL_DEFAULT_COMPANIES", "PWP,DIN,DAN,DWT"))
    demo: bool = _bool("CONTROL_CENTER_DEMO", False)
    auto_resume_stale: bool = _bool("CONTROL_AUTO_RESUME_STALE", False)

    def panen_roots(self, company: str) -> list[str]:
        value = os.getenv(f"CONTROL_{company.upper()}_PANEN_ROOT", "").strip()
        return [v.strip() for v in value.split(";") if v.strip()]

    def transport_roots(self, company: str) -> list[str]:
        value = os.getenv(f"CONTROL_{company.upper()}_TRANSPORT_ROOT", "").strip()
        return [v.strip() for v in value.split(";") if v.strip()]


CONFIG = ControlConfig()
