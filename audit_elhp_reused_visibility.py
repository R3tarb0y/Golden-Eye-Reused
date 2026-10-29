from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DASHBOARD_DIR = ROOT / "dashboard"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

from queries import load_visual_reused_audit_monthly, load_visual_reused_audit_groups  # noqa: E402


def _fmt(v):
    try:
        return f"{int(v or 0):,}"
    except Exception:
        return str(v)


def diagnose_row(row: dict) -> str:
    raw = int(row.get("raw_visual_usage") or 0)
    visual = int(row.get("visual_usage") or 0)
    main = int(row.get("dashboard_usage") or 0)
    official = int(row.get("official_dashboard_usage") or 0)
    calc = int(row.get("calculated_verified_usage") or 0)
    sap = int(row.get("sap_oph") or 0)
    unverified = int(row.get("unverified_oph") or 0)
    wrong = int(row.get("wrong_estate") or 0)

    if raw == 0:
        return "UPSTREAM: tidak ada visual Gold aktif"
    if official != calc:
        return f"SAP GATE DELTA: calculated={calc:,} official={official:,}"
    if main != official:
        return f"ELHP MAIN BUG: main={main:,} harus SAP-locked={official:,}"
    if official > sap and sap > 0:
        return "FAIL: official melebihi SAP OPH"
    notes = ["MAIN=SAP LOCKED"]
    if visual > official:
        notes.append(f"visual_only={visual-official:,} tetap di Visual Audit")
    if unverified:
        notes.append(f"unverified={unverified:,}")
    if wrong:
        notes.append(f"wrong_estate={wrong:,}")
    return "PASS | " + "; ".join(notes)


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only DAN/DWT SAP-locked main + visual audit")
    ap.add_argument("--source-group", default="ALL", choices=["ALL", "DAN", "DWT"])
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--month", help="Opsional YYYY-MM untuk fokus satu bulan")
    args = ap.parse_args()

    companies = ["DAN", "DWT"] if args.source_group == "ALL" else [args.source_group]
    print("=" * 168)
    print("GOLDEN EYE - ELHP SAP-LOCK + VISUAL AUDIT (READ ONLY) - v162")
    print("=" * 168)
    print("Kontrak: MAIN ELHP = exact SAP same-month + group>=2 + ownership (DAN=PU*, DWT=ME*). Visual-only tidak hilang, tetapi hanya di Visual Audit.")

    overall_fail = False
    for company in companies:
        monthly = load_visual_reused_audit_monthly("ELHP", [company], args.year)
        if args.month and not monthly.empty:
            monthly = monthly[monthly["month"].astype(str).eq(args.month)].copy()

        print("\n" + "=" * 168)
        print(f"{company} | year={args.year}")
        print("=" * 168)
        print(
            f"{'MONTH':<9} {'RAW':>8} {'VISUAL':>9} {'MAIN_SAP':>10} {'UNVER':>9} {'SAP_OK':>9} "
            f"{'OFFICIAL':>9} {'SAP_OPH':>10} {'WRONG_EST':>10} {'VIS_ONLY':>10}  DIAGNOSIS"
        )
        print("-" * 168)
        if monthly.empty:
            print("Tidak ada row audit.")
            overall_fail = True
            continue

        for rec in monthly.to_dict("records"):
            diag = diagnose_row(rec)
            if "BUG" in diag or diag.startswith("FAIL") or "DELTA" in diag:
                overall_fail = True
            visual = int(rec.get('visual_usage') or 0)
            official = int(rec.get('official_dashboard_usage') or 0)
            print(
                f"{str(rec.get('month','')):<9} {_fmt(rec.get('raw_visual_usage')):>8} {_fmt(visual):>9} "
                f"{_fmt(rec.get('dashboard_usage')):>10} {_fmt(rec.get('unverified_oph')):>9} "
                f"{_fmt(rec.get('calculated_verified_usage')):>9} {_fmt(official):>9} "
                f"{_fmt(rec.get('sap_oph')):>10} {_fmt(rec.get('wrong_estate')):>10} {_fmt(max(visual-official,0)):>10}  {diag}"
            )

        focus_month = args.month or "2026-01"
        groups = load_visual_reused_audit_groups("ELHP", [company], args.year, [focus_month], limit=50)
        print("-" * 168)
        print(f"TOP VISUAL GROUPS {company} {focus_month} (audit only; tidak otomatis masuk MAIN)")
        if groups.empty:
            print("  Tidak ada group visual pada bulan fokus.")
        else:
            for r in groups.head(20).to_dict("records"):
                print(
                    f"  group={r.get('group_id')} visual={_fmt(r.get('visual_usage'))} "
                    f"with_oph={_fmt(r.get('with_oph'))} sap_same={_fmt(r.get('sap_same_month'))} "
                    f"verified={_fmt(r.get('verified_usage'))} unverified={_fmt(r.get('unverified_oph'))} "
                    f"wrong_estate={_fmt(r.get('wrong_estate'))} status={r.get('verification_status')}"
                )

        bad_main = int((monthly['dashboard_usage'].fillna(0) - monthly['official_dashboard_usage'].fillna(0)).abs().sum())
        official_delta = int(monthly['official_delta'].fillna(0).abs().sum())
        over = int(monthly['official_over_sap'].fillna(0).sum())
        print("-" * 168)
        if bad_main == 0 and official_delta == 0 and over == 0:
            print(f"PASS {company}: MAIN ELHP = SAP-verified official; anti-spike lock aktif.")
        else:
            print(f"FAIL {company}: main_vs_official={bad_main:,} official_delta={official_delta:,} official_over_sap={over:,}")
            overall_fail = True

    print("\n" + "=" * 168)
    print("RESULT:", "CHECK/FAIL" if overall_fail else "PASS")
    print("=" * 168)
    return 1 if overall_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
