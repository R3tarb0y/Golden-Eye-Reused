from __future__ import annotations

import csv
import hashlib
import os
from pathlib import Path
from typing import Any, Iterable

from psycopg2.extras import execute_values

from .config import Settings
from .db import connection, qname


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
PROGRESS_EVERY = 25_000


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS processing.panen_nas_path_index (
                source_group text NOT NULL,
                period_year integer NOT NULL,
                file_name_upper text NOT NULL,
                file_name text NOT NULL,
                nas_path text NOT NULL,
                source_root text NOT NULL,
                file_size_bytes bigint,
                file_mtime_ns bigint,
                expected_sha256 text,
                resolution_status text NOT NULL,
                indexed_at timestamptz NOT NULL DEFAULT now(),
                is_active boolean NOT NULL DEFAULT true,
                PRIMARY KEY (source_group, period_year, file_name_upper)
            );
            CREATE INDEX IF NOT EXISTS ix_panen_nas_path_active_name
                ON processing.panen_nas_path_index
                   (source_group, period_year, is_active, file_name_upper);
            """
        )
        cur.execute(
            """
            ALTER TABLE processing.panen_nas_path_index
                ALTER COLUMN file_size_bytes DROP NOT NULL
            """
        )


def _load_targets(
    settings: Settings,
    source_group: str,
    period_year: int,
) -> tuple[dict[str, tuple[str, str]], dict[str, int]]:
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            WITH active_gold_name AS (
                SELECT source_group,
                       file_uid,
                       min(nullif(btrim(image_name), '')) AS file_name
                FROM gold.reused_photo_detail
                WHERE is_active=true
                GROUP BY source_group, file_uid
            )
            SELECT upper(coalesce(
                       nullif(btrim(f.file_name), ''),
                       nullif(btrim(g.file_name), ''),
                       nullif(btrim(d.file_name), ''),
                       nullif(btrim(i.file_name::text), '')
                   ))
                       AS file_name_upper,
                   min(coalesce(
                       nullif(btrim(f.file_name), ''),
                       nullif(btrim(g.file_name), ''),
                       nullif(btrim(d.file_name), ''),
                       nullif(btrim(i.file_name::text), '')
                   ))
                       AS file_name,
                   CASE
                       WHEN count(DISTINCT nullif(
                           coalesce(
                               nullif(btrim(g.file_sha256), ''),
                               nullif(btrim(f.file_sha256), ''),
                               nullif(btrim(i.file_sha256::text), '')
                           ), ''
                       ))=1
                       THEN min(nullif(
                           coalesce(
                               nullif(btrim(g.file_sha256), ''),
                               nullif(btrim(f.file_sha256), ''),
                               nullif(btrim(i.file_sha256::text), '')
                           ), ''
                       ))
                       ELSE NULL
                   END AS expected_sha256
            FROM processing.image_context c
            LEFT JOIN processing.image_feature f
              ON f.file_uid=c.file_uid
             AND f.model_version=%s
             AND f.is_transport=false
            LEFT JOIN processing.gambar_reused g
              ON g.source_group=upper(c.source_group)
             AND g.file_uid=c.file_uid
             AND g.is_active=true
            LEFT JOIN active_gold_name d
              ON d.source_group=upper(c.source_group)
             AND d.file_uid=c.file_uid
            LEFT JOIN {qname(settings.silver_schema, settings.image_table)} i
              ON i.file_uid::text=c.file_uid
            WHERE upper(c.source_group)=%s
              AND c.period_date IS NOT NULL
              AND extract(year FROM c.period_date)::integer=%s
              AND coalesce(i.is_transport, false)=false
              AND coalesce(
                  nullif(btrim(f.file_name), ''),
                  nullif(btrim(g.file_name), ''),
                  nullif(btrim(d.file_name), ''),
                  nullif(btrim(i.file_name::text), '')
              ) IS NOT NULL
            GROUP BY upper(coalesce(
                nullif(btrim(f.file_name), ''),
                nullif(btrim(g.file_name), ''),
                nullif(btrim(d.file_name), ''),
                nullif(btrim(i.file_name::text), '')
            ))
            """,
            (settings.model_version, source_group, period_year),
        )
        targets = {
            str(row[0]): (str(row[1]), _clean(row[2]).lower())
            for row in cur.fetchall()
            if row and _clean(row[0])
        }
        cur.execute(
            f"""
            WITH active_gold_name AS (
                SELECT source_group,
                       file_uid,
                       min(nullif(btrim(image_name), '')) AS file_name
                FROM gold.reused_photo_detail
                WHERE is_active=true
                GROUP BY source_group, file_uid
            ), resolved AS (
                SELECT c.file_uid,
                       nullif(btrim(coalesce(c.oph_key, '')), '') IS NOT NULL
                           AS has_oph,
                       (
                           f.file_uid IS NULL
                           OR f.is_transport=true
                           OR f.is_valid=false
                           OR f.full_embedding IS NULL
                           OR f.embedding_dim IS NULL
                           OR f.embedding_dim <= 0
                       ) AS needs_embedding,
                       CASE
                           WHEN nullif(btrim(f.file_name), '') IS NOT NULL
                               THEN 'FEATURE'
                           WHEN nullif(btrim(g.file_name), '') IS NOT NULL
                               THEN 'HOT_STORE'
                           WHEN nullif(btrim(d.file_name), '') IS NOT NULL
                               THEN 'ACTIVE_GOLD'
                           WHEN nullif(btrim(i.file_name::text), '') IS NOT NULL
                               THEN 'SILVER'
                           ELSE 'UNNAMED'
                       END AS filename_source,
                       coalesce(
                           nullif(btrim(f.file_name), ''),
                           nullif(btrim(g.file_name), ''),
                           nullif(btrim(d.file_name), ''),
                           nullif(btrim(i.file_name::text), '')
                       ) AS file_name
                FROM processing.image_context c
                LEFT JOIN processing.image_feature f
                  ON f.file_uid=c.file_uid
                 AND f.model_version=%s
                 AND f.is_transport=false
                LEFT JOIN processing.gambar_reused g
                  ON g.source_group=upper(c.source_group)
                 AND g.file_uid=c.file_uid
                 AND g.is_active=true
                LEFT JOIN active_gold_name d
                  ON d.source_group=upper(c.source_group)
                 AND d.file_uid=c.file_uid
                LEFT JOIN {qname(settings.silver_schema, settings.image_table)} i
                  ON i.file_uid::text=c.file_uid
                WHERE upper(c.source_group)=%s
                  AND c.period_date IS NOT NULL
                  AND extract(year FROM c.period_date)::integer=%s
                  AND coalesce(i.is_transport, false)=false
            )
            SELECT count(*)::bigint AS context_rows,
                   count(*) FILTER (WHERE file_name IS NOT NULL)::bigint
                       AS named_context_rows,
                   count(*) FILTER (WHERE file_name IS NULL)::bigint
                       AS unnamed_context_rows,
                   count(*) FILTER (
                       WHERE has_oph AND needs_embedding
                   )::bigint AS oph_needs_embedding_rows,
                   count(*) FILTER (
                       WHERE has_oph AND needs_embedding
                         AND filename_source='FEATURE'
                   )::bigint AS oph_missing_from_feature_name,
                   count(*) FILTER (
                       WHERE has_oph AND needs_embedding
                         AND filename_source='HOT_STORE'
                   )::bigint AS oph_missing_from_hot_store_name,
                   count(*) FILTER (
                       WHERE has_oph AND needs_embedding
                         AND filename_source='ACTIVE_GOLD'
                   )::bigint AS oph_missing_from_active_gold_name,
                   count(*) FILTER (
                       WHERE has_oph AND needs_embedding
                         AND filename_source='SILVER'
                   )::bigint AS oph_missing_from_silver_name,
                   count(*) FILTER (
                       WHERE has_oph AND needs_embedding
                         AND filename_source='UNNAMED'
                   )::bigint AS oph_missing_unnamed
            FROM resolved
            """,
            (settings.model_version, source_group, period_year),
        )
        row = cur.fetchone() or (0,) * 9
        diagnostics = {
            "context_rows": int(row[0] or 0),
            "named_context_rows": int(row[1] or 0),
            "unnamed_context_rows": int(row[2] or 0),
            "oph_needs_embedding_rows": int(row[3] or 0),
            "oph_missing_from_feature_name": int(row[4] or 0),
            "oph_missing_from_hot_store_name": int(row[5] or 0),
            "oph_missing_from_active_gold_name": int(row[6] or 0),
            "oph_missing_from_silver_name": int(row[7] or 0),
            "oph_missing_unnamed": int(row[8] or 0),
        }
        return targets, diagnostics


def _walk_roots(
    roots: list[Path],
    wanted: set[str],
) -> tuple[dict[str, Path], dict[str, list[Path]], int, int]:
    first: dict[str, Path] = {}
    duplicates: dict[str, list[Path]] = {}
    scanned = errors = 0
    for root in roots:
        if not root.exists() or not root.is_dir():
            raise FileNotFoundError(f"NAS root tidak tersedia: {root}")
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                scanned += 1
                if scanned % PROGRESS_EVERY == 0:
                    print(
                        f"[PANEN NAS INDEX] scanned={scanned:,} "
                        f"matched={len(first):,} duplicates={len(duplicates):,}",
                        flush=True,
                    )
                if Path(filename).suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                key = filename.strip().upper()
                if key not in wanted:
                    continue
                path = Path(dirpath) / filename
                existing = first.get(key)
                if existing is None:
                    first[key] = path
                elif path != existing:
                    bucket = duplicates.setdefault(key, [existing])
                    if len(bucket) < 20:
                        bucket.append(path)
    return first, duplicates, scanned, errors


def _resolve_duplicate(
    paths: list[Path],
    expected_sha256: str,
) -> tuple[Path | None, str]:
    if not expected_sha256:
        return None, "AMBIGUOUS_NO_EXPECTED_HASH"
    matches: list[Path] = []
    for path in sorted(set(paths), key=lambda value: str(value).upper()):
        try:
            if _sha256(path).lower() == expected_sha256:
                matches.append(path)
        except OSError:
            continue
    if not matches:
        return None, "AMBIGUOUS_HASH_NOT_MATCHED"
    return matches[0], "READY_DUPLICATE_HASH_PROVEN"


def index_panen_nas_paths(
    settings: Settings,
    source_group: str,
    period_year: int,
    nas_roots: Iterable[str | Path],
    *,
    apply: bool = False,
    report_path: str | None = None,
) -> dict[str, int | str | bool]:
    source_group = str(source_group or "").upper().strip()
    if source_group not in {"PWP", "DIN"}:
        raise ValueError("index-panen-nas hanya untuk PWP atau DIN")
    period_year = int(period_year)
    roots = [Path(value) for value in nas_roots]
    if not roots:
        raise ValueError("Minimal satu --nas-root wajib diberikan")

    targets, target_diagnostics = _load_targets(
        settings, source_group, period_year
    )
    print(
        f"[PANEN NAS INDEX] source={source_group} year={period_year} "
        f"targets={len(targets):,} "
        f"named_context={target_diagnostics['named_context_rows']:,} "
        f"unnamed_context={target_diagnostics['unnamed_context_rows']:,} "
        f"roots={len(roots)} apply={apply}",
        flush=True,
    )
    print(
        "[PANEN NAS INDEX] OPH_NEEDS_EMBEDDING "
        f"total={target_diagnostics['oph_needs_embedding_rows']:,} "
        f"name_feature="
        f"{target_diagnostics['oph_missing_from_feature_name']:,} "
        f"name_hot_store="
        f"{target_diagnostics['oph_missing_from_hot_store_name']:,} "
        f"name_active_gold="
        f"{target_diagnostics['oph_missing_from_active_gold_name']:,} "
        f"name_silver="
        f"{target_diagnostics['oph_missing_from_silver_name']:,} "
        f"unnamed={target_diagnostics['oph_missing_unnamed']:,}",
        flush=True,
    )
    first, duplicates, scanned, errors = _walk_roots(roots, set(targets))
    print(
        f"[PANEN NAS INDEX] scan_complete scanned={scanned:,} "
        f"matched={len(first):,}; resolving metadata tanpa stat ulang ...",
        flush=True,
    )

    ready_rows: list[tuple[Any, ...]] = []
    problem_rows: list[dict[str, Any]] = []
    for key, (file_name, expected_sha256) in targets.items():
        path = first.get(key)
        status = "READY_UNIQUE_FILENAME"
        candidate_count = 1 if path else 0
        if key in duplicates:
            candidate_count = len(duplicates[key])
            path, status = _resolve_duplicate(
                duplicates[key], expected_sha256
            )
        elif path is None:
            status = "NAS_FILE_NOT_FOUND"

        if path is None:
            problem_rows.append(
                {
                    "source_group": source_group,
                    "period_year": period_year,
                    "file_name": file_name,
                    "expected_sha256": expected_sha256,
                    "status": status,
                    "candidate_count": candidate_count,
                }
            )
            continue
        source_root = next(
            (str(root) for root in roots if str(path).upper().startswith(str(root).upper())),
            str(path.parent),
        )
        ready_rows.append(
            (
                source_group,
                period_year,
                key,
                path.name,
                str(path),
                source_root,
                None,
                None,
                expected_sha256 or None,
                status,
                True,
            )
        )

    report = Path(
        report_path
        or f"panen_nas_index_{source_group.lower()}_{period_year}.csv"
    ).resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "source_group", "period_year", "file_name",
                "expected_sha256", "status", "candidate_count",
            ],
        )
        writer.writeheader()
        writer.writerows(problem_rows)

    if apply:
        print(
            f"[PANEN NAS INDEX] db_write_start rows={len(ready_rows):,}",
            flush=True,
        )
        with connection(settings) as conn:
            _ensure_table(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE processing.panen_nas_path_index
                    SET is_active=false, indexed_at=now()
                    WHERE source_group=%s AND period_year=%s
                    """,
                    (source_group, period_year),
                )
                if ready_rows:
                    insert_sql = """
                        INSERT INTO processing.panen_nas_path_index
                            (source_group, period_year, file_name_upper,
                             file_name, nas_path, source_root,
                             file_size_bytes, file_mtime_ns,
                             expected_sha256, resolution_status, is_active)
                        VALUES %s
                        ON CONFLICT (source_group, period_year, file_name_upper)
                        DO UPDATE SET
                            file_name=excluded.file_name,
                            nas_path=excluded.nas_path,
                            source_root=excluded.source_root,
                            file_size_bytes=excluded.file_size_bytes,
                            file_mtime_ns=excluded.file_mtime_ns,
                            expected_sha256=excluded.expected_sha256,
                            resolution_status=excluded.resolution_status,
                            indexed_at=now(),
                            is_active=true
                        """
                    write_batch = 10_000
                    for start in range(0, len(ready_rows), write_batch):
                        batch = ready_rows[start : start + write_batch]
                        execute_values(
                            cur,
                            insert_sql,
                            batch,
                            page_size=5000,
                        )
                        written = start + len(batch)
                        if written % 50_000 == 0 or written == len(ready_rows):
                            print(
                                f"[PANEN NAS INDEX] db_written="
                                f"{written:,}/{len(ready_rows):,}",
                                flush=True,
                            )

    print(
        f"[PANEN NAS INDEX] scanned={scanned:,} ready={len(ready_rows):,} "
        f"unresolved={len(problem_rows):,} duplicates={len(duplicates):,} "
        f"report={report}",
        flush=True,
    )
    return {
        "targets": len(targets),
        **target_diagnostics,
        "scanned": scanned,
        "ready": len(ready_rows),
        "unresolved": len(problem_rows),
        "duplicates": len(duplicates),
        "errors": errors,
        "report": str(report),
        "applied": apply,
    }
