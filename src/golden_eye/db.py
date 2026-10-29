from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values, register_uuid

from .config import ROOT, Settings


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# psycopg2 tidak selalu mengaktifkan adapter uuid.UUID secara otomatis.
# Pipeline memakai UUID pada seluruh tabel per-run, jadi adapter didaftarkan
# sekali di lapisan koneksi pusat sebelum ada query yang dijalankan.
register_uuid()


def ident(value: str) -> str:
    if not _IDENT.fullmatch(str(value or "")):
        raise ValueError(f"Identifier PostgreSQL tidak aman: {value!r}")
    return '"' + value + '"'


def qname(schema: str, table: str) -> str:
    return f"{ident(schema)}.{ident(table)}"


@contextmanager
def connection(settings: Settings, *, autocommit: bool = False):
    conn = psycopg2.connect(**settings.connection_kwargs())
    conn.autocommit = autocommit
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def pipeline_job_lock(
    settings: Settings,
    job_name: str,
    *,
    lock_scope: str | None = None,
):
    """Cegah dua heavy pipeline memakai DB/GPU secara bersamaan.

    Session-level advisory lock otomatis dilepas PostgreSQL bila Python ditutup
    atau koneksi putus. Lock connection memakai autocommit sehingga tidak
    menjadi ``idle in transaction`` dan tidak menahan snapshot/VACUUM.
    """
    global_lock_name = "golden_eye_db_first:exclusive_pipeline"
    lock_name = (
        f"golden_eye_db_first:scoped:{lock_scope}"
        if lock_scope
        else global_lock_name
    )
    lock_mode = "scoped-job" if lock_scope else "single-heavy-job"
    with connection(settings, autocommit=True) as conn, conn.cursor() as cur:
        shared_gate = False
        if lock_scope:
            cur.execute(
                "SELECT pg_try_advisory_lock_shared(hashtext(%s)::bigint)",
                (global_lock_name,),
            )
            shared_gate = bool(cur.fetchone()[0])
        else:
            shared_gate = True
        acquired = False
        if shared_gate:
            cur.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)::bigint)",
                (lock_name,),
            )
            acquired = bool(cur.fetchone()[0])
        if not acquired:
            if lock_scope and shared_gate:
                cur.execute(
                    "SELECT pg_advisory_unlock_shared(hashtext(%s)::bigint)",
                    (global_lock_name,),
                )
            raise RuntimeError(
                f"Masih ada job Golden Eye dengan scope {lock_scope or 'global'} "
                "yang aktif. Tunggu job tersebut selesai atau bersihkan session "
                "lama di pg_stat_activity."
            )
        print(f"[JOB LOCK] acquired job={job_name}; mode={lock_mode}", flush=True)
        try:
            yield
        finally:
            cur.execute("SELECT pg_advisory_unlock(hashtext(%s)::bigint)", (lock_name,))
            if lock_scope:
                cur.execute(
                    "SELECT pg_advisory_unlock_shared(hashtext(%s)::bigint)",
                    (global_lock_name,),
                )
            print(f"[JOB LOCK] released job={job_name}", flush=True)


def init_database(settings: Settings) -> None:
    sql_dir = ROOT / "sql"
    with connection(settings) as conn:
        with conn.cursor() as cur:
            for path in sorted(sql_dir.glob("*.sql")):
                sql = path.read_text(encoding="utf-8")
                sql = sql.replace("{{SILVER_SCHEMA}}", ident(settings.silver_schema))
                sql = sql.replace("{{IMAGE_TABLE}}", ident(settings.image_table))
                try:
                    cur.execute(sql)
                except Exception as exc:
                    raise RuntimeError(
                        f"INIT-DB gagal pada migration {path.name}: {exc}"
                    ) from exc
                print(f"Applied: {path.name}")


def table_exists(conn, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema=%s AND table_name=%s
            )
            """,
            (schema, table),
        )
        return bool(cur.fetchone()[0])


def table_columns(conn, schema: str, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema=%s AND table_name=%s
            ORDER BY ordinal_position
            """,
            (schema, table),
        )
        return [row[0] for row in cur.fetchall()]


def create_run(settings: Settings, source_group: str) -> uuid.UUID:
    run_id = uuid.uuid4()
    payload = {
        "model_version": settings.model_version,
        "review_threshold": settings.review_threshold,
        "reused_threshold": settings.reused_threshold,
        "sap_period_year": settings.sap_period_year,
        "sap_scope_mode": settings.sap_scope_mode,
        "sap_prfnr_scope": list(settings.sap_prfnr_values(source_group)),
        "sap_estate_scope": list(settings.sap_estate_scope_values(source_group)),
        "sap_prfnr_configured": list(settings.sap_configured_prfnr_values(source_group)),
        "match_top_k": settings.match_top_k,
        "match_mode": settings.match_mode,
        "match_engine": settings.match_engine,
        "faiss_index_mode": settings.faiss_index_mode,
        "match_auto_cap_top_k": settings.match_auto_cap_top_k,
        "match_require_mutual_knn": settings.match_require_mutual_knn,
        "match_component_max_size": settings.match_component_max_size,
        "validation_full_pair_max_group": settings.validation_full_pair_max_group,
        "validation_device": settings.validation_device,
        "validation_gpu_pair_batch": settings.validation_gpu_pair_batch,
        "validation_cpu_workers": settings.validation_cpu_workers,
        "validation_nas_io_workers": settings.validation_nas_io_workers,
        "validation_cpu_pair_batch": settings.validation_cpu_pair_batch,
        "validation_db_member_batch": settings.validation_db_member_batch,
        "validation_db_candidate_batch": settings.validation_db_candidate_batch,
        "validation_max_audit_pairs_per_group": settings.validation_max_audit_pairs_per_group,
        "validation_group_mean_similarity": settings.validation_group_mean_similarity,
        "validation_member_mean_similarity": settings.validation_member_mean_similarity,
        "validation_representative_similarity": settings.validation_representative_similarity,
        "low_light_guard": settings.low_light_guard,
        "low_light_max_mean": settings.low_light_max_mean,
        "low_light_min_dark_fraction": settings.low_light_min_dark_fraction,
        "low_light_near_exact_similarity": settings.low_light_near_exact_similarity,
        "low_light_min_edgevec_similarity": settings.low_light_min_edgevec_similarity,
        "low_light_min_orb_inliers": settings.low_light_min_orb_inliers,
        "low_light_min_orb_ratio": settings.low_light_min_orb_ratio,
        "duplicate_require_structural_evidence": settings.duplicate_require_structural_evidence,
        "duplicate_geometric_similarity": settings.duplicate_geometric_similarity,
        "duplicate_require_hash_support": settings.duplicate_require_hash_support,
        "max_accepted_group_size": settings.max_accepted_group_size,
    }
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO processing.pipeline_run
                (run_id, source_group, stage, status, settings)
            VALUES (%s, %s, 'CREATED', 'RUNNING', %s::jsonb)
            """,
            (run_id, source_group.upper(), json.dumps(payload)),
        )
    return run_id


def update_run(
    settings: Settings,
    run_id: uuid.UUID,
    *,
    stage: str,
    status: str = "RUNNING",
    metrics: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> None:
    finished = "now()" if status in {"SUCCEEDED", "FAILED"} else "NULL"
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE processing.pipeline_run
            SET stage=%s,
                status=%s,
                metrics=metrics || %s::jsonb,
                error_message=%s,
                finished_at={finished},
                updated_at=now()
            WHERE run_id=%s
            """,
            (stage, status, json.dumps(metrics or {}), error_message, run_id),
        )


def fetch_all_dict(conn, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params or ())
        return [dict(row) for row in cur.fetchall()]


def insert_rows(
    cur,
    table_sql: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    page_size: int = 1000,
    suffix: str = "",
) -> None:
    rows = list(rows)
    if not rows:
        return
    cols = ", ".join(ident(c) for c in columns)
    execute_values(
        cur,
        f"INSERT INTO {table_sql} ({cols}) VALUES %s {suffix}",
        rows,
        page_size=page_size,
    )


def named_cursor_batches(
    conn,
    sql: str,
    params: Sequence[Any] | None,
    fetch_size: int,
) -> Iterator[list[dict[str, Any]]]:
    name = f"ge_{uuid.uuid4().hex}"
    with conn.cursor(name=name, cursor_factory=RealDictCursor) as cur:
        cur.itersize = fetch_size
        cur.execute(sql, params or ())
        while True:
            batch = cur.fetchmany(fetch_size)
            if not batch:
                break
            yield [dict(row) for row in batch]
