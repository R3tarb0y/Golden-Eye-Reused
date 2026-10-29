from __future__ import annotations

import calendar
from datetime import datetime
from pathlib import Path
from time import perf_counter

from flask import Flask, jsonify, render_template, request, send_file
from waitress import serve
from psycopg2.errors import UniqueViolation

from .config import CONFIG
from .db import (
    bootstrap,
    create_job,
    get_schedule,
    interrupt_orphan_jobs,
    job_detail,
    latest_successful_month,
    list_jobs,
    request_cancel,
    retry_failed_job,
    resume_paused_job,
    resume_failed_job,
    update_schedule,
    worker_states,
)
from .pipeline import VALID_PIPELINES, configuration_status
from .lineage import build_lineage_model
from .health import source_health, source_health_cached
from .visual_audit import build_visual_audit_payload
from .server_diagnostics import run_silver_diagnostics
from .pg_storage import storage_report, preview_drop_backup, confirm_drop_backup
from .hot_evidence_audit import hot_evidence_report

app = Flask(__name__, template_folder="templates", static_folder="static")


def _previous_month() -> str:
    now = datetime.now()
    year, month = now.year, now.month - 1
    if month == 0:
        year -= 1
        month = 12
    return f"{year:04d}-{month:02d}"


@app.get("/")
def home():
    return render_template(
        "index.html",
        pipelines=VALID_PIPELINES,
        default_month=_previous_month(),
        default_companies=list(CONFIG.default_companies),
    )


@app.get("/api/state")
def api_state():
    jobs = list_jobs(30)
    workers = worker_states()
    for worker in workers:
        worker["online"] = float(worker.get("heartbeat_age_seconds") or 9999) < 20 and worker.get("status") != "OFFLINE"
    return jsonify({
        "jobs": jobs,
        "workers": workers,
        "schedule": get_schedule(),
        "config": configuration_status(),
        "suggested_month": _previous_month(),
        "latest_successful_month": latest_successful_month(),
        "now": datetime.now().isoformat(),
        "lineage": build_lineage_model(),
        "health": source_health_cached(),
    })


@app.get("/api/jobs/<job_id>")
def api_job(job_id: str):
    detail = job_detail(job_id)
    if not detail:
        return jsonify({"error": "Job tidak ditemukan"}), 404
    return jsonify(detail)


@app.post("/api/jobs")
def api_create_job():
    payload = request.get_json(force=True) or {}
    try:
        job_id = create_job(
            str(payload.get("pipeline_name") or "MONTHLY_FULL"),
            str(payload.get("target_month") or ""),
            list(payload.get("companies") or []),
            requested_by=str(payload.get("requested_by") or "web"),
            request_source="WEB",
        )
        return jsonify({"job_id": job_id}), 201
    except UniqueViolation:
        return jsonify({"error": "Scope ini baru saja dibuat oleh proses lain. Refresh halaman lalu coba kembali."}), 409
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/jobs/<job_id>/cancel")
def api_cancel(job_id: str):
    try:
        request_cancel(job_id)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/jobs/<job_id>/retry")
def api_retry(job_id: str):
    try:
        new_job = retry_failed_job(job_id)
        return jsonify({"job_id": new_job}), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/jobs/<job_id>/resume")
def api_resume(job_id: str):
    try:
        detail = job_detail(job_id)
        if not detail:
            return jsonify({"error": "Job tidak ditemukan"}), 404
        if detail.get("status") == "PAUSED":
            resumed = resume_paused_job(job_id)
            return jsonify({"job_id": job_id, "mode": "CHECKPOINT", "status": resumed.get("status"), "current_step": resumed.get("current_step")}), 200
        return jsonify(resume_failed_job(job_id)), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/control/clear-orphans")
def api_clear_orphans():
    try:
        count = interrupt_orphan_jobs(live_seconds=30)
        return jsonify({"ok": True, "interrupted": count})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/schedule")
def api_schedule():
    try:
        return jsonify(update_schedule(request.get_json(force=True) or {}))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400




@app.get("/api/lineage")
def api_lineage():
    return jsonify(build_lineage_model())


@app.get("/api/health")
def api_health():
    return jsonify(source_health())


@app.get("/api/diagnostics/silver")
def api_silver_diagnostics():
    try:
        company = str(request.args.get("company") or "PWP").strip().upper()
        kind = str(request.args.get("kind") or "PANEN").strip().upper()
        return jsonify(run_silver_diagnostics(company, kind))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/visual-audit")
def api_visual_audit():
    try:
        system = str(request.args.get("system") or "").strip().upper() or None
        raw_companies = str(request.args.get("company") or "").strip()
        companies = [x.strip().upper() for x in raw_companies.split(",") if x.strip()]
        raw_months = str(request.args.get("month") or "").strip()
        months = [x.strip() for x in raw_months.split(",") if x.strip()]
        year = int(request.args.get("year") or 2026)
        limit = min(3000, max(50, int(request.args.get("limit") or 1000)))
        return jsonify(build_visual_audit_payload(system, companies, year, months, limit))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/storage/relations")
def api_storage_relations():
    started = perf_counter()
    try:
        result = storage_report(int(request.args.get("limit") or 80))
        print(
            f"[CONTROL PG STORAGE] status=OK relations={len(result.get('relations', []))} "
            f"elapsed_ms={(perf_counter() - started) * 1000:.1f}",
            flush=True,
        )
        return jsonify(result)
    except Exception as exc:
        print(
            f"[CONTROL PG STORAGE] status=FAILED "
            f"elapsed_ms={(perf_counter() - started) * 1000:.1f} "
            f"error={type(exc).__name__}: {exc}",
            flush=True,
        )
        return jsonify({"error": str(exc)}), 503


@app.get("/api/storage/hot-evidence-audit")
def api_storage_hot_evidence_audit():
    """Expensive metadata-only audit; explicit click only, not periodic refresh."""
    started = perf_counter()
    try:
        result = hot_evidence_report()
        print(f"[HOT EVIDENCE AUDIT] status=OK elapsed_ms={(perf_counter()-started)*1000:.0f}", flush=True)
        return jsonify(result)
    except Exception as exc:
        print(f"[HOT EVIDENCE AUDIT] status=FAILED error={type(exc).__name__}: {exc}", flush=True)
        return jsonify({"error":str(exc)}), 503


@app.post("/api/storage/delete-preview")
def api_storage_delete_preview():
    try:
        data = request.get_json(silent=True) or {}
        return jsonify(preview_drop_backup(
            str(data.get("relation") or ""),
            request.headers.get("X-Storage-Admin-Token", ""),
            bool(data.get("backup_verified")),
        ))
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/storage/delete-confirm")
def api_storage_delete_confirm():
    try:
        data = request.get_json(silent=True) or {}
        return jsonify(confirm_drop_backup(
            str(data.get("relation") or ""),
            str(data.get("preview_token") or ""),
            str(data.get("typed_name") or ""),
            request.headers.get("X-Storage-Admin-Token", ""),
        ))
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/log")
def api_log():
    path = str(request.args.get("path") or "").strip()
    if not path:
        return jsonify({"error": "log path kosong"}), 400
    p = Path(path).resolve()
    if CONFIG.log_dir not in p.parents:
        return jsonify({"error": "log path di luar control log directory"}), 403
    if not p.exists():
        return jsonify({"error": "log belum tersedia"}), 404
    return send_file(p, mimetype="text/plain", as_attachment=False)


def run() -> None:
    bootstrap()
    CONFIG.log_dir.mkdir(parents=True, exist_ok=True)
    CONFIG.report_dir.mkdir(parents=True, exist_ok=True)
    print(f"Golden Eye Control Center: http://{CONFIG.host}:{CONFIG.port}", flush=True)
    serve(app, host=CONFIG.host, port=CONFIG.port, threads=8)
