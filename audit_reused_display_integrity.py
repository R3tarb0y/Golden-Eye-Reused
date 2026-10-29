from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DASHBOARD_DIR = ROOT / "dashboard"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

from queries import load_visual_reused_audit_monthly  # noqa: E402


def fmt(v):
    try:
        return f"{int(v or 0):,}"
    except Exception:
        return str(v)


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only HARMONY visual / ELHP SAP-lock integrity audit")
    ap.add_argument("--source-group", default="ALL", choices=["ALL", "PWP", "DIN", "DAN", "DWT"])
    ap.add_argument("--year", type=int, default=2026)
    args = ap.parse_args()
    companies = ["PWP", "DIN", "DAN", "DWT"] if args.source_group == "ALL" else [args.source_group]
    failed = False
    print("=" * 156)
    print("GOLDEN EYE - REUSED DISPLAY INTEGRITY v162 (READ ONLY)")
    print("HARMONY: MAIN=VISUAL | ELHP: MAIN=SAP VERIFIED + SAME MONTH + GROUP>=2 + OWNERSHIP | DAN Jan audit-only")
    print("=" * 156)
    for company in companies:
        system = "HARMONY" if company in {"PWP", "DIN"} else "ELHP"
        df = load_visual_reused_audit_monthly(system, [company], args.year)
        print(f"\n{company} [{system}]")
        print(f"{'MONTH':<9} {'RAW':>8} {'VISUAL':>9} {'MAIN':>9} {'UNVER':>9} {'SAP_OK':>9} {'OFFICIAL':>9} {'SAP_OPH':>10} {'WRONG':>9} {'EXPECTED':>10}")
        print("-" * 112)
        if df.empty:
            print("NO DATA")
            failed = True
            continue
        company_fail = False
        for r in df.to_dict('records'):
            visual = int(r.get('visual_usage') or 0)
            sap_ok = int(r.get('calculated_verified_usage') or 0)
            official = int(r.get('official_dashboard_usage') or 0)
            main = int(r.get('dashboard_usage') or 0)
            expected = visual if system == "HARMONY" else official
            if main != expected or official != sap_ok or int(r.get('official_over_sap') or 0) > 0:
                company_fail = True
            print(
                f"{str(r.get('month','')):<9} {fmt(r.get('raw_visual_usage')):>8} {fmt(visual):>9} "
                f"{fmt(main):>9} {fmt(r.get('unverified_oph')):>9} {fmt(sap_ok):>9} {fmt(official):>9} "
                f"{fmt(r.get('sap_oph')):>10} {fmt(r.get('wrong_estate')):>9} {fmt(expected):>10}"
            )
        if company_fail:
            print(f"FAIL {company}: {'MAIN harus VISUAL' if system=='HARMONY' else 'MAIN ELHP harus sama dengan SAP-verified official'}.")
            failed = True
        else:
            if system == "HARMONY":
                print(f"PASS {company}: HARMONY main visual konsisten; SAP tetap status sekunder.")
            else:
                print(f"PASS {company}: ELHP main terkunci SAP; visual spike tidak masuk main dashboard.")
    print("\nRESULT:", "CHECK/FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
