from __future__ import annotations

import csv
import hashlib
import mimetypes
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from PIL import Image
from psycopg2.extras import execute_values

from .config import Settings
from .db import connection, fetch_all_dict, qname, table_columns
from .utils import parse_date_from_filename
from .io_diagnostics import probe_nas_root


@dataclass(frozen=True)
class SourceFile:
    root: Path
    path: Path
    relative_path: str
    logical_name: str
    size: int
    mtime_epoch: int


@dataclass(frozen=True)
class ExistingRow:
    file_uid: str
    file_size_bytes: int | None
    file_mtime_epoch: int | None
    file_sha256: str | None
    has_bytea: bool
    bytea_len: int | None


@dataclass
class ScanStats:
    entries: int = 0
    directories: int = 0
    eligible_files: int = 0
    eligible_bytes: int = 0
    errors: int = 0
    month_accepted: int = 0
    month_outside: int = 0
    month_unparsed: int = 0
    target_outside: int = 0


_MONTH_PATTERN = re.compile(r"^20[0-9]{2}-(0[1-9]|1[0-2])$")
_MONTH_REPORT_SAMPLE_LIMIT = 200
_CORRUPT_INDEX_PATTERN = re.compile(
    r'index\s+"([A-Za-z_][A-Za-z0-9_]*)"\s+contains\s+unexpected\s+zero\s+page',
    re.IGNORECASE,
)


def silver_write_failure_message(
    exc: BaseException,
    *,
    schema: str,
    table: str,
) -> str | None:
    """Return an actionable message for physical PostgreSQL storage failures."""
    detail = str(exc or "").strip()
    lowered = detail.lower()
    physical_markers = (
        "contains unexpected zero page",
        "invalid page in block",
        "could not read blocks",
        "could not open file \"base/",
        "previous segment is only",
    )
    if not any(marker in lowered for marker in physical_markers):
        return None
    match = _CORRUPT_INDEX_PATTERN.search(detail)
    if match:
        damaged_index = qname(schema, match.group(1))
        repair = f"REINDEX INDEX {damaged_index};"
        damaged_object = damaged_index
    else:
        damaged_object = qname(schema, table)
        repair = f"REINDEX TABLE {damaged_object};"
    return (
        "SILVER_STORAGE_CORRUPTED: PostgreSQL tidak dapat menulis ke "
        f"{damaged_object}. Batch aktif di-rollback dan tidak dicoba dua kali; "
        "batch lama yang sudah commit tetap aman. Jalankan dari DBeaver: "
        f"{repair} Setelah selesai jalankan ANALYZE {qname(schema, table)}; "
        "lalu ulangi command merge-month yang sama (resume/anti-duplikasi aktif). "
        f"Detail asli: {detail}"
    )


def normalize_expected_month(value: str | None) -> str | None:
    if value is None:
        return None
    month = str(value).strip()
    if not _MONTH_PATTERN.fullmatch(month):
        raise ValueError("--expected-month harus format YYYY-MM, contoh 2026-07")
    return month


def iter_month_scoped_files(
    files: Iterable[SourceFile],
    expected_month: str | None,
    *,
    timestamp_utc_offset_hours: int,
    report_rows: list[dict[str, Any]],
    stats: ScanStats,
) -> Iterable[SourceFile]:
    """Keep only files whose camera/filename date belongs to one month."""
    month = normalize_expected_month(expected_month)
    for item in files:
        if month is None:
            stats.month_accepted += 1
            yield item
            continue

        parsed = parse_date_from_filename(
            item.path.name,
            timestamp_utc_offset_hours,
        )
        if parsed is None:
            stats.month_unparsed += 1
            report_rows.append(
                {
                    "status": "MONTH_UNPARSED_SKIPPED",
                    "file_name": item.path.name,
                    "file_path": str(item.path),
                    "file_size_bytes": item.size,
                    "detail": f"expected_month={month}",
                }
            )
            continue
        actual_month = parsed.strftime("%Y-%m")
        if actual_month != month:
            stats.month_outside += 1
            report_rows.append(
                {
                    "status": "MONTH_OUT_OF_SCOPE_SKIPPED",
                    "file_name": item.path.name,
                    "file_path": str(item.path),
                    "file_size_bytes": item.size,
                    "detail": (
                        f"expected_month={month}; filename_month={actual_month}"
                    ),
                }
            )
            continue
        stats.month_accepted += 1
        yield item


_REQUIRED_COLUMNS = {
    "file_uid",
    "source_group",
    "source_root",
    "source_folder",
    "file_path",
    "relative_path",
    "file_name",
    "file_name_upper",
    "file_ext",
    "mime_type",
    "is_transport",
    "file_size_bytes",
    "file_mtime_epoch",
    "file_sha256",
    "image_bytes",
    "bytea_len",
    "load_status",
    "file_exists",
    "zero_byte",
    "last_checked_at",
    "loaded_at",
}


def logical_name(value: str) -> str:
    return Path(str(value or "").strip()).name.upper()


def deterministic_file_uid(source_group: str, file_name: str) -> str:
    """Stable text UID without collapsing same bytes under different names."""
    key = f"{source_group.upper().strip()}\0{logical_name(file_name)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def discover_source_files(
    roots: Iterable[str | Path],
    valid_extensions: Iterable[str],
    *,
    max_files: int | None = None,
) -> tuple[list[SourceFile], list[dict[str, Any]]]:
    extensions = {
        value.lower() if str(value).startswith(".") else "." + str(value).lower()
        for value in valid_extensions
    }
    discovered: list[SourceFile] = []
    problems: list[dict[str, Any]] = []
    for raw_root in roots:
        root = Path(raw_root).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"Source root tidak ditemukan: {root}")
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            try:
                stat = path.stat()
            except OSError as exc:
                problems.append(
                    {
                        "status": "STAT_FAILED",
                        "file_name": path.name,
                        "file_path": str(path),
                        "detail": str(exc),
                    }
                )
                continue
            discovered.append(
                SourceFile(
                    root=root,
                    path=path,
                    relative_path=str(path.relative_to(root)),
                    logical_name=logical_name(path.name),
                    size=int(stat.st_size),
                    mtime_epoch=int(stat.st_mtime),
                )
            )
            if max_files is not None and len(discovered) >= max_files:
                return discovered, problems
    return discovered, problems


def iter_source_files(
    roots: Iterable[str | Path],
    valid_extensions: Iterable[str],
    problems: list[dict[str, Any]],
    stats: ScanStats,
    *,
    max_files: int | None = None,
    progress_every: int = 5000,
    expected_month: str | None = None,
    timestamp_utc_offset_hours: int = 7,
    target_names: set[str] | None = None,
    found_target_names: set[str] | None = None,
) -> Iterable[SourceFile]:
    """Stream NAS entries and reject other months before file stat/read."""
    month = normalize_expected_month(expected_month)
    extensions = {
        value.lower() if str(value).startswith(".") else "." + str(value).lower()
        for value in valid_extensions
    }
    scan_started = time.monotonic()
    last_progress_at = scan_started
    last_progress_entries = 0
    for raw_root in roots:
        root = Path(raw_root).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"Source root tidak ditemukan: {root}")
        print(f"[SILVER SCAN] root={root}", flush=True)
        pending_directories = [root]
        while pending_directories:
            directory = pending_directories.pop()
            stats.directories += 1
            try:
                entries = os.scandir(directory)
            except OSError as exc:
                stats.errors += 1
                problems.append(
                    {
                        "status": "DIRECTORY_SCAN_FAILED",
                        "file_path": str(directory),
                        "detail": str(exc),
                    }
                )
                continue
            try:
                with entries:
                    for entry in entries:
                        stats.entries += 1
                        if (
                            progress_every > 0
                            and stats.entries % progress_every == 0
                        ):
                            now_mono = time.monotonic()
                            elapsed = max(0.001, now_mono - scan_started)
                            interval = max(0.001, now_mono - last_progress_at)
                            overall_rate = stats.entries / elapsed
                            interval_rate = (stats.entries - last_progress_entries) / interval
                            print(
                                f"[SILVER SCAN] entries={stats.entries:,} "
                                f"folders={stats.directories:,} "
                                f"target_images={stats.eligible_files:,} "
                                f"outside_names={stats.month_outside:,} "
                                f"unparsed_names={stats.month_unparsed:,} "
                                f"errors={stats.errors:,} "
                                f"rate={overall_rate:,.0f}/s "
                                f"recent_rate={interval_rate:,.0f}/s "
                                f"elapsed={elapsed:,.1f}s",
                                flush=True,
                            )
                            if interval >= 30.0 and interval_rate < 250.0:
                                print(
                                    f"[SILVER SCAN WARN] NAS enumeration lambat: "
                                    f"recent_rate={interval_rate:,.0f}/s selama {interval:,.1f}s",
                                    flush=True,
                                )
                            last_progress_at = now_mono
                            last_progress_entries = stats.entries
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                pending_directories.append(Path(entry.path))
                                continue
                            # Extension check precedes is_file/stat so unrelated
                            # NAS entries do not each trigger an extra metadata read.
                            path = Path(entry.path)
                            if path.suffix.lower() not in extensions:
                                continue
                            entry_logical_name = logical_name(entry.name)
                            if (
                                target_names is not None
                                and entry_logical_name not in target_names
                            ):
                                stats.target_outside += 1
                                continue
                            # Pada root campuran Jan-Juli, tentukan bulan hanya
                            # dari nama file sebelum is_file/stat. Dengan begitu
                            # file lama tidak memicu metadata read NAS, hash,
                            # decode, atau BYTEA read/write.
                            if month is not None:
                                parsed = parse_date_from_filename(
                                    entry.name,
                                    timestamp_utc_offset_hours,
                                )
                                if parsed is None:
                                    stats.month_unparsed += 1
                                    if (
                                        stats.month_unparsed
                                        <= _MONTH_REPORT_SAMPLE_LIMIT
                                    ):
                                        problems.append(
                                            {
                                                "status": "MONTH_UNPARSED_SKIPPED",
                                                "file_name": entry.name,
                                                "file_path": entry.path,
                                                "detail": f"expected_month={month}",
                                            }
                                        )
                                    continue
                                actual_month = parsed.strftime("%Y-%m")
                                if actual_month != month:
                                    stats.month_outside += 1
                                    if (
                                        stats.month_outside
                                        <= _MONTH_REPORT_SAMPLE_LIMIT
                                    ):
                                        problems.append(
                                            {
                                                "status": "MONTH_OUT_OF_SCOPE_SKIPPED",
                                                "file_name": entry.name,
                                                "file_path": entry.path,
                                                "detail": (
                                                    f"expected_month={month}; "
                                                    f"filename_month={actual_month}"
                                                ),
                                            }
                                        )
                                    continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            stat = entry.stat(follow_symlinks=False)
                        except OSError as exc:
                            stats.errors += 1
                            problems.append(
                                {
                                    "status": "STAT_FAILED",
                                    "file_name": entry.name,
                                    "file_path": entry.path,
                                    "detail": str(exc),
                                }
                            )
                            continue

                        stats.eligible_files += 1
                        stats.eligible_bytes += max(0, int(stat.st_size))
                        stats.month_accepted += 1
                        if found_target_names is not None:
                            found_target_names.add(entry_logical_name)
                        yield SourceFile(
                            root=root,
                            path=path,
                            relative_path=str(path.relative_to(root)),
                            logical_name=entry_logical_name,
                            size=int(stat.st_size),
                            mtime_epoch=int(stat.st_mtime),
                        )
                        if (
                            max_files is not None
                            and stats.eligible_files >= max_files
                        ):
                            return
            except OSError as exc:
                stats.errors += 1
                problems.append(
                    {
                        "status": "DIRECTORY_ITERATION_FAILED",
                        "file_path": str(directory),
                        "detail": str(exc),
                    }
                )


def resolve_duplicate_logical_names(
    files: list[SourceFile],
    source_group: str,
    report_rows: list[dict[str, Any]],
) -> tuple[list[SourceFile], int]:
    """Choose one exact duplicate and block ambiguous same-name collisions."""
    groups: dict[str, list[SourceFile]] = {}
    for item in files:
        groups.setdefault(item.logical_name, []).append(item)

    survivors: list[SourceFile] = []
    conflict_count = 0
    for logical in sorted(groups):
        candidates = sorted(groups[logical], key=lambda item: str(item.path).upper())
        if len(candidates) == 1:
            survivors.append(candidates[0])
            continue

        by_hash: dict[str, list[SourceFile]] = {}
        for item in candidates:
            try:
                digest = hashlib.sha256(item.path.read_bytes()).hexdigest()
            except Exception as exc:
                report_rows.append(
                    {
                        "status": "DUPLICATE_PREFLIGHT_READ_FAILED",
                        "source_group": source_group,
                        "file_name": item.path.name,
                        "file_path": str(item.path),
                        "file_size_bytes": item.size,
                        "detail": str(exc),
                    }
                )
                continue
            by_hash.setdefault(digest, []).append(item)

        if not by_hash:
            continue
        if len(by_hash) > 1:
            conflict_count += 1
            for digest, items in sorted(by_hash.items()):
                for item in items:
                    report_rows.append(
                        {
                            "status": "CONFLICT_SAME_NAME_DIFFERENT_HASH",
                            "source_group": source_group,
                            "file_name": item.path.name,
                            "file_path": str(item.path),
                            "file_size_bytes": item.size,
                            "file_sha256": digest,
                            "detail": (
                                f"logical_name={logical}; loader dibatalkan "
                                "sebelum penulisan"
                            ),
                        }
                    )
            continue

        digest, exact_duplicates = next(iter(by_hash.items()))
        survivor = exact_duplicates[0]
        survivors.append(survivor)
        for duplicate in exact_duplicates[1:]:
            report_rows.append(
                {
                    "status": "DUPLICATE_LOGICAL_SAME_HASH_SKIPPED",
                    "source_group": source_group,
                    "file_name": duplicate.path.name,
                    "file_path": str(duplicate.path),
                    "file_size_bytes": duplicate.size,
                    "file_sha256": digest,
                    "detail": f"survivor={survivor.path}",
                }
            )
    return survivors, conflict_count


def _load_identity_map(settings: Settings, source_group: str) -> dict[str, str]:
    """Reuse old IDs so feature/context rows remain connected after TRUNCATE."""
    source_group = source_group.upper()
    identities: dict[str, str] = {}
    with connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (upper(trim(file_name)))
                       upper(trim(file_name)) AS logical_name,
                       file_uid::text AS file_uid
                FROM processing.image_feature
                WHERE upper(source_group)=upper(%s)
                  AND nullif(trim(file_name), '') IS NOT NULL
                ORDER BY upper(trim(file_name)),
                         is_valid DESC,
                         embedded_at DESC,
                         file_uid
                """,
                (source_group,),
            )
            for logical, file_uid in cur.fetchall():
                identities[str(logical)] = str(file_uid)

            # Gold-active identity is authoritative over an old non-canonical
            # feature identity for the same logical filename.
            cur.execute(
                """
                SELECT DISTINCT ON (upper(trim(file_name)))
                       upper(trim(file_name)) AS logical_name,
                       file_uid::text AS file_uid
                FROM processing.gambar_reused
                WHERE upper(source_group)=upper(%s)
                  AND is_active=true
                ORDER BY upper(trim(file_name)), recovered_at DESC, file_uid
                """,
                (source_group,),
            )
            for logical, file_uid in cur.fetchall():
                identities[str(logical)] = str(file_uid)
    return identities


def _load_existing(settings: Settings, source_group: str) -> dict[str, ExistingRow]:
    table = qname(settings.silver_schema, settings.image_table)
    with connection(settings) as conn:
        rows = fetch_all_dict(
            conn,
            f"""
            SELECT file_uid::text AS file_uid,
                   file_size_bytes::bigint AS file_size_bytes,
                   file_mtime_epoch::bigint AS file_mtime_epoch,
                   file_sha256::text AS file_sha256,
                   (image_bytes IS NOT NULL) AS has_bytea,
                   bytea_len::bigint AS bytea_len
            FROM {table}
            WHERE upper(source_group::text)=upper(%s)
            """,
            (source_group,),
        )
    return {
        str(row["file_uid"]): ExistingRow(
            file_uid=str(row["file_uid"]),
            file_size_bytes=(
                int(row["file_size_bytes"])
                if row.get("file_size_bytes") is not None
                else None
            ),
            file_mtime_epoch=(
                int(row["file_mtime_epoch"])
                if row.get("file_mtime_epoch") is not None
                else None
            ),
            file_sha256=(
                str(row["file_sha256"]).lower()
                if row.get("file_sha256")
                else None
            ),
            has_bytea=bool(row.get("has_bytea")),
            bytea_len=(
                int(row["bytea_len"]) if row.get("bytea_len") is not None else None
            ),
        )
        for row in rows
    }


def _validate_contract(settings: Settings) -> None:
    with connection(settings) as conn:
        columns = set(
            table_columns(conn, settings.silver_schema, settings.image_table)
        )
        missing = sorted(_REQUIRED_COLUMNS - columns)
        if missing:
            raise RuntimeError(
                "Kontrak silver.image_store_all_files tidak lengkap; kolom hilang: "
                + ", ".join(missing)
            )
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_index x
                    JOIN pg_class t ON t.oid=x.indrelid
                    JOIN pg_namespace n ON n.oid=t.relnamespace
                    JOIN pg_attribute a
                      ON a.attrelid=t.oid AND a.attnum=ANY(x.indkey)
                    WHERE n.nspname=%s
                      AND t.relname=%s
                      AND x.indisunique=true
                    GROUP BY x.indexrelid
                    HAVING count(*) FILTER (WHERE a.attname='file_uid')=1
                       AND count(*)=1
                )
                """,
                (settings.silver_schema, settings.image_table),
            )
            if not bool(cur.fetchone()[0]):
                raise RuntimeError(
                    "Loader memerlukan UNIQUE/PRIMARY KEY tunggal pada file_uid. "
                    "Jangan lanjut sebelum index sumber dipulihkan."
                )


def _decode_image(payload: bytes) -> None:
    # verify() memeriksa struktur; load() memaksa seluruh pixel didecode.
    from io import BytesIO

    with Image.open(BytesIO(payload)) as image:
        image.verify()
    with Image.open(BytesIO(payload)) as image:
        image.load()


def _write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "status",
        "source_group",
        "file_uid",
        "file_name",
        "file_path",
        "file_size_bytes",
        "file_sha256",
        "detail",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _load_recovery_target_names(
    report_path: str | Path,
    source_group: str,
) -> set[str]:
    path = Path(report_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Recovery report tidak ditemukan: {path}")
    names: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "file_name" not in reader.fieldnames:
            raise RuntimeError(f"CSV recovery tidak mempunyai kolom file_name: {path}")
        for row in reader:
            row_group = str(row.get("source_group") or "").upper().strip()
            if row_group and row_group != source_group:
                continue
            name = logical_name(str(row.get("file_name") or ""))
            if name:
                names.add(name)
    if not names:
        raise RuntimeError(
            f"CSV recovery tidak mempunyai target untuk source_group={source_group}: {path}"
        )
    return names


def _disk_preflight(
    settings: Settings,
    required_bytes: int,
    *,
    allow_low_disk: bool,
) -> dict[str, Any]:
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute("SHOW data_directory")
        data_directory = str(cur.fetchone()[0])
    usage = None
    checked_path = data_directory
    errors: list[str] = []
    candidates = [data_directory]
    drive_root = Path(data_directory).anchor
    if drive_root and drive_root not in candidates:
        candidates.append(drive_root)
    for candidate in candidates:
        try:
            usage = shutil.disk_usage(candidate)
            checked_path = candidate
            break
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
    if usage is None:
        print(
            "[SILVER LOAD] WARN kapasitas disk PostgreSQL tidak dapat dibaca: "
            + " | ".join(errors),
            flush=True,
        )
        return {"data_directory": data_directory, "disk_check": "UNAVAILABLE"}

    # BYTEA besar menghasilkan heap/TOAST/index dan WAL. Faktor 1.20 + 20 GiB
    # merupakan guard praktis, bukan estimasi ukuran fisik presisi.
    minimum_free = int(required_bytes * 1.20) + 20 * 1024**3
    result = {
        "data_directory": data_directory,
        "checked_path": checked_path,
        "free_bytes": int(usage.free),
        "minimum_free_bytes": minimum_free,
    }
    print(
        "[SILVER LOAD] disk "
        f"free={usage.free / 1024**3:,.1f} GiB "
        f"minimum_estimate={minimum_free / 1024**3:,.1f} GiB "
        f"checked_path={checked_path}",
        flush=True,
    )
    if usage.free < minimum_free and not allow_low_disk:
        raise RuntimeError(
            "Free disk di bawah guard loader. Kosongkan storage atau ulangi dengan "
            "--allow-low-disk hanya jika Anda sudah memeriksa tablespace/WAL."
        )
    return result


def load_silver_from_folders(
    settings: Settings,
    source_group: str,
    roots: list[str],
    *,
    batch_size: int = 64,
    report_path: str | None = None,
    apply: bool = False,
    max_files: int | None = None,
    allow_low_disk: bool = False,
    expected_month: str | None = None,
    input_report_path: str | None = None,
) -> dict[str, Any]:
    source_group = source_group.upper().strip()
    if not roots:
        raise ValueError("Berikan minimal satu --source-root")
    if batch_size < 1:
        raise ValueError("--batch-size minimal 1")
    expected_month = normalize_expected_month(expected_month)
    if expected_month and input_report_path:
        raise ValueError("Gunakan salah satu --expected-month atau --input-report")
    target_names = (
        _load_recovery_target_names(input_report_path, source_group)
        if input_report_path
        else None
    )
    found_target_names: set[str] = set()

    _validate_contract(settings)
    report = Path(
        report_path or f"silver_reload_{source_group.lower()}.csv"
    ).resolve()
    print("=" * 78)
    load_kind = (
        "REPORT RECOVERY"
        if target_names is not None
        else "INCREMENTAL MONTH"
        if expected_month
        else "FULL BYTEA"
    )
    print(f"SILVER {load_kind} LOAD: {source_group}")
    print("=" * 78)
    print(
        f"mode=STREAM_SCAN roots={len(roots)} batch_size={batch_size} "
        f"max_files={max_files or 'ALL'} expected_month={expected_month or 'ALL'} "
        f"target_names={len(target_names) if target_names is not None else 'ALL'} "
        f"apply={apply}",
        flush=True,
    )
    for probe_root in roots:
        try:
            probe = probe_nas_root(probe_root, sample_entries=25, read_bytes=0)
            smb = probe.get("smb") or {}
            print(
                "[SERVER PROBE] "
                f"status={probe.get('status')} "
                f"server={probe.get('server') or '-'} "
                f"smb445_ms={smb.get('latency_ms') if smb.get('latency_ms') is not None else '-'} "
                f"path_ms={probe.get('exists_latency_ms') if probe.get('exists_latency_ms') is not None else '-'} "
                f"scan25_ms={probe.get('scandir_latency_ms') if probe.get('scandir_latency_ms') is not None else '-'} "
                f"sample_rate={probe.get('sample_rate_per_sec') if probe.get('sample_rate_per_sec') is not None else '-'} "
                f"root={probe_root}",
                flush=True,
            )
            if probe.get("error"):
                print(f"[SERVER PROBE WARN] {probe.get('error')}", flush=True)
        except Exception as exc:
            print(f"[SERVER PROBE WARN] probe gagal root={probe_root}: {exc}", flush=True)
    print("[SILVER LOAD] membaca identity lama dan status resume ...", flush=True)
    identity_map = _load_identity_map(settings, source_group)
    existing = _load_existing(settings, source_group)
    print(
        f"identity_reuse={len(identity_map):,} existing_silver={len(existing):,} "
        "scan=belum dimulai",
        flush=True,
    )
    report_rows: list[dict[str, Any]] = []
    stats = ScanStats()
    files = iter_source_files(
        roots,
        settings.valid_extensions or (),
        report_rows,
        stats,
        max_files=max_files,
        expected_month=expected_month,
        timestamp_utc_offset_hours=settings.filename_timestamp_utc_offset_hours,
        target_names=target_names,
        found_target_names=found_target_names,
    )

    if not apply:
        try:
            for _ in files:
                pass
        except BaseException:
            _write_report(report, report_rows)
            print(
                f"[SILVER SCAN] interrupted entries={stats.entries:,} "
                f"images={stats.eligible_files:,}; database tidak diubah",
                flush=True,
            )
            raise
        if target_names is not None:
            for name in sorted(target_names - found_target_names):
                report_rows.append(
                    {
                        "status": "TARGET_FILE_NOT_FOUND",
                        "source_group": source_group,
                        "file_name": name,
                        "detail": "nama dari input-report tidak ditemukan pada NAS root",
                    }
                )
        _write_report(report, report_rows)
        print(
            "DRY-RUN selesai; payload tidak didecode dan database tidak diubah.",
            flush=True,
        )
        print(
            f"[MONTH GUARD] accepted={stats.month_accepted:,} "
            f"outside={stats.month_outside:,} unparsed={stats.month_unparsed:,}",
            flush=True,
        )
        if expected_month and stats.month_accepted == 0:
            raise RuntimeError(
                f"Tidak ada file filename-month={expected_month} pada root yang diberikan."
            )
        print(f"Tambahkan --apply setelah preflight diperiksa. report={report}")
        return {
            "source_group": source_group,
            "discovered": stats.month_accepted,
            "scanned_images": (
                stats.eligible_files + stats.month_outside + stats.month_unparsed
            ),
            "month_outside": stats.month_outside,
            "month_unparsed": stats.month_unparsed,
            "expected_month": expected_month,
            "target_names": len(target_names) if target_names is not None else None,
            "target_found": len(found_target_names),
            "bytes": stats.eligible_bytes,
            "scanned_entries": stats.entries,
            "scan_errors": stats.errors,
            "identity_reuse": len(identity_map),
            "existing": len(existing),
            "applied": False,
            "report": str(report),
        }

    # Streaming tidak mengetahui total payload sebelum scan selesai. Guard ini
    # memastikan ruang kerja minimum; free disk tetap perlu dipantau pada full load.
    _disk_preflight(
        settings,
        0,
        allow_low_disk=allow_low_disk,
    )

    table = qname(settings.silver_schema, settings.image_table)
    insert_sql = f"""
        INSERT INTO {table} (
            file_uid, source_group, source_root, source_folder, file_path,
            relative_path, file_name, file_name_upper, file_ext, mime_type,
            is_transport, file_size_bytes, file_mtime_epoch, file_sha256,
            image_bytes, bytea_len, load_status, file_exists, zero_byte,
            last_checked_at, loaded_at
        ) VALUES %s
        ON CONFLICT (file_uid) DO UPDATE SET
            source_group=excluded.source_group,
            source_root=excluded.source_root,
            source_folder=excluded.source_folder,
            file_path=excluded.file_path,
            relative_path=excluded.relative_path,
            file_name=excluded.file_name,
            file_name_upper=excluded.file_name_upper,
            file_ext=excluded.file_ext,
            mime_type=excluded.mime_type,
            is_transport=excluded.is_transport,
            file_size_bytes=excluded.file_size_bytes,
            file_mtime_epoch=excluded.file_mtime_epoch,
            file_sha256=excluded.file_sha256,
            image_bytes=excluded.image_bytes,
            bytea_len=excluded.bytea_len,
            load_status=excluded.load_status,
            file_exists=true,
            zero_byte=false,
            last_checked_at=now(),
            loaded_at=now()
    """

    batch: list[tuple[Any, ...]] = []
    existing_at_start = set(existing)
    seen_by_logical: dict[str, tuple[str, str]] = {}
    inserted = updated = skipped = invalid = conflicts = 0
    decoded = 0
    flush_failed = False

    def flush() -> None:
        nonlocal inserted, updated, flush_failed
        if not batch:
            return
        rows_to_write = list(batch)
        try:
            with connection(settings) as conn, conn.cursor() as cur:
                execute_values(
                    cur,
                    insert_sql,
                    rows_to_write,
                    page_size=len(rows_to_write),
                )
        except Exception as exc:
            # Jangan memanggil flush() sekali lagi dari exception handler. Jika
            # relation/index fisik rusak, retry identik hanya menghasilkan
            # traceback kedua dan membingungkan status commit.
            flush_failed = True
            message = silver_write_failure_message(
                exc,
                schema=settings.silver_schema,
                table=settings.image_table,
            )
            if message:
                report_rows.append(
                    {
                        "status": "SILVER_STORAGE_CORRUPTED",
                        "source_group": source_group,
                        "file_uid": "",
                        "file_name": "",
                        "file_path": "",
                        "file_size_bytes": "",
                        "detail": message,
                    }
                )
                raise RuntimeError(message) from exc
            raise
        flush_failed = False
        for row in rows_to_write:
            file_uid = str(row[0])
            if file_uid in existing_at_start:
                updated += 1
            else:
                inserted += 1
            existing[file_uid] = ExistingRow(
                file_uid=file_uid,
                file_size_bytes=int(row[11]),
                file_mtime_epoch=int(row[12]),
                file_sha256=str(row[13]),
                has_bytea=True,
                bytea_len=int(row[15]),
            )
        batch.clear()
        print(
            f"[SILVER LOAD] committed images={stats.eligible_files:,} "
            f"decoded={decoded:,} inserted={inserted:,} updated={updated:,} "
            f"skipped={skipped:,} invalid={invalid:,}",
            flush=True,
        )

    try:
        for item in files:
            file_uid = identity_map.get(
                item.logical_name
            ) or deterministic_file_uid(source_group, item.path.name)
            old = existing.get(file_uid)
            if (
                old
                and old.has_bytea
                and old.file_sha256
                and old.file_size_bytes == item.size
                and old.file_mtime_epoch == item.mtime_epoch
                and old.bytea_len == item.size
            ):
                seen_by_logical.setdefault(
                    item.logical_name,
                    (old.file_sha256, "SILVER_EXISTING"),
                )
                skipped += 1
                continue

            if item.size <= 0:
                invalid += 1
                report_rows.append(
                    {
                        "status": "ZERO_BYTE",
                        "source_group": source_group,
                        "file_uid": file_uid,
                        "file_name": item.path.name,
                        "file_path": str(item.path),
                        "file_size_bytes": item.size,
                        "detail": "file kosong",
                    }
                )
                continue

            try:
                payload = item.path.read_bytes()
                digest = hashlib.sha256(payload).hexdigest()
                _decode_image(payload)
                decoded += 1
            except Exception as exc:
                invalid += 1
                report_rows.append(
                    {
                        "status": "DECODE_OR_READ_FAILED",
                        "source_group": source_group,
                        "file_uid": file_uid,
                        "file_name": item.path.name,
                        "file_path": str(item.path),
                        "file_size_bytes": item.size,
                        "detail": str(exc),
                    }
                )
                continue

            if (
                old
                and old.has_bytea
                and old.file_sha256
                and old.file_sha256 != digest
            ):
                conflicts += 1
                report_rows.append(
                    {
                        "status": "CONFLICT_EXISTING_NAME_DIFFERENT_HASH",
                        "source_group": source_group,
                        "file_uid": file_uid,
                        "file_name": item.path.name,
                        "file_path": str(item.path),
                        "file_size_bytes": item.size,
                        "file_sha256": digest,
                        "detail": f"existing_sha256={old.file_sha256}",
                    }
                )
                raise RuntimeError(
                    "Nama file sudah ada di Silver dengan payload berbeda. "
                    f"Data lama tidak ditimpa; periksa report={report}"
                )

            if (
                old
                and old.has_bytea
                and old.file_sha256 == digest
                and old.bytea_len == len(payload)
            ):
                seen_by_logical.setdefault(
                    item.logical_name,
                    (digest, "SILVER_EXISTING"),
                )
                skipped += 1
                continue

            first = seen_by_logical.get(item.logical_name)
            if first:
                first_hash, first_path = first
                if first_hash == digest:
                    skipped += 1
                    report_rows.append(
                        {
                            "status": "DUPLICATE_LOGICAL_SAME_HASH_SKIPPED",
                            "source_group": source_group,
                            "file_uid": file_uid,
                            "file_name": item.path.name,
                            "file_path": str(item.path),
                            "file_size_bytes": item.size,
                            "file_sha256": digest,
                            "detail": f"survivor={first_path}",
                        }
                    )
                    continue
                conflicts += 1
                report_rows.append(
                    {
                        "status": "CONFLICT_SAME_NAME_DIFFERENT_HASH",
                        "source_group": source_group,
                        "file_uid": file_uid,
                        "file_name": item.path.name,
                        "file_path": str(item.path),
                        "file_size_bytes": item.size,
                        "file_sha256": digest,
                        "detail": f"survivor={first_path}",
                    }
                )
                raise RuntimeError(
                    "Nama file sama mempunyai isi berbeda. Batch sebelumnya "
                    f"tetap aman; periksa report={report}"
                )
            seen_by_logical[item.logical_name] = (digest, str(item.path))

            mime_type = mimetypes.guess_type(item.path.name)[0] or "image/jpeg"
            batch.append(
                (
                    file_uid,
                    source_group,
                    str(item.root),
                    item.path.parent.name,
                    str(item.path),
                    item.relative_path,
                    item.path.name,
                    item.logical_name,
                    item.path.suffix.lower(),
                    mime_type,
                    item.logical_name.startswith("TRANSPORT_"),
                    len(payload),
                    item.mtime_epoch,
                    digest,
                    payload,
                    len(payload),
                    "LOADED_BYTEA",
                    True,
                    False,
                    datetime.now(timezone.utc),
                    datetime.now(timezone.utc),
                )
            )
            if len(batch) >= batch_size:
                flush()
    except BaseException:
        # Commit valid files already decoded in the current partial batch so
        # Ctrl+C truly resumes instead of repeating up to batch_size files.
        # Bila exception berasal dari flush/database, batch tersebut sudah
        # rollback dan tidak boleh langsung dicoba lagi di koneksi baru.
        if not flush_failed:
            flush()
        _write_report(report, report_rows)
        print(
            f"[SILVER LOAD] interrupted images={stats.eligible_files:,} "
            f"committed={inserted + updated:,} skipped={skipped:,} "
            f"invalid={invalid:,} report={report}",
            flush=True,
        )
        raise
    flush()
    if target_names is not None:
        for name in sorted(target_names - found_target_names):
            report_rows.append(
                {
                    "status": "TARGET_FILE_NOT_FOUND",
                    "source_group": source_group,
                    "file_name": name,
                    "detail": "nama dari input-report tidak ditemukan pada NAS root",
                }
            )
    _write_report(report, report_rows)

    print(
        f"[MONTH GUARD] accepted={stats.month_accepted:,} "
        f"outside={stats.month_outside:,} unparsed={stats.month_unparsed:,}",
        flush=True,
    )
    if expected_month and stats.month_accepted == 0:
        raise RuntimeError(
            f"Tidak ada file filename-month={expected_month} pada root yang diberikan."
        )

    with connection(settings) as conn:
        rows = fetch_all_dict(
            conn,
            f"""
            SELECT count(*)::bigint AS rows,
                   count(*) FILTER (WHERE image_bytes IS NOT NULL)::bigint AS bytea_rows,
                   coalesce(sum(bytea_len) FILTER (
                       WHERE image_bytes IS NOT NULL
                   ), 0)::bigint AS declared_bytes
            FROM {table}
            WHERE upper(source_group::text)=upper(%s)
            """,
            (source_group,),
        )
    result = {
        "source_group": source_group,
        "discovered": stats.month_accepted,
        "scanned_images": (
            stats.eligible_files + stats.month_outside + stats.month_unparsed
        ),
        "month_outside": stats.month_outside,
        "month_unparsed": stats.month_unparsed,
        "expected_month": expected_month,
        "target_names": len(target_names) if target_names is not None else None,
        "target_found": len(found_target_names),
        "target_missing": (
            len(target_names - found_target_names)
            if target_names is not None
            else 0
        ),
        "scanned_entries": stats.entries,
        "scan_errors": stats.errors,
        "discovered_bytes": stats.eligible_bytes,
        "decoded": decoded,
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "invalid": invalid,
        "conflicts": conflicts,
        "silver_rows": int(rows[0]["rows"]),
        "silver_bytea_rows": int(rows[0]["bytea_rows"]),
        "silver_declared_bytes": int(rows[0]["declared_bytes"]),
        "applied": True,
        "report": str(report),
    }
    print(f"[SILVER LOAD] DONE {result}", flush=True)
    return result
