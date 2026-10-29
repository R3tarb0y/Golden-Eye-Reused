from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from golden_eye.config import Settings
from golden_eye.db import connection, fetch_all_dict, qname, table_columns, table_exists


def _n(v):
    return int(v or 0)


def _pct(a, b):
    return 0.0 if not b else (float(a) * 100.0 / float(b))


def _month_expr(col: str) -> str:
    digits = f"regexp_replace(btrim({col}::text), '[^0-9]', '', 'g')"
    return (
        f"CASE WHEN length({digits}) >= 6 "
        f"THEN substring({digits} from 1 for 4) || '-' || substring({digits} from 5 for 2) END"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="READ ONLY audit: cari penyebab DIN reused mengecil setelah SAP visibility gate."
    )
    parser.add_argument("--year", type=int, default=None)
    args = parser.parse_args()

    settings = Settings()
    year = int(args.year or settings.sap_period_year)
    source = "DIN"
    sap_code = str(settings.sap_company_code(source)).upper().strip()
    configured_scope = tuple(settings.sap_estate_scope_values(source))

    with connection(settings) as conn:
        print("=" * 132)
        print("GOLDEN EYE - DIN REUSED LOSS ROOT-CAUSE AUDIT (READ ONLY)")
        print("=" * 132)
        print(f"SAP source       : {settings.sap_schema}.{settings.sap_table}")
        print(f"SAP period year  : {year}")
        print(f"DIN SAP BUKRS    : {sap_code}")
        print(f"DIN PRFNR scope  : {list(configured_scope)}")
        print()

        if not table_exists(conn, settings.sap_schema, settings.sap_table):
            raise RuntimeError(f"SAP source {settings.sap_schema}.{settings.sap_table} tidak ditemukan")
        for relation in (
            ("gold", "reused_photo_detail"),
            ("gold", "sap_oph_scope_key"),
            ("gold", "reused_photo_detail_sap_matched"),
            ("gold", "reused_photo_detail_sap_verified"),
        ):
            if not table_exists(conn, *relation):
                raise RuntimeError(f"Relation {relation[0]}.{relation[1]} belum ada")

        sap_columns = table_columns(conn, settings.sap_schema, settings.sap_table)
        sap_map = {c.lower(): c for c in sap_columns}
        prfnr_col = next((sap_map[n] for n in ("prfnr", "estate", "estate_name") if n in sap_map), None)
        oph_col = next((sap_map[n] for n in ("oph", "harvest_ticket", "tiket_panen", "no_bcc") if n in sap_map), None)
        date_col = next((sap_map[n] for n in ("budat", "posting_date", "period_date") if n in sap_map), None)
        company_col = next((sap_map[n] for n in ("company_code", "company_code_sap", "company", "bukrs", "companycode", "co_code") if n in sap_map), None)
        if not prfnr_col or not oph_col or not date_col:
            raise RuntimeError(
                f"Kolom SAP wajib tidak lengkap. prfnr={prfnr_col} oph={oph_col} date={date_col} company={company_col}"
            )

        # Section 0 - active runs
        active_runs = fetch_all_dict(
            conn,
            """
            SELECT run_id, min(published_at) AS first_published, max(published_at) AS last_published,
                   count(*)::bigint AS groups, sum(usage_count)::bigint AS group_usage
            FROM gold.reused_photo_group
            WHERE is_active=true AND processing.canonical_source_group(source_group)='DIN'
            GROUP BY run_id
            ORDER BY last_published DESC
            """,
        )
        print("ACTIVE DIN GOLD RUN")
        if not active_runs:
            print("  TIDAK ADA active Gold DIN")
        else:
            for r in active_runs:
                print(
                    f"  run={r['run_id']} groups={_n(r['groups']):,} group_usage={_n(r['group_usage']):,} "
                    f"published={r['first_published']} .. {r['last_published']}"
                )
        print()

        # Resolve exact PRFNR from source. DIN is exact by config, but this also checks actual data.
        scope = tuple(v.upper().strip() for v in configured_scope if v.strip())
        if not scope:
            raise RuntimeError("DIN PRFNR scope kosong")

        company_filter = ""
        params = [list(scope), str(year)]
        if company_col:
            company_filter = f" AND upper(btrim(s.\"{company_col}\"::text))=%s"
            params.append(sap_code)
        mexpr = _month_expr(f's."{date_col}"')
        source_months = fetch_all_dict(
            conn,
            f"""
            SELECT {mexpr} AS month,
                   count(*)::bigint AS rows,
                   count(DISTINCT processing.normalize_oph(s.\"{oph_col}\"::text))::bigint AS distinct_oph
            FROM {qname(settings.sap_schema, settings.sap_table)} s
            WHERE upper(btrim(s.\"{prfnr_col}\"::text))=ANY(%s)
              AND left(btrim(s.\"{date_col}\"::text),4)=%s
              {company_filter}
              AND {mexpr} IS NOT NULL
            GROUP BY 1
            ORDER BY 1
            """,
            tuple(params),
        )

        key_months = fetch_all_dict(
            conn,
            """
            SELECT sap_month AS month,
                   count(*)::bigint AS key_rows,
                   count(DISTINCT oph_key)::bigint AS distinct_oph
            FROM gold.sap_oph_scope_key
            WHERE source_group='DIN' AND period_year=%s
            GROUP BY sap_month
            ORDER BY sap_month
            """,
            (year,),
        )
        source_by_month = {str(r['month']): r for r in source_months}
        key_by_month = {str(r['month']): r for r in key_months}

        print("SAP SOURCE vs EXACT KEY")
        print(f"{'MONTH':<8} {'SAP_ROWS':>12} {'SAP_OPH':>12} {'KEY_OPH':>12} {'KEY/SAP':>10}")
        print("-" * 62)
        for month in sorted(set(source_by_month) | set(key_by_month)):
            s = source_by_month.get(month, {})
            k = key_by_month.get(month, {})
            sap_oph = _n(s.get('distinct_oph'))
            key_oph = _n(k.get('distinct_oph'))
            print(f"{month:<8} {_n(s.get('rows')):>12,} {sap_oph:>12,} {key_oph:>12,} {_pct(key_oph, sap_oph):>9.2f}%")
        print()

        # Main stage audit. any_sap = exact OPH exists anywhere in DIN SAP key; same_month = business gate.
        stages = fetch_all_dict(
            conn,
            """
            WITH visual AS (
                SELECT d.run_id, d.group_id, d.file_uid,
                       to_char(d.tanggal,'YYYY-MM') AS month,
                       processing.normalize_oph(d.oph) AS oph_key,
                       coalesce(nullif(btrim(d.match_source),''),'(EMPTY)') AS match_source
                FROM gold.reused_photo_detail d
                WHERE d.is_active=true
                  AND processing.canonical_source_group(d.source_group)='DIN'
                  AND d.tanggal IS NOT NULL
                  AND extract(year FROM d.tanggal)::int=%s
            ), flags AS (
                SELECT v.*,
                       (nullif(v.oph_key,'') IS NOT NULL) AS has_oph,
                       EXISTS (
                           SELECT 1 FROM gold.sap_oph_scope_key k
                           WHERE k.source_group='DIN' AND k.period_year=%s
                             AND k.oph_key=v.oph_key
                       ) AS sap_any_month,
                       EXISTS (
                           SELECT 1 FROM gold.sap_oph_scope_key k
                           WHERE k.source_group='DIN' AND k.period_year=%s
                             AND k.oph_key=v.oph_key AND k.sap_month=v.month
                       ) AS sap_same_month
                FROM visual v
            ), group_flags AS (
                SELECT run_id, group_id,
                       count(*)::bigint AS visual_members,
                       count(*) FILTER (WHERE sap_same_month)::bigint AS same_month_members
                FROM flags
                GROUP BY run_id, group_id
            ), verified AS (
                SELECT run_id, group_id, count(*)::bigint AS verified_members
                FROM gold.reused_photo_detail_sap_verified
                WHERE processing.canonical_source_group(source_group)='DIN'
                  AND tanggal IS NOT NULL
                  AND extract(year FROM tanggal)::int=%s
                GROUP BY run_id, group_id
            )
            SELECT f.month,
                   count(*)::bigint AS visual_usage,
                   count(DISTINCT (f.run_id,f.group_id))::bigint AS visual_groups,
                   count(*) FILTER (WHERE f.has_oph)::bigint AS with_oph,
                   count(*) FILTER (WHERE NOT f.has_oph)::bigint AS no_oph,
                   count(*) FILTER (WHERE f.sap_any_month)::bigint AS sap_any_month,
                   count(*) FILTER (WHERE f.sap_same_month)::bigint AS sap_same_month,
                   count(*) FILTER (WHERE f.sap_any_month AND NOT f.sap_same_month)::bigint AS sap_wrong_month,
                   count(*) FILTER (WHERE f.has_oph AND NOT f.sap_any_month)::bigint AS oph_not_in_sap,
                   count(*) FILTER (WHERE f.match_source LIKE 'UNVERIFIED%%')::bigint AS unverified_context,
                   count(DISTINCT (f.run_id,f.group_id)) FILTER (
                       WHERE gf.same_month_members=0
                   )::bigint AS groups_zero_sap,
                   count(DISTINCT (f.run_id,f.group_id)) FILTER (
                       WHERE gf.same_month_members=1
                   )::bigint AS groups_one_sap,
                   count(DISTINCT (f.run_id,f.group_id)) FILTER (
                       WHERE gf.same_month_members>=2
                   )::bigint AS groups_two_plus_sap,
                   count(*) FILTER (
                       WHERE EXISTS (
                           SELECT 1 FROM verified vv
                           WHERE vv.run_id=f.run_id AND vv.group_id=f.group_id
                       ) AND f.sap_same_month
                   )::bigint AS verified_usage
            FROM flags f
            JOIN group_flags gf USING(run_id,group_id)
            GROUP BY f.month
            ORDER BY f.month
            """,
            (year, year, year, year),
        )

        print("DIN LOSS FUNNEL BY MONTH")
        print(
            f"{'MONTH':<8} {'VISUAL':>9} {'GROUPS':>8} {'WITH_OPH':>9} {'ANY_SAP':>9} "
            f"{'SAME_MO':>9} {'WRONG_MO':>9} {'NOT_SAP':>9} {'G=1':>7} {'G>=2':>7} {'FINAL':>9}"
        )
        print("-" * 118)
        for r in stages:
            print(
                f"{r['month']:<8} {_n(r['visual_usage']):>9,} {_n(r['visual_groups']):>8,} "
                f"{_n(r['with_oph']):>9,} {_n(r['sap_any_month']):>9,} {_n(r['sap_same_month']):>9,} "
                f"{_n(r['sap_wrong_month']):>9,} {_n(r['oph_not_in_sap']):>9,} "
                f"{_n(r['groups_one_sap']):>7,} {_n(r['groups_two_plus_sap']):>7,} {_n(r['verified_usage']):>9,}"
            )
        print()

        # Match-source breakdown pinpoints context enrichment source responsible for losses.
        match_breakdown = fetch_all_dict(
            conn,
            """
            WITH visual AS (
                SELECT d.run_id, d.group_id, d.file_uid,
                       to_char(d.tanggal,'YYYY-MM') AS month,
                       processing.normalize_oph(d.oph) AS oph_key,
                       coalesce(nullif(btrim(d.match_source),''),'(EMPTY)') AS match_source
                FROM gold.reused_photo_detail d
                WHERE d.is_active=true
                  AND processing.canonical_source_group(d.source_group)='DIN'
                  AND d.tanggal IS NOT NULL
                  AND extract(year FROM d.tanggal)::int=%s
            )
            SELECT v.match_source,
                   count(*)::bigint AS visual_usage,
                   count(*) FILTER (WHERE nullif(v.oph_key,'') IS NOT NULL)::bigint AS with_oph,
                   count(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM gold.sap_oph_scope_key k
                       WHERE k.source_group='DIN' AND k.period_year=%s
                         AND k.oph_key=v.oph_key AND k.sap_month=v.month
                   ))::bigint AS same_month_sap
            FROM visual v
            GROUP BY v.match_source
            ORDER BY visual_usage DESC, v.match_source
            """,
            (year, year),
        )
        print("MATCH_SOURCE LOSS BREAKDOWN")
        print(f"{'MATCH_SOURCE':<54} {'VISUAL':>10} {'WITH_OPH':>10} {'SAME_MO':>10} {'PASS%':>8}")
        print("-" * 98)
        for r in match_breakdown:
            vis = _n(r['visual_usage'])
            same = _n(r['same_month_sap'])
            print(
                f"{str(r['match_source'])[:54]:<54} {vis:>10,} {_n(r['with_oph']):>10,} "
                f"{same:>10,} {_pct(same, vis):>7.2f}%"
            )
        print()

        # Where same OPH exists in SAP but in another month.
        month_shift = fetch_all_dict(
            conn,
            """
            WITH v AS (
                SELECT DISTINCT to_char(d.tanggal,'YYYY-MM') AS gold_month,
                       processing.normalize_oph(d.oph) AS oph_key
                FROM gold.reused_photo_detail d
                WHERE d.is_active=true
                  AND processing.canonical_source_group(d.source_group)='DIN'
                  AND d.tanggal IS NOT NULL
                  AND extract(year FROM d.tanggal)::int=%s
                  AND nullif(processing.normalize_oph(d.oph),'') IS NOT NULL
            )
            SELECT v.gold_month, k.sap_month,
                   count(DISTINCT v.oph_key)::bigint AS distinct_oph
            FROM v
            JOIN gold.sap_oph_scope_key k
              ON k.source_group='DIN' AND k.period_year=%s AND k.oph_key=v.oph_key
            WHERE k.sap_month<>v.gold_month
              AND NOT EXISTS (
                  SELECT 1 FROM gold.sap_oph_scope_key k2
                  WHERE k2.source_group='DIN' AND k2.period_year=%s
                    AND k2.oph_key=v.oph_key AND k2.sap_month=v.gold_month
              )
            GROUP BY v.gold_month, k.sap_month
            ORDER BY distinct_oph DESC, v.gold_month, k.sap_month
            LIMIT 30
            """,
            (year, year, year),
        )
        print("TOP GOLD MONTH -> SAP OTHER MONTH (OPH exists, month differs)")
        if not month_shift:
            print("  Tidak ada mismatch month yang signifikan terdeteksi.")
        else:
            print(f"{'GOLD_MONTH':<12} {'SAP_MONTH':<12} {'DISTINCT_OPH':>14}")
            print("-" * 42)
            for r in month_shift:
                print(f"{r['gold_month']:<12} {r['sap_month']:<12} {_n(r['distinct_oph']):>14,}")
        print()

        # Dashboard-view check: if verified detail exists but cleaned monthly is much smaller,
        # the loss is in the compatibility monthly join rather than SAP matching.
        dashboard_rows = fetch_all_dict(
            conn,
            """
            WITH v AS (
                SELECT to_char(tanggal,'YYYY-MM') AS month, count(*)::bigint AS verified_detail
                FROM gold.reused_photo_detail_sap_verified
                WHERE processing.canonical_source_group(source_group)='DIN'
                  AND tanggal IS NOT NULL AND extract(year FROM tanggal)::int=%s
                GROUP BY 1
            ), m AS (
                SELECT to_char(month,'YYYY-MM') AS month, sum(usage_count)::bigint AS dashboard_monthly
                FROM gold.reused_photo_dashboard_monthly_cleaned
                WHERE processing.canonical_source_group(source_group)='DIN'
                  AND extract(year FROM month)::int=%s
                GROUP BY 1
            )
            SELECT coalesce(v.month,m.month) AS month,
                   coalesce(v.verified_detail,0)::bigint AS verified_detail,
                   coalesce(m.dashboard_monthly,0)::bigint AS dashboard_monthly
            FROM v FULL JOIN m USING(month)
            ORDER BY 1
            """,
            (year, year),
        )
        print("VERIFIED DETAIL vs DASHBOARD MONTHLY")
        print(f"{'MONTH':<8} {'VERIFIED_DETAIL':>18} {'DASHBOARD_MONTHLY':>20} {'DELTA':>12}")
        print("-" * 66)
        for r in dashboard_rows:
            delta = _n(r['dashboard_monthly']) - _n(r['verified_detail'])
            print(
                f"{r['month']:<8} {_n(r['verified_detail']):>18,} {_n(r['dashboard_monthly']):>20,} {delta:>+12,}"
            )
        print()

        # Automatic diagnosis based on totals.
        total = {
            'visual': sum(_n(r['visual_usage']) for r in stages),
            'with_oph': sum(_n(r['with_oph']) for r in stages),
            'any_sap': sum(_n(r['sap_any_month']) for r in stages),
            'same': sum(_n(r['sap_same_month']) for r in stages),
            'wrong': sum(_n(r['sap_wrong_month']) for r in stages),
            'not_sap': sum(_n(r['oph_not_in_sap']) for r in stages),
            'final': sum(_n(r['verified_usage']) for r in stages),
        }
        key_total = sum(_n(r['distinct_oph']) for r in key_months)
        sap_total = sum(_n(r['distinct_oph']) for r in source_months)
        dashboard_total = sum(_n(r['dashboard_monthly']) for r in dashboard_rows)

        print("AUTOMATIC DIAGNOSIS")
        print(f"  SAP source distinct/month sum : {sap_total:,}")
        print(f"  SAP key distinct/month sum    : {key_total:,} ({_pct(key_total, sap_total):.2f}% dari source)")
        print(f"  Visual reused usage           : {total['visual']:,}")
        print(f"  Dengan OPH                    : {total['with_oph']:,} ({_pct(total['with_oph'], total['visual']):.2f}% visual)")
        print(f"  OPH ada di SAP (any month)    : {total['any_sap']:,} ({_pct(total['any_sap'], total['with_oph']):.2f}% with OPH)")
        print(f"  OPH match SAP bulan sama      : {total['same']:,} ({_pct(total['same'], total['with_oph']):.2f}% with OPH)")
        print(f"  OPH ada tapi bulan berbeda    : {total['wrong']:,}")
        print(f"  OPH tidak ada di SAP DIN      : {total['not_sap']:,}")
        print(f"  Final setelah group >=2       : {total['final']:,} ({_pct(total['final'], total['same']):.2f}% same-month)")
        print(f"  Dashboard monthly total       : {dashboard_total:,}")
        print()

        findings = []
        if sap_total and _pct(key_total, sap_total) < 95:
            findings.append("SAP_KEY_LOSS: exact key jauh lebih kecil dari SAP source; cek BUKRS/PRFNR/cutoff/key refresh.")
        if total['visual'] and _pct(total['with_oph'], total['visual']) < 70:
            findings.append("CONTEXT_OPH_LOSS: banyak visual reused tidak memiliki OPH terpercaya setelah publish sanitization.")
        if total['with_oph'] and _pct(total['any_sap'], total['with_oph']) < 70:
            findings.append("OPH_MEMBERSHIP_LOSS: banyak OPH Gold DIN tidak ditemukan pada SAP DIN scope.")
        if total['any_sap'] and _pct(total['same'], total['any_sap']) < 80:
            findings.append("MONTH_MISMATCH: OPH ada di SAP DIN tetapi tanggal/bulan Gold berbeda dari BUDAT SAP.")
        if total['same'] and _pct(total['final'], total['same']) < 75:
            findings.append("GROUP_COLLAPSE: banyak group hanya menyisakan 1 SAP-valid member; gate minimal 2 menghapus group dari dashboard.")
        if total['final'] and dashboard_total < total['final'] * 0.95:
            findings.append("MONTHLY_VIEW_JOIN_LOSS: detail SAP-verified ada tetapi monthly cleaned lebih kecil; cek join grain view 022.")
        if not findings:
            findings.append("Tidak ada satu loss besar yang dominan; lihat tabel per bulan dan match_source untuk pola spesifik.")
        for idx, finding in enumerate(findings, 1):
            print(f"  {idx}. {finding}")
        print("=" * 132)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
