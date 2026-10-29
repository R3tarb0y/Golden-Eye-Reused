from __future__ import annotations

import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Iterator

import numpy as np
import torch
from psycopg2.extras import execute_values

from .config import Settings
from .db import connection, fetch_all_dict, update_run
from .utils import UnionFind, unpack_vector


_EDGE_SCORE_SCALE = 10_000


@dataclass(frozen=True)
class ImageRecord:
    file_uid: str
    file_name: str


def _load_vectors(
    settings: Settings,
    source_group: str,
    *,
    allow_nas_validation: bool = False,
) -> tuple[list[ImageRecord], np.ndarray]:
    rank_filter = "WHERE logical_rank=1" if settings.deduplicate_logical_filename else ""
    source_storage_joins = "" if allow_nas_validation else """
                LEFT JOIN processing.source_bytea_exception bx
                  ON bx.source_group=upper(f.source_group)
                 AND bx.file_uid=f.file_uid
                 AND bx.is_active=true
                LEFT JOIN processing.gambar_reused r
                  ON r.source_group=upper(f.source_group)
                 AND r.file_uid=f.file_uid
                 AND r.is_active=true
    """
    source_storage_guard = (
        "" if allow_nas_validation
        else "AND (bx.file_uid IS NULL OR r.file_uid IS NOT NULL)"
    )
    with connection(settings) as conn:
        rows = fetch_all_dict(
            conn,
            f"""
            WITH eligible AS (
                SELECT
                    f.file_uid,
                    f.file_name,
                    f.embedding_dim,
                    f.full_embedding,
                    row_number() OVER (
                        PARTITION BY upper(trim(f.file_name))
                        ORDER BY
                            (c.period_date IS NOT NULL) DESC,
                            f.embedded_at DESC,
                            f.file_uid
                    ) AS logical_rank
                FROM processing.image_feature f
                LEFT JOIN processing.image_context c ON c.file_uid=f.file_uid
                {source_storage_joins}
                WHERE upper(f.source_group)=upper(%s)
                  AND f.model_version=%s
                  AND f.is_valid=true
                  AND f.is_transport=false
                  AND lower(f.file_name) ~ %s
                  AND upper(f.file_name) !~ %s
                  AND f.full_embedding IS NOT NULL
                  {source_storage_guard}
            )
            SELECT file_uid, file_name, embedding_dim, full_embedding
            FROM eligible
            {rank_filter}
            ORDER BY file_uid
            """,
            (
                source_group,
                settings.model_version,
                settings.valid_extension_regex(),
                settings.excluded_filename_regex(),
            ),
        )
    if not rows:
        return [], np.empty((0, 0), dtype=np.float32)

    dimension = int(rows[0]["embedding_dim"])
    matrix = np.empty((len(rows), dimension), dtype=np.float32)
    images: list[ImageRecord] = []
    for idx, row in enumerate(rows):
        row_dimension = int(row["embedding_dim"])
        if row_dimension != dimension:
            raise RuntimeError(
                f"Dimensi embedding tidak konsisten: expected={dimension}, "
                f"file_uid={row['file_uid']} actual={row_dimension}"
            )
        matrix[idx] = unpack_vector(row["full_embedding"], row_dimension)
        images.append(ImageRecord(file_uid=row["file_uid"], file_name=row["file_name"]))
    return images, matrix


def _bounded_top_k(configured_top_k: int, max_edges: int, image_count: int) -> int:
    if image_count <= 0:
        return configured_top_k
    return max(1, min(configured_top_k, max_edges // image_count))


def _pack_edge(score: float, direction_mask: int) -> int:
    return (int(round(score * _EDGE_SCORE_SCALE)) << 2) | (direction_mask & 0b11)


def _edge_score(packed: int) -> float:
    return float(packed >> 2) / _EDGE_SCORE_SCALE


def _edge_direction_mask(packed: int) -> int:
    return packed & 0b11


def _faiss_knn(
    settings: Settings,
    matrix: np.ndarray,
    top_k: int,
) -> Iterator[tuple[int, int, float]]:
    import faiss  # type: ignore

    mode = settings.faiss_index_mode
    if mode == "auto":
        mode = "flat" if len(matrix) <= settings.faiss_flat_max_images else "hnsw"

    contiguous = np.ascontiguousarray(matrix, dtype=np.float32)
    build_started = time.time()
    if mode == "hnsw":
        print(
            f"[MATCH] engine=FAISS HNSW approximate | images={len(matrix):,} "
            f"M={settings.faiss_hnsw_m} "
            f"efConstruction={settings.faiss_hnsw_ef_construction} "
            f"efSearch={max(settings.faiss_hnsw_ef_search, top_k + 1)}"
        )
        index = faiss.IndexHNSWFlat(
            matrix.shape[1],
            settings.faiss_hnsw_m,
            faiss.METRIC_INNER_PRODUCT,
        )
        index.hnsw.efConstruction = settings.faiss_hnsw_ef_construction
        index.hnsw.efSearch = max(settings.faiss_hnsw_ef_search, top_k + 1)
    else:
        print(f"[MATCH] engine=FAISS IndexFlatIP exact | images={len(matrix):,}")
        index = faiss.IndexFlatIP(matrix.shape[1])

    print("[MATCH] membangun index FAISS...")
    index.add(contiguous)
    print(f"[MATCH] index FAISS siap | elapsed={time.time() - build_started:,.1f}s")
    k = min(len(matrix), top_k + 1)
    search_started = time.time()
    total_blocks = (len(matrix) + settings.match_query_block - 1) // settings.match_query_block
    for block_no, start in enumerate(
        range(0, len(matrix), settings.match_query_block),
        start=1,
    ):
        stop = min(len(matrix), start + settings.match_query_block)
        scores, indices = index.search(np.ascontiguousarray(matrix[start:stop]), k)
        if block_no == 1 or block_no % 10 == 0 or block_no == total_blocks:
            print(
                f"[MATCH] FAISS search={stop:,}/{len(matrix):,} "
                f"blocks={block_no}/{total_blocks} "
                f"elapsed={time.time() - search_started:,.1f}s"
            )
        for local_i, (score_row, index_row) in enumerate(zip(scores, indices)):
            i = start + local_i
            yielded = 0
            for score, j in zip(score_row, index_row):
                if j < 0 or i == int(j):
                    continue
                yield i, int(j), float(score * 100.0)
                yielded += 1
                if yielded >= top_k:
                    break


def _torch_knn(
    matrix: np.ndarray,
    top_k: int,
    query_block: int,
    reference_block: int,
    use_gpu: bool,
) -> Iterator[tuple[int, int, float]]:
    device = torch.device("cuda" if use_gpu and torch.cuda.is_available() else "cpu")
    device_name = torch.cuda.get_device_name(0) if device.type == "cuda" else "CPU"
    print(
        f"[MATCH] engine=Torch blockwise exact top-k | "
        f"device={device} ({device_name}) | images={len(matrix):,}"
    )
    all_cpu = torch.from_numpy(matrix)
    # +1 karena self-match bernilai 1.0 dan dibuang saat hasil dibaca.
    k = min(len(matrix), max(2, top_k + 1))

    search_started = time.time()
    total_blocks = (len(matrix) + query_block - 1) // query_block
    for block_no, q_start in enumerate(range(0, len(matrix), query_block), start=1):
        q_stop = min(len(matrix), q_start + query_block)
        query = all_cpu[q_start:q_stop].to(device)
        best_scores = torch.full((len(query), k), -2.0, device=device)
        best_indices = torch.full((len(query), k), -1, dtype=torch.long, device=device)

        for r_start in range(0, len(matrix), reference_block):
            r_stop = min(len(matrix), r_start + reference_block)
            refs = all_cpu[r_start:r_stop].to(device)
            scores = query @ refs.T
            ref_ids = torch.arange(r_start, r_stop, device=device).expand(len(query), -1)

            combined_scores = torch.cat([best_scores, scores], dim=1)
            combined_indices = torch.cat([best_indices, ref_ids], dim=1)
            best_scores, positions = torch.topk(combined_scores, k=k, dim=1)
            best_indices = torch.gather(combined_indices, 1, positions)

        if block_no == 1 or block_no % 5 == 0 or block_no == total_blocks:
            print(
                f"[MATCH] Torch search={q_stop:,}/{len(matrix):,} "
                f"blocks={block_no}/{total_blocks} "
                f"elapsed={time.time() - search_started:,.1f}s device={device}"
            )

        score_np = best_scores.cpu().numpy()
        index_np = best_indices.cpu().numpy()
        for local_i in range(len(query)):
            i = q_start + local_i
            yielded = 0
            for score, j in zip(score_np[local_i], index_np[local_i]):
                if j < 0 or i == int(j):
                    continue
                yield i, int(j), float(score * 100.0)
                yielded += 1
                if yielded >= top_k:
                    break


def _cuda_match_ready() -> tuple[bool, str]:
    if not torch.cuda.is_available():
        return False, "torch.cuda.is_available()=False"
    try:
        left = torch.ones((16, 16), dtype=torch.float32, device="cuda")
        right = left @ left
        _ = float(right[0, 0].item())
        torch.cuda.synchronize()
        return True, torch.cuda.get_device_name(0)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _torch_exact(
    matrix: np.ndarray,
    threshold: float,
    query_block: int,
    reference_block: int,
    use_gpu: bool,
) -> Iterator[tuple[int, int, float]]:
    device = torch.device("cuda" if use_gpu and torch.cuda.is_available() else "cpu")
    all_cpu = torch.from_numpy(matrix)
    cutoff = threshold / 100.0

    for q_start in range(0, len(matrix), query_block):
        q_stop = min(len(matrix), q_start + query_block)
        query = all_cpu[q_start:q_stop].to(device)
        for r_start in range(q_start, len(matrix), reference_block):
            r_stop = min(len(matrix), r_start + reference_block)
            refs = all_cpu[r_start:r_stop].to(device)
            scores = query @ refs.T
            hits = torch.nonzero(scores >= cutoff, as_tuple=False).cpu().numpy()
            scores_cpu = scores.cpu().numpy()
            for local_i, local_j in hits:
                i = q_start + int(local_i)
                j = r_start + int(local_j)
                if i >= j:
                    continue
                yield i, j, float(scores_cpu[local_i, local_j] * 100.0)


def _edge_iterator(
    settings: Settings,
    matrix: np.ndarray,
    top_k: int,
) -> Iterable[tuple[int, int, float]]:
    if settings.match_mode == "exact":
        return _torch_exact(
            matrix,
            settings.review_threshold,
            settings.match_query_block,
            settings.match_reference_block,
            settings.use_gpu,
        )

    engine = settings.match_engine
    cuda_ready, cuda_info = _cuda_match_ready() if settings.use_gpu else (False, "USE_GPU=false")
    if engine == "torch":
        if settings.use_gpu and not cuda_ready:
            raise RuntimeError(
                "MATCH_ENGINE=torch meminta CUDA, tetapi CUDA matcher tidak siap: "
                f"{cuda_info}. Pasang PyTorch yang mendukung GPU ini atau gunakan "
                "MATCH_ENGINE=faiss."
            )
        if not settings.use_gpu and len(matrix) > settings.torch_fallback_max_images:
            raise RuntimeError(
                f"Torch CPU ditolak untuk canonical image={len(matrix):,}. "
                "Aktifkan USE_GPU=true atau gunakan MATCH_ENGINE=faiss."
            )
        return _torch_knn(
            matrix,
            top_k,
            settings.match_query_block,
            settings.match_reference_block,
            settings.use_gpu,
        )

    if engine == "auto" and cuda_ready:
        print(f"[MATCH] CUDA siap ({cuda_info}); MATCH_ENGINE=auto memilih Torch GPU")
        return _torch_knn(
            matrix,
            top_k,
            settings.match_query_block,
            settings.match_reference_block,
            True,
        )
    if engine == "auto" and settings.use_gpu:
        print(f"[MATCH] CUDA tidak siap ({cuda_info}); fallback ke FAISS")

    try:
        import faiss  # noqa: F401

        return _faiss_knn(settings, matrix, top_k)
    except ImportError:
        if engine == "faiss" or len(matrix) > settings.torch_fallback_max_images:
            raise RuntimeError(
                f"FAISS tidak tersedia dan canonical image={len(matrix):,} melebihi "
                f"TORCH_FALLBACK_MAX_IMAGES={settings.torch_fallback_max_images:,}. "
                "Pasang faiss-cpu atau aktifkan PyTorch CUDA yang kompatibel."
            )
        print("[MATCH] FAISS tidak tersedia; fallback ke Torch CPU untuk source kecil")
        return _torch_knn(
            matrix,
            top_k,
            settings.match_query_block,
            settings.match_reference_block,
            False,
        )


def match_source(
    settings: Settings,
    run_id: uuid.UUID,
    source_group: str,
    *,
    allow_nas_validation: bool = False,
) -> dict[str, int]:
    update_run(settings, run_id, stage="MATCH")
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM processing.candidate_group WHERE run_id=%s", (run_id,))
        if int(cur.fetchone()[0]) > 0:
            cur.execute("SELECT count(*) FROM processing.candidate_edge WHERE run_id=%s", (run_id,))
            metrics = {"candidate_groups": -1, "candidate_edges": int(cur.fetchone()[0]), "match_resumed": 1}
            update_run(settings, run_id, stage="MATCH", metrics=metrics)
            return metrics

    images, matrix = _load_vectors(
        settings,
        source_group,
        allow_nas_validation=allow_nas_validation,
    )
    print(
        f"[MATCH] vector_source=processing.image_feature "
        f"validation_payload={'NAS_CANDIDATE_STREAM' if allow_nas_validation else 'DB_BYTEA'} "
        f"images={len(images):,}",
        flush=True,
    )
    if len(images) < 2:
        metrics = {"match_images": len(images), "candidate_edges": 0, "candidate_groups": 0}
        update_run(settings, run_id, stage="MATCH", metrics=metrics)
        return metrics

    effective_top_k = settings.match_top_k
    if settings.match_auto_cap_top_k:
        effective_top_k = _bounded_top_k(
            settings.match_top_k,
            settings.max_match_edges,
            len(images),
        )
        if effective_top_k < settings.match_top_k:
            print(
                f"[MATCH] top_k otomatis dibatasi {settings.match_top_k} -> "
                f"{effective_top_k} untuk {len(images):,} image agar hasil neighbor "
                f"tidak melewati MAX_MATCH_EDGES={settings.max_match_edges:,}"
            )
    uf = UnionFind(len(images))
    # Nilai edge dipadatkan menjadi score + direction bit. Mutual KNN berarti
    # pasangan muncul pada daftar neighbor kedua arah; ini memutus hub/bridge
    # satu arah yang sebelumnya menyebabkan komponen raksasa.
    best_edges: dict[tuple[int, int], int] = {}
    started = time.time()
    mutual_edges = 0
    blocked_confirmed_unions = 0

    seen = 0
    for seen, (i, j, score) in enumerate(
        _edge_iterator(settings, matrix, effective_top_k),
        start=1,
    ):
        if seen % 1_000_000 == 0:
            print(
                f"[MATCH] examined={seen:,} accepted_unique={len(best_edges):,} "
                f"mutual={mutual_edges:,} blocked_union={blocked_confirmed_unions:,}"
            )
        if score < settings.review_threshold:
            continue
        left, right = (i, j) if i < j else (j, i)
        key = (left, right)
        direction_bit = 0b01 if i == left else 0b10
        previous = best_edges.get(key)
        previous_mask = _edge_direction_mask(previous) if previous is not None else 0
        previous_score = _edge_score(previous) if previous is not None else -1.0
        new_mask = previous_mask | direction_bit
        new_score = max(previous_score, score)
        best_edges[key] = _pack_edge(new_score, new_mask)
        if previous is None and len(best_edges) > settings.max_match_edges:
            raise RuntimeError(
                f"Candidate edge melewati MAX_MATCH_EDGES={settings.max_match_edges:,}. "
                "Proses dihentikan sebelum RAM penuh. Jalankan audit-source dan "
                "pastikan DEDUP_LOGICAL_FILENAME=true."
            )
        became_mutual = new_mask == 0b11 and previous_mask != 0b11
        if became_mutual:
            mutual_edges += 1
        grouping_edge_ready = became_mutual if settings.match_require_mutual_knn else previous is None
        if grouping_edge_ready and new_score >= settings.reused_threshold:
            if not uf.union(left, right, max_size=settings.match_component_max_size):
                blocked_confirmed_unions += 1

    eligible_edge_count = (
        mutual_edges if settings.match_require_mutual_knn else len(best_edges)
    )
    print(
        f"[MATCH] neighbor scan selesai | examined={seen:,} "
        f"unique_edges={len(best_edges):,} eligible_edges={eligible_edge_count:,} "
        f"one_way_dropped={len(best_edges) - eligible_edge_count:,} "
        f"blocked_confirmed_union={blocked_confirmed_unions:,}"
    )
    print("[MATCH] membentuk confirmed/review components...")

    confirmed_components = uf.components(minimum_size=2)
    confirmed_components.sort(key=lambda members: (-len(members), images[min(members)].file_uid))
    confirmed_members = {idx for members in confirmed_components for idx in members}

    # REVIEW dibuat terpisah dan hanya untuk gambar yang belum masuk confirmed.
    # Ini mendekati flow lama: Confirmed divalidasi dahulu, lalu candidate review
    # direcluster tanpa menggandakan anggota confirmed.
    review_uf = UnionFind(len(images))
    blocked_review_unions = 0
    for (i, j), packed in best_edges.items():
        if settings.match_require_mutual_knn and _edge_direction_mask(packed) != 0b11:
            continue
        score = _edge_score(packed)
        if (
            settings.review_threshold <= score < settings.reused_threshold
            and i not in confirmed_members
            and j not in confirmed_members
        ):
            if not review_uf.union(i, j, max_size=settings.match_component_max_size):
                blocked_review_unions += 1
    review_components = review_uf.components(minimum_size=2)
    review_components = [
        members for members in review_components if not any(idx in confirmed_members for idx in members)
    ]
    review_components.sort(key=lambda members: (-len(members), images[min(members)].file_uid))

    component_records = [
        ("CONFIRMED", members) for members in confirmed_components
    ] + [
        ("REVIEW", members) for members in review_components
    ]
    max_candidate_size = max((len(members) for _kind, members in component_records), default=0)
    if max_candidate_size > settings.match_component_max_size:
        raise RuntimeError(
            f"Internal guard gagal: candidate max={max_candidate_size:,} melebihi "
            f"MATCH_COMPONENT_MAX_SIZE={settings.match_component_max_size:,}"
        )
    print(
        f"[MATCH] component guard OK | candidates={len(component_records):,} "
        f"max_candidate={max_candidate_size:,} cap={settings.match_component_max_size:,}"
    )
    candidate_rows = []
    member_rows = []

    membership: dict[int, int] = {}
    for record_idx, (_kind, members) in enumerate(component_records):
        for idx in members:
            membership[idx] = record_idx

    scores_by_record: dict[int, list[float]] = defaultdict(list)
    degree = np.zeros(len(images), dtype=np.int64)
    score_sum = np.zeros(len(images), dtype=np.float64)
    for (i, j), packed in best_edges.items():
        if settings.match_require_mutual_knn and _edge_direction_mask(packed) != 0b11:
            continue
        score = _edge_score(packed)
        record_i = membership.get(i)
        if record_i is None or record_i != membership.get(j):
            continue
        kind = component_records[record_i][0]
        if kind == "CONFIRMED" and score < settings.reused_threshold:
            continue
        if kind == "REVIEW" and not (settings.review_threshold <= score < settings.reused_threshold):
            continue
        scores_by_record[record_i].append(score)
        degree[i] += 1
        degree[j] += 1
        score_sum[i] += score
        score_sum[j] += score

    for record_idx, (_candidate_kind, members) in enumerate(component_records):
        group_id = record_idx + 1
        representative_idx = max(
            members,
            key=lambda idx: (degree[idx], score_sum[idx] / max(1, degree[idx]), images[idx].file_uid),
        )
        scores = scores_by_record.get(record_idx, [])
        candidate_rows.append(
            (
                run_id,
                group_id,
                source_group.upper(),
                images[representative_idx].file_uid,
                len(members),
                max(scores) if scores else None,
                float(np.mean(scores)) if scores else None,
                len(members) > settings.validation_full_pair_max_group,
            )
        )
        for idx in members:
            member_rows.append(
                (
                    run_id,
                    group_id,
                    images[idx].file_uid,
                    images[idx].file_name,
                    source_group.upper(),
                    idx == representative_idx,
                )
            )

    edge_rows = []
    for (i, j), packed in best_edges.items():
        if settings.match_require_mutual_knn and _edge_direction_mask(packed) != 0b11:
            continue
        score = _edge_score(packed)
        edge_rows.append(
            (
                run_id,
                images[i].file_uid,
                images[j].file_uid,
                score,
                "CONFIRMED" if score >= settings.reused_threshold else "REVIEW",
            )
        )

    print(
        f"[MATCH] siap simpan | candidates={len(candidate_rows):,} "
        f"members={len(member_rows):,} edges={len(edge_rows):,}"
    )
    with connection(settings) as conn, conn.cursor() as cur:
        if edge_rows:
            print("[MATCH] insert candidate_edge...")
            execute_values(
                cur,
                """
                INSERT INTO processing.candidate_edge
                    (run_id, file_uid_a, file_uid_b, similarity, tier)
                VALUES %s
                ON CONFLICT (run_id, file_uid_a, file_uid_b) DO UPDATE SET
                    similarity=excluded.similarity,
                    tier=excluded.tier
                """,
                edge_rows,
                page_size=5000,
            )
        if candidate_rows:
            print("[MATCH] insert candidate_group/member...")
            execute_values(
                cur,
                """
                INSERT INTO processing.candidate_group
                    (run_id, candidate_group_id, source_group, representative_file_uid,
                     usage_count, max_edge_similarity, mean_edge_similarity,
                     requires_oversize_validation)
                VALUES %s
                ON CONFLICT (run_id, candidate_group_id) DO NOTHING
                """,
                candidate_rows,
                page_size=1000,
            )
            execute_values(
                cur,
                """
                INSERT INTO processing.candidate_group_member
                    (run_id, candidate_group_id, file_uid, file_name, source_group, is_representative)
                VALUES %s
                ON CONFLICT (run_id, candidate_group_id, file_uid) DO NOTHING
                """,
                member_rows,
                page_size=5000,
            )

    metrics = {
        "match_images": len(images),
        "match_effective_top_k": effective_top_k,
        "match_mutual_edges": mutual_edges,
        "match_one_way_edges_dropped": len(best_edges) - eligible_edge_count,
        "match_blocked_confirmed_unions": blocked_confirmed_unions,
        "match_blocked_review_unions": blocked_review_unions,
        "candidate_edges": len(edge_rows),
        "candidate_groups": len(candidate_rows),
        "max_candidate_group_size": max_candidate_size,
        "confirmed_candidate_groups": len(confirmed_components),
        "confirmed_candidate_images": len(confirmed_members),
        "review_candidate_groups": len(review_components),
        "review_candidate_images": sum(len(members) for members in review_components),
        "confirmed_edges": sum(
            1
            for packed in best_edges.values()
            if (not settings.match_require_mutual_knn or _edge_direction_mask(packed) == 0b11)
            and _edge_score(packed) >= settings.reused_threshold
        ),
        "review_edges": sum(
            1
            for packed in best_edges.values()
            if (not settings.match_require_mutual_knn or _edge_direction_mask(packed) == 0b11)
            and settings.review_threshold <= _edge_score(packed) < settings.reused_threshold
        ),
        "oversize_candidate_groups": sum(
            1 for _kind, members in component_records
            if len(members) > settings.max_accepted_group_size
        ),
        "sparse_validation_candidate_groups": sum(1 for row in candidate_rows if row[-1]),
        "match_seconds": int(time.time() - started),
    }
    update_run(settings, run_id, stage="MATCH", metrics=metrics)
    return metrics
