from __future__ import annotations

import json
import socket
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from psycopg2.extras import RealDictCursor

from golden_eye.config import Settings
from golden_eye.db import connection

from .config import ROOT
from .pipeline import build_plan, normalize_companies, validate_month

SETTINGS = Settings()


@contextmanager
def worker_singleton_lock():
    """Allow only one active Control Center worker for this Golden Eye DB.

    Session advisory lock is released automatically if the worker process or
    PostgreSQL connection dies. This prevents a manual launcher and Windows
    Scheduled Task from consuming the same queue concurrently.
    """
    lock_name = "golden_eye_control_center:worker_singleton"
    with connection(SETTINGS, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)::bigint)",
            (lock_name,),
        )
        acquired = bool(cur.fetchone()[0])
        try:
            yield acquired
        finally:
            if acquired:
                cur.execute(
                    "SELECT pg_advisory_unlock(hashtext(%s)::bigint)",
                    (lock_name,),
                )


def bootstrap() -> None:
    scripts = [
        ROOT / "sql" / "018_control_center.sql",
        ROOT / "sql" / "019_control_center_simple_rerun.sql",
    ]
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        for path in scripts:
            if path.exists():
                cur.execute(path.read_text(encoding="utf-8"))


def _dicts(cur) -> list[dict[str, Any]]:
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def serialize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_jsonable(row) for row in rows]



def reconcile_scope_for_new_run(
    pipeline_name: str,
    target_month: str,
    companies: list[str],
    *,
    live_seconds: int = 30,
) -> dict[str, Any]:
    """Clear stale DB state before a deliberate new run.

    A same-scope job blocks creation only when a live worker heartbeat points to
    that exact job. Orphan PENDING/RUNNING jobs and PAUSED jobs are preserved in
    history as INTERRUPTED so they cannot leave a permanent scope lock.
    """
    month = validate_month(target_month)
    companies = normalize_companies(companies)
    pipeline_name = str(pipeline_name or "MONTHLY_FULL").upper().strip()
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT j.job_id, j.status,
                   EXISTS (
                       SELECT 1
                       FROM control.worker_state w
                       WHERE w.current_job_id=j.job_id
                         AND w.status NOT IN ('OFFLINE','ERROR')
                         AND w.heartbeat_at >= now() - (%s * interval '1 second')
                   ) AS live_worker
            FROM control.pipeline_job j
            WHERE j.pipeline_name=%s AND j.target_month=%s AND j.companies=%s
              AND j.status IN ('PENDING','RUNNING','PAUSED')
            ORDER BY j.created_at DESC
            FOR UPDATE
            """,
            (int(live_seconds), pipeline_name, month, companies),
        )
        rows = cur.fetchall()
        blockers = [str(row[0]) for row in rows if bool(row[2])]
        if blockers:
            return {"blocked": True, "blockers": blockers, "interrupted": []}

        interrupted: list[str] = []
        for job_id, status, _live in rows:
            cur.execute(
                """
                UPDATE control.pipeline_job_step
                SET status=CASE WHEN status='RUNNING' THEN 'FAILED' ELSE status END,
                    finished_at=CASE WHEN status='RUNNING' THEN now() ELSE finished_at END,
                    error_message=CASE
                        WHEN status='RUNNING' THEN 'Interrupted/superseded before a new deliberate run.'
                        ELSE error_message
                    END
                WHERE job_id=%s
                """,
                (job_id,),
            )
            cur.execute(
                """
                UPDATE control.pipeline_job
                SET status='INTERRUPTED', finished_at=now(), heartbeat_at=NULL,
                    current_step=NULL, cancel_requested=false,
                    error_message='Superseded by a new deliberate run; no live worker owned this job.'
                WHERE job_id=%s
                """,
                (job_id,),
            )
            interrupted.append(str(job_id))
        return {"blocked": False, "blockers": [], "interrupted": interrupted}


def interrupt_orphan_jobs(*, live_seconds: int = 30) -> int:
    """Mark orphan PENDING/RUNNING/PAUSED jobs INTERRUPTED without deleting history."""
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT j.job_id
            FROM control.pipeline_job j
            WHERE j.status IN ('PENDING','RUNNING','PAUSED')
              AND NOT EXISTS (
                  SELECT 1 FROM control.worker_state w
                  WHERE w.current_job_id=j.job_id
                    AND w.status NOT IN ('OFFLINE','ERROR')
                    AND w.heartbeat_at >= now() - (%s * interval '1 second')
              )
            FOR UPDATE
            """,
            (int(live_seconds),),
        )
        ids = [row[0] for row in cur.fetchall()]
        for job_id in ids:
            cur.execute(
                """
                UPDATE control.pipeline_job_step
                SET status=CASE WHEN status='RUNNING' THEN 'FAILED' ELSE status END,
                    finished_at=CASE WHEN status='RUNNING' THEN now() ELSE finished_at END,
                    error_message=CASE WHEN status='RUNNING'
                        THEN 'Interrupted because no live Control Worker owns this job.'
                        ELSE error_message END
                WHERE job_id=%s
                """,
                (job_id,),
            )
            cur.execute(
                """
                UPDATE control.pipeline_job
                SET status='INTERRUPTED', finished_at=now(), heartbeat_at=NULL,
                    current_step=NULL, cancel_requested=false,
                    error_message='Orphan control state cleared; safe to create a new run.'
                WHERE job_id=%s
                """,
                (job_id,),
            )
        return len(ids)


def create_job(
    pipeline_name: str,
    target_month: str,
    companies: list[str],
    *,
    requested_by: str = "web",
    request_source: str = "WEB",
    retry_of: str | None = None,
    retry_from_order: int | None = None,
) -> str:
    month = validate_month(target_month)
    companies = normalize_companies(companies)
    pipeline_name = str(pipeline_name or "MONTHLY_FULL").upper().strip()
    reconciliation = reconcile_scope_for_new_run(pipeline_name, month, companies)
    if reconciliation.get("blocked"):
        raise RuntimeError(
            "Job scope ini benar-benar sedang dieksekusi oleh Control Worker aktif: "
            + ", ".join(reconciliation.get("blockers") or [])
        )
    plan = build_plan(pipeline_name, month, companies)
    job_id = uuid.uuid4()
    metadata = {"retry_from_order": retry_from_order} if retry_from_order else {}
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO control.pipeline_job
                (job_id, pipeline_name, target_month, companies, requested_by,
                 request_source, retry_of, metadata)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            """,
            (
                job_id, pipeline_name, month, companies, requested_by,
                request_source, uuid.UUID(retry_of) if retry_of else None,
                json.dumps(metadata),
            ),
        )
        for order, step in enumerate(plan, start=1):
            status = "SKIPPED" if retry_from_order and order < retry_from_order else "WAITING"
            cur.execute(
                """
                INSERT INTO control.pipeline_job_step
                    (job_id, step_order, step_key, step_name, company, status,
                     command_text, metadata, resume_supported)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                """,
                (
                    job_id, order, step.key, step.name, step.company, status,
                    step.command_text, json.dumps(step.metadata),
                    bool(step.metadata.get("resume_mode")),
                ),
            )
    return str(job_id)


def list_jobs(limit: int = 30) -> list[dict[str, Any]]:
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT j.*,
                   count(s.step_id) AS step_count,
                   count(s.step_id) FILTER (WHERE s.status='SUCCESS') AS success_steps,
                   count(s.step_id) FILTER (WHERE s.status='FAILED') AS failed_steps
            FROM control.pipeline_job j
            LEFT JOIN control.pipeline_job_step s ON s.job_id=j.job_id
            GROUP BY j.job_id
            ORDER BY j.created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return serialize_rows(_dicts(cur))


def job_detail(job_id: str) -> dict[str, Any] | None:
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM control.pipeline_job WHERE job_id=%s", (uuid.UUID(job_id),))
        job_rows = _dicts(cur)
        if not job_rows:
            return None
        cur.execute(
            "SELECT * FROM control.pipeline_job_step WHERE job_id=%s ORDER BY step_order",
            (uuid.UUID(job_id),),
        )
        job = serialize_rows(job_rows)[0]
        job["steps"] = serialize_rows(_dicts(cur))
        return job


def claim_next_job(worker_id: str) -> dict[str, Any] | None:
    with connection(SETTINGS) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM control.pipeline_job
            WHERE status='PENDING'
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """
        )
        row = cur.fetchone()
        if not row:
            return None
        job_id = row["job_id"]
        cur.execute(
            """
            UPDATE control.pipeline_job
            SET status='RUNNING', started_at=COALESCE(started_at, now()), heartbeat_at=now()
            WHERE job_id=%s
            """,
            (job_id,),
        )
        return _jsonable(dict(row))


def waiting_steps(job_id: str) -> list[dict[str, Any]]:
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM control.pipeline_job_step
            WHERE job_id=%s AND status='WAITING'
            ORDER BY step_order
            """,
            (uuid.UUID(job_id),),
        )
        return serialize_rows(_dicts(cur))


def set_step_running(job_id: str, step_id: int, step_name: str, log_path: str) -> None:
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE control.pipeline_job_step SET status='RUNNING', started_at=now(), log_path=%s WHERE step_id=%s",
            (log_path, step_id),
        )
        cur.execute(
            "UPDATE control.pipeline_job SET current_step=%s, heartbeat_at=now() WHERE job_id=%s",
            (step_name, uuid.UUID(job_id)),
        )


def update_step_tail(step_id: int, tail: str) -> None:
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE control.pipeline_job_step SET log_tail=%s WHERE step_id=%s",
            (tail[-12000:], step_id),
        )


def update_step_checkpoint(
    step_id: int,
    *,
    golden_run_id: str | None = None,
    checkpoint_stage: str | None = None,
    raw_complete: bool | None = None,
    silver_complete: bool | None = None,
    checkpoint_detail: str | None = None,
) -> None:
    sets = ["last_checkpoint_at=now()"]
    params: list[Any] = []
    if golden_run_id is not None:
        sets.append("golden_run_id=%s")
        params.append(uuid.UUID(str(golden_run_id)))
    if checkpoint_stage is not None:
        sets.append("checkpoint_stage=%s")
        params.append(str(checkpoint_stage).upper())
    if raw_complete is not None:
        sets.append("raw_complete=%s")
        params.append(bool(raw_complete))
    if silver_complete is not None:
        sets.append("silver_complete=%s")
        params.append(bool(silver_complete))
    if checkpoint_detail is not None:
        sets.append("checkpoint_detail=%s")
        params.append(str(checkpoint_detail)[:1000])
    params.append(int(step_id))
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE control.pipeline_job_step SET {', '.join(sets)} WHERE step_id=%s",
            tuple(params),
        )


def finish_step(step_id: int, success: bool, return_code: int, tail: str, error: str | None = None) -> None:
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE control.pipeline_job_step
            SET status=%s, finished_at=now(), return_code=%s, log_tail=%s, error_message=%s
            WHERE step_id=%s
            """,
            ("SUCCESS" if success else "FAILED", return_code, tail[-12000:], error, step_id),
        )


def update_job_progress(job_id: str) -> None:
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE status IN ('SUCCESS','SKIPPED')) AS done
            FROM control.pipeline_job_step WHERE job_id=%s
            """,
            (uuid.UUID(job_id),),
        )
        total, done = cur.fetchone()
        pct = (100.0 * done / total) if total else 0.0
        cur.execute(
            "UPDATE control.pipeline_job SET progress_pct=%s, heartbeat_at=now() WHERE job_id=%s",
            (pct, uuid.UUID(job_id)),
        )


def finish_job(job_id: str, status: str, error: str | None = None) -> None:
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE control.pipeline_job
            SET status=%s, progress_pct=CASE WHEN %s='SUCCESS' THEN 100 ELSE progress_pct END,
                finished_at=now(), heartbeat_at=now(), error_message=%s,
                current_step=NULL
            WHERE job_id=%s
            """,
            (status, status, error, uuid.UUID(job_id)),
        )


def cancel_requested(job_id: str) -> bool:
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute("SELECT cancel_requested FROM control.pipeline_job WHERE job_id=%s", (uuid.UUID(job_id),))
        row = cur.fetchone()
        return bool(row and row[0])


def request_cancel(job_id: str) -> None:
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute("UPDATE control.pipeline_job SET cancel_requested=true WHERE job_id=%s", (uuid.UUID(job_id),))


def resume_failed_job(job_id: str) -> dict[str, Any]:
    """Requeue job yang sama dari step FAILED tanpa membuang checkpoint internal."""
    detail = job_detail(job_id)
    if not detail:
        raise ValueError("Job tidak ditemukan")
    failed = [s for s in detail["steps"] if s["status"] == "FAILED"]
    if not failed:
        raise ValueError("Tidak ada step FAILED untuk dilanjutkan")
    first_order = min(int(s["step_order"]) for s in failed)
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE control.pipeline_job_step
            SET status='WAITING', finished_at=NULL, return_code=NULL,
                error_message=NULL, attempt=attempt+1, resume_count=resume_count+1
            WHERE job_id=%s AND step_order=%s AND status='FAILED'
            """,
            (uuid.UUID(job_id), first_order),
        )
        cur.execute(
            """
            UPDATE control.pipeline_job
            SET status='PENDING', finished_at=NULL, heartbeat_at=NULL,
                error_message=NULL, cancel_requested=false,
                current_step='RESUME QUEUED'
            WHERE job_id=%s
            """,
            (uuid.UUID(job_id),),
        )
    refreshed = job_detail(job_id) or {}
    step = next((s for s in refreshed.get("steps", []) if int(s["step_order"]) == first_order), {})
    return {
        "job_id": job_id,
        "step_order": first_order,
        "checkpoint_stage": step.get("checkpoint_stage"),
        "golden_run_id": step.get("golden_run_id"),
        "silver_complete": step.get("silver_complete"),
        "mode": "CHECKPOINT" if step.get("golden_run_id") or step.get("silver_complete") else "STEP_RESTART",
    }


def retry_failed_job(job_id: str, requested_by: str = "web") -> str:
    detail = job_detail(job_id)
    if not detail:
        raise ValueError("Job tidak ditemukan")
    failed = [s for s in detail["steps"] if s["status"] == "FAILED"]
    if not failed:
        raise ValueError("Tidak ada step FAILED untuk di-retry")
    first_failed = min(int(s["step_order"]) for s in failed)
    return create_job(
        detail["pipeline_name"], detail["target_month"], detail["companies"],
        requested_by=requested_by, request_source="RETRY", retry_of=job_id,
        retry_from_order=first_failed,
    )


def get_schedule() -> dict[str, Any]:
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM control.pipeline_schedule WHERE schedule_id=1")
        rows = _dicts(cur)
        return serialize_rows(rows)[0]


def update_schedule(payload: dict[str, Any]) -> dict[str, Any]:
    companies = normalize_companies(payload.get("companies") or ["PWP", "DIN", "DAN", "DWT"])
    day = max(1, min(28, int(payload.get("run_day", 5))))
    hour = max(0, min(23, int(payload.get("run_hour", 2))))
    minute = max(0, min(59, int(payload.get("run_minute", 0))))
    enabled = bool(payload.get("enabled"))
    pipeline_name = str(payload.get("pipeline_name") or "MONTHLY_FULL").upper()
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE control.pipeline_schedule
            SET enabled=%s, pipeline_name=%s, companies=%s,
                run_day=%s, run_hour=%s, run_minute=%s, updated_at=now()
            WHERE schedule_id=1
            """,
            (enabled, pipeline_name, companies, day, hour, minute),
        )
    return get_schedule()



def job_heartbeat(job_id: str) -> None:
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE control.pipeline_job SET heartbeat_at=now() WHERE job_id=%s AND status='RUNNING'",
            (uuid.UUID(job_id),),
        )


def pause_job(job_id: str) -> dict[str, Any]:
    """Park a job without discarding internal Golden Eye checkpoints."""
    jid = uuid.UUID(job_id)
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE control.pipeline_job_step
            SET status='WAITING', finished_at=NULL, return_code=NULL,
                error_message='Paused manually; checkpoint preserved.'
            WHERE job_id=%s AND status='RUNNING'
            """,
            (jid,),
        )
        cur.execute(
            """
            UPDATE control.pipeline_job
            SET status='PAUSED', heartbeat_at=NULL, finished_at=NULL,
                current_step='PAUSED MANUALLY',
                error_message='Execution paused; checkpoint preserved. Resume explicitly from Control Center.'
            WHERE job_id=%s AND status IN ('RUNNING','PENDING','FAILED')
            """,
            (jid,),
        )
    return job_detail(job_id) or {}


def resume_paused_job(job_id: str) -> dict[str, Any]:
    jid = uuid.UUID(job_id)
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE control.pipeline_job
            SET status='PENDING', heartbeat_at=NULL, finished_at=NULL,
                cancel_requested=false, current_step='RESUME QUEUED', error_message=NULL
            WHERE job_id=%s AND status='PAUSED'
            """,
            (jid,),
        )
        if cur.rowcount == 0:
            raise ValueError('Job bukan PAUSED atau tidak ditemukan')
    return job_detail(job_id) or {}


def pause_active_jobs() -> int:
    """Park all active jobs after OS processes have been stopped."""
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT job_id FROM control.pipeline_job
            WHERE status IN ('RUNNING','PENDING')
            FOR UPDATE
            """
        )
        ids = [row[0] for row in cur.fetchall()]
        for jid in ids:
            cur.execute(
                """
                UPDATE control.pipeline_job_step
                SET status='WAITING', finished_at=NULL, return_code=NULL,
                    error_message='Paused manually; checkpoint preserved.'
                WHERE job_id=%s AND status='RUNNING'
                """,
                (jid,),
            )
            cur.execute(
                """
                UPDATE control.pipeline_job
                SET status='PAUSED', heartbeat_at=NULL, finished_at=NULL,
                    current_step='PAUSED MANUALLY',
                    error_message='Execution paused; checkpoint preserved.'
                WHERE job_id=%s
                """,
                (jid,),
            )
        return len(ids)


def park_stale_jobs(stale_seconds: int = 300) -> int:
    """Convert orphan RUNNING jobs to PAUSED instead of auto-restarting them."""
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT job_id FROM control.pipeline_job
            WHERE status='RUNNING'
              AND COALESCE(heartbeat_at, started_at, created_at) < now() - (%s * interval '1 second')
            FOR UPDATE
            """,
            (int(stale_seconds),),
        )
        ids = [row[0] for row in cur.fetchall()]
        for jid in ids:
            cur.execute(
                """
                UPDATE control.pipeline_job_step
                SET status='WAITING', finished_at=NULL, return_code=NULL,
                    error_message='Worker heartbeat hilang; job diparkir PAUSED, checkpoint dipertahankan.'
                WHERE job_id=%s AND status='RUNNING'
                """,
                (jid,),
            )
            cur.execute(
                """
                UPDATE control.pipeline_job
                SET status='PAUSED', heartbeat_at=NULL, finished_at=NULL,
                    current_step='PAUSED - WORKER LOST',
                    error_message='Worker heartbeat hilang; resume harus dilakukan secara eksplisit.'
                WHERE job_id=%s
                """,
                (jid,),
            )
        return len(ids)


def recover_stale_jobs(stale_seconds: int = 300) -> int:
    """Requeue orphan RUNNING jobs so worker baru melanjutkan checkpoint terakhir.

    Step tidak dibuat ulang dan job_id tetap sama. Untuk merge-month, command resume
    ditentukan dari raw_complete/silver_complete/golden_run_id/checkpoint_stage.
    """
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT job_id FROM control.pipeline_job
            WHERE status='RUNNING'
              AND cancel_requested=false
              AND COALESCE(heartbeat_at, started_at, created_at) < now() - (%s * interval '1 second')
            FOR UPDATE
            """,
            (int(stale_seconds),),
        )
        ids = [row[0] for row in cur.fetchall()]
        for job_id in ids:
            cur.execute(
                """
                UPDATE control.pipeline_job_step
                SET status='WAITING', finished_at=NULL, return_code=NULL,
                    attempt=attempt+1, resume_count=resume_count+1,
                    error_message='Worker heartbeat hilang; AUTO RESUME dari checkpoint terakhir.'
                WHERE job_id=%s AND status='RUNNING'
                """,
                (job_id,),
            )
            cur.execute(
                """
                UPDATE control.pipeline_job
                SET status='PENDING', finished_at=NULL, current_step='AUTO RESUME QUEUED',
                    heartbeat_at=NULL,
                    error_message='Worker heartbeat hilang; job yang sama di-queue untuk resume checkpoint.'
                WHERE job_id=%s
                """,
                (job_id,),
            )
        return len(ids)


def worker_heartbeat(worker_id: str, status: str, pid: int, current_job_id: str | None = None, error: str | None = None) -> None:
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO control.worker_state(worker_id, hostname, pid, status, current_job_id, heartbeat_at, last_error)
            VALUES (%s,%s,%s,%s,%s,now(),%s)
            ON CONFLICT (worker_id) DO UPDATE SET
                hostname=excluded.hostname, pid=excluded.pid, status=excluded.status,
                current_job_id=excluded.current_job_id, heartbeat_at=now(), last_error=excluded.last_error
            """,
            (worker_id, socket.gethostname(), pid, status, uuid.UUID(current_job_id) if current_job_id else None, error),
        )


def worker_states() -> list[dict[str, Any]]:
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT *, EXTRACT(EPOCH FROM (now()-heartbeat_at)) AS heartbeat_age_seconds
            FROM control.worker_state ORDER BY heartbeat_at DESC
            """
        )
        return serialize_rows(_dicts(cur))


def latest_successful_month() -> str | None:
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute("SELECT max(target_month) FROM control.pipeline_job WHERE status='SUCCESS' AND pipeline_name='MONTHLY_FULL'")
        row = cur.fetchone()
        return row[0] if row and row[0] else None


def job_exists_for_month(month: str, pipeline: str, companies: list[str] | None = None) -> bool:
    companies = normalize_companies(companies or ["PWP", "DIN", "DAN", "DWT"])
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS(
                SELECT 1 FROM control.pipeline_job
                WHERE target_month=%s AND pipeline_name=%s AND companies=%s
                  AND status IN ('PENDING','RUNNING','SUCCESS')
            )
            """,
            (month, pipeline, companies),
        )
        return bool(cur.fetchone()[0])


def mark_schedule_enqueued(month: str) -> None:
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute("UPDATE control.pipeline_schedule SET last_enqueued_month=%s, updated_at=now() WHERE schedule_id=1", (month,))
