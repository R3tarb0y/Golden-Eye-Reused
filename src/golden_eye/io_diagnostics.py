from __future__ import annotations

import ctypes
import os
import socket
import time
from pathlib import Path
from typing import Any


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 1)


def resolve_unc_path(value: str | os.PathLike[str]) -> str:
    """Best-effort conversion of a mapped Windows drive to its UNC path."""
    raw = str(value or "").strip()
    if not raw:
        return raw
    if raw.startswith("\\\\"):
        return raw
    if os.name != "nt":
        return raw
    drive = Path(raw).drive
    if not drive or len(drive) < 2 or drive[1] != ":":
        return raw
    try:
        # WNetGetConnectionW expects e.g. "I:" and returns \\server\share.
        mpr = ctypes.windll.mpr
        size = ctypes.c_ulong(2048)
        buf = ctypes.create_unicode_buffer(size.value)
        rc = mpr.WNetGetConnectionW(ctypes.c_wchar_p(drive), buf, ctypes.byref(size))
        if rc == 0 and buf.value:
            suffix = raw[len(drive):].lstrip("\\/")
            base = buf.value.rstrip("\\/")
            return base + ("\\" + suffix if suffix else "")
    except Exception:
        pass
    return raw


def network_host_from_path(value: str | os.PathLike[str]) -> str | None:
    resolved = resolve_unc_path(value)
    if not resolved.startswith("\\\\"):
        return None
    parts = resolved.lstrip("\\").split("\\", 1)
    return parts[0].strip() or None


def tcp_probe(host: str | None, port: int, timeout: float = 3.0) -> dict[str, Any]:
    if not host:
        return {"ok": None, "host": None, "port": int(port), "latency_ms": None, "error": "host tidak dapat ditentukan"}
    started = time.perf_counter()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            pass
        return {"ok": True, "host": host, "port": int(port), "latency_ms": _ms(time.perf_counter() - started), "error": None}
    except Exception as exc:
        return {"ok": False, "host": host, "port": int(port), "latency_ms": _ms(time.perf_counter() - started), "error": str(exc)[:240]}


def probe_nas_root(
    root_value: str | os.PathLike[str],
    *,
    sample_entries: int = 200,
    read_bytes: int = 0,
) -> dict[str, Any]:
    """Read-only NAS probe. It never modifies a file or directory."""
    raw = str(root_value)
    resolved = resolve_unc_path(raw)
    host = network_host_from_path(raw)
    out: dict[str, Any] = {
        "path": raw,
        "resolved_path": resolved,
        "server": host,
        "smb": tcp_probe(host, 445) if host else {"ok": None, "host": None, "port": 445, "latency_ms": None, "error": "local/mapped server unresolved"},
        "exists": False,
        "exists_latency_ms": None,
        "scandir_latency_ms": None,
        "sample_entries": 0,
        "sample_rate_per_sec": None,
        "sample_file": None,
        "stat_latency_ms": None,
        "read_latency_ms": None,
        "read_mb_s": None,
        "error": None,
    }
    path = Path(raw)
    started = time.perf_counter()
    try:
        exists = path.exists()
        out["exists"] = bool(exists)
        out["exists_latency_ms"] = _ms(time.perf_counter() - started)
        if not exists:
            out["error"] = "path tidak tersedia"
            return out
    except Exception as exc:
        out["exists_latency_ms"] = _ms(time.perf_counter() - started)
        out["error"] = f"exists failed: {exc}"[:240]
        return out

    first_file: Path | None = None
    count = 0
    started = time.perf_counter()
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                count += 1
                if first_file is None:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            first_file = Path(entry.path)
                    except OSError:
                        pass
                if count >= max(1, int(sample_entries)):
                    break
        elapsed = time.perf_counter() - started
        out["scandir_latency_ms"] = _ms(elapsed)
        out["sample_entries"] = count
        out["sample_rate_per_sec"] = round(count / elapsed, 1) if elapsed > 0 else None
    except Exception as exc:
        out["scandir_latency_ms"] = _ms(time.perf_counter() - started)
        out["error"] = f"scandir failed: {exc}"[:240]
        return out

    if first_file is not None:
        out["sample_file"] = first_file.name
        started = time.perf_counter()
        try:
            first_file.stat()
            out["stat_latency_ms"] = _ms(time.perf_counter() - started)
        except Exception as exc:
            out["stat_latency_ms"] = _ms(time.perf_counter() - started)
            out["error"] = f"stat failed: {exc}"[:240]
            return out
        if read_bytes > 0:
            started = time.perf_counter()
            try:
                with first_file.open("rb") as fh:
                    payload = fh.read(int(read_bytes))
                elapsed = time.perf_counter() - started
                out["read_latency_ms"] = _ms(elapsed)
                mb = len(payload) / (1024.0 * 1024.0)
                out["read_mb_s"] = round(mb / elapsed, 2) if elapsed > 0 else None
            except Exception as exc:
                out["read_latency_ms"] = _ms(time.perf_counter() - started)
                out["error"] = f"read failed: {exc}"[:240]

    # Classification is intentionally conservative: this is diagnostic, not a hard gate.
    rate = out.get("sample_rate_per_sec")
    exists_ms = out.get("exists_latency_ms")
    smb_ms = (out.get("smb") or {}).get("latency_ms")
    if out.get("error"):
        out["status"] = "FAILED"
    elif (out.get("smb") or {}).get("ok") is False:
        out["status"] = "FAILED"
    elif (exists_ms is not None and exists_ms > 1500) or (smb_ms is not None and smb_ms > 800) or (rate is not None and rate < 250):
        out["status"] = "SLOW"
    else:
        out["status"] = "READY"
    return out
