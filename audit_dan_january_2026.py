from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DASH = ROOT / "dashboard"
if str(DASH) not in sys.path:
    sys.path.insert(0, str(DASH))

from db import read_df_resilient  # noqa: E402


def f(v):
    try:
        return f"{int(v or 0):,}"
    except Exception:
        return str(v)


def main() -> int:
    ap = argparse.ArgumentParser(description="DAN January 2026 root-cause audit; read only")
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--month", type=int, default=1)
    args = ap.parse_args()
    month = f"{args.year:04d}-{args.month:02d}"

    print("=" * 150)
    print(f"DAN {month} - ROOT CAUSE AUDIT (READ ONLY)")
    print("Main ELHP tetap SAP-locked. Audit ini hanya menjelaskan visual yang tidak masuk main.")
    print("=" * 150)

    sql_summary = """
    WITH d AS (
      SELECT
        run_id, group_id, file_uid, tanggal, estate, oph, match_source,
        nullif(processing.normalize_oph(oph), '') AS oph_key,
        processing.elhp_estate_matches_company('DAN', estate) AS estate_ok
      FROM gold.reused_photo_detail
      WHERE is_active=true
        AND processing.canonical_source_group(source_group)='DAN'
        AND to_char(tanggal, 'YYYY-MM')=%s
    ), x AS (
      SELECT d.*,
        EXISTS (
          SELECT 1 FROM gold.sap_oph_scope_key k
          WHERE k.source_group='DAN'
            AND k.sap_month=%s
            AND k.period_year=%s
            AND k.oph_key=d.oph_key
        ) AS sap_same
      FROM d
    )
    SELECT
      count(*) AS raw_usage,
      count(*) FILTER (WHERE estate_ok) AS estate_ok_usage,
      count(*) FILTER (WHERE NOT estate_ok) AS wrong_estate_usage,
      count(*) FILTER (WHERE estate_ok AND (oph_key IS NULL OR upper(coalesce(match_source,'')) LIKE 'UNVERIFIED_OPH_%%')) AS estate_ok_unverified,
      count(*) FILTER (WHERE estate_ok AND sap_same) AS estate_ok_sap_same,
      count(DISTINCT (run_id,group_id)) AS raw_groups,
      count(DISTINCT (run_id,group_id)) FILTER (WHERE estate_ok) AS estate_ok_groups
    FROM x
    """
    s = read_df_resilient(sql_summary, [month, month, args.year], operation="DAN January summary")
    if not s.empty:
        r=s.iloc[0].to_dict()
        print("SUMMARY")
        for k in ["raw_usage","estate_ok_usage","wrong_estate_usage","estate_ok_unverified","estate_ok_sap_same","raw_groups","estate_ok_groups"]:
            print(f"  {k:<28}: {f(r.get(k))}")

    sql_estate = """
    SELECT coalesce(nullif(btrim(estate),''), '<EMPTY>') AS estate,
           count(*) AS usages,
           count(DISTINCT (run_id,group_id)) AS groups,
           count(*) FILTER (WHERE nullif(processing.normalize_oph(oph),'') IS NOT NULL) AS with_oph,
           count(*) FILTER (WHERE upper(coalesce(match_source,'')) LIKE 'UNVERIFIED_OPH_%%') AS unverified
    FROM gold.reused_photo_detail
    WHERE is_active=true
      AND processing.canonical_source_group(source_group)='DAN'
      AND to_char(tanggal, 'YYYY-MM')=%s
    GROUP BY 1
    ORDER BY usages DESC, estate
    LIMIT 30
    """
    e = read_df_resilient(sql_estate, [month], operation="DAN January estate breakdown")
    print("\nTOP ESTATE VALUES")
    if e.empty:
        print("  NO DATA")
    else:
        print(f"{'ESTATE':<40} {'USAGES':>10} {'GROUPS':>10} {'WITH_OPH':>10} {'UNVER':>10}")
        for r in e.to_dict('records'):
            print(f"{str(r.get('estate','')):<40} {f(r.get('usages')):>10} {f(r.get('groups')):>10} {f(r.get('with_oph')):>10} {f(r.get('unverified')):>10}")

    sql_match = """
    SELECT coalesce(nullif(btrim(match_source),''), '<EMPTY>') AS match_source,
           count(*) AS usages,
           count(*) FILTER (WHERE processing.elhp_estate_matches_company('DAN', estate)) AS estate_ok,
           count(*) FILTER (WHERE NOT processing.elhp_estate_matches_company('DAN', estate)) AS wrong_estate
    FROM gold.reused_photo_detail
    WHERE is_active=true
      AND processing.canonical_source_group(source_group)='DAN'
      AND to_char(tanggal, 'YYYY-MM')=%s
    GROUP BY 1
    ORDER BY usages DESC, match_source
    LIMIT 30
    """
    m = read_df_resilient(sql_match, [month], operation="DAN January match source breakdown")
    print("\nMATCH SOURCE BREAKDOWN")
    if m.empty:
        print("  NO DATA")
    else:
        print(f"{'MATCH_SOURCE':<70} {'USAGES':>10} {'ESTATE_OK':>10} {'WRONG':>10}")
        for r in m.to_dict('records'):
            print(f"{str(r.get('match_source','')):<70} {f(r.get('usages')):>10} {f(r.get('estate_ok')):>10} {f(r.get('wrong_estate')):>10}")

    print("\nINTERPRETASI")
    print("  - MAIN DAN tetap tidak boleh memakai raw visual Januari tanpa SAP proof.")
    print("  - Jika wrong_estate mendominasi, cek historical source/company/estate mapping Januari; jangan bypass dashboard.")
    print("  - Jika estate_ok banyak tetapi SAP same-month=0, fokus ke OPH/context Januari.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
