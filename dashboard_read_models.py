"""Atomic PostgreSQL physical snapshots for dashboard SELECT paths.

Read models mirror authoritative cleaned Gold views (including SAP gates).
Creates new immutable tables, indexes, and switches a pointer in one commit.
Old versions retained for rollback/audit; no source Gold modifications.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from uuid import uuid4
from psycopg2 import sql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src'))
load_dotenv(ROOT / '.env')
from golden_eye.config import Settings  # noqa: E402
from golden_eye.db import connection  # noqa: E402

CORE = (
    'reused_photo_group_summary_cleaned',
    'elhp_gt_group_summary_cleaned',
    'reused_photo_dashboard_monthly_cleaned',
    'elhp_dashboard_monthly_cleaned',
)
DETAIL = ('reused_photo_detail_transaction', 'elhp_gt_detail_transaction')


def refresh(models: tuple[str, ...], timeout_seconds: int = 900) -> list[dict]:
    results=[]
    suffix = uuid4().hex[:12]
    settings = Settings()
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout=%s", (int(timeout_seconds * 1000),))
        # Transactional advisory lock prevents concurrent manual refreshes.
        cur.execute("SELECT pg_try_advisory_xact_lock(hashtext('golden_eye_dashboard_read_models')::bigint)")
        if not cur.fetchone()[0]:
            raise RuntimeError('Read-model refresh lain masih berjalan.')
        cur.execute("CREATE SCHEMA IF NOT EXISTS dashboard_cache")
        cur.execute("""CREATE TABLE IF NOT EXISTS dashboard_cache.active_snapshot (
            source_name text PRIMARY KEY,
            snapshot_name text NOT NULL,
            refreshed_at timestamptz NOT NULL DEFAULT now(),
            row_count bigint NOT NULL DEFAULT 0
        )""")
        for model in models:
            if model not in CORE + DETAIL:
                raise ValueError('Model tidak dikenali')
            name = f'snapshot_{model}_{suffix}'
            started=time.perf_counter()
            source = sql.SQL('{}.{}').format(sql.Identifier('gold'), sql.Identifier(model))
            target = sql.SQL('{}.{}').format(sql.Identifier('dashboard_cache'), sql.Identifier(name))
            cur.execute(sql.SQL('CREATE TABLE {} AS SELECT * FROM {}').format(target, source))
            # CTAS rowcount is driver-dependent: count the newly built local table.
            cur.execute(sql.SQL('SELECT count(*) FROM {}').format(target))
            row_count = int(cur.fetchone()[0])
            cur.execute("""SELECT column_name FROM information_schema.columns
                           WHERE table_schema='dashboard_cache' AND table_name=%s""", (name,))
            columns = {r[0] for r in cur.fetchall()}
            for col in ('source_group','source_company','group_id','run_id','month'):
                if col in columns:
                    cur.execute(sql.SQL('CREATE INDEX {} ON {} ({})').format(
                        sql.Identifier(f'idx_{suffix}_{model[:20]}_{col}'[:63]),
                        target, sql.Identifier(col)))
            cur.execute(sql.SQL('ANALYZE {}').format(target))
            cur.execute("""INSERT INTO dashboard_cache.active_snapshot(source_name,snapshot_name,refreshed_at,row_count)
                           VALUES (%s,%s,clock_timestamp(),%s)
                           ON CONFLICT(source_name) DO UPDATE
                           SET snapshot_name=EXCLUDED.snapshot_name, refreshed_at=EXCLUDED.refreshed_at,
                               row_count=EXCLUDED.row_count""",(model,name,row_count))
            elapsed=round(time.perf_counter()-started,2)
            results.append({'model':model,'table':name,'rows':row_count,'elapsed_seconds':elapsed})
            print(f'[READ MODEL] {model}: rows={row_count:,} seconds={elapsed:.1f}',flush=True)
    print('[READ MODEL] PUBLISHED physical snapshots atomically; existing Gold left untouched.',flush=True)
    return results


def status():
    with connection(Settings()) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('dashboard_cache.active_snapshot')")
        if cur.fetchone()[0] is None: return []
        cur.execute("SELECT source_name,snapshot_name,refreshed_at,row_count FROM dashboard_cache.active_snapshot ORDER BY 1")
        return cur.fetchall()


def main():
    p=argparse.ArgumentParser(description='Build fast, physical dashboard snapshots from the authoritative cleaned Gold views')
    p.add_argument('--apply',action='store_true',help='Build snapshots; otherwise only show status')
    p.add_argument('--include-detail',action='store_true',help='Also snapshot detail views (more disk usage)')
    p.add_argument('--timeout-seconds',type=int,default=900)
    a=p.parse_args()
    if not a.apply:
        rows=status()
        print('Read-model snapshots:',*(f'{r[0]} => {r[1]} rows={r[3]} asof={r[2]}' for r in rows),sep='\n')
        return 0
    # If detail snapshots were enabled in an earlier run, keep refreshing them
    # on all subsequent publishes. Never serve stale historical detail silently.
    previous = {str(row[0]) for row in status()}
    detail_active = any(model in previous for model in DETAIL)
    models=CORE + (DETAIL if a.include_detail or detail_active else ())
    if detail_active and not a.include_detail:
        print('[READ MODEL] Existing detail snapshots detected; also refreshing detail for consistency.',flush=True)
    refresh(models,max(30,a.timeout_seconds))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
