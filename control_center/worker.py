from __future__ import annotations

import os
import re
import signal
import subprocess
import time
import uuid
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

from .config import CONFIG, ROOT
from .db import (
    bootstrap,
    cancel_requested,
    claim_next_job,
    create_job,
    finish_job,
    finish_step,
    get_schedule,
    job_exists_for_month,
    job_heartbeat,
    recover_stale_jobs,
    park_stale_jobs,
    mark_schedule_enqueued,
    set_step_running,
    update_job_progress,
    update_step_tail,
    update_step_checkpoint,
    waiting_steps,
    worker_heartbeat,
    worker_singleton_lock,
)
from .pipeline import build_plan, configuration_status, command_for_step

STOP = False


def _signal(_signum, _frame):
    global STOP
    STOP = True


def _previous_month() -> str:
    now = datetime.now()
    year, month = now.year, now.month - 1
    if month == 0:
        year -= 1
        month = 12
    return f"{year:04d}-{month:02d}"


def maybe_schedule() -> None:
    schedule = get_schedule()
    if not schedule.get("enabled"):
        return
    now = datetime.now()
    day = int(schedule.get("run_day") or 5)
    hour = int(schedule.get("run_hour") or 2)
    minute = int(schedule.get("run_minute") or 0)
    if now.day < day or (now.day == day and (now.hour, now.minute) < (hour, minute)):
        return
    month = _previous_month()
    pipeline = str(schedule.get("pipeline_name") or "MONTHLY_FULL")
    companies = list(schedule.get("companies") or ["PWP", "DIN", "DAN", "DWT"])
    if not configuration_status(companies).get("complete"):
        return
    if schedule.get("last_enqueued_month") == month or job_exists_for_month(month, pipeline, companies):
        return
    create_job(
        pipeline,
        month,
        companies,
        requested_by="scheduler",
        request_source="SCHEDULE",
    )
    mark_schedule_enqueued(month)
    print(f"[CONTROL] scheduled {pipeline} {month}", flush=True)


RUN_ID_RE = re.compile(r"(?:\[MONTH MERGE\] )?run_id=([0-9a-fA-F-]{36})")
STAGE_RE = re.compile(r"^STAGE\s+(EMBED|MATCH|VALIDATE|PUBLISH)\s*$", re.I)
VALIDATE_COMMIT_RE = re.compile(r"\[VALIDATE\].*committed.*completed_this_call=([^\s]+)", re.I)
SILVER_SCAN_RE = re.compile(
    r"\[SILVER SCAN\] entries=([0-9,]+).*?target_images=([0-9,]+).*?outside_names=([0-9,]+).*?(?:recent_rate=([0-9,.]+)/s)?",
    re.I,
)
SERVER_PROBE_RE = re.compile(r"\[SERVER PROBE\] status=([^\s]+).*?server=([^\s]+)", re.I)


def _live_detail(step: dict, detail: str, *, force: bool = False) -> None:
    now_mono = time.monotonic()
    last = float(step.get("_live_detail_at") or 0.0)
    if not force and (now_mono - last) < 8.0:
        return
    update_step_checkpoint(int(step["step_id"]), checkpoint_detail=detail)
    step["checkpoint_detail"] = detail
    step["_live_detail_at"] = now_mono


def _checkpoint_from_line(step: dict, line: str) -> None:
    step_id = int(step["step_id"])
    server_match = SERVER_PROBE_RE.search(line)
    if server_match:
        _live_detail(step, f"SILVER · server {server_match.group(2)} · {server_match.group(1)}", force=True)
    if "[SILVER LOAD] membaca identity" in line:
        _live_detail(step, "SILVER · membaca status existing/resume", force=True)
    if "[SILVER SCAN] root=" in line:
        _live_detail(step, "SILVER · scanning NAS filename", force=True)
    scan_match = SILVER_SCAN_RE.search(line)
    if scan_match:
        entries, target, outside, recent = scan_match.groups()
        suffix = f" · {recent}/s" if recent else ""
        _live_detail(step, f"SILVER · scan {entries} entries · target {target} · outside {outside}{suffix}")
    if "[MONTH GUARD]" in line:
        _live_detail(step, "SILVER · month scan selesai, finalisasi metadata", force=True)
    if "[MONTH MERGE] sinkronisasi Context" in line or line.startswith("[CONTEXT]") or line.startswith("[RAW CONTEXT]"):
        _live_detail(step, "CONTEXT · sinkronisasi metadata bisnis", force=True)
    if "[RAW SYNC] DONE" in line:
        update_step_checkpoint(step_id, raw_complete=True, checkpoint_detail="RAW DONE")
        step["raw_complete"] = True
    if "[SILVER LOAD] DONE" in line:
        update_step_checkpoint(step_id, raw_complete=True, silver_complete=True, checkpoint_detail="SILVER ✓ · lanjut Context/Embed")
        step["raw_complete"] = True
        step["silver_complete"] = True
        step["checkpoint_detail"] = "SILVER ✓ · lanjut Context/Embed"
    match = RUN_ID_RE.search(line)
    if match:
        update_step_checkpoint(step_id, golden_run_id=match.group(1), checkpoint_detail="Golden Eye run created")
        step["golden_run_id"] = match.group(1)
    stage_match = STAGE_RE.match(line.strip())
    if stage_match:
        stage = stage_match.group(1).upper()
        update_step_checkpoint(step_id, checkpoint_stage=stage, checkpoint_detail=f"Stage {stage}")
        step["checkpoint_stage"] = stage
    validate_match = VALIDATE_COMMIT_RE.search(line)
    if validate_match:
        detail = f"VALIDATE committed {validate_match.group(1)}"
        update_step_checkpoint(step_id, checkpoint_stage="VALIDATE", checkpoint_detail=detail)
        step["checkpoint_stage"] = "VALIDATE"
        step["checkpoint_detail"] = detail


def _hydrate_checkpoint_from_existing_log(step: dict) -> None:
    """Recover checkpoint metadata from a previous Control Center log.

    This is mainly for jobs that started on an older Control Center version,
    before golden_run_id/stage checkpoint fields were persisted live.
    We read a small head + tail window only, so a multi-GB log is never loaded.
    """
    log_value = str(step.get("log_path") or "").strip()
    if not log_value:
        return
    path = Path(log_value)
    if not path.exists() or not path.is_file():
        return
    try:
        size = path.stat().st_size
        head_bytes = b""
        tail_bytes = b""
        with path.open("rb") as fh:
            head_bytes = fh.read(min(size, 256 * 1024))
            if size > 256 * 1024:
                fh.seek(max(0, size - 512 * 1024))
                tail_bytes = fh.read(512 * 1024)
        payload = (head_bytes + b"\n" + tail_bytes).decode("utf-8", errors="replace")
        before = (
            step.get("golden_run_id"),
            step.get("checkpoint_stage"),
            bool(step.get("raw_complete")),
            bool(step.get("silver_complete")),
        )
        for line in payload.splitlines():
            _checkpoint_from_line(step, line)
        after = (
            step.get("golden_run_id"),
            step.get("checkpoint_stage"),
            bool(step.get("raw_complete")),
            bool(step.get("silver_complete")),
        )
        if after != before:
            print(
                f"[CONTROL RECOVERY] hydrated checkpoint from old log "
                f"step={step.get('step_key')} run_id={step.get('golden_run_id') or '-'} "
                f"stage={step.get('checkpoint_stage') or '-'} "
                f"raw_done={bool(step.get('raw_complete'))} "
                f"silver_done={bool(step.get('silver_complete'))}",
                flush=True,
            )
    except Exception as exc:
        print(f"[CONTROL RECOVERY WARN] cannot inspect old log: {exc}", flush=True)


def _run_step(job_id: str, step: dict, command: list[str]) -> tuple[bool, int, str]:
    CONFIG.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = CONFIG.log_dir / f"{job_id}_{int(step['step_order']):02d}_{step['step_key']}.log"
    set_step_running(job_id, int(step["step_id"]), step["step_name"], str(log_path))
    tail = deque(maxlen=180)
    env = os.environ.copy()
    # A Scheduled Task on Windows can inherit a legacy ANSI code page (often
    # cp1252). Force every Golden Eye child command to use UTF-8 for stdout/
    # stderr so symbols in labels/logs cannot crash an otherwise successful job.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONLEGACYWINDOWSSTDIO", None)
    with log_path.open("a", encoding="utf-8", errors="replace") as log:
        log.write(f"\n=== {datetime.now().isoformat()} START ===\n")
        log.write("COMMAND: " + " ".join(command) + "\n\n")
        log.flush()
        proc = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        last_push = time.monotonic()
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            _checkpoint_from_line(step, line)
            print(f"[{step['step_key']}] {line}", flush=True)
            log.write(line + "\n")
            log.flush()
            tail.append(line)
            if time.monotonic() - last_push >= 2:
                update_step_tail(int(step["step_id"]), "\n".join(tail))
                last_push = time.monotonic()
        return_code = proc.wait()
        combined = "\n".join(tail)
        update_step_tail(int(step["step_id"]), combined)
        return return_code == 0, return_code, combined


def execute_job(worker_id: str, job: dict) -> None:
    job_id = str(job["job_id"])
    worker_heartbeat(worker_id, "RUNNING", os.getpid(), job_id)
    heartbeat_stop = threading.Event()

    def heartbeat_loop():
        while not heartbeat_stop.wait(CONFIG.worker_heartbeat_seconds):
            try:
                worker_heartbeat(worker_id, "RUNNING", os.getpid(), job_id)
                job_heartbeat(job_id)
            except Exception as exc:
                print(f"[CONTROL HEARTBEAT WARN] {exc}", flush=True)

    heartbeat_thread = threading.Thread(target=heartbeat_loop, name="control-heartbeat", daemon=True)
    heartbeat_thread.start()
    try:
        plan = build_plan(job["pipeline_name"], job["target_month"], job["companies"])
        plan_by_key = {step.key: step for step in plan}
        for db_step in waiting_steps(job_id):
            if STOP or cancel_requested(job_id):
                finish_job(job_id, "CANCELLED")
                return
            spec = plan_by_key.get(db_step["step_key"])
            if spec is None:
                error = f"Step definition hilang: {db_step['step_key']}"
                finish_step(int(db_step["step_id"]), False, 2, error, error)
                finish_job(job_id, "FAILED", error)
                return
            _hydrate_checkpoint_from_existing_log(db_step)
            command = command_for_step(spec, db_step)
            if command != spec.command:
                print(
                    f"[CONTROL RESUME] {db_step['step_name']} "
                    f"run_id={db_step.get('golden_run_id') or '-'} "
                    f"stage={db_step.get('checkpoint_stage') or '-'} "
                    f"silver_done={bool(db_step.get('silver_complete'))}",
                    flush=True,
                )
            success, return_code, tail = _run_step(job_id, db_step, command)
            finish_step(
                int(db_step["step_id"]), success, return_code, tail,
                None if success else f"Exit code {return_code}",
            )
            update_job_progress(job_id)
            if not success:
                finish_job(job_id, "FAILED", f"{db_step['step_name']} gagal (exit code {return_code})")
                return
        if cancel_requested(job_id):
            finish_job(job_id, "CANCELLED")
        else:
            finish_job(job_id, "SUCCESS")
    except Exception as exc:
        finish_job(job_id, "FAILED", str(exc)[:4000])
        raise
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)
        worker_heartbeat(worker_id, "IDLE", os.getpid(), None)


def run_worker() -> int:
    global STOP
    signal.signal(signal.SIGINT, _signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _signal)
    bootstrap()

    # Only one queue consumer may be active. This is separate from the heavy
    # Golden Eye job lock; it prevents two Control Center workers from racing.
    with worker_singleton_lock() as acquired:
        if not acquired:
            print("[CONTROL] another worker is already active; this worker will exit.", flush=True)
            return 23

        if CONFIG.auto_resume_stale:
            recovered = recover_stale_jobs(CONFIG.resume_stale_seconds)
            if recovered:
                print(f"[CONTROL] recovered stale jobs={recovered}", flush=True)
        else:
            parked = park_stale_jobs(CONFIG.resume_stale_seconds)
            if parked:
                print(f"[CONTROL] parked stale jobs={parked}; explicit resume required", flush=True)

        worker_id = f"worker-{uuid.uuid4().hex[:8]}"
        worker_heartbeat(worker_id, "IDLE", os.getpid())
        print(f"[CONTROL] worker started id={worker_id}", flush=True)
        last_schedule = 0.0
        last_recovery = time.monotonic()
        recovery_interval = max(10.0, min(30.0, CONFIG.resume_stale_seconds / 3.0))

        while not STOP:
            try:
                now_mono = time.monotonic()
                if now_mono - last_recovery >= recovery_interval:
                    if CONFIG.auto_resume_stale:
                        recovered = recover_stale_jobs(CONFIG.resume_stale_seconds)
                        if recovered:
                            print(f"[CONTROL] recovered stale jobs={recovered}", flush=True)
                    else:
                        parked = park_stale_jobs(CONFIG.resume_stale_seconds)
                        if parked:
                            print(f"[CONTROL] parked stale jobs={parked}; explicit resume required", flush=True)
                    last_recovery = now_mono

                if now_mono - last_schedule >= 30:
                    maybe_schedule()
                    last_schedule = now_mono

                job = claim_next_job(worker_id)
                if job:
                    execute_job(worker_id, job)
                else:
                    worker_heartbeat(worker_id, "IDLE", os.getpid())
                    time.sleep(CONFIG.worker_poll_seconds)
            except KeyboardInterrupt:
                STOP = True
            except Exception as exc:
                print(f"[CONTROL WORKER ERROR] {exc}", flush=True)
                worker_heartbeat(worker_id, "ERROR", os.getpid(), None, str(exc)[:2000])
                time.sleep(max(CONFIG.worker_poll_seconds, 5))

        worker_heartbeat(worker_id, "OFFLINE", os.getpid())
        print("[CONTROL] worker stopped", flush=True)
        return 0

