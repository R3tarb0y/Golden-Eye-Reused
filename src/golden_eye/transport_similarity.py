from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterator

import numpy as np
from psycopg2.extras import execute_values

from .config import Settings
from .db import connection, fetch_all_dict, named_cursor_batches
from .features import ResNetFeatureExtractor
from .utils import unpack_vector


def _category(score: float | None, settings: Settings) -> tuple[str, bool | None, bool]:
    if score is None:
        return "SKOR TIDAK TERSEDIA", None, False
    threshold = settings.harmony_transaction_same_threshold
    if score >= threshold:
        return f"SAMA ≥ {threshold:g}%", True, False
    return f"BERBEDA < {threshold:g}%", False, False


def _pending_pair_features(
    settings: Settings,
    source_group: str,
    period_year: int,
    fetch_size: int,
) -> Iterator[list[dict[str, Any]]]:
    with connection(settings) as conn:
        yield from named_cursor_batches(
            conn,
            """
                SELECT DISTINCT ON (c.file_uid)
                   c.file_uid,
                   c.source_group,
                   c.evidence_file_name AS file_name,
                   g.file_sha256,
                   true AS is_transport,
                   g.image_bytes
            FROM processing.transport_kt_before_context c
            JOIN processing.gambar_reused g
              ON g.source_group=c.source_group
             AND g.file_uid=c.file_uid
             AND g.is_active=true
            LEFT JOIN processing.image_feature f
              ON f.file_uid=c.file_uid
             AND f.model_version=%s
            WHERE c.source_group=%s
              AND c.period_year=%s
              AND c.is_active=true
              AND c.load_status='READY'
              AND (
                        f.file_uid IS NULL
                    OR f.file_sha256 IS DISTINCT FROM g.file_sha256
                    OR f.is_valid=false
                    OR f.full_embedding IS NULL
                    OR f.embedding_dim IS NULL
                    OR f.embedding_dim <= 0
                  )
            ORDER BY c.file_uid, c.period_date DESC NULLS LAST, c.evidence_key
            """,
            (
                settings.model_version,
                source_group,
                period_year,
            ),
            max(1, fetch_size),
        )


def _embed_pair_features(
    settings: Settings,
    batches: Iterator[list[dict[str, Any]]],
    batch_size: int,
) -> tuple[int, int]:
    extractor = ResNetFeatureExtractor(settings)
    succeeded = failed = 0
    printed_failures = 0
    with connection(settings) as conn:
        for source_batch in batches:
            for start in range(0, len(source_batch), batch_size):
                batch = source_batch[start : start + batch_size]
                result = extractor.process(batch)
                if result.failed and printed_failures < 20:
                    for failed_row in result.rows:
                        if failed_row[-2] is not False:
                            continue
                        print(
                            f"[KP/KT EMBED][FAILED] file_uid={failed_row[0]} "
                            f"file={failed_row[4]} error={failed_row[-1]}",
                            flush=True,
                        )
                        printed_failures += 1
                        if printed_failures >= 20:
                            break
                with conn.cursor() as cur:
                    execute_values(
                        cur,
                        """
                    INSERT INTO processing.image_feature
                        (file_uid, model_version, file_sha256, source_group,
                         file_name, is_transport, embedding_dim, full_embedding,
                         center_embedding, is_valid, error_message)
                    VALUES %s
                    ON CONFLICT (file_uid, model_version) DO UPDATE SET
                        file_sha256=excluded.file_sha256,
                        source_group=excluded.source_group,
                        file_name=excluded.file_name,
                        is_transport=excluded.is_transport,
                        embedding_dim=excluded.embedding_dim,
                        full_embedding=excluded.full_embedding,
                        center_embedding=excluded.center_embedding,
                        is_valid=excluded.is_valid,
                        error_message=excluded.error_message,
                        embedded_at=now()
                    """,
                        result.rows,
                        page_size=500,
                    )
                conn.commit()
                succeeded += result.succeeded
                failed += result.failed
                print(
                    f"[KP/KT EMBED] checked={succeeded + failed:,} "
                    f"ok={succeeded:,} failed={failed:,} "
                    f"device={extractor.device}",
                    flush=True,
                )
    return succeeded, failed


def _panen_scope_metrics(
    settings: Settings,
    source_group: str,
    period_year: int,
) -> dict[str, int]:
    with connection(settings) as conn:
        rows = fetch_all_dict(
            conn,
            """
            WITH ready_transport AS (
                SELECT DISTINCT ON (c.oph_key) c.oph_key
                FROM processing.transport_kt_before_context c
                JOIN processing.gambar_reused g
                  ON g.source_group=c.source_group
                 AND g.file_uid=c.file_uid
                 AND g.is_active=true
                WHERE c.source_group=%s
                  AND c.period_year=%s
                  AND c.is_active=true
                  AND c.load_status='READY'
                ORDER BY c.oph_key, c.period_date DESC NULLS LAST, c.evidence_key
            )
            SELECT
                count(*)::bigint AS all_panen_images,
                count(*) FILTER (
                    WHERE nullif(trim(c.oph_key), '') IS NOT NULL
                )::bigint AS panen_with_oph,
                count(*) FILTER (
                    WHERE t.oph_key IS NOT NULL
                )::bigint AS panen_with_ready_transport,
                count(*) FILTER (
                    WHERE f.file_uid IS NOT NULL
                      AND f.is_valid=true
                      AND f.full_embedding IS NOT NULL
                      AND coalesce(f.embedding_dim, 0) > 0
                )::bigint AS panen_with_valid_embedding
            FROM processing.image_context c
            LEFT JOIN ready_transport t ON t.oph_key=c.oph_key
            LEFT JOIN processing.image_feature f
              ON f.file_uid=c.file_uid
             AND f.model_version=%s
            WHERE upper(c.source_group)=%s
              AND c.period_date IS NOT NULL
              AND extract(year FROM c.period_date)::integer=%s
            """,
            (
                source_group,
                period_year,
                settings.model_version,
                source_group,
                period_year,
            ),
        )
    row = rows[0] if rows else {}
    return {
        "all_panen_images": int(row.get("all_panen_images") or 0),
        "panen_with_oph": int(row.get("panen_with_oph") or 0),
        "panen_with_ready_transport": int(
            row.get("panen_with_ready_transport") or 0
        ),
        "panen_with_valid_embedding": int(
            row.get("panen_with_valid_embedding") or 0
        ),
    }


def _pair_rows(
    settings: Settings,
    source_group: str,
    period_year: int,
    *,
    only_pending: bool = False,
) -> Iterator[list[dict[str, Any]]]:
    with connection(settings) as conn:
        yield from named_cursor_batches(
            conn,
            """
            WITH panen AS (
                SELECT c.oph_key,
                       c.file_uid AS panen_file_uid,
                       c.period_date AS panen_date,
                       coalesce(nullif(trim(f.file_name), ''), c.file_uid)
                           AS panen_file_name,
                       f.embedding_dim AS panen_dim,
                       f.full_embedding AS panen_embedding
                FROM processing.image_context c
                LEFT JOIN processing.image_feature f
                  ON f.file_uid=c.file_uid
                 AND f.model_version=%s
                 AND f.is_valid=true
                WHERE upper(c.source_group)=%s
                  AND c.oph IS NOT NULL
                  AND nullif(trim(c.oph_key), '') IS NOT NULL
                  AND c.period_date IS NOT NULL
                  AND extract(year FROM c.period_date)::integer=%s
            ), transport AS (
                SELECT DISTINCT ON (c.oph_key)
                       c.oph_key,
                       c.file_uid AS transport_file_uid,
                       c.evidence_file_name AS transport_file_name
                FROM processing.transport_kt_before_context c
                JOIN processing.gambar_reused g
                  ON g.source_group=c.source_group
                 AND g.file_uid=c.file_uid
                 AND g.is_active=true
                WHERE c.source_group=%s
                  AND c.period_year=%s
                  AND c.is_active=true
                  AND c.load_status='READY'
                ORDER BY c.oph_key, c.period_date DESC NULLS LAST, c.evidence_key
            ), candidate_pairs AS (
                SELECT p.oph_key,
                       p.panen_file_uid,
                       p.panen_file_name,
                       t.transport_file_uid,
                       t.transport_file_name,
                       p.panen_dim,
                       p.panen_embedding,
                       tf.embedding_dim AS transport_dim,
                       tf.full_embedding AS transport_embedding,
                       existing.similarity_score AS existing_similarity_score,
                       row_number() OVER (
                           PARTITION BY p.oph_key
                           ORDER BY
                               (existing.similarity_score IS NOT NULL) DESC,
                               (
                                   p.panen_embedding IS NOT NULL
                                   AND p.panen_dim IS NOT NULL
                               ) DESC,
                               p.panen_date DESC NULLS LAST,
                               p.panen_file_uid
                       ) AS pair_rank
                FROM panen p
                JOIN transport t ON t.oph_key=p.oph_key
                LEFT JOIN processing.image_feature tf
                  ON tf.file_uid=t.transport_file_uid
                 AND tf.model_version=%s
                 AND tf.is_valid=true
                LEFT JOIN processing.transport_kt_similarity existing
                  ON existing.source_group=%s
                 AND existing.period_year=%s
                 AND existing.panen_file_uid=p.panen_file_uid
                 AND existing.transport_file_uid=t.transport_file_uid
                 AND existing.is_active=true
            )
            SELECT oph_key,
                   panen_file_uid,
                   panen_file_name,
                   transport_file_uid,
                   transport_file_name,
                   panen_dim,
                   panen_embedding,
                   transport_dim,
                   transport_embedding
            FROM candidate_pairs
            WHERE NOT %s
               OR (pair_rank=1 AND existing_similarity_score IS NULL)
            ORDER BY oph_key, panen_file_uid
            """,
            (
                settings.model_version,
                source_group,
                period_year,
                source_group,
                period_year,
                settings.model_version,
                source_group,
                period_year,
                only_pending,
            ),
            max(1, settings.db_fetch_size),
        )


def score_transport_kt_before(
    settings: Settings,
    source_group: str,
    period_year: int,
    *,
    batch_size: int = 64,
    apply: bool = False,
    only_pending: bool = False,
    report_path: str | None = None,
) -> dict[str, Any]:
    source_group = source_group.upper().strip()
    if source_group not in {"PWP", "DIN"}:
        raise ValueError("Scoring KT Before hanya diizinkan untuk PWP atau DIN")
    if not 1 <= batch_size <= 256:
        raise ValueError("--batch-size harus 1..256")

    print("=" * 78)
    print(
        f"{source_group} KP vs KT_BEFORE SIMILARITY | year={period_year} | "
        f"model={settings.model_version} | same_threshold="
        f"{settings.harmony_transaction_same_threshold:g}% | apply={apply} | "
        f"mode={'ONLY_PENDING' if only_pending else 'FULL_REFRESH'}"
    )
    print("=" * 78)

    scope_metrics = _panen_scope_metrics(settings, source_group, period_year)
    print(
        "all_panen_images={all_panen_images:,} "
        "panen_with_oph={panen_with_oph:,} "
        "panen_with_valid_embedding={panen_with_valid_embedding:,} "
        "panen_with_ready_transport={panen_with_ready_transport:,}".format(
            **scope_metrics
        )
    )
    embedded_ok = embedded_failed = 0
    if apply:
        embedded_ok, embedded_failed = _embed_pair_features(
            settings,
            _pending_pair_features(
                settings, source_group, period_year, batch_size
            ),
            batch_size,
        )
        print(
            f"transport_features_processed={embedded_ok + embedded_failed:,}"
        )
    else:
        print(
            "DRY-RUN: embedding Transport yang kurang tidak dibaca ke RAM. "
            "Tambahkan --apply untuk memprosesnya secara streaming."
        )

    category_counts: dict[str, int] = {}
    missing_features = 0
    missing_panen_features = 0
    missing_transport_features = 0
    output = Path(
        report_path
        or f"transport_similarity_{source_group.lower()}_{period_year}.csv"
    ).resolve()
    pair_count = 0
    with output.open("w", encoding="utf-8-sig", newline="") as handle, \
            connection(settings) as write_conn:
        fields = [
            "source_group", "period_year", "oph_key", "panen_file_uid",
            "panen_file_name", "transport_file_uid", "transport_file_name",
            "similarity_score", "similarity_category",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        if apply and only_pending:
            # Previous runs could have persisted technical NULL rows. They are
            # not scores and must not hide the fact that the pair is pending;
            # clear only those rows so the next run can retry them cleanly.
            with write_conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE processing.transport_kt_similarity
                    SET is_active=false
                    WHERE source_group=%s
                      AND period_year=%s
                      AND is_active=true
                      AND similarity_score IS NULL
                    """,
                    (source_group, period_year),
                )
        if apply and not only_pending:
            with write_conn.cursor() as cur:
                cur.execute(
                """
                UPDATE processing.transport_kt_similarity
                SET is_active=false
                WHERE source_group=%s AND period_year=%s AND is_active=true
                """,
                (source_group, period_year),
            )
        for pair_batch in _pair_rows(
            settings,
            source_group,
            period_year,
            only_pending=only_pending,
        ):
            db_rows: list[tuple[Any, ...]] = []
            for row in pair_batch:
                score: float | None = None
                panen_feature_ready = bool(
                    row.get("panen_embedding") is not None
                    and row.get("panen_dim")
                )
                transport_feature_ready = bool(
                    row.get("transport_embedding") is not None
                    and row.get("transport_dim")
                )
                if not panen_feature_ready:
                    missing_panen_features += 1
                if not transport_feature_ready:
                    missing_transport_features += 1
                if panen_feature_ready and transport_feature_ready:
                    left = unpack_vector(
                        row["panen_embedding"], int(row["panen_dim"])
                    )
                    right = unpack_vector(
                        row["transport_embedding"], int(row["transport_dim"])
                    )
                    if left.shape == right.shape:
                        score = float(np.dot(left, right) * 100.0)
                if score is None:
                    missing_features += 1
                category, is_same, is_review = _category(score, settings)
                category_counts[category] = category_counts.get(category, 0) + 1
                writer.writerow({
                    "source_group": source_group,
                    "period_year": period_year,
                    "oph_key": row["oph_key"],
                    "panen_file_uid": row["panen_file_uid"],
                    "panen_file_name": row["panen_file_name"],
                    "transport_file_uid": row["transport_file_uid"],
                    "transport_file_name": row["transport_file_name"],
                    "similarity_score": "" if score is None else round(score, 4),
                    "similarity_category": category,
                })
                if score is not None or not only_pending:
                    db_rows.append((
                        source_group, period_year, row["oph_key"],
                        row["panen_file_uid"], row["panen_file_name"],
                        row["transport_file_uid"], row["transport_file_name"],
                        settings.model_version, score, category, is_same,
                        is_review, True,
                    ))
            pair_count += len(pair_batch)
            if apply and db_rows:
                with write_conn.cursor() as cur:
                    execute_values(
                        cur,
                    """
                    INSERT INTO processing.transport_kt_similarity AS current_row
                        (source_group, period_year, oph_key, panen_file_uid,
                         panen_file_name, transport_file_uid, transport_file_name,
                         model_version, similarity_score, similarity_category,
                         is_same, is_review, is_active)
                    VALUES %s
                    ON CONFLICT (
                        source_group, period_year, panen_file_uid,
                        transport_file_uid
                    ) DO UPDATE SET
                        oph_key=excluded.oph_key,
                        panen_file_name=excluded.panen_file_name,
                        transport_file_name=excluded.transport_file_name,
                        model_version=excluded.model_version,
                        similarity_score=excluded.similarity_score,
                        similarity_category=excluded.similarity_category,
                        is_same=excluded.is_same,
                        is_review=excluded.is_review,
                        calculated_at=now(),
                        is_active=true
                    """,
                        db_rows,
                        page_size=500,
                    )
            print(
                f"[KP/KT SCORE] pairs={pair_count:,} "
                f"missing_features={missing_features:,} "
                f"missing_panen={missing_panen_features:,} "
                f"missing_transport={missing_transport_features:,}",
                flush=True,
            )

    print(
        f"pairs={pair_count:,} missing_features={missing_features:,} "
        f"missing_panen={missing_panen_features:,} "
        f"missing_transport={missing_transport_features:,}"
    )
    for category, count in sorted(category_counts.items()):
        # Windows Scheduled Task / legacy console can still expose cp1252.
        # Keep the canonical DB/UI category (e.g. "SAMA ≥ 83%"), but make
        # terminal-only summary ASCII-safe so logging can never abort scoring.
        console_category = category.replace("≥", ">=").replace("≤", "<=")
        print(f"  {console_category}: {count:,}")
    print(f"report={output}")
    if not apply:
        print("DRY-RUN saja; hasil similarity belum disimpan.")
    return {
        **scope_metrics,
        "pairs": pair_count,
        "missing_features": missing_features,
        "missing_panen_features": missing_panen_features,
        "missing_transport_features": missing_transport_features,
        "category_counts": category_counts,
        "transport_embedded_ok": embedded_ok,
        "transport_embedded_failed": embedded_failed,
        "only_pending": only_pending,
        "applied": apply,
        "report": str(output),
    }
