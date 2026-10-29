"""Read-only Golden Eye native dashboard latency tracer (no Docker, no DDL).

Usage: python dashboard_native_trace.py --system ELHP --company DAN
       python dashboard_native_trace.py --system HARMONY --company PWP --include-detail
       python dashboard_native_trace.py --company PWP --system HARMONY --image-key SOME_IMAGE.jpg
"""
from __future__ import annotations

import argparse
import json
import inspect
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "dashboard"))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system", choices=["HARMONY", "ELHP"], required=True)
    parser.add_argument("--company", choices=["PWP", "DIN", "DAN", "DWT"], required=True)
    parser.add_argument("--include-detail", action="store_true", help="Optional heavier detail read")
    parser.add_argument("--image-key", help="Optional one-photo lookup; reads image payload")
    parser.add_argument("--output", default="dashboard_native_trace.json", help="JSON output in project directory")
    args = parser.parse_args()
    if (args.system == "ELHP") != (args.company in {"DAN", "DWT"}):
        parser.error("ELHP only for DAN/DWT; HARMONY only for PWP/DIN")

    import db
    import queries
    report = {
        "system": args.system,
        "company": args.company,
        "database_host": os.getenv("PG_HOST", "localhost"),
        "docker": False,
        "stages": [],
        "notes": [
            "Views remain authoritative for SAP-verified ELHP.",
            "All timings are local to this machine; compare against original V186."
        ],
    }

    def measure(label, fn):
        started = time.perf_counter()
        print(f"[TRACE] {label}: started", flush=True)
        try:
            result = fn()
            secs = round((time.perf_counter() - started) * 1000, 1)
            rows = len(result) if hasattr(result, "__len__") and not isinstance(result, (str, bytes)) else None
            record = {"stage": label, "ms": secs, "rows": rows, "status": "OK"}
            report["stages"].append(record)
            print(f"[TRACE] {label}: {secs:,.1f} ms, rows={rows}", flush=True)
            return result
        except Exception as exc:
            secs = round((time.perf_counter() - started) * 1000, 1)
            record = {"stage": label, "ms": secs, "status": "FAILED", "error": f"{type(exc).__name__}: {str(exc)[:350]}"}
            report["stages"].append(record)
            print(f"[TRACE ERROR] {label}: {record['error']}", flush=True)
            return None

    measure("postgres_ping", db.ping)
    measure("company_options (no scan)", lambda: queries.get_company_options(args.system))
    company = [args.company]
    grp_name = db.TABLE_ELHP_GROUP if args.system == "ELHP" else db.TABLE_GROUP
    monthly_name = db.TABLE_ELHP_DASHBOARD_MONTHLY if args.system == "ELHP" else db.TABLE_DASHBOARD_MONTHLY
    scoped = "source_companies" in inspect.signature(db.read_table).parameters
    report["sql_company_pushdown_available"] = scoped
    if not scoped:
        report["notes"].append("Legacy read_table has no SQL company pushdown; SQL stage reads all companies.")
    measure("sql_gold_group_source", lambda: db.read_table(grp_name, **({"source_companies": company} if scoped else {})))
    measure("sql_gold_monthly_source", lambda: db.read_table(monthly_name, **({"source_companies": company} if scoped else {})))
    measure("normalize_and_attach_group_status", lambda: queries.load_groups(args.system, company))
    measure("normalized_gold_monthly", lambda: queries.load_dashboard_monthly(args.system, company))
    if args.include_detail:
        measure("load_full_company_detail (heavy)", lambda: queries.load_details(args.system, company))
    if args.image_key:
        def read_one():
            row = db.fetch_image_bytes(args.image_key)
            return len(row[0]) if row else 0
        measure("single_image_hot_first_then_nas", read_one)
    report["hot_first"] = not db.NAS_IMAGE_FIRST
    report["query_cache_seconds"] = db.QUERY_CACHE_SECONDS
    report["data_cache_seconds"] = queries.DATA_CACHE_SECONDS
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print("[TRACE] report=" + str(output), flush=True)
    return 1 if any(x["status"] == "FAILED" for x in report["stages"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
