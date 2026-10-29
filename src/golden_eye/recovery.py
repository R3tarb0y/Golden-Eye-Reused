from __future__ import annotations

import argparse
import csv
import hashlib
import io
import mimetypes
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image
from psycopg2.extras import RealDictCursor, execute_values

from .config import Settings
from .db import connection, fetch_all_dict, pipeline_job_lock, qname

RECOVERY_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
}

REUSED_IMAGE_TABLE = "processing.gambar_reused"
TARGETED_RECOVERY_TABLE = "processing.gambar_reused_targeted_recovery"


@dataclass(frozen=True)
class Target:
    file_uid: str
    file_name: str
    logical_name: str
    expected_hash: str
    expected_size: int | None
    mime_type: str


@dataclass(frozen=True)
class Donor:
    source_group: str
    file_uid: str
    file_name: str
    logical_name: str
    stored_hash: str
    stored_size: int | None


def _logical_name(value: str) -> str:
    return Path(str(value or "").replace("\\", "/")).name.strip().upper()


def _clean_hash(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if len(text) in {32, 40, 64} and all(c in "0123456789abcdef" for c in text) else ""


def _payload_hashes(payload: bytes) -> dict[int, str]:
    return {
        32: hashlib.md5(payload).hexdigest(),  # noqa: S324 - cocokkan metadata legacy
        40: hashlib.sha1(payload).hexdigest(),  # noqa: S324 - cocokkan metadata legacy
        64: hashlib.sha256(payload).hexdigest(),
    }


def _hash_matches(payload: bytes, expected: str) -> bool:
    expected = _clean_hash(expected)
    return not expected or _payload_hashes(payload)[len(expected)] == expected


def _verify_image(payload: bytes) -> None:
    if not payload:
        raise ValueError("file kosong")
    with Image.open(io.BytesIO(payload)) as image:
        image.verify()
    with Image.open(io.BytesIO(payload)) as image:
        image.load()


def _chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _choose_donor(target: Target, donors: list[Donor]) -> tuple[Donor | None, str]:
    if not donors:
        return None, "NO_DB_DONOR"

    expected_hash = _clean_hash(target.expected_hash)
    if expected_hash:
        exact_hash = [d for d in donors if _clean_hash(d.stored_hash) == expected_hash]
        if exact_hash:
            return sorted(exact_hash, key=lambda d: (d.source_group, d.file_uid))[0], "DB_EXACT_HASH"

    same_name = [d for d in donors if d.logical_name == target.logical_name]
    if target.expected_size is not None:
        same_name = [d for d in same_name if d.stored_size in {None, target.expected_size}]
    if not same_name:
        return None, "NO_DB_DONOR"

    hashes = {_clean_hash(d.stored_hash) for d in same_name if _clean_hash(d.stored_hash)}
    if len(same_name) > 1 and len(hashes) > 1:
        return None, "AMBIGUOUS_DB_DONOR"
    return sorted(same_name, key=lambda d: (d.source_group, d.file_uid))[0], "DB_EXACT_NAME_SIZE"


def _load_targets(settings: Settings, source_group: str) -> tuple[list[Target], int]:
    image_table = qname(settings.silver_schema, settings.image_table)
    with connection(settings) as conn:
        rows = fetch_all_dict(
            conn,
            f"""
            SELECT i.file_uid::text AS file_uid,
                   i.file_name::text AS file_name,
                   upper(trim(i.file_name::text)) AS logical_name,
                   i.file_sha256::text AS expected_hash,
                   coalesce(i.bytea_len, i.file_size_bytes)::bigint AS expected_size,
                   coalesce(i.mime_type::text, 'image/jpeg') AS mime_type,
                   (r.file_uid IS NOT NULL) AS already_recovered
            FROM processing.source_bytea_exception bx
            JOIN {image_table} i
              ON upper(i.source_group::text)=bx.source_group
             AND i.file_uid::text=bx.file_uid
            LEFT JOIN processing.gambar_reused r
              ON r.source_group=bx.source_group
             AND r.file_uid=bx.file_uid
             AND r.is_active=true
            WHERE bx.source_group=%s
              AND bx.is_active=true
            ORDER BY i.file_uid::text
            """,
            (source_group,),
        )
    targets = [
        Target(
            file_uid=str(row["file_uid"]),
            file_name=str(row["file_name"] or ""),
            logical_name=_logical_name(row["logical_name"] or row["file_name"]),
            expected_hash=_clean_hash(row.get("expected_hash")),
            expected_size=int(row["expected_size"]) if row.get("expected_size") is not None else None,
            mime_type=str(row.get("mime_type") or "image/jpeg"),
        )
        for row in rows
        if not bool(row.get("already_recovered"))
    ]
    already = sum(bool(row.get("already_recovered")) for row in rows)
    return targets, already


def _load_donor_metadata(settings: Settings, source_groups: list[str]) -> list[Donor]:
    if not source_groups:
        return []
    image_table = qname(settings.silver_schema, settings.image_table)
    with connection(settings) as conn:
        rows = fetch_all_dict(
            conn,
            f"""
            SELECT upper(i.source_group::text) AS source_group,
                   i.file_uid::text AS file_uid,
                   i.file_name::text AS file_name,
                   upper(trim(i.file_name::text)) AS logical_name,
                   coalesce(r.file_sha256, i.file_sha256::text) AS stored_hash,
                   coalesce(r.bytea_len, i.bytea_len, i.file_size_bytes)::bigint AS stored_size
            FROM {image_table} i
            LEFT JOIN processing.source_bytea_exception bx
              ON bx.source_group=upper(i.source_group::text)
             AND bx.file_uid=i.file_uid::text
             AND bx.is_active=true
            LEFT JOIN processing.gambar_reused r
              ON r.source_group=upper(i.source_group::text)
             AND r.file_uid=i.file_uid::text
             AND r.is_active=true
            WHERE upper(i.source_group::text)=ANY(%s)
              AND (i.image_bytes IS NOT NULL OR r.image_bytes IS NOT NULL)
              AND (bx.file_uid IS NULL OR r.file_uid IS NOT NULL)
            """,
            (source_groups,),
        )
    return [
        Donor(
            source_group=str(row["source_group"]),
            file_uid=str(row["file_uid"]),
            file_name=str(row["file_name"] or ""),
            logical_name=_logical_name(row["logical_name"] or row["file_name"]),
            stored_hash=_clean_hash(row.get("stored_hash")),
            stored_size=int(row["stored_size"]) if row.get("stored_size") is not None else None,
        )
        for row in rows
    ]


def _image_uid_join_condition(settings: Settings) -> str:
    with connection(settings) as conn:
        rows = fetch_all_dict(
            conn,
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema=%s AND table_name=%s AND column_name='file_uid'
            LIMIT 1
            """,
            (settings.silver_schema, settings.image_table),
        )
    data_type = str(rows[0]["data_type"]).lower() if rows else ""
    if data_type == "uuid":
        return "i.file_uid=w.file_uid::uuid"
    if data_type in {"text", "character varying", "character"}:
        return "i.file_uid=w.file_uid"
    return "i.file_uid::text=w.file_uid"


def _fetch_donor_payloads(
    settings: Settings,
    donors: list[Donor],
    uid_join_condition: str,
) -> dict[tuple[str, str], bytes]:
    if not donors:
        return {}
    image_table = qname(settings.silver_schema, settings.image_table)
    values = ",".join(["(%s,%s)"] * len(donors))
    params: list[str] = []
    for donor in donors:
        params.extend([donor.source_group, donor.file_uid])
    sql = f"""
        WITH wanted(source_group, file_uid) AS (VALUES {values})
        SELECT upper(i.source_group::text) AS source_group,
               i.file_uid::text AS file_uid,
               coalesce(r.image_bytes, i.image_bytes) AS image_bytes
        FROM wanted w
        JOIN {image_table} i
          ON upper(i.source_group::text)=w.source_group
         AND {uid_join_condition}
        LEFT JOIN processing.gambar_reused r
          ON r.source_group=w.source_group
         AND r.file_uid=w.file_uid
         AND r.is_active=true
        ORDER BY coalesce(r.ctid, i.ctid)
    """
    with connection(settings) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return {
            (str(row["source_group"]), str(row["file_uid"])): bytes(row["image_bytes"])
            for row in cur.fetchall()
            if row.get("image_bytes") is not None
        }


def _persist(
    settings: Settings,
    rows: list[tuple[Any, ...]],
    *,
    force_replace: bool = False,
) -> None:
    if not rows:
        return
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('processing.gambar_reused')"
        )
        if cur.fetchone()[0] is None:
            raise RuntimeError(
                "Tabel processing.gambar_reused belum ada. "
                "Jalankan: py run_pipeline.py init-db"
            )
        conflict_guard = "" if force_replace else """
            WHERE current_row.file_sha256 IS DISTINCT FROM excluded.file_sha256
               OR current_row.bytea_len IS DISTINCT FROM excluded.bytea_len
               OR current_row.mime_type IS DISTINCT FROM excluded.mime_type
               OR current_row.file_name IS DISTINCT FROM excluded.file_name
               OR current_row.is_active IS DISTINCT FROM true
        """
        execute_values(
            cur,
            f"""
            INSERT INTO processing.gambar_reused AS current_row
                (source_group, file_uid, file_name, mime_type, file_sha256,
                 bytea_len, image_bytes, recovery_source, match_method, is_active)
            VALUES %s
            ON CONFLICT (source_group, file_uid) DO UPDATE SET
                file_name=excluded.file_name,
                mime_type=excluded.mime_type,
                file_sha256=excluded.file_sha256,
                bytea_len=excluded.bytea_len,
                image_bytes=excluded.image_bytes,
                recovery_source=excluded.recovery_source,
                match_method=excluded.match_method,
                recovered_at=now(),
                last_verified_at=now(),
                is_active=true
            {conflict_guard}
            """,
            rows,
            page_size=min(32, len(rows)),
        )



def _ensure_targeted_recovery_table(settings: Settings) -> None:
    """Create a small healthy overlay table for targeted report/dashboard repairs.

    This table is intentionally separate from processing.gambar_reused so a
    corrupt TOAST heap in the canonical hot store is never rewritten/touched
    during a targeted recovery.
    """
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS processing.gambar_reused_targeted_recovery (
                source_group text NOT NULL,
                file_uid text NOT NULL,
                file_name text NOT NULL,
                mime_type text,
                file_sha256 text NOT NULL,
                bytea_len bigint NOT NULL,
                image_bytes bytea NOT NULL,
                recovery_source text NOT NULL,
                match_method text NOT NULL,
                recovered_at timestamptz NOT NULL DEFAULT now(),
                last_verified_at timestamptz NOT NULL DEFAULT now(),
                is_active boolean NOT NULL DEFAULT true,
                PRIMARY KEY (source_group, file_uid),
                CONSTRAINT ck_gambar_reused_targeted_len
                    CHECK (bytea_len > 0 AND octet_length(image_bytes) = bytea_len)
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_gambar_reused_targeted_active_name
            ON processing.gambar_reused_targeted_recovery
               (source_group, is_active, upper(trim(file_name)))
            """
        )


def _persist_targeted_recovery(settings: Settings, rows: list[tuple[Any, ...]]) -> None:
    if not rows:
        return
    _ensure_targeted_recovery_table(settings)
    with connection(settings) as conn, conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO processing.gambar_reused_targeted_recovery AS current_row
                (source_group, file_uid, file_name, mime_type, file_sha256,
                 bytea_len, image_bytes, recovery_source, match_method, is_active)
            VALUES %s
            ON CONFLICT (source_group, file_uid) DO UPDATE SET
                file_name=excluded.file_name,
                mime_type=excluded.mime_type,
                file_sha256=excluded.file_sha256,
                bytea_len=excluded.bytea_len,
                image_bytes=excluded.image_bytes,
                recovery_source=excluded.recovery_source,
                match_method=excluded.match_method,
                recovered_at=now(),
                last_verified_at=now(),
                is_active=true
            """,
            rows,
            page_size=min(32, len(rows)),
        )

def _index_nas(roots: list[Path], wanted_names: set[str]) -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = defaultdict(list)
    remaining = set(wanted_names)
    for root in roots:
        if not remaining:
            break
        if not root.exists():
            raise FileNotFoundError(f"NAS root tidak ditemukan: {root}")
        # Mayoritas downloader menyimpan file langsung pada root. Jalur cepat
        # ini mencegah os.walk ratusan ribu file pada job harian.
        direct_found = 0
        for logical_name in tuple(remaining):
            direct_path = root / logical_name
            if direct_path.is_file():
                found[logical_name].append(direct_path)
                remaining.discard(logical_name)
                direct_found += 1
        print(
            f"[RECOVERY] root={root} direct_found={direct_found:,} "
            f"remaining_before_walk={len(remaining):,}",
            flush=True,
        )
        if not remaining:
            continue

        print(f"[RECOVERY] scan recursive NAS root={root}", flush=True)
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                key = filename.strip().upper()
                if key in remaining:
                    found[key].append(Path(dirpath) / filename)
                    remaining.discard(key)
            # Recovery aktif tidak membuat inventaris seluruh NAS. Begitu
            # seluruh target ditemukan, walk dihentikan agar latency jaringan
            # dan pembacaan directory tidak diulang tanpa manfaat.
            if not remaining:
                print(
                    f"[RECOVERY] seluruh target ditemukan; scan dihentikan "
                    f"pada root={root}",
                    flush=True,
                )
                break
    return found


def _read_report_image_keys(report_path: Path) -> list[str]:
    if not report_path.exists():
        raise FileNotFoundError(f"CSV report tidak ditemukan: {report_path}")
    with report_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = {
            str(column or "").strip().lower(): str(column)
            for column in (reader.fieldnames or [])
        }
        key_column = next(
            (
                columns[name]
                for name in ("image_key", "file_name", "image_name", "file_uid")
                if name in columns
            ),
            None,
        )
        if not key_column:
            raise ValueError(
                "CSV harus mempunyai kolom image_key, file_name, image_name, atau file_uid"
            )
        values = [
            str(row.get(key_column) or "").strip()
            for row in reader
            if str(row.get(key_column) or "").strip()
        ]
    return list(dict.fromkeys(values))


def _gold_evidence_targets(
    settings: Settings,
    source_group: str,
    requested_keys: list[str] | None,
    *,
    skip_existing: bool = False,
    transport_only: bool = False,
    panen_only: bool = False,
) -> tuple[list[Target], list[str], int]:
    """Ambil identitas gambar dari Gold tanpa membaca relation Silver rusak."""
    source_group = source_group.upper().strip()
    upper_keys = [value.upper() for value in (requested_keys or [])]
    # CSV audit kosong berarti tidak ada target recovery. Sebelumnya [] jatuh
    # ke query tanpa filter dan seluruh Gold aktif ikut divalidasi ulang.
    if requested_keys is not None and not upper_keys:
        return [], [], 0
    key_filter = ""
    params: list[Any] = [source_group]
    if upper_keys:
        key_filter = """
          AND (
                upper(trim(coalesce(file_uid, '')))=ANY(%s)
                OR upper(trim(file_name))=ANY(%s)
              )
        """
        params.extend([upper_keys, upper_keys])
    role_filter = ""
    if transport_only:
        role_filter = "AND image_role IN ('TRANSPORT_DT', 'TRANSPORT_FT')"
    elif panen_only:
        role_filter = "AND image_role='PANEN'"

    with connection(settings) as conn:
        rows = fetch_all_dict(
            conn,
            f"""
            WITH evidence AS (
                SELECT upper(source_group) AS source_group,
                       file_uid::text AS file_uid,
                       image_name::text AS file_name,
                       'PANEN'::text AS image_role
                FROM gold.reused_photo_detail
                WHERE is_active=true
                  AND upper(source_group)=%s
                  AND (
                        upper(source_group) NOT IN ('DAN', 'DWT')
                        OR processing.elhp_estate_matches_company(
                            upper(source_group), estate
                        )
                      )

                UNION ALL

                SELECT upper(source_group) AS source_group,
                       foto_dt_uid::text AS file_uid,
                       foto_dt::text AS file_name,
                       'TRANSPORT_DT'::text AS image_role
                FROM gold.reused_photo_detail
                WHERE is_active=true
                  AND upper(source_group)=%s
                  AND (
                        upper(source_group) NOT IN ('DAN', 'DWT')
                        OR processing.elhp_estate_matches_company(
                            upper(source_group), estate
                        )
                      )
                  AND nullif(trim(foto_dt::text), '') IS NOT NULL

                UNION ALL

                SELECT upper(source_group) AS source_group,
                       foto_ft_uid::text AS file_uid,
                       foto_ft::text AS file_name,
                       'TRANSPORT_FT'::text AS image_role
                FROM gold.reused_photo_detail
                WHERE is_active=true
                  AND upper(source_group)=%s
                  AND (
                        upper(source_group) NOT IN ('DAN', 'DWT')
                        OR processing.elhp_estate_matches_company(
                            upper(source_group), estate
                        )
                      )
                  AND nullif(trim(foto_ft::text), '') IS NOT NULL
            )
            SELECT DISTINCT source_group, file_uid, file_name
            FROM evidence
            WHERE nullif(trim(file_name), '') IS NOT NULL
              {role_filter}
              {key_filter}
            ORDER BY file_name, file_uid
            """,
            tuple([source_group, source_group, source_group] + params[1:]),
        )

    targets: list[Target] = []
    resolved_keys: set[str] = set()
    seen: set[tuple[str, str] | str] = set()
    for row in rows:
        file_name = str(row.get("file_name") or "").strip()
        logical_name = _logical_name(file_name)
        if (
            not logical_name
            or Path(logical_name).suffix.lower() not in RECOVERY_IMAGE_EXTENSIONS
        ):
            continue
        file_uid = str(row.get("file_uid") or "").strip()
        if not file_uid:
            file_uid = "GOLD-RECOVERY-" + hashlib.sha256(
                f"{source_group}\0{logical_name}".encode("utf-8")
            ).hexdigest()
        # Untuk dashboard, satu payload per logical filename sudah cukup.
        # Transport kadang direferensikan banyak transaksi/UID; menyimpan
        # BYTEA yang sama berulang hanya memboroskan TOAST.
        identity: tuple[str, str] | str = (
            logical_name if transport_only else (file_uid, logical_name)
        )
        if identity in seen:
            continue
        seen.add(identity)
        mime_type = mimetypes.guess_type(file_name)[0] or "image/jpeg"
        targets.append(
            Target(
                file_uid=file_uid,
                file_name=file_name,
                logical_name=logical_name,
                expected_hash="",
                expected_size=None,
                mime_type=mime_type,
            )
        )
        resolved_keys.add(file_uid.upper())
        resolved_keys.add(file_name.upper())
        resolved_keys.add(logical_name)

    already_available = 0
    if skip_existing and targets:
        with connection(settings) as conn:
            hot_store_exists = bool(
                fetch_all_dict(
                    conn,
                    "SELECT to_regclass(%s) IS NOT NULL AS present",
                    (REUSED_IMAGE_TABLE,),
                )[0]["present"]
            )
            existing: set[tuple[str, str]] = set()
            existing_names: set[str] = set()
            if hot_store_exists:
                rows = fetch_all_dict(
                    conn,
                    """
                    SELECT file_uid, file_name
                    FROM processing.gambar_reused
                    WHERE source_group=%s
                      AND is_active=true
                    """,
                    (source_group,),
                )
                existing = {
                    (
                        str(row.get("file_uid") or ""),
                        _logical_name(str(row.get("file_name") or "")),
                    )
                    for row in rows
                }
                existing_names = {
                    _logical_name(str(row.get("file_name") or "")) for row in rows
                }

            pending: list[Target] = []
            for target in targets:
                identity = (target.file_uid, target.logical_name)
                if identity in existing or (
                    transport_only and target.logical_name in existing_names
                ):
                    already_available += 1
                else:
                    pending.append(target)
            targets = pending

    unmatched = [
        key
        for key in (requested_keys or [])
        if key.upper() not in resolved_keys and _logical_name(key) not in resolved_keys
    ]
    return targets, unmatched, already_available


def recover_gold_evidence_from_folders(
    settings: Settings,
    source_group: str,
    *,
    nas_roots: list[Path],
    input_report: Path | None,
    active_gold: bool,
    transport_only: bool = False,
    panen_only: bool = False,
    batch_size: int = 64,
    apply: bool = False,
    report_path: str | None = None,
    targeted_overlay: bool = False,
) -> dict[str, Any]:
    """Pulihkan evidence aktif langsung dari Gold + folder, tanpa membaca Silver.

    targeted_overlay=True menyimpan hanya target yang diminta ke overlay sehat
    dan tidak pernah menulis processing.gambar_reused yang TOAST-nya rusak.
    """
    source_group = source_group.upper().strip()
    if not nas_roots:
        raise ValueError("Mode --input-report/--active-gold memerlukan --nas-root")
    if not 1 <= batch_size <= 256:
        raise ValueError("--batch-size harus 1..256")
    if bool(input_report) == bool(active_gold):
        raise ValueError("Pilih tepat satu --input-report atau --active-gold")
    if transport_only and not active_gold:
        raise ValueError("--transport-only hanya dapat dipakai bersama --active-gold")
    if panen_only and not active_gold:
        raise ValueError("--panen-only hanya dapat dipakai bersama --active-gold")
    if transport_only and panen_only:
        raise ValueError("Pilih salah satu --transport-only atau --panen-only")
    if apply and targeted_overlay:
        _ensure_targeted_recovery_table(settings)
    elif apply:
        with connection(settings) as conn:
            present = bool(
                fetch_all_dict(
                    conn,
                    "SELECT to_regclass(%s) IS NOT NULL AS present",
                    (REUSED_IMAGE_TABLE,),
                )[0]["present"]
            )
        if not present:
            raise RuntimeError(
                "processing.gambar_reused belum ada. Jalankan dahulu: "
                "py run_pipeline.py init-db"
            )

    requested_keys = (
        _read_report_image_keys(input_report) if input_report is not None else None
    )
    targets, ignored_input_keys, already_available = _gold_evidence_targets(
        settings,
        source_group,
        requested_keys,
        # Full active-Gold recovery is the repeatable/daily path. Existing
        # healthy identities are not read or rewritten, avoiding MVCC/TOAST
        # bloat. A CSV report remains a targeted repair and may overwrite a
        # known-bad row.
        skip_existing=bool(active_gold and apply),
        transport_only=transport_only,
        panen_only=panen_only,
    )
    mode = (
        "TARGETED_REPORT_OVERLAY"
        if targeted_overlay and input_report is not None
        else "REPORT_CSV"
        if input_report is not None
        else "ACTIVE_GOLD_TRANSPORT"
        if transport_only
        else "ACTIVE_GOLD_PANEN"
        if panen_only
        else "ACTIVE_GOLD"
    )
    output_report = Path(
        report_path or f"gold_recovery_{source_group.lower()}_{mode.lower()}.csv"
    ).resolve()
    print(
        f"[GOLD RECOVERY] source={source_group} mode={mode} "
        f"targets={len(targets):,} "
        f"already_available={already_available:,} "
        f"ignored_other_source_keys={len(ignored_input_keys):,} "
        f"apply={apply}",
        flush=True,
    )

    wanted_names = {target.logical_name for target in targets}
    nas_index = _index_nas(nas_roots, wanted_names) if wanted_names else {}
    validated_by_name: dict[str, tuple[Path, bytes, str] | Exception] = {}
    report_rows: list[dict[str, Any]] = []
    recovered = 0
    unresolved = 0

    for batch_no, target_batch in enumerate(_chunks(targets, batch_size), start=1):
        write_rows: list[tuple[Any, ...]] = []
        for target in target_batch:
            cached = validated_by_name.get(target.logical_name)
            if cached is None:
                candidates = nas_index.get(target.logical_name, [])
                accepted: tuple[Path, bytes, str] | None = None
                errors: list[str] = []
                hashes: dict[str, tuple[Path, bytes]] = {}
                for path in candidates:
                    try:
                        payload = path.read_bytes()
                        _verify_image(payload)
                        sha256 = hashlib.sha256(payload).hexdigest()
                        hashes.setdefault(sha256, (path, payload))
                    except Exception as exc:
                        errors.append(f"{path}: {exc}")
                if len(hashes) == 1:
                    sha256, (path, payload) = next(iter(hashes.items()))
                    accepted = (path, payload, sha256)
                    validated_by_name[target.logical_name] = accepted
                    cached = accepted
                elif len(hashes) > 1:
                    error = ValueError(
                        f"nama file ambigu: ditemukan {len(hashes)} payload berbeda"
                    )
                    validated_by_name[target.logical_name] = error
                    cached = error
                else:
                    error = ValueError(
                        "; ".join(errors)[:1000]
                        if errors
                        else "nama file tidak ditemukan pada folder sumber"
                    )
                    validated_by_name[target.logical_name] = error
                    cached = error

            if isinstance(cached, Exception):
                unresolved += 1
                report_rows.append(
                    {
                        "file_uid": target.file_uid,
                        "file_name": target.file_name,
                        "status": "NOT_RECOVERED",
                        "match_method": f"NAS_{mode}_EXACT_NAME_DECODED",
                        "recovery_source": "",
                        "message": str(cached)[:1000],
                    }
                )
                continue

            path, payload, sha256 = cached
            write_rows.append(
                (
                    source_group,
                    target.file_uid,
                    target.file_name,
                    target.mime_type,
                    sha256,
                    len(payload),
                    payload,
                    f"NAS:{path}",
                    f"NAS_{mode}_EXACT_NAME_DECODED",
                    True,
                )
            )
            recovered += 1
            report_rows.append(
                {
                    "file_uid": target.file_uid,
                    "file_name": target.file_name,
                    "status": "RECOVERED_NAS",
                    "match_method": f"NAS_{mode}_EXACT_NAME_DECODED",
                    "recovery_source": str(path),
                    "message": "",
                }
            )
        if apply:
            if targeted_overlay:
                _persist_targeted_recovery(settings, write_rows)
            else:
                # CSV deep-audit dapat menunjuk row yang metadata hash/ukurannya
                # benar tetapi payload TOAST fisiknya rusak. Row tersebut wajib
                # ditulis ulang walaupun metadata tidak berubah.
                _persist(
                    settings,
                    write_rows,
                    force_replace=input_report is not None,
                )
        print(
            f"[GOLD RECOVERY] batch={batch_no:,} recovered={recovered:,} "
            f"unresolved={unresolved:,}",
            flush=True,
        )

    output_report.parent.mkdir(parents=True, exist_ok=True)
    with output_report.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "file_uid",
                "file_name",
                "status",
                "match_method",
                "recovery_source",
                "message",
            ],
        )
        writer.writeheader()
        writer.writerows(report_rows)

    result = {
        "source_group": source_group,
        "mode": mode,
        "targets": len(targets),
        "already_available": already_available,
        "recovered": recovered,
        "ignored_other_source_keys": len(ignored_input_keys),
        "remaining": unresolved,
        "applied": apply,
        "report": str(output_report),
        "targeted_overlay": bool(targeted_overlay),
    }
    print(f"[GOLD RECOVERY] DONE {result}", flush=True)
    return result


def recover_corrupt_bytea(
    settings: Settings,
    source_group: str,
    *,
    donor_source_groups: list[str],
    nas_roots: list[Path],
    batch_size: int = 64,
    apply: bool = False,
    report_path: str | None = None,
) -> dict[str, Any]:
    source_group = source_group.upper().strip()
    donor_source_groups = list(dict.fromkeys(x.upper().strip() for x in donor_source_groups if x.strip()))
    if source_group in donor_source_groups:
        raise ValueError("Target source-group tidak boleh menjadi donor dirinya sendiri")
    if not donor_source_groups and not nas_roots:
        raise ValueError("Berikan minimal satu --donor-source-group atau --nas-root")
    if not 1 <= batch_size <= 256:
        raise ValueError("--batch-size harus 1..256")

    targets, already_recovered = _load_targets(settings, source_group)
    total_corrupt = len(targets) + already_recovered
    print(
        f"[RECOVERY] target={source_group} corrupt={total_corrupt:,} "
        f"already_recovered={already_recovered:,} pending={len(targets):,} "
        f"mode={'APPLY' if apply else 'DRY-RUN'}",
        flush=True,
    )
    report = Path(report_path or f"bytea_recovery_{source_group.lower()}.csv").resolve()
    if not targets:
        print("[RECOVERY] tidak ada exception aktif yang belum mempunyai recovery", flush=True)
        return {"target": source_group, "pending": 0, "recovered": already_recovered, "report": str(report)}

    target_by_uid = {target.file_uid: target for target in targets}
    unresolved = set(target_by_uid)
    report_rows: list[dict[str, Any]] = []
    recovered_db = recovered_nas = failed_validation = 0
    started = time.time()
    uid_join_condition = _image_uid_join_condition(settings)

    donors = _load_donor_metadata(settings, donor_source_groups)
    donors_by_name: dict[str, list[Donor]] = defaultdict(list)
    donors_by_hash: dict[str, list[Donor]] = defaultdict(list)
    for donor in donors:
        donors_by_name[donor.logical_name].append(donor)
        if donor.stored_hash:
            donors_by_hash[donor.stored_hash].append(donor)
    print(f"[RECOVERY] donor metadata={len(donors):,} groups={donor_source_groups}", flush=True)

    selected: list[tuple[Target, Donor, str]] = []
    for target in targets:
        candidates = []
        if target.expected_hash:
            candidates.extend(donors_by_hash.get(target.expected_hash, []))
        candidates.extend(donors_by_name.get(target.logical_name, []))
        unique = {(d.source_group, d.file_uid): d for d in candidates}
        donor, method = _choose_donor(target, list(unique.values()))
        if donor:
            selected.append((target, donor, method))

    for batch_no, selection_batch in enumerate(_chunks(selected, batch_size), start=1):
        unique_donors = list({(d.source_group, d.file_uid): d for _, d, _ in selection_batch}.values())
        try:
            payloads = _fetch_donor_payloads(
                settings,
                unique_donors,
                uid_join_condition,
            )
        except Exception as exc:
            print(f"[RECOVERY] donor batch={batch_no:,} gagal dibaca: {exc}", flush=True)
            payloads = {}
        write_rows: list[tuple[Any, ...]] = []
        for target, donor, method in selection_batch:
            payload = payloads.get((donor.source_group, donor.file_uid))
            try:
                if payload is None:
                    raise ValueError("BYTEA donor tidak ditemukan/dapat dibaca")
                _verify_image(payload)
                if not _hash_matches(payload, target.expected_hash):
                    raise ValueError("hash donor berbeda dari metadata target")
                sha256 = hashlib.sha256(payload).hexdigest()
                write_rows.append(
                    (
                        source_group,
                        target.file_uid,
                        target.file_name,
                        target.mime_type,
                        sha256,
                        len(payload),
                        payload,
                        f"DB:{donor.source_group}:{donor.file_uid}",
                        method,
                        True,
                    )
                )
                unresolved.discard(target.file_uid)
                recovered_db += 1
                report_rows.append({"file_uid": target.file_uid, "file_name": target.file_name, "status": "RECOVERED_DB", "match_method": method, "recovery_source": f"DB:{donor.source_group}:{donor.file_uid}", "message": ""})
            except Exception as exc:
                failed_validation += 1
                report_rows.append({"file_uid": target.file_uid, "file_name": target.file_name, "status": "DB_DONOR_REJECTED", "match_method": method, "recovery_source": f"DB:{donor.source_group}:{donor.file_uid}", "message": str(exc)[:1000]})
        if apply:
            _persist(settings, write_rows)
        print(
            f"[RECOVERY] DB batch={batch_no:,} recovered={recovered_db:,} "
            f"remaining={len(unresolved):,} elapsed={time.time() - started:,.1f}s",
            flush=True,
        )

    if nas_roots and unresolved:
        wanted_names = {target_by_uid[uid].logical_name for uid in unresolved}
        nas_index = _index_nas(nas_roots, wanted_names)
        pending_nas = [target_by_uid[uid] for uid in sorted(unresolved)]
        for batch_no, target_batch in enumerate(_chunks(pending_nas, batch_size), start=1):
            write_rows = []
            for target in target_batch:
                candidates = nas_index.get(target.logical_name, [])
                accepted: tuple[Path, bytes] | None = None
                errors: list[str] = []
                for path in candidates:
                    try:
                        if target.expected_size is not None and path.stat().st_size != target.expected_size:
                            errors.append(f"{path}: size berbeda")
                            continue
                        payload = path.read_bytes()
                        _verify_image(payload)
                        if not _hash_matches(payload, target.expected_hash):
                            errors.append(f"{path}: hash berbeda")
                            continue
                        accepted = (path, payload)
                        break
                    except Exception as exc:
                        errors.append(f"{path}: {exc}")
                if accepted is None:
                    report_rows.append({"file_uid": target.file_uid, "file_name": target.file_name, "status": "NOT_RECOVERED", "match_method": "NAS_EXACT_NAME_SIZE_HASH", "recovery_source": "", "message": "; ".join(errors)[:1000] if errors else "nama file tidak ditemukan pada NAS"})
                    continue
                path, payload = accepted
                sha256 = hashlib.sha256(payload).hexdigest()
                write_rows.append((source_group, target.file_uid, target.file_name, target.mime_type, sha256, len(payload), payload, f"NAS:{path}", "NAS_EXACT_NAME_SIZE_HASH", True))
                unresolved.discard(target.file_uid)
                recovered_nas += 1
                report_rows.append({"file_uid": target.file_uid, "file_name": target.file_name, "status": "RECOVERED_NAS", "match_method": "NAS_EXACT_NAME_SIZE_HASH", "recovery_source": str(path), "message": ""})
            if apply:
                _persist(settings, write_rows)
            print(
                f"[RECOVERY] NAS batch={batch_no:,} recovered={recovered_nas:,} "
                f"remaining={len(unresolved):,} elapsed={time.time() - started:,.1f}s",
                flush=True,
            )

    reported = {str(row["file_uid"]) for row in report_rows}
    for uid in sorted(unresolved):
        if uid not in reported:
            target = target_by_uid[uid]
            report_rows.append({"file_uid": uid, "file_name": target.file_name, "status": "NOT_RECOVERED", "match_method": "", "recovery_source": "", "message": "tidak ada donor exact yang valid"})

    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["file_uid", "file_name", "status", "match_method", "recovery_source", "message"])
        writer.writeheader()
        writer.writerows(report_rows)

    result = {
        "target": source_group,
        "corrupt": total_corrupt,
        "already_recovered": already_recovered,
        "recovered_db": recovered_db,
        "recovered_nas": recovered_nas,
        "remaining": len(unresolved),
        "failed_validation": failed_validation,
        "applied": apply,
        "report": str(report),
    }
    print(f"[RECOVERY] DONE {result}", flush=True)
    if not apply and recovered_db + recovered_nas:
        print("[RECOVERY] DRY-RUN: ulangi dengan --apply untuk menyimpan recovery", flush=True)
    return result


def cleanup_legacy_recovery(
    settings: Settings,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Hapus recovery v1/v2 hanya setelah gambar_reused menutup seluruh Gold."""
    coverage_sql = """
        WITH evidence AS (
            SELECT upper(source_group) AS source_group,
                   nullif(trim(file_uid::text), '') AS file_uid,
                   trim(image_name::text) AS file_name
            FROM gold.reused_photo_detail
            WHERE is_active=true
              AND nullif(trim(image_name::text), '') IS NOT NULL

            UNION ALL

            SELECT upper(source_group),
                   nullif(trim(foto_dt_uid::text), ''),
                   trim(foto_dt::text)
            FROM gold.reused_photo_detail
            WHERE is_active=true
              AND nullif(trim(foto_dt::text), '') IS NOT NULL

            UNION ALL

            SELECT upper(source_group),
                   nullif(trim(foto_ft_uid::text), ''),
                   trim(foto_ft::text)
            FROM gold.reused_photo_detail
            WHERE is_active=true
              AND nullif(trim(foto_ft::text), '') IS NOT NULL
        ),
        normalized AS (
            SELECT DISTINCT source_group, file_uid, file_name
            FROM evidence
            WHERE file_name ~* '\\.(jpg|jpeg|png|bmp|webp|tif|tiff)$'
        )
        SELECT e.source_group,
               count(*)::bigint AS active_images,
               count(*) FILTER (
                   WHERE EXISTS (
                       SELECT 1
                       FROM processing.gambar_reused r
                       WHERE r.is_active=true
                         AND r.source_group=e.source_group
                         AND (
                               (e.file_uid IS NOT NULL AND r.file_uid=e.file_uid)
                               OR upper(trim(r.file_name))=upper(trim(e.file_name))
                             )
                   )
               )::bigint AS ready_images
        FROM normalized e
        GROUP BY e.source_group
        ORDER BY e.source_group
    """
    with connection(settings) as conn:
        present = fetch_all_dict(
            conn,
            "SELECT to_regclass(%s) IS NOT NULL AS present",
            (REUSED_IMAGE_TABLE,),
        )[0]["present"]
        if not present:
            raise RuntimeError(
                "processing.gambar_reused belum ada. Jalankan init-db dahulu."
            )

        coverage = fetch_all_dict(conn, coverage_sql)
        # Full detoast read: memastikan seluruh hot store dapat dibaca dan
        # panjang payload masih sesuai sebelum relation legacy dihapus.
        payload_check = fetch_all_dict(
            conn,
            """
            SELECT count(*)::bigint AS rows,
                   coalesce(sum(bytea_len), 0)::bigint AS declared_bytes,
                   coalesce(sum(octet_length(image_bytes)), 0)::bigint
                       AS readable_bytes,
                   count(*) FILTER (
                       WHERE octet_length(image_bytes) <> bytea_len
                   )::bigint AS length_mismatch
            FROM processing.gambar_reused
            WHERE is_active=true
            """,
        )[0]
        legacy_sizes = fetch_all_dict(
            conn,
            """
            SELECT relation,
                   CASE
                     WHEN to_regclass(relation) IS NULL THEN 0
                     ELSE pg_total_relation_size(to_regclass(relation))
                   END::bigint AS total_bytes
            FROM (
                VALUES
                    ('processing.source_bytea_recovery'::text),
                    ('processing.source_bytea_recovery_v2'::text)
            ) AS legacy(relation)
            ORDER BY relation
            """,
        )

    missing_total = 0
    print("=" * 78)
    print("GAMBAR REUSED / LEGACY RECOVERY CLEANUP")
    print("=" * 78)
    for row in coverage:
        active = int(row["active_images"])
        ready = int(row["ready_images"])
        missing = active - ready
        missing_total += missing
        print(
            f"{row['source_group']}: active={active:,} "
            f"ready={ready:,} missing={missing:,}"
        )
    print(
        "HOT BYTEA "
        f"rows={int(payload_check['rows']):,} "
        f"declared={int(payload_check['declared_bytes']):,} "
        f"readable={int(payload_check['readable_bytes']):,} "
        f"length_mismatch={int(payload_check['length_mismatch']):,}"
    )
    for row in legacy_sizes:
        print(f"LEGACY {row['relation']}: bytes={int(row['total_bytes']):,}")

    if not coverage or int(payload_check["rows"]) == 0:
        raise RuntimeError("STOP: gambar_reused kosong atau Gold aktif tidak terbaca.")
    if missing_total:
        raise RuntimeError(
            f"STOP: masih ada {missing_total:,} evidence Gold tanpa gambar_reused."
        )
    if int(payload_check["length_mismatch"]):
        raise RuntimeError("STOP: panjang BYTEA gambar_reused tidak konsisten.")

    if not apply:
        print("DRY-RUN lulus; tabel legacy belum dihapus. Tambahkan --apply.")
        return {
            "coverage": coverage,
            "payload_check": payload_check,
            "legacy_sizes": legacy_sizes,
            "applied": False,
        }

    with connection(settings) as conn, conn.cursor() as cur:
        # RESTRICT adalah default; dependency tak terduga akan menggagalkan
        # cleanup tanpa menghapus objek lain.
        cur.execute(
            "DROP TABLE IF EXISTS processing.source_bytea_recovery_v2 RESTRICT"
        )
        cur.execute(
            "DROP TABLE IF EXISTS processing.source_bytea_recovery RESTRICT"
        )
    print("OK recovery v1/v2 dihapus; processing.gambar_reused tetap aktif.")
    return {
        "coverage": coverage,
        "payload_check": payload_check,
        "legacy_sizes": legacy_sizes,
        "applied": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Pulihkan BYTEA ke processing.gambar_reused: dari exception, "
            "CSV report, atau seluruh evidence Gold aktif"
        )
    )
    parser.add_argument("--source-group", required=True, help="Target korup, contoh DAN")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--input-report",
        help=(
            "CSV image_recovery_needed.csv; target di-resolve dari Gold tanpa "
            "membaca Silver"
        ),
    )
    mode.add_argument(
        "--active-gold",
        action="store_true",
        help=(
            "Pulihkan seluruh gambar yang dipakai Gold aktif untuk dashboard/report "
            "tanpa membaca Silver"
        ),
    )
    evidence_role = parser.add_mutually_exclusive_group()
    evidence_role.add_argument(
        "--transport-only",
        action="store_true",
        help=(
            "Bersama --active-gold, pulihkan hanya foto transport DT/FT aktif. "
            "Foto panen dan Silver tidak dipindai/dimuat."
        ),
    )
    evidence_role.add_argument(
        "--panen-only",
        action="store_true",
        help=(
            "Bersama --active-gold, pulihkan hanya foto panen aktif. "
            "Foto transport dan Silver tidak dipindai/dimuat."
        ),
    )
    parser.add_argument(
        "--donor-source-group",
        action="append",
        default=[],
        help="Source sehat; dapat diulang, contoh ELHP_DAN",
    )
    parser.add_argument(
        "--nas-root",
        action="append",
        default=[],
        help=r"Root folder NAS; dapat diulang, contoh \\server\share\DAN",
    )
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--report", help="CSV hasil recovery")
    parser.add_argument(
        "--targeted-overlay",
        action="store_true",
        help=(
            "Khusus --input-report: simpan hasil ke "
            "processing.gambar_reused_targeted_recovery, tidak menyentuh "
            "hot store utama yang TOAST-nya rusak."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Simpan ke processing.gambar_reused; tanpa flag ini hanya dry-run. "
            "Mode --active-gold melewati row yang sudah tersedia."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.transport_only and not args.active_gold:
        raise ValueError("--transport-only harus dipakai bersama --active-gold")
    if args.panen_only and not args.active_gold:
        raise ValueError("--panen-only harus dipakai bersama --active-gold")
    settings = Settings()
    settings.validate()
    with pipeline_job_lock(settings, f"bytea-recovery:{args.source_group.upper()}"):
        if args.input_report or args.active_gold:
            if args.donor_source_group:
                raise ValueError(
                    "--donor-source-group tidak dipakai pada mode "
                    "--input-report/--active-gold"
                )
            recover_gold_evidence_from_folders(
                settings,
                args.source_group,
                nas_roots=[Path(value) for value in args.nas_root],
                input_report=Path(args.input_report) if args.input_report else None,
                active_gold=bool(args.active_gold),
                transport_only=bool(args.transport_only),
                panen_only=bool(args.panen_only),
                batch_size=args.batch_size,
                apply=args.apply,
                report_path=args.report,
                targeted_overlay=bool(args.targeted_overlay),
            )
        else:
            recover_corrupt_bytea(
                settings,
                args.source_group,
                donor_source_groups=args.donor_source_group,
                nas_roots=[Path(value) for value in args.nas_root],
                batch_size=args.batch_size,
                apply=args.apply,
                report_path=args.report,
            )
    return 0
