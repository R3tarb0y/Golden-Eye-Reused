from __future__ import annotations

import argparse
import uuid

from golden_eye.config import Settings
from golden_eye.db import connection, fetch_all_dict

UNSAFE = (
    "MATCH_BY_RAW_ACTOR_DATE_UNIQUE_ESTATE",
    "MATCH_BY_RAW_ACTOR_DATE_UNIQUE_ESTATE_NO_OPH",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only audit for DIN legacy OPH SAP normalization")
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    run_id = uuid.UUID(args.run_id)
    settings = Settings()

    with connection(settings) as conn:
        rows = fetch_all_dict(
            conn,
            """
            WITH unsafe AS (
                SELECT
                    m.file_uid,
                    processing.normalize_oph(c.oph) AS oph_key,
                    to_char(c.period_date, 'YYYY-MM') AS month,
                    upper(coalesce(c.match_method, '')) AS match_method
                FROM processing.validated_group_member m
                JOIN processing.image_context c ON c.file_uid=m.file_uid
                WHERE m.run_id=%s
                  AND upper(coalesce(c.match_method, ''))=ANY(%s)
            ), classified AS (
                SELECT
                    u.*,
                    EXISTS (
                        SELECT 1
                        FROM gold.sap_oph_scope_key k
                        WHERE k.source_group='DIN'
                          AND k.period_year=%s
                          AND k.oph_key=u.oph_key
                          AND k.sap_month=u.month
                    ) AS same_month_sap
                FROM unsafe u
            )
            SELECT
                count(*)::bigint AS unsafe_total,
                count(*) FILTER (WHERE nullif(oph_key, '') IS NOT NULL)::bigint AS with_oph,
                count(*) FILTER (WHERE same_month_sap)::bigint AS sap_normalizable,
                count(*) FILTER (WHERE NOT same_month_sap)::bigint AS remains_unverified,
                count(DISTINCT oph_key) FILTER (WHERE same_month_sap)::bigint AS normalizable_distinct_oph
            FROM classified
            """,
            (run_id, list(UNSAFE), int(settings.sap_period_year)),
        )[0]

    print("=" * 78)
    print("DIN LEGACY OPH -> SAME-MONTH SAP NORMALIZATION AUDIT (READ ONLY)")
    print("=" * 78)
    print(f"run_id                  : {run_id}")
    print(f"unsafe_total            : {int(rows.get('unsafe_total') or 0):,}")
    print(f"with_original_oph       : {int(rows.get('with_oph') or 0):,}")
    print(f"sap_normalizable        : {int(rows.get('sap_normalizable') or 0):,}")
    print(f"remains_unverified      : {int(rows.get('remains_unverified') or 0):,}")
    print(f"normalizable_distinct_oph: {int(rows.get('normalizable_distinct_oph') or 0):,}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
