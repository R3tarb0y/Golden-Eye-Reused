from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .config import CONFIG, ROOT

VALID_COMPANIES = ("PWP", "DIN", "DAN", "DWT")
VALID_PIPELINES = {
    "MONTHLY_FULL": "Full Monthly Pipeline",
    "DATA_AND_REUSED": "Reused Photo Monthly Update",
    "TRANSPORT_ONLY": "Panen vs Transport Only",
    "REPORT_ONLY": "Generate Reports Only",
}


@dataclass
class StepSpec:
    key: str
    name: str
    command: list[str]
    company: str | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def command_text(self) -> str:
        def safe(v: str) -> str:
            if any(token in v.upper() for token in ("PASSWORD=", "PG_PASSWORD", "MSSQL_PASSWORD")):
                return "***"
            return v
        return " ".join(shlex.quote(safe(str(v))) for v in self.command)


def validate_month(month: str) -> str:
    month = str(month or "").strip()
    if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month):
        raise ValueError("Target month harus YYYY-MM, contoh 2026-08")
    return month


def normalize_companies(companies: Iterable[str]) -> list[str]:
    out: list[str] = []
    for value in companies or []:
        company = str(value).upper().strip()
        if company not in VALID_COMPANIES:
            raise ValueError(f"Company tidak dikenal: {company}")
        if company not in out:
            out.append(company)
    if not out:
        raise ValueError("Pilih minimal satu company")
    return out


def _py(script: str, *args: str) -> list[str]:
    return [CONFIG.python_exe, str(ROOT / script), *[str(v) for v in args]]


def _append_multi(args: list[str], flag: str, values: list[str]) -> None:
    for value in values:
        args.extend([flag, value])


def _require_roots(company: str, kind: str) -> list[str]:
    roots = CONFIG.panen_roots(company) if kind == "panen" else CONFIG.transport_roots(company)
    if not roots:
        env_name = f"CONTROL_{company}_{kind.upper()}_ROOT"
        raise ValueError(f"{env_name} belum diisi di .env")
    return roots


def _merge_month_command(
    company: str,
    month: str,
    roots: list[str],
    *,
    run_id: str | None = None,
    from_stage: str = "embed",
    resume_after_silver: bool = False,
    skip_raw_sync: bool = False,
) -> list[str]:
    cmd = _py(
        "run_pipeline.py", "merge-month",
        "--source-group", company,
        "--month", month,
        "--from-stage", from_stage,
        "--to-stage", "publish",
        "--apply",
    )
    if run_id:
        cmd.extend(["--run-id", str(run_id)])
    if resume_after_silver:
        cmd.append("--resume-after-silver")
    if skip_raw_sync:
        cmd.append("--skip-raw-sync")
    _append_multi(cmd, "--source-root", roots)
    return cmd


def command_for_step(spec: StepSpec, db_step: dict) -> list[str]:
    """Return normal command or a checkpoint-aware resume command."""
    if spec.metadata.get("resume_mode") != "MERGE_MONTH":
        return list(spec.command)

    company = str(spec.company or db_step.get("company") or "").upper()
    month = str(spec.metadata.get("target_month") or "")
    roots = list(spec.metadata.get("source_roots") or [])
    run_id = db_step.get("golden_run_id")
    stage = str(db_step.get("checkpoint_stage") or "").upper()
    silver_complete = bool(db_step.get("silver_complete"))
    raw_complete = bool(db_step.get("raw_complete"))

    # Begitu run_id sudah ada, RAW/Silver/Context tidak perlu dijalankan lagi.
    # `run` memakai snapshot run yang sama; VALIDATE akan membaca
    # processing.validation_progress dan melewati candidate COMPLETED.
    if run_id:
        resume_stage = stage.lower() if stage in {"EMBED", "MATCH", "VALIDATE", "PUBLISH"} else "embed"
        cmd = _py(
            "run_pipeline.py", "run",
            "--source-group", company,
            "--run-id", str(run_id),
            "--from-stage", resume_stage,
            "--to-stage", "publish",
        )
        _append_multi(cmd, "--validation-nas-root", roots)
        return cmd

    # Silver sudah committed tetapi run_id belum sempat dibuat: lewati RAW dan
    # NAS load, lakukan Context guard lalu buat run baru dari EMBED.
    if silver_complete:
        return _merge_month_command(
            company, month, roots,
            from_stage="embed", resume_after_silver=True,
        )

    # RAW selesai, Silver belum selesai: jangan query ulang DB45. Loader Silver
    # sendiri upsert/resumable sehingga row committed tidak diduplikasi.
    if raw_complete:
        return _merge_month_command(
            company, month, roots,
            from_stage="embed", skip_raw_sync=True,
        )

    return list(spec.command)


def build_plan(pipeline_name: str, target_month: str, companies: Iterable[str]) -> list[StepSpec]:
    pipeline_name = str(pipeline_name or "MONTHLY_FULL").upper().strip()
    if pipeline_name not in VALID_PIPELINES:
        raise ValueError(f"Pipeline tidak dikenal: {pipeline_name}")
    month = validate_month(target_month)
    selected = normalize_companies(companies)
    year = month[:4]
    steps: list[StepSpec] = []

    # v153: DB60 ZPAY is copied directly into the physical Silver mirror.
    # ALL means all source rows in the date window; company/estate scope is
    # applied later by SAP KPI. Existing IDs are skipped (append-only safe).
    # The loader restarts from the watermark month to close partial months.
    if pipeline_name != "REPORT_ONLY":
        steps.append(StepSpec(
            key="sync_sap_db60",
            name="SAP DB60 → Silver PostgreSQL · Copy New Data",
            command=_py(
                "run_pipeline.py", "sync-sap-db60",
                "--through-month", month,
                "--source-group", "ALL",
                "--apply",
            ),
            metadata={"category": "SOURCE", "target_month": month},
        ))

    if pipeline_name in {"MONTHLY_FULL", "DATA_AND_REUSED"}:
        for company in selected:
            roots = _require_roots(company, "panen")
            cmd = _merge_month_command(company, month, roots)
            steps.append(StepSpec(
                key=f"merge_month_{company.lower()}",
                name=f"Reused Photo {company} · Sync Month + Detect",
                company=company,
                command=cmd,
                metadata={
                    "category": "DATA_REUSED",
                    "resume_mode": "MERGE_MONTH",
                    "target_month": month,
                    "source_roots": roots,
                },
            ))

    if pipeline_name in {"MONTHLY_FULL", "TRANSPORT_ONLY"}:
        for company in [c for c in selected if c in {"PWP", "DIN"}]:
            panen_roots = _require_roots(company, "panen")
            transport_roots = _require_roots(company, "transport")

            cmd = _py("run_pipeline.py", "index-panen-nas", "--source-group", company, "--year", year, "--apply")
            _append_multi(cmd, "--nas-root", panen_roots)
            steps.append(StepSpec(
                key=f"panen_index_{company.lower()}",
                name=f"{company} · Index Foto Panen NAS",
                company=company,
                command=cmd,
                metadata={"category": "TRANSPORT"},
            ))

            cmd = _py("run_pipeline.py", "sync-transport-kt-before", "--source-group", company, "--year", year, "--apply")
            _append_multi(cmd, "--nas-root", transport_roots)
            steps.append(StepSpec(
                key=f"transport_sync_{company.lower()}",
                name=f"{company} · Pull Foto Transport",
                company=company,
                command=cmd,
                metadata={"category": "TRANSPORT"},
            ))

            cmd = _py("run_pipeline.py", "embed-panen-nas", "--source-group", company, "--year", year, "--apply")
            _append_multi(cmd, "--nas-root", panen_roots)
            steps.append(StepSpec(
                key=f"panen_embed_{company.lower()}",
                name=f"{company} · Prepare Panen Embedding",
                company=company,
                command=cmd,
                metadata={"category": "TRANSPORT"},
            ))

            steps.append(StepSpec(
                key=f"transport_score_{company.lower()}",
                name=f"{company} · Detect Panen vs Transport",
                company=company,
                command=_py(
                    "run_pipeline.py", "score-transport-kt-before",
                    "--source-group", company,
                    "--year", year,
                    "--only-pending",
                    "--apply",
                ),
                metadata={"category": "TRANSPORT"},
            ))

    if pipeline_name != "REPORT_ONLY":
        steps.append(StepSpec(
            key="refresh_sap_kpi",
            name="Refresh SAP KPI Snapshot",
            command=_py("run_pipeline.py", "refresh-sap-kpi", "--source-group", "ALL"),
            metadata={"category": "PUBLISH"},
        ))
        steps.append(StepSpec(
            key="ensure_dashboard_contract",
            name="Ensure Dashboard View Contract",
            command=_py("run_pipeline.py", "ensure-dashboard-contract", "--apply"),
            metadata={"category": "PUBLISH"},
        ))
        steps.append(StepSpec(
            key="verify_gold",
            name="Verify Dashboard Cleaned Gold",
            command=_py("run_pipeline.py", "verify-dashboard"),
            metadata={"category": "PUBLISH"},
        ))
        for company in [c for c in selected if c in {"DAN", "DWT"}]:
            steps.append(StepSpec(
                key=f"audit_estates_{company.lower()}",
                name=f"Audit ELHP Estate Scope · {company}",
                company=company,
                command=_py("run_pipeline.py", "audit-estates", "--source-group", company),
                metadata={"category": "PUBLISH"},
            ))
            steps.append(StepSpec(
                key=f"audit_months_{company.lower()}",
                name=f"Audit ELHP Month Coverage · {company}",
                company=company,
                command=_py("run_pipeline.py", "audit-months", "--source-group", company),
                metadata={"category": "PUBLISH"},
            ))

    if pipeline_name != "REPORT_ONLY" and os.getenv("DASHBOARD_AUTO_REFRESH_READ_MODELS", "false").strip().lower() in {"1", "true", "yes"}:
        steps.append(StepSpec(
            key="refresh_dashboard_read_models",
            name="Refresh PostgreSQL Dashboard Physical Read Models",
            command=_py("dashboard_read_models.py", "--apply"),
            metadata={"category": "PUBLISH"},
        ))

    CONFIG.report_dir.mkdir(parents=True, exist_ok=True)
    report_target = CONFIG.report_dir / month.replace("-", "_")
    report_target.mkdir(parents=True, exist_ok=True)
    if pipeline_name in {"MONTHLY_FULL", "REPORT_ONLY"}:
        if any(c in {"PWP", "DIN"} for c in selected):
            transport_company = "ALL" if {"PWP", "DIN"}.issubset(set(selected)) else next(c for c in selected if c in {"PWP", "DIN"})
            steps.append(StepSpec(
                key="report_transport",
                name="Generate Panen vs Transport Report",
                command=_py(
                    "dashboard/generate_transport_validation_report.py",
                    "--company", transport_company,
                    "--year", year,
                    "--output-dir", str(report_target / "transport"),
                ),
                metadata={"category": "REPORT", "output_dir": str(report_target / "transport")},
            ))
        if set(selected) == set(VALID_COMPANIES):
            steps.append(StepSpec(
                key="report_reused",
                name="Generate Reused Photo Bulletin · ALL",
                command=_py(
                    "dashboard/generate_pdf_reports.py",
                    "--system", "ALL",
                    "--company", "ALL",
                    "--combined",
                    "--output-dir", str(report_target / "reused"),
                ),
                metadata={"category": "REPORT", "output_dir": str(report_target / "reused")},
            ))
        else:
            for company in selected:
                system = "HARMONY" if company in {"PWP", "DIN"} else "ELHP"
                steps.append(StepSpec(
                    key=f"report_reused_{company.lower()}",
                    name=f"Generate Reused Photo Bulletin · {company}",
                    company=company,
                    command=_py(
                        "dashboard/generate_pdf_reports.py",
                        "--system", system,
                        "--company", company,
                        "--output-dir", str(report_target / "reused" / company.lower()),
                    ),
                    metadata={"category": "REPORT", "output_dir": str(report_target / "reused" / company.lower())},
                ))

    return steps


def configuration_status(companies: Iterable[str] | None = None) -> dict:
    selected = normalize_companies(companies or CONFIG.default_companies)
    rows = []
    complete = True
    for company in selected:
        panen = CONFIG.panen_roots(company)
        transport = CONFIG.transport_roots(company) if company in {"PWP", "DIN"} else []
        required_transport = company in {"PWP", "DIN"}
        ok = bool(panen) and (bool(transport) if required_transport else True)
        complete = complete and ok
        rows.append({
            "company": company,
            "panen_roots": panen,
            "transport_roots": transport,
            "ok": ok,
        })
    return {"complete": complete, "companies": rows}
