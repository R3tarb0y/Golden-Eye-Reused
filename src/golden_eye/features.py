from __future__ import annotations

import io
import hashlib
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image, ImageOps
from psycopg2.extras import execute_values
from torchvision import models, transforms

from .config import Settings
from .db import connection, named_cursor_batches, qname, update_run
from .utils import pack_vector


class _ProducerFailure:
    def __init__(self, error: BaseException):
        self.error = error


_PREFETCH_DONE = object()


def _prefetched_source_batches(
    settings: Settings,
    sql: str,
    params: tuple[Any, ...],
):
    """Read the next DB batch while CUDA processes the current batch."""
    output: queue.Queue[Any] = queue.Queue(maxsize=settings.embed_db_prefetch_batches)
    stop = threading.Event()

    def put(item: Any) -> bool:
        while not stop.is_set():
            try:
                output.put(item, timeout=0.5)
                return True
            except queue.Full:
                continue
        return False

    def produce() -> None:
        try:
            with connection(settings) as source_conn:
                for batch in named_cursor_batches(
                    source_conn,
                    sql,
                    params,
                    settings.db_fetch_size,
                ):
                    if not put(batch):
                        return
        except BaseException as exc:
            put(_ProducerFailure(exc))
        finally:
            put(_PREFETCH_DONE)

    worker = threading.Thread(
        target=produce,
        name="golden-eye-bytea-prefetch",
        daemon=True,
    )
    worker.start()
    try:
        while True:
            wait_started = time.perf_counter()
            item = output.get()
            waited = time.perf_counter() - wait_started
            if item is _PREFETCH_DONE:
                break
            if isinstance(item, _ProducerFailure):
                raise item.error
            yield item, waited
    finally:
        stop.set()
        worker.join(timeout=2.0)


@dataclass
class FeatureBatchResult:
    rows: list[tuple[Any, ...]]
    succeeded: int
    failed: int


class ResNetFeatureExtractor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.device = torch.device(
            "cuda" if settings.use_gpu and torch.cuda.is_available() else "cpu"
        )
        if settings.model_name.lower() != "resnet18":
            raise ValueError("Versi repository ini mendukung MODEL_NAME=resnet18")

        if settings.model_weights_path:
            model = models.resnet18(weights=None)
            state = torch.load(settings.model_weights_path, map_location="cpu", weights_only=True)
            model.load_state_dict(state)
            self.transform = transforms.Compose(
                [
                    transforms.Resize(256),
                    transforms.CenterCrop(224),
                    transforms.ToTensor(),
                    transforms.Normalize(
                        mean=[0.485, 0.456, 0.406],
                        std=[0.229, 0.224, 0.225],
                    ),
                ]
            )
        else:
            weights = models.ResNet18_Weights.DEFAULT
            model = models.resnet18(weights=weights)
            self.transform = weights.transforms()

        self.model = torch.nn.Sequential(*list(model.children())[:-1]).eval().to(self.device)
        self.dimension = 512

    def _decode(self, payload: bytes | memoryview) -> Image.Image:
        with Image.open(io.BytesIO(bytes(payload))) as image:
            return ImageOps.exif_transpose(image).convert("RGB")

    def _center_crop(self, image: Image.Image) -> Image.Image:
        ratio = self.settings.center_crop_ratio
        width, height = image.size
        crop_w = max(1, int(width * ratio))
        crop_h = max(1, int(height * ratio))
        left = max(0, (width - crop_w) // 2)
        top = max(0, (height - crop_h) // 2)
        return image.crop((left, top, left + crop_w, top + crop_h))

    @torch.inference_mode()
    def process(self, source_rows: list[dict[str, Any]]) -> FeatureBatchResult:
        valid_meta: list[dict[str, Any]] = []
        full_tensors: list[torch.Tensor] = []
        center_tensors: list[torch.Tensor] = []
        failed_rows: list[tuple[Any, ...]] = []

        for row in source_rows:
            try:
                image = self._decode(row["image_bytes"])
                full_tensors.append(self.transform(image))
                center_tensors.append(self.transform(self._center_crop(image)))
                valid_meta.append(row)
            except Exception as exc:
                failed_rows.append(
                    (
                        row["file_uid"],
                        self.settings.model_version,
                        row.get("file_sha256"),
                        row["source_group"],
                        row["file_name"],
                        bool(row.get("is_transport", False)),
                        None,
                        None,
                        None,
                        False,
                        str(exc)[:1000],
                    )
                )

        output_rows: list[tuple[Any, ...]] = []
        if valid_meta:
            full_batch = torch.stack(full_tensors).to(self.device, non_blocking=True)
            center_batch = torch.stack(center_tensors).to(self.device, non_blocking=True)
            combined = torch.cat([full_batch, center_batch], dim=0)
            feats = self.model(combined).flatten(1)
            feats = F.normalize(feats, p=2, dim=1).cpu().numpy().astype(np.float32)
            full_feats, center_feats = np.split(feats, 2, axis=0)

            for row, full, center in zip(valid_meta, full_feats, center_feats):
                output_rows.append(
                    (
                        row["file_uid"],
                        self.settings.model_version,
                        row.get("file_sha256"),
                        row["source_group"],
                        row["file_name"],
                        bool(row.get("is_transport", False)),
                        self.dimension,
                        pack_vector(full),
                        pack_vector(center),
                        True,
                        None,
                    )
                )

        output_rows.extend(failed_rows)
        return FeatureBatchResult(
            rows=output_rows,
            succeeded=len(valid_meta),
            failed=len(failed_rows),
        )


def embed_source(settings: Settings, run_id: uuid.UUID, source_group: str) -> dict[str, Any]:
    update_run(settings, run_id, stage="EMBED")
    extractor = ResNetFeatureExtractor(settings)
    image_table = qname(settings.silver_schema, settings.image_table)
    # UUID file_uid has no relationship with the physical location of a large
    # TOAST value. The legacy ORDER BY therefore turns a sequential source scan
    # into random BYTEA reads. Physical mode leaves PostgreSQL's source scan in
    # heap order; embedding/matching accuracy is independent of read order.
    order_clause = (
        "" if settings.embed_source_read_order == "physical" else "ORDER BY i.file_uid"
    )
    sql = f"""
        SELECT
            i.file_uid::text AS file_uid,
            i.source_group::text AS source_group,
            i.file_name::text AS file_name,
            coalesce(r.file_sha256, i.file_sha256::text) AS file_sha256,
            coalesce(i.is_transport, false) AS is_transport,
            coalesce(r.image_bytes, i.image_bytes) AS image_bytes
        FROM {image_table} i
        LEFT JOIN processing.image_feature f
          ON f.file_uid=i.file_uid::text
         AND f.model_version=%s
        LEFT JOIN processing.source_bytea_exception bx
          ON bx.source_group=upper(i.source_group::text)
         AND bx.file_uid=i.file_uid::text
         AND bx.is_active=true
        LEFT JOIN processing.gambar_reused r
          ON r.source_group=upper(i.source_group::text)
         AND r.file_uid=i.file_uid::text
         AND r.is_active=true
        WHERE upper(i.source_group::text)=upper(%s)
          AND coalesce(i.file_exists, true)=true
          AND coalesce(i.zero_byte, false)=false
          AND (r.image_bytes IS NOT NULL OR i.image_bytes IS NOT NULL)
          AND left(lower(i.file_name::text), 7) <> 'before_'
          AND lower(i.file_name::text) ~ %s
          AND upper(i.file_name::text) !~ %s
          AND (bx.file_uid IS NULL OR r.file_uid IS NOT NULL)
          AND (
                f.file_uid IS NULL
                OR f.file_sha256 IS DISTINCT FROM coalesce(r.file_sha256, i.file_sha256::text)
                OR f.is_valid=false
                OR f.full_embedding IS NULL
                OR f.embedding_dim IS NULL
                OR f.embedding_dim <= 0
          )
        {order_clause}
    """

    total = succeeded = failed = 0
    printed_errors = 0
    started = time.time()
    last_log = time.perf_counter()
    db_wait_total = 0.0
    compute_total = 0.0
    write_total = 0.0
    params = (
        settings.model_version,
        source_group,
        settings.valid_extension_regex(),
        settings.excluded_filename_regex(),
    )
    print(
        f"[EMBED] source_read_order={settings.embed_source_read_order} "
        f"db_fetch={settings.db_fetch_size:,} "
        f"prefetch_batches={settings.embed_db_prefetch_batches} "
        f"embed_batch={settings.embed_batch_size:,}"
    )
    with connection(settings) as write_conn:
        for db_batch, db_wait in _prefetched_source_batches(settings, sql, params):
            db_wait_total += db_wait
            for start in range(0, len(db_batch), settings.embed_batch_size):
                batch = db_batch[start : start + settings.embed_batch_size]
                compute_started = time.perf_counter()
                result = extractor.process(batch)
                compute_seconds = time.perf_counter() - compute_started
                compute_total += compute_seconds
                if result.failed and printed_errors < 10:
                    for failed_row in [row for row in result.rows if row[-2] is False]:
                        print(
                            f"[EMBED][FAILED] file_uid={failed_row[0]} "
                            f"file={failed_row[4]} error={failed_row[-1]}"
                        )
                        printed_errors += 1
                        if printed_errors >= 10:
                            break
                write_started = time.perf_counter()
                with write_conn.cursor() as cur:
                    execute_values(
                        cur,
                        """
                        INSERT INTO processing.image_feature
                            (file_uid, model_version, file_sha256, source_group, file_name, is_transport,
                             embedding_dim, full_embedding, center_embedding, is_valid, error_message)
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
                write_conn.commit()
                write_seconds = time.perf_counter() - write_started
                write_total += write_seconds
                total += len(batch)
                succeeded += result.succeeded
                failed += result.failed
                elapsed = max(0.001, time.time() - started)
                now = time.perf_counter()
                instant_elapsed = max(0.001, now - last_log)
                last_log = now
                print(
                    f"[EMBED] read={total:,} ok={succeeded:,} failed={failed:,} "
                    f"speed_now={len(batch) / instant_elapsed:,.1f} "
                    f"speed_avg={total / elapsed:,.1f} img/s "
                    f"db_wait_total={db_wait_total:,.1f}s "
                    f"compute={compute_seconds:,.2f}s write={write_seconds:,.2f}s "
                    f"device={extractor.device}"
                )

    metrics = {
        "embedded_read": total,
        "embedded_ok": succeeded,
        "embedded_failed": failed,
        "embedded_db_wait_seconds": round(db_wait_total, 3),
        "embedded_compute_seconds": round(compute_total, 3),
        "embedded_write_seconds": round(write_total, 3),
        "embedded_source_read_order": settings.embed_source_read_order,
        "embedded_prefetch_batches": settings.embed_db_prefetch_batches,
    }
    update_run(settings, run_id, stage="EMBED", metrics=metrics)
    return metrics


def embed_panen_nas_missing(
    settings: Settings,
    source_group: str,
    period_year: int,
    *,
    batch_size: int = 64,
) -> dict[str, Any]:
    """Buat embedding KP yang BYTEA-nya hanya tersedia di NAS.

    Context tetap menjadi sumber identitas transaksi. Registry NAS menjadi
    sumber byte foto; tidak ada BYTEA yang disalin ke tabel Silver.
    """
    source_group = source_group.upper().strip()
    if source_group not in {"PWP", "DIN"}:
        raise ValueError("Embedding NAS hanya diizinkan untuk PWP atau DIN")
    if not 1 <= batch_size <= 256:
        raise ValueError("--batch-size harus 1..256")

    image_table = qname(settings.silver_schema, settings.image_table)
    extractor = ResNetFeatureExtractor(settings)
    sql = f"""
        WITH active_gold_name AS (
            SELECT source_group,
                   file_uid,
                   min(nullif(btrim(image_name), '')) AS file_name
            FROM gold.reused_photo_detail
            WHERE is_active=true
            GROUP BY source_group, file_uid
        )
        SELECT c.file_uid,
               upper(c.source_group) AS source_group,
               coalesce(
                   nullif(trim(f.file_name), ''),
                   nullif(trim(g.file_name), ''),
                   nullif(trim(d.file_name), ''),
                   nullif(trim(i.file_name::text), '')
               )
                   AS file_name,
               coalesce(
                   nullif(trim(g.file_sha256), ''),
                   nullif(trim(f.file_sha256), ''),
                   nullif(trim(i.file_sha256::text), '')
               )
                   AS file_sha256,
               n.nas_path,
               n.expected_sha256
        FROM processing.image_context c
        LEFT JOIN processing.image_feature f
          ON f.file_uid=c.file_uid
         AND f.model_version=%s
        LEFT JOIN processing.gambar_reused g
          ON g.source_group=upper(c.source_group)
         AND g.file_uid=c.file_uid
         AND g.is_active=true
        LEFT JOIN active_gold_name d
          ON d.source_group=upper(c.source_group)
         AND d.file_uid=c.file_uid
        LEFT JOIN {image_table} i
          ON i.file_uid::text=c.file_uid
        JOIN processing.panen_nas_path_index n
          ON n.source_group=upper(c.source_group)
         AND n.period_year=%s
         AND n.file_name_upper=upper(btrim(coalesce(
               nullif(trim(f.file_name), ''),
               nullif(trim(g.file_name), ''),
               nullif(trim(d.file_name), ''),
               nullif(trim(i.file_name::text), '')
         )))
         AND n.is_active=true
        WHERE upper(c.source_group)=%s
          AND c.period_date IS NOT NULL
          AND extract(year FROM c.period_date)::integer=%s
          AND nullif(trim(coalesce(c.oph_key, '')), '') IS NOT NULL
          AND coalesce(i.is_transport, false)=false
          AND nullif(trim(n.nas_path), '') IS NOT NULL
          AND (
                f.file_uid IS NULL
                OR f.is_transport=true
                OR f.is_valid=false
                OR f.full_embedding IS NULL
                OR f.embedding_dim IS NULL
                OR f.embedding_dim <= 0
          )
        ORDER BY c.file_uid
    """
    params = (
        settings.model_version,
        period_year,
        source_group,
        period_year,
    )
    total = succeeded = failed = skipped = 0
    started = time.time()
    print(
        f"[EMBED NAS] source={source_group} year={period_year} "
        f"batch={batch_size} device={extractor.device}",
        flush=True,
    )
    with connection(settings) as write_conn, connection(settings) as read_conn:
        for db_batch in named_cursor_batches(
            read_conn, sql, params, max(1, batch_size)
        ):
            source_rows: list[dict[str, Any]] = []
            for row in db_batch:
                path = str(row.get("nas_path") or "").strip()
                try:
                    if not path:
                        skipped += 1
                        continue
                    with open(path, "rb") as handle:
                        payload = handle.read()
                    if not payload:
                        skipped += 1
                        continue
                    digest = hashlib.sha256(payload).hexdigest()
                    expected = str(row.get("expected_sha256") or "").strip().lower()
                    if expected and digest != expected:
                        failed += 1
                        print(
                            f"[EMBED NAS][HASH MISMATCH] file_uid={row['file_uid']} "
                            f"file={row.get('file_name')}",
                            flush=True,
                        )
                        continue
                    source_rows.append(
                        {
                            "file_uid": str(row["file_uid"]),
                            "source_group": source_group,
                            "file_name": row.get("file_name") or str(row["file_uid"]),
                            "file_sha256": digest,
                            "is_transport": False,
                            "image_bytes": payload,
                        }
                    )
                except (OSError, ValueError) as exc:
                    failed += 1
                    print(
                        f"[EMBED NAS][FAILED] file_uid={row['file_uid']} "
                        f"file={row.get('file_name')} error={str(exc)[:300]}",
                        flush=True,
                    )
            for start in range(0, len(source_rows), batch_size):
                result = extractor.process(source_rows[start : start + batch_size])
                with write_conn.cursor() as cur:
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
                write_conn.commit()
                succeeded += result.succeeded
                failed += result.failed
            total += len(db_batch)
            print(
                f"[EMBED NAS] checked={total:,} ok={succeeded:,} "
                f"failed={failed:,} skipped={skipped:,} "
                f"elapsed={time.time() - started:,.1f}s",
                flush=True,
            )
    return {
        "source_group": source_group,
        "period_year": int(period_year),
        "embedded_read": total,
        "embedded_ok": succeeded,
        "embedded_failed": failed,
        "embedded_skipped": skipped,
        "device": str(extractor.device),
    }
