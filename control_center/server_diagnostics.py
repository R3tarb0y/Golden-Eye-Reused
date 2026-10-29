from __future__ import annotations

import os
import socket
import time
from typing import Any

from golden_eye.config import Settings
from golden_eye.db import connection
from golden_eye.io_diagnostics import probe_nas_root, tcp_probe

from .config import CONFIG

SETTINGS = Settings()


def _postgres_probe() -> dict[str, Any]:
    result: dict[str, Any] = {
        "host": SETTINGS.pg_host,
        "port": SETTINGS.pg_port,
        "tcp": tcp_probe(SETTINGS.pg_host, SETTINGS.pg_port),
        "query_latency_ms": None,
        "active_sessions": None,
        "waiting_sessions": None,
        "lock_waiters": [],
        "long_queries": [],
        "status": "FAILED",
        "error": None,
    }
    started = time.perf_counter()
    try:
        with connection(SETTINGS) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
            result["query_latency_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
            cur.execute(
                """
                SELECT
                    count(*) FILTER (WHERE state <> 'idle') AS active_sessions,
                    count(*) FILTER (WHERE wait_event IS NOT NULL AND state <> 'idle') AS waiting_sessions
                FROM pg_stat_activity
                WHERE datname = current_database()
                """
            )
            row = cur.fetchone() or (0, 0)
            result["active_sessions"] = int(row[0] or 0)
            result["waiting_sessions"] = int(row[1] or 0)
            cur.execute(
                """
                SELECT pid, state, wait_event_type, wait_event,
                       round(extract(epoch from (now()-query_start))::numeric, 1) AS seconds,
                       left(regexp_replace(query, '[\\n\\r\\t]+', ' ', 'g'), 180)
                FROM pg_stat_activity
                WHERE datname=current_database()
                  AND pid <> pg_backend_pid()
                  AND state <> 'idle'
                  AND (wait_event_type='Lock' OR now()-query_start > interval '60 seconds')
                ORDER BY query_start
                LIMIT 12
                """
            )
            rows = cur.fetchall()
            for pid, state, wet, we, seconds, query in rows:
                item = {
                    "pid": int(pid), "state": state, "wait_event_type": wet,
                    "wait_event": we, "seconds": float(seconds or 0), "query": query,
                }
                if wet == "Lock":
                    result["lock_waiters"].append(item)
                else:
                    result["long_queries"].append(item)
        latency = float(result["query_latency_ms"] or 0)
        longest = max((float(x.get("seconds") or 0) for x in result["long_queries"]), default=0.0)
        result["longest_query_seconds"] = longest
        if result["lock_waiters"]:
            result["status"] = "WARNING"
        elif longest >= 300:
            # V182: connectivity may be healthy while the database is busy on a
            # pathological/very long pipeline query. Surface that distinction.
            result["status"] = "WARNING"
        elif latency > 1000:
            result["status"] = "SLOW"
        else:
            result["status"] = "READY"
    except Exception as exc:
        result["query_latency_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        result["error"] = str(exc)[:400]
    return result


def run_silver_diagnostics(company: str = "PWP", kind: str = "PANEN") -> dict[str, Any]:
    company = str(company or "PWP").upper().strip()
    kind = str(kind or "PANEN").upper().strip()
    if company not in {"PWP", "DIN", "DAN", "DWT"}:
        raise ValueError(f"Company tidak dikenal: {company}")
    if kind not in {"PANEN", "TRANSPORT"}:
        raise ValueError("kind harus PANEN atau TRANSPORT")
    roots = CONFIG.panen_roots(company) if kind == "PANEN" else CONFIG.transport_roots(company)
    if not roots:
        raise ValueError(f"Root {company} {kind} belum dikonfigurasi")
    nas = [probe_nas_root(root, sample_entries=250, read_bytes=1024 * 1024) for root in roots]
    pg = _postgres_probe()
    nas_failed = any(x.get("status") == "FAILED" for x in nas)
    nas_slow = any(x.get("status") == "SLOW" for x in nas)
    if nas_failed or pg.get("status") == "FAILED":
        overall = "FAILED"
    elif nas_slow or pg.get("status") in {"SLOW", "WARNING"}:
        overall = "WARNING"
    else:
        overall = "READY"
    reason = []
    if nas_failed:
        reason.append("NAS tidak dapat diakses")
    elif nas_slow:
        reason.append("enumerasi NAS lambat")
    if pg.get("lock_waiters"):
        reason.append(f"PostgreSQL memiliki {len(pg['lock_waiters'])} lock waiter")
    elif float(pg.get("longest_query_seconds") or 0) >= 300:
        mins = float(pg.get("longest_query_seconds") or 0) / 60.0
        reason.append(f"PostgreSQL BUSY: query pipeline berjalan {mins:.1f} menit")
    elif pg.get("status") == "SLOW":
        reason.append("latensi PostgreSQL tinggi")
    if not reason:
        reason.append("connection server terlihat normal")
    return {
        "overall": overall,
        "company": company,
        "kind": kind,
        "hostname": socket.gethostname(),
        "postgres": pg,
        "nas": nas,
        "reason": "; ".join(reason),
    }
