from __future__ import annotations

import csv
import hashlib
import io
import mimetypes
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from PIL import Image
from psycopg2.extras import execute_values

from .config import Settings
from .db import connection, fetch_all_dict
from .utils import normalize_oph


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
_SAFE_SQL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DIRECT_NAS_LOOKUP_MAX = 5_000
_NAS_PROGRESS_EVERY = 25_000


@dataclass(frozen=True)
class TransportEvidence:
    oph: str
    oph_key: str
    period_date: date | None
    evidence_path: str
    file_name: str
    evidence_key: str
    file_uid: str
    source_row_count: int = 1


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _sql_ident(value: str) -> str:
    value = str(value or "").strip()
    if not _SAFE_SQL_NAME.fullmatch(value):
        raise ValueError(f"Identifier Microsoft SQL tidak aman: {value!r}")
    return "[" + value + "]"


def _split_table(value: str) -> tuple[str, str]:
    parts = [part.strip() for part in str(value or "").split(".") if part.strip()]
    if len(parts) == 1:
        parts.insert(0, "dbo")
    if len(parts) != 2:
        raise ValueError("MSSQL_TRANSPORT_TABLE harus berbentuk schema.table")
    _sql_ident(parts[0])
    _sql_ident(parts[1])
    return parts[0], parts[1]


def _resolve_column(
    columns: dict[str, str],
    configured: str,
    aliases: tuple[str, ...],
    *,
    required: bool,
) -> str | None:
    for candidate in (configured, *aliases):
        actual = columns.get(str(candidate or "").upper().strip())
        if actual:
            return actual
    if required:
        raise RuntimeError(
            "Kolom Microsoft SQL tidak ditemukan. Kandidat="
            + ",".join((configured, *aliases))
        )
    return None


def _clean_evidence_value(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().strip('"').strip("'")
    return "" if text.lower() in {"", "none", "null", "nan"} else text


def extract_evidence_file_names(value: Any) -> list[str]:
    """Ambil basename evidence dari path Windows, URL, atau list sederhana."""
    text = _clean_evidence_value(value)
    if not text:
        return []
    text = text.strip("[]")
    values = [part.strip().strip('"').strip("'") for part in re.split(r"[;|,\r\n]+", text)]
    out: list[str] = []
    for item in values:
        if not item:
            continue
        parsed = urlparse(item)
        candidate = unquote(parsed.path if parsed.scheme else item).replace("/", "\\")
        name = PureWindowsPath(candidate).name.strip()
        if not name:
            continue
        if Path(name).suffix.lower() not in IMAGE_EXTENSIONS:
            # Sebagian source menyimpan key tanpa ekstensi; downloader PWP
            # menghasilkan JPG. Tambahkan .jpg secara deterministik.
            if "." not in name:
                name += ".jpg"
            else:
                continue
        upper = name.upper()
        if upper not in {item.upper() for item in out}:
            out.append(name)
    return out


def transport_file_uid(source_group: str, file_name: str) -> str:
    digest = hashlib.sha256(
        f"{source_group.upper()}\0{file_name.upper()}".encode("utf-8")
    ).hexdigest()
    return "TRANSPORT-KT-" + digest


def _all_harmony_panens_oph(
    settings: Settings, source_group: str, period_year: int
) -> dict[str, str]:
    """Seluruh OPH foto panen Harmony di Context, bukan hanya Gold reused."""
    with connection(settings) as conn:
        rows = fetch_all_dict(
            conn,
            """
            SELECT DISTINCT ON (c.oph_key)
                   c.oph::text AS oph,
                   c.oph_key::text AS oph_key
            FROM processing.image_context c
            WHERE upper(c.source_group)=%s
              AND c.oph IS NOT NULL
              AND nullif(trim(c.oph_key), '') IS NOT NULL
              AND c.period_date IS NOT NULL
              AND extract(year FROM c.period_date)::integer=%s
            ORDER BY c.oph_key, c.period_date DESC, c.updated_at DESC
            """,
            (source_group.upper(), int(period_year)),
        )
    return {str(row["oph_key"]): str(row["oph"]) for row in rows}


def _mssql_connection(settings: Settings):
    try:
        import pyodbc  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "pyodbc belum terpasang. Jalankan: py -m pip install pyodbc"
        ) from exc
    try:
        available = list(pyodbc.drivers())
    except Exception:
        available = []
    driver = settings.mssql_driver
    if available and driver not in available:
        preferred = (
            "ODBC Driver 18 for SQL Server",
            "ODBC Driver 17 for SQL Server",
            "SQL Server Native Client 11.0",
            "SQL Server",
        )
        driver = next((name for name in preferred if name in available), driver)
        print(
            f"[MSSQL] driver {settings.mssql_driver!r} tidak tersedia; "
            f"pakai {driver!r}",
            flush=True,
        )
    try:
        return pyodbc.connect(
            settings.mssql_connection_string(driver), timeout=30
        )
    except Exception as exc:
        suffix = f" Driver tersedia={available}" if available else ""
        raise RuntimeError(
            f"Koneksi Microsoft SQL gagal memakai driver {driver!r}."
            + suffix
        ) from exc


def _read_mssql_evidence(
    settings: Settings,
    target_oph: dict[str, str],
    source_group: str,
    period_year: int,
) -> tuple[list[TransportEvidence], dict[str, Any]]:
    schema, table = _split_table(settings.mssql_transport_table)
    conn = _mssql_connection(settings)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA=? AND TABLE_NAME=?
            """,
            (schema, table),
        )
        columns = {str(row[0]).upper(): str(row[0]) for row in cur.fetchall()}
        oph_col = _resolve_column(
            columns,
            settings.mssql_transport_oph_column,
            ("OPH", "TIKET_PANEN", "NO_BCC"),
            required=True,
        )
        evidence_col = _resolve_column(
            columns,
            settings.mssql_transport_evidence_column,
            ("FFB_EVIDENCE_KT_BEFORE",),
            required=True,
        )
        company_col = _resolve_column(
            columns,
            settings.mssql_transport_company_column,
            ("COMPANY", "BUKRS"),
            required=True,
        )
        year_col = _resolve_column(
            columns,
            settings.mssql_transport_year_column,
            ("YEAR", "TAHUN"),
            required=False,
        )
        date_col = _resolve_column(
            columns,
            settings.mssql_transport_date_column,
            ("REPORT_DATE", "TANGGAL", "DATE"),
            required=not bool(year_col),
        )
        date_expr = (
            f"TRY_CONVERT(date, {_sql_ident(date_col)})"
            if date_col
            else "CAST(NULL AS date)"
        )
        year_filter = (
            f"TRY_CONVERT(int, {_sql_ident(year_col)})=?"
            if year_col
            else f"YEAR({date_expr})=?"
        )
        table_sql = f"{_sql_ident(schema)}.{_sql_ident(table)}"
        query = f"""
            SELECT
                CAST({_sql_ident(oph_col)} AS nvarchar(255)) AS oph,
                CAST({_sql_ident(evidence_col)} AS nvarchar(2048)) AS evidence_path,
                {date_expr} AS period_date
            FROM {table_sql}
            WHERE upper(ltrim(rtrim(CAST({_sql_ident(company_col)} AS nvarchar(50)))))=?
              AND {year_filter}
              AND {_sql_ident(evidence_col)} IS NOT NULL
              AND ltrim(rtrim(CAST({_sql_ident(evidence_col)} AS nvarchar(2048))))<>''
        """
        cur.execute(query, (source_group.upper(), int(period_year)))
        cur.arraysize = 5000

        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        scanned = 0
        matched_rows = 0
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                break
            scanned += len(batch)
            for row in batch:
                oph = str(row[0] or "").strip()
                oph_key = normalize_oph(oph)
                if not oph_key or oph_key not in target_oph:
                    continue
                matched_rows += 1
                evidence_path = _clean_evidence_value(row[1])
                row_date = row[2]
                if isinstance(row_date, datetime):
                    row_date = row_date.date()
                if not isinstance(row_date, date):
                    row_date = None
                for file_name in extract_evidence_file_names(evidence_path):
                    evidence_key = file_name.upper()
                    key = (oph_key, evidence_key)
                    current = grouped.get(key)
                    if current is None:
                        grouped[key] = {
                            "oph": oph or target_oph[oph_key],
                            "oph_key": oph_key,
                            "period_date": row_date,
                            "evidence_path": evidence_path,
                            "file_name": file_name,
                            "count": 1,
                        }
                    else:
                        current["count"] += 1
                        if row_date and (
                            current["period_date"] is None
                            or row_date > current["period_date"]
                        ):
                            current["period_date"] = row_date
                            current["evidence_path"] = evidence_path

        evidence = [
            TransportEvidence(
                oph=value["oph"],
                oph_key=value["oph_key"],
                period_date=value["period_date"],
                evidence_path=value["evidence_path"],
                file_name=value["file_name"],
                evidence_key=value["file_name"].upper(),
                file_uid=transport_file_uid(source_group, value["file_name"]),
                source_row_count=int(value["count"]),
            )
            for value in grouped.values()
        ]
        evidence.sort(key=lambda item: (item.oph_key, item.evidence_key))
        return evidence, {
            "mssql_rows_scanned": scanned,
            "mssql_rows_matching_target_oph": matched_rows,
            "resolved_columns": {
                "oph": oph_col,
                "evidence": evidence_col,
                "company": company_col,
                "year": year_col,
                "date": date_col,
            },
        }
    finally:
        conn.close()


def _existing_hot_files(
    settings: Settings,
    source_group: str,
    evidence_keys: list[str],
) -> dict[str, str]:
    if not evidence_keys:
        return {}
    wanted = {str(value).upper().strip() for value in evidence_keys if value}
    with connection(settings) as conn:
        rows = fetch_all_dict(
            conn,
            """
            SELECT DISTINCT ON (upper(trim(file_name)))
                   upper(trim(file_name)) AS evidence_key,
                   file_uid
            FROM processing.gambar_reused
            WHERE source_group=%s AND is_active=true
            ORDER BY upper(trim(file_name)),
                     (file_uid LIKE 'TRANSPORT-KT-%%') DESC,
                     last_verified_at DESC
            """,
            (source_group.upper(),),
        )
    return {
        str(row["evidence_key"]): str(row["file_uid"])
        for row in rows
        if str(row["evidence_key"]) in wanted
    }


def _index_nas(
    roots: list[Path], evidence: list[TransportEvidence]
) -> dict[str, list[Path]]:
    wanted = {item.evidence_key for item in evidence}
    found: dict[str, list[Path]] = defaultdict(list)
    remaining = set(wanted)
    raw_paths: dict[str, set[str]] = defaultdict(set)
    for item in evidence:
        raw_paths[item.evidence_key].add(item.evidence_path)

    for root in roots:
        if not root.exists():
            raise FileNotFoundError(f"NAS root tidak ditemukan: {root}")
        direct_lookup = len(remaining) <= _DIRECT_NAS_LOOKUP_MAX
        print(
            f"[TRANSPORT NAS] strategy={'DIRECT_THEN_WALK' if direct_lookup else 'ONE_PASS_WALK'} "
            f"targets={len(remaining):,}; direct_limit={_DIRECT_NAS_LOOKUP_MAX:,}",
            flush=True,
        )
        if direct_lookup:
            for key in tuple(remaining):
                candidates = [root / key]
                for raw in raw_paths.get(key, set()):
                    normalized = raw.replace("\\", "/").lstrip("/")
                    if ":/" not in normalized and not urlparse(raw).scheme:
                        candidates.append(root / Path(normalized))
                for path in candidates:
                    if path.is_file():
                        found[key].append(path)
                if found.get(key):
                    remaining.discard(key)
        print(
            f"[TRANSPORT NAS] root={root} direct_found={len(wanted)-len(remaining):,} "
            f"remaining={len(remaining):,}",
            flush=True,
        )
        if not remaining:
            break
        started = time.monotonic()
        scanned_files = 0
        next_progress = _NAS_PROGRESS_EVERY
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                key = filename.upper().strip()
                if key in remaining:
                    found[key].append(Path(dirpath) / filename)
                    remaining.discard(key)
            scanned_files += len(filenames)
            if scanned_files >= next_progress:
                elapsed = max(time.monotonic() - started, 0.001)
                print(
                    f"[TRANSPORT NAS] scanned_files={scanned_files:,} "
                    f"found={len(wanted)-len(remaining):,}/{len(wanted):,} "
                    f"remaining={len(remaining):,} rate={scanned_files/elapsed:,.0f} file/s "
                    f"elapsed={elapsed/60:.1f}m",
                    flush=True,
                )
                next_progress = (
                    scanned_files // _NAS_PROGRESS_EVERY + 1
                ) * _NAS_PROGRESS_EVERY
            if not remaining:
                print("[TRANSPORT NAS] seluruh target ditemukan; scan dihentikan", flush=True)
                break
        elapsed = max(time.monotonic() - started, 0.001)
        print(
            f"[TRANSPORT NAS] scan_complete root={root} "
            f"scanned_files={scanned_files:,} found={len(wanted)-len(remaining):,} "
            f"remaining={len(remaining):,} elapsed={elapsed/60:.1f}m",
            flush=True,
        )
    return found


def _validated_payload(paths: list[Path]) -> tuple[Path, bytes, str, str] | Exception:
    payloads: dict[str, tuple[Path, bytes, str]] = {}
    errors: list[str] = []
    for path in paths:
        try:
            payload = path.read_bytes()
            if not payload:
                raise ValueError("file kosong")
            with Image.open(io.BytesIO(payload)) as image:
                image.verify()
                mime = Image.MIME.get(image.format, mimetypes.guess_type(path.name)[0])
            digest = hashlib.sha256(payload).hexdigest()
            payloads.setdefault(digest, (path, payload, mime or "image/jpeg"))
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    if len(payloads) == 1:
        digest, (path, payload, mime) = next(iter(payloads.items()))
        return path, payload, digest, mime
    if len(payloads) > 1:
        return ValueError(f"nama sama mempunyai {len(payloads)} payload berbeda")
    return ValueError("; ".join(errors)[:1000] or "file tidak ditemukan")


def _persist_image_batch(
    settings: Settings,
    image_rows: list[tuple[Any, ...]],
    *,
    force_replace: bool = False,
) -> int:
    """Commit satu batch BYTEA agar RAM tetap konstan dan job dapat di-resume."""
    if not image_rows:
        return 0
    conflict_guard = "" if force_replace else """
        WHERE current_row.file_sha256 IS DISTINCT FROM excluded.file_sha256
           OR current_row.bytea_len IS DISTINCT FROM excluded.bytea_len
           OR current_row.is_active IS DISTINCT FROM true
    """
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('processing.gambar_reused')")
        if not cur.fetchone()[0]:
            raise RuntimeError(
                "Tabel processing.gambar_reused belum ada. "
                "Jalankan py run_pipeline.py init-db"
            )
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
            image_rows,
            page_size=min(32, len(image_rows)),
        )
    return len(image_rows)


def _persist(
    settings: Settings,
    source_group: str,
    period_year: int,
    evidence: list[TransportEvidence],
    outcomes: dict[str, tuple[str, str, str]],
    image_rows: list[tuple[Any, ...]],
    *,
    force_replace: bool = False,
) -> tuple[int, int]:
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('processing.transport_kt_before_context'), "
            "to_regclass('processing.gambar_reused')"
        )
        present = cur.fetchone()
        if not all(present):
            raise RuntimeError("Tabel transport belum ada. Jalankan py run_pipeline.py init-db")
        # Row lama tidak dihapus. Ia hanya dinonaktifkan setelah pembacaan MSSQL
        # dan NAS selesai, sehingga kegagalan di tengah proses tidak merusak dashboard.
        cur.execute(
            """
            UPDATE processing.transport_kt_before_context
            SET is_active=false, synced_at=now()
            WHERE source_group=%s AND period_year=%s AND is_active=true
            """,
            (source_group, period_year),
        )
        context_written = 0
        context_batch_size = 5_000
        for start in range(0, len(evidence), context_batch_size):
            context_rows = []
            for item in evidence[start : start + context_batch_size]:
                status, nas_path, error = outcomes[item.evidence_key]
                context_rows.append(
                    (
                        source_group,
                        item.oph,
                        item.oph_key,
                        period_year,
                        item.period_date,
                        item.evidence_path,
                        item.file_name,
                        item.evidence_key,
                        item.file_uid,
                        settings.mssql_transport_table,
                        item.source_row_count,
                        status,
                        nas_path or None,
                        error or None,
                        True,
                    )
                )
            execute_values(
                cur,
                """
                INSERT INTO processing.transport_kt_before_context AS current_row
                    (source_group, oph, oph_key, period_year, period_date,
                     evidence_path, evidence_file_name, evidence_key, file_uid,
                     source_table, source_row_count, load_status, nas_path,
                     last_error, is_active)
                VALUES %s
                ON CONFLICT (source_group, oph_key, evidence_key) DO UPDATE SET
                    oph=excluded.oph,
                    period_year=excluded.period_year,
                    period_date=excluded.period_date,
                    evidence_path=excluded.evidence_path,
                    evidence_file_name=excluded.evidence_file_name,
                    file_uid=excluded.file_uid,
                    source_table=excluded.source_table,
                    source_row_count=excluded.source_row_count,
                    load_status=excluded.load_status,
                    nas_path=excluded.nas_path,
                    last_error=excluded.last_error,
                    synced_at=now(),
                    is_active=true
                """,
                context_rows,
                page_size=500,
            )
            context_written += len(context_rows)
            print(
                f"[TRANSPORT CONTEXT] staged={context_written:,}/{len(evidence):,}",
                flush=True,
            )
        if image_rows:
            conflict_guard = "" if force_replace else """
                WHERE current_row.file_sha256 IS DISTINCT FROM excluded.file_sha256
                   OR current_row.bytea_len IS DISTINCT FROM excluded.bytea_len
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
                image_rows,
                page_size=32,
            )
            image_changed = len(image_rows) if force_replace else cur.rowcount
        else:
            image_changed = 0
    return context_written, max(0, int(image_changed))


def sync_transport_kt_before(
    settings: Settings,
    source_group: str,
    period_year: int,
    nas_roots: list[str | Path],
    *,
    apply: bool = False,
    refresh_existing: bool = False,
    report_path: str | None = None,
    batch_size: int = 64,
) -> dict[str, Any]:
    source_group = source_group.upper().strip()
    if source_group not in {"PWP", "DIN"}:
        raise ValueError("FFB_EVIDENCE_KT_BEFORE hanya diizinkan untuk PWP atau DIN")
    if not 2000 <= int(period_year) <= 2100:
        raise ValueError("--year harus antara 2000 dan 2100")
    batch_size = int(batch_size)
    if not 1 <= batch_size <= 1_000:
        raise ValueError("--batch-size harus antara 1 dan 1000")
    roots = [Path(value) for value in nas_roots]
    if not roots:
        raise ValueError("Minimal satu --nas-root wajib diberikan")

    print("=" * 78)
    print(
        f"{source_group} TRANSPORT KT_BEFORE SYNC | "
        f"year={period_year} | apply={apply}"
    )
    print("=" * 78)
    target_oph = _all_harmony_panens_oph(settings, source_group, period_year)
    print(f"all_harmony_panen_distinct_oph={len(target_oph):,}")
    evidence, source_metrics = _read_mssql_evidence(
        settings, target_oph, source_group, period_year
    )
    matched_oph = {item.oph_key for item in evidence}
    print(
        f"mssql_rows_scanned={source_metrics['mssql_rows_scanned']:,} "
        f"matched_rows={source_metrics['mssql_rows_matching_target_oph']:,} "
        f"matched_oph={len(matched_oph):,} distinct_evidence={len(evidence):,}"
    )
    print(f"resolved_columns={source_metrics['resolved_columns']}")

    existing_by_name = (
        _existing_hot_files(
            settings,
            source_group,
            [item.evidence_key for item in evidence],
        )
        if apply
        else {}
    )
    # Bila foto dengan nama yang sama sudah ada dari recovery DT/FT lama,
    # arahkan context KT ke UID tersebut. BYTEA tidak disalin ulang.
    evidence = [
        replace(item, file_uid=existing_by_name.get(item.evidence_key, item.file_uid))
        for item in evidence
    ]
    existing = set(existing_by_name.values())
    pending = [
        item for item in evidence if refresh_existing or item.file_uid not in existing
    ]
    already_ready = len(evidence) - len(pending)
    print(
        f"already_hot={already_ready:,} pending_nas={len(pending):,} "
        f"write_batch_size={batch_size:,}"
    )
    nas_index = _index_nas(roots, pending) if pending else {}

    if apply and target_oph and not evidence:
        raise RuntimeError(
            "Preflight menghasilkan 0 evidence untuk seluruh OPH foto panen Harmony; "
            "context lama tidak dinonaktifkan. Periksa kolom/scope MSSQL."
        )

    outcomes: dict[str, tuple[str, str, str]] = {}
    missing_mssql_oph = sorted(set(target_oph) - matched_oph)
    output = Path(
        report_path
        or f"transport_kt_before_{source_group.lower()}_{period_year}.csv"
    ).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "source_group", "year", "oph", "oph_key", "period_date",
        "ffb_evidence_kt_before", "file_name", "file_uid", "status",
        "nas_path", "error",
    ]
    image_batch: list[tuple[Any, ...]] = []
    seen_image_uids: set[str] = set()
    image_changed = 0
    ready = 0
    failed = 0
    processed = 0
    started = time.monotonic()
    progress_every = max(1_000, batch_size * 50)
    next_progress = progress_every
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for item in evidence:
            if item.file_uid in existing and not refresh_existing:
                status, nas_path, error = "READY", "", ""
            else:
                paths = nas_index.get(item.evidence_key, [])
                result = _validated_payload(paths)
                if isinstance(result, Exception):
                    status = (
                        "FILE_MISSING" if not paths else "INVALID_OR_AMBIGUOUS"
                    )
                    nas_path, error = "", str(result)
                else:
                    path, payload, digest, mime = result
                    status, nas_path, error = "READY", str(path), ""
                    if apply and item.file_uid not in seen_image_uids:
                        image_batch.append(
                            (
                                source_group,
                                item.file_uid,
                                item.file_name,
                                mime,
                                digest,
                                len(payload),
                                payload,
                                str(path),
                                "MSSQL_FFB_EVIDENCE_KT_BEFORE_OPH_EXACT",
                                True,
                            )
                        )
                        seen_image_uids.add(item.file_uid)
            outcomes[item.evidence_key] = (status, nas_path, error)
            ready += int(status == "READY")
            failed += int(status != "READY")
            processed += 1
            writer.writerow(
                {
                    "source_group": source_group,
                    "year": period_year,
                    "oph": item.oph,
                    "oph_key": item.oph_key,
                    "period_date": item.period_date or "",
                    "ffb_evidence_kt_before": item.evidence_path,
                    "file_name": item.file_name,
                    "file_uid": item.file_uid,
                    "status": status,
                    "nas_path": nas_path,
                    "error": error,
                }
            )
            if apply and len(image_batch) >= batch_size:
                image_changed += _persist_image_batch(
                    settings,
                    image_batch,
                    force_replace=refresh_existing,
                )
                image_batch.clear()
            if processed >= next_progress or processed == len(evidence):
                elapsed = max(time.monotonic() - started, 0.001)
                rate = processed / elapsed
                remaining_seconds = (len(evidence) - processed) / max(rate, 0.001)
                print(
                    f"[TRANSPORT LOAD] processed={processed:,}/{len(evidence):,} "
                    f"ready={ready:,} failed={failed:,} committed={image_changed:,} "
                    f"rate={rate:,.1f} evidence/s elapsed={elapsed/60:.1f}m "
                    f"eta={remaining_seconds/60:.1f}m",
                    flush=True,
                )
                handle.flush()
                next_progress = (
                    processed // progress_every + 1
                ) * progress_every
        if apply and image_batch:
            image_changed += _persist_image_batch(
                settings,
                image_batch,
                force_replace=refresh_existing,
            )
            image_batch.clear()
        for oph_key in missing_mssql_oph:
            writer.writerow(
                {
                    "source_group": source_group,
                    "year": period_year,
                    "oph": target_oph[oph_key],
                    "oph_key": oph_key,
                    "period_date": "",
                    "ffb_evidence_kt_before": "",
                    "file_name": "",
                    "file_uid": "",
                    "status": "MSSQL_EVIDENCE_MISSING",
                    "nas_path": "",
                    "error": (
                        "OPH foto panen Harmony tidak mempunyai KT_BEFORE "
                        "pada scope MSSQL"
                    ),
                }
            )

    context_written = 0
    if apply:
        context_written, _ = _persist(
            settings,
            source_group,
            period_year,
            evidence,
            outcomes,
            [],
            force_replace=refresh_existing,
        )
    metrics = {
        "source_group": source_group,
        "period_year": period_year,
        "all_harmony_panen_distinct_oph": len(target_oph),
        "mssql_matched_distinct_oph": len(matched_oph),
        "mssql_missing_all_panen_oph": len(missing_mssql_oph),
        "distinct_evidence": len(evidence),
        "ready_evidence": ready,
        "failed_evidence": failed,
        "already_hot": already_ready,
        "context_written": context_written,
        "image_rows_changed": image_changed,
        "applied": apply,
        "report": str(output),
    }
    print(f"[TRANSPORT KT] DONE {metrics}")
    if missing_mssql_oph:
        print(
            "WARN seluruh OPH foto panen tanpa KT_BEFORE di MSSQL="
            f"{len(missing_mssql_oph):,}; ini bukan error loader NAS."
        )
    if not apply:
        print("DRY-RUN saja; PostgreSQL tidak diubah. Tambahkan --apply setelah preflight.")
    return metrics
