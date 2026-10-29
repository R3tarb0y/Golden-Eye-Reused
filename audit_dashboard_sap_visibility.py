from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from golden_eye.config import Settings
from golden_eye.db import connection, fetch_all_dict, table_exists


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only audit: visual Gold vs exact SAP-visible reused usage by month."
    )
    parser.add_argument("--source-group", default="ALL")
    args = parser.parse_args()

    requested = str(args.source_group or "ALL").upper().strip()
    sources = ["PWP", "DIN", "DAN", "DWT"] if requested == "ALL" else [requested]
    settings = Settings()

    with connection(settings) as conn:
        if not table_exists(conn, "gold", "sap_oph_scope_key"):
            raise RuntimeError(
                "gold.sap_oph_scope_key belum ada. Jalankan init-db dari v150 lalu refresh-sap ALL."
            )
        rows = fetch_all_dict(
            conn,
            """
            WITH visual AS (
                SELECT processing.canonical_source_group(source_group) AS source_group,
                       to_char(tanggal, 'YYYY-MM') AS month,
                       count(*)::bigint AS visual_usage,
                       count(*) FILTER (
                           WHERE nullif(processing.normalize_oph(oph), '') IS NOT NULL
                       )::bigint AS usage_with_oph
                FROM gold.reused_photo_detail
                WHERE is_active=true
                  AND tanggal IS NOT NULL
                  AND processing.canonical_source_group(source_group)=ANY(%s)
                GROUP BY 1,2
            ), sap_match AS (
                SELECT processing.canonical_source_group(d.source_group) AS source_group,
                       to_char(d.tanggal, 'YYYY-MM') AS month,
                       count(*)::bigint AS sap_matched_usage,
                       count(DISTINCT processing.normalize_oph(d.oph))::bigint AS sap_matched_distinct_oph
                FROM gold.reused_photo_detail_sap_matched d
                WHERE processing.canonical_source_group(d.source_group)=ANY(%s)
                GROUP BY 1,2
            ), verified AS (
                SELECT processing.canonical_source_group(d.source_group) AS source_group,
                       to_char(d.tanggal, 'YYYY-MM') AS month,
                       count(*)::bigint AS dashboard_usage,
                       count(DISTINCT processing.normalize_oph(d.oph))::bigint AS dashboard_distinct_oph
                FROM gold.reused_photo_detail_sap_verified d
                WHERE processing.canonical_source_group(d.source_group)=ANY(%s)
                GROUP BY 1,2
            ), sap AS (
                SELECT source_group, sap_month AS month,
                       count(DISTINCT oph_key)::bigint AS sap_distinct_oph
                FROM gold.sap_oph_scope_key
                WHERE period_year=%s
                  AND source_group=ANY(%s)
                GROUP BY source_group, sap_month
            ), months AS (
                SELECT source_group, month FROM visual
                UNION SELECT source_group, month FROM sap_match
                UNION SELECT source_group, month FROM verified
                UNION SELECT source_group, month FROM sap
            )
            SELECT m.source_group, m.month,
                   coalesce(v.visual_usage,0)::bigint AS visual_usage,
                   coalesce(v.usage_with_oph,0)::bigint AS usage_with_oph,
                   coalesce(sm.sap_matched_usage,0)::bigint AS sap_matched_usage,
                   coalesce(sm.sap_matched_distinct_oph,0)::bigint AS sap_matched_distinct_oph,
                   coalesce(x.dashboard_usage,0)::bigint AS dashboard_usage,
                   coalesce(x.dashboard_distinct_oph,0)::bigint AS dashboard_distinct_oph,
                   coalesce(s.sap_distinct_oph,0)::bigint AS sap_distinct_oph,
                   (coalesce(v.visual_usage,0)-coalesce(sm.sap_matched_usage,0))::bigint AS excluded_not_exact_sap,
                   CASE WHEN coalesce(s.sap_distinct_oph,0)>0
                        THEN round(coalesce(x.dashboard_usage,0)::numeric * 100 / s.sap_distinct_oph, 2)
                        ELSE NULL END AS dashboard_pct_of_sap
            FROM months m
            LEFT JOIN visual v USING(source_group, month)
            LEFT JOIN sap_match sm USING(source_group, month)
            LEFT JOIN verified x USING(source_group, month)
            LEFT JOIN sap s USING(source_group, month)
            ORDER BY m.source_group, m.month
            """,
            (sources, sources, sources, int(settings.sap_period_year), sources),
        )

        print("=" * 118)
        print("GOLDEN EYE - SAP VERIFIED DASHBOARD VISIBILITY AUDIT")
        print("=" * 118)
        print(
            f"{'SRC':<5} {'MONTH':<8} {'VISUAL':>10} {'WITH_OPH':>10} {'SAP_MATCH':>10} "
            f"{'DASHBOARD':>10} {'SAP_OPH':>10} {'EXCLUDED':>10} {'DASH/SAP':>10}"
        )
        print("-" * 118)
        for row in rows:
            pct = "N/A" if row.get("dashboard_pct_of_sap") is None else f"{row['dashboard_pct_of_sap']}%"
            print(
                f"{row['source_group']:<5} {row['month']:<8} "
                f"{int(row['visual_usage']):>10,} {int(row['usage_with_oph']):>10,} "
                f"{int(row['sap_matched_usage']):>10,} {int(row['dashboard_usage']):>10,} "
                f"{int(row['sap_distinct_oph']):>10,} {int(row['excluded_not_exact_sap']):>10,} "
                f"{pct:>10}"
            )

        bad = [
            row for row in rows
            if int(row.get("dashboard_usage") or 0) > int(row.get("sap_distinct_oph") or 0)
            and int(row.get("sap_distinct_oph") or 0) > 0
        ]
        print("-" * 118)
        if bad:
            print("FAIL: dashboard usage masih melebihi DISTINCT OPH SAP pada bulan berikut:")
            for row in bad:
                print(
                    f"  {row['source_group']} {row['month']} dashboard={int(row['dashboard_usage']):,} "
                    f"sap={int(row['sap_distinct_oph']):,}"
                )
            return 2
        print("PASS: setiap bulan dashboard usage <= DISTINCT OPH SAP pada scope yang sama.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
