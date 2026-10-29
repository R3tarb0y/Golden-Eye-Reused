from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT / "src"))

from golden_eye.config import Settings  # noqa: E402
from golden_eye.db import connection  # noqa: E402


ACTIVE_TABLES = (
    "gold.reused_photo_group",
    "gold.reused_photo_detail",
    "gold.reused_photo_group_summary",
    "gold.reused_photo_dashboard_monthly",
    "gold.oph_audit",
)


def _print_history(cur, source_group: str, limit: int) -> None:
    cur.execute(
        """
        WITH gold_run AS (
            SELECT run_id,
                   count(*)::bigint AS groups,
                   coalesce(sum(usage_count), 0)::bigint AS usages,
                   coalesce(max(usage_count), 0)::bigint AS max_group,
                   min(mean_pair_score) AS min_mean,
                   bool_or(is_active) AS active,
                   max(published_at) AS published_at
            FROM gold.reused_photo_group
            WHERE upper(source_group)=upper(%s)
            GROUP BY run_id
        ), gold_month AS (
            SELECT run_id,
                   array_agg(DISTINCT to_char(tanggal, 'YYYY-MM')
                             ORDER BY to_char(tanggal, 'YYYY-MM')) FILTER (
                       WHERE tanggal IS NOT NULL
                   ) AS months
            FROM gold.reused_photo_detail
            WHERE upper(source_group)=upper(%s)
            GROUP BY run_id
        )
        SELECT g.run_id::text, g.groups, g.usages, g.max_group,
               g.min_mean, g.active, g.published_at,
               p.stage, p.status, coalesce(m.months, '{}'::text[]) AS months
        FROM gold_run g
        LEFT JOIN gold_month m USING (run_id)
        LEFT JOIN processing.pipeline_run p USING (run_id)
        ORDER BY g.published_at DESC NULLS LAST
        LIMIT %s
        """,
        (source_group, source_group, limit),
    )
    rows = cur.fetchall()
    print("=" * 108)
    print(f"RIWAYAT GOLD: {source_group}")
    print("=" * 108)
    if not rows:
        print("Belum ada run Gold untuk source_group tersebut.")
        return
    for row in rows:
        marker = "ACTIVE" if row["active"] else "old"
        print(
            f"{marker:6} {row['run_id']} | groups={row['groups']:,} "
            f"usages={row['usages']:,} max={row['max_group']:,} "
            f"months={','.join(row['months'] or []) or '-'} "
            f"min_mean={row['min_mean']} pipeline={row['status']}/{row['stage']}"
        )


def _target_audit(cur, source_group: str, run_id: uuid.UUID) -> dict[str, object]:
    cur.execute(
        """
        SELECT count(*)::bigint AS groups,
               coalesce(sum(usage_count), 0)::bigint AS usages,
               coalesce(max(usage_count), 0)::bigint AS max_group,
               min(mean_pair_score) AS min_mean
        FROM gold.reused_photo_group
        WHERE upper(source_group)=upper(%s) AND run_id=%s
        """,
        (source_group, run_id),
    )
    audit = dict(cur.fetchone())
    cur.execute(
        """
        SELECT count(*)::bigint AS details,
               count(*) FILTER (
                   WHERE upper(image_name) ~ '(^|[^A-Z0-9])AI([^A-Z0-9]|$)'
               )::bigint AS ai_names,
               array_agg(DISTINCT to_char(tanggal, 'YYYY-MM')
                         ORDER BY to_char(tanggal, 'YYYY-MM')) FILTER (
                   WHERE tanggal IS NOT NULL
               ) AS months
        FROM gold.reused_photo_detail
        WHERE upper(source_group)=upper(%s) AND run_id=%s
        """,
        (source_group, run_id),
    )
    audit.update(dict(cur.fetchone()))
    cur.execute(
        """
        SELECT count(*)::bigint AS summaries
        FROM gold.reused_photo_group_summary
        WHERE upper(source_group)=upper(%s) AND run_id=%s
        """,
        (source_group, run_id),
    )
    audit.update(dict(cur.fetchone()))
    cur.execute(
        """
        SELECT count(*)::bigint AS duplicate_uids
        FROM (
            SELECT file_uid
            FROM gold.reused_photo_detail
            WHERE upper(source_group)=upper(%s) AND run_id=%s
            GROUP BY file_uid
            HAVING count(DISTINCT group_id) > 1
        ) duplicate_member
        """,
        (source_group, run_id),
    )
    audit.update(dict(cur.fetchone()))
    cur.execute(
        """
        SELECT count(*)::bigint AS bad_representatives
        FROM (
            SELECT group_id
            FROM gold.reused_photo_detail
            WHERE upper(source_group)=upper(%s) AND run_id=%s
            GROUP BY group_id
            HAVING count(*) FILTER (WHERE is_representative=1) <> 1
        ) bad_group
        """,
        (source_group, run_id),
    )
    audit.update(dict(cur.fetchone()))
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Aktifkan kembali snapshot Gold lama secara atomik. Default hanya audit; "
            "tambahkan --apply untuk menyimpan. Tidak ada row yang dihapus."
        )
    )
    parser.add_argument("--source-group", default="PWP")
    parser.add_argument("--run-id")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    source_group = str(args.source_group).upper().strip()
    settings = Settings()
    settings.validate()

    with connection(settings) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        _print_history(cur, source_group, max(1, args.limit))
        if not args.run_id:
            print("\nPilih run_id yang benar, lalu ulangi dengan --run-id <UUID>.")
            print("Tanpa --apply perintah tetap DRY-RUN.")
            return 0

        try:
            run_id = uuid.UUID(str(args.run_id))
        except ValueError as exc:
            raise SystemExit(f"run_id tidak valid: {args.run_id!r}") from exc

        audit = _target_audit(cur, source_group, run_id)
        print("-" * 108)
        print(f"TARGET {run_id}: {audit}")
        failures = []
        if int(audit["groups"] or 0) < 1:
            failures.append("target tidak mempunyai gold group")
        if int(audit["groups"] or 0) != int(audit["summaries"] or 0):
            failures.append("jumlah group dan summary berbeda")
        if int(audit["usages"] or 0) != int(audit["details"] or 0):
            failures.append("sum usage_count dan jumlah detail berbeda")
        if int(audit["max_group"] or 0) > 500:
            failures.append("target mempunyai group >500")
        if int(audit["ai_names"] or 0) > 0:
            failures.append("target masih mempunyai filename token AI")
        if int(audit["duplicate_uids"] or 0) > 0:
            failures.append("target mempunyai file_uid di lebih dari satu group")
        if int(audit["bad_representatives"] or 0) > 0:
            failures.append("target mempunyai group tanpa/lebih dari satu representative")
        if failures:
            print("RESTORE DIBATALKAN:")
            for failure in failures:
                print(f"  - {failure}")
            return 1

        if not args.apply:
            print("DRY-RUN LULUS. Ulangi perintah yang sama dengan --apply untuk mengaktifkan target.")
            return 0

        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"golden_eye_restore:{source_group}",),
        )
        # Bentuk aggregate exact untuk snapshot lama yang dipublish sebelum
        # strict hotfix. Ini membuat chart cepat segera setelah restore.
        cur.execute(
            """
            INSERT INTO gold.reused_photo_dashboard_monthly
                (run_id, group_id, source_group, source_system, source_company,
                 month, estate, afdeling, kerani_panen, mandor, pemanen,
                 usage_count, is_active, published_at)
            SELECT
                run_id, group_id, source_group, source_system, source_company,
                date_trunc('month', tanggal)::date,
                coalesce(btrim(estate), ''),
                coalesce(btrim(afdeling), ''),
                coalesce(btrim(kerani_panen), ''),
                coalesce(btrim(mandor), ''),
                coalesce(btrim(pemanen), ''),
                count(*)::integer, true, max(published_at)
            FROM gold.reused_photo_detail
            WHERE upper(source_group)=upper(%s)
              AND run_id=%s
              AND tanggal IS NOT NULL
              AND nullif(btrim(coalesce(estate, '')), '') IS NOT NULL
            GROUP BY
                run_id, group_id, source_group, source_system, source_company,
                date_trunc('month', tanggal)::date,
                coalesce(btrim(estate), ''), coalesce(btrim(afdeling), ''),
                coalesce(btrim(kerani_panen), ''), coalesce(btrim(mandor), ''),
                coalesce(btrim(pemanen), '')
            ON CONFLICT (
                run_id, group_id, month, estate, afdeling,
                kerani_panen, mandor, pemanen
            ) DO UPDATE SET
                usage_count=excluded.usage_count,
                is_active=true,
                published_at=excluded.published_at
            """,
            (source_group, run_id),
        )
        for table in ACTIVE_TABLES:
            cur.execute(
                f"UPDATE {table} SET is_active=false "
                "WHERE upper(source_group)=upper(%s) AND is_active=true",
                (source_group,),
            )
            cur.execute(
                f"UPDATE {table} SET is_active=true "
                "WHERE upper(source_group)=upper(%s) AND run_id=%s",
                (source_group, run_id),
            )

    print(f"APPLIED: Gold aktif {source_group} sekarang memakai run_id={run_id}")
    print("Data run lain tidak dihapus; rollback tetap dapat dilakukan dengan skrip yang sama.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
