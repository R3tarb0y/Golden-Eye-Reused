from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

# Run from Golden Eye project root. The script intentionally imports the
# project's own DB/config helpers and is READ-ONLY.
try:
    from src.golden_eye.config import Settings
    from src.golden_eye.db import connection
except Exception as exc:
    raise SystemExit(
        "Run this file from C:\\golden_eye_db_first so 'src.golden_eye' can be imported. "
        f"Import error: {exc}"
    )


def qident(v: str) -> str:
    return '"' + str(v).replace('"', '""') + '"'


def qname(schema: str, table: str) -> str:
    return f"{qident(schema)}.{qident(table)}"


def table_exists(cur, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (name,))
    return cur.fetchone()[0] is not None


def column_exists(cur, schema: str, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT EXISTS (
          SELECT 1 FROM information_schema.columns
          WHERE table_schema=%s AND table_name=%s AND column_name=%s
        )
        """,
        (schema, table, column),
    )
    return bool(cur.fetchone()[0])


def norm_source(value: Any) -> str:
    s = str(value or "").upper().strip()
    return {"ELHP_DAN": "DAN", "ELHP_DWT": "DWT", "ELHP_DIN": "DIN"}.get(s, s)


def norm_name(value: Any) -> str:
    return os.path.basename(str(value or "").replace("\\", "/")).upper().strip()


def path_under_roots(path: str, roots: list[str]) -> bool:
    if not roots:
        return True
    p = os.path.normcase(os.path.abspath(path))
    for root in roots:
        try:
            r = os.path.normcase(os.path.abspath(root))
            if os.path.commonpath([p, r]) == r:
                return True
        except Exception:
            # UNC/mapped-drive combinations can make commonpath raise.
            if p.startswith(os.path.normcase(str(root))):
                return True
    return False


def verify_path(path: str | None, expected_size: int | None, roots: list[str]) -> tuple[str, int | None, str]:
    if not path:
        return "NO_NAS_PATH", None, ""
    p = str(path).strip()
    if not p:
        return "NO_NAS_PATH", None, ""
    if not path_under_roots(p, roots):
        return "PATH_OUTSIDE_NAS_ROOT", None, p
    try:
        st = os.stat(p)
    except FileNotFoundError:
        return "NAS_FILE_NOT_FOUND", None, p
    except OSError as exc:
        return "NAS_IO_ERROR", None, f"{p} | {exc}"
    actual = int(st.st_size)
    if actual <= 0:
        return "NAS_ZERO_BYTE", actual, p
    if expected_size not in (None, 0) and int(expected_size) != actual:
        return "NAS_SIZE_MISMATCH", actual, p
    return "NAS_VERIFIED", actual, p


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "READ-ONLY audit: classify Golden Eye image BYTEA as NAS-verified, local-only, "
            "or missing. Does not UPDATE/DELETE anything."
        )
    )
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--limit", type=int, default=0, help="0 = all rows")
    ap.add_argument("--source-group", action="append", default=[], help="Filter source group, e.g. PWP, DIN, DAN, DWT. Repeatable.")
    ap.add_argument("--output-dir", default="storage_audit_nas")
    ap.add_argument(
        "--nas-root",
        action="append",
        default=[],
        help="Allowed NAS root. Repeat for multiple roots. If omitted, any stored path is checked.",
    )
    args = ap.parse_args()

    workers = max(1, min(64, int(args.workers)))
    roots = [str(Path(x)) for x in args.nas_root if str(x).strip()]
    source_groups = [norm_source(x) for x in args.source_group if str(x).strip()]
    settings = Settings()
    outdir = Path(args.output_dir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    detail_csv = outdir / f"nas_vs_local_detail_{stamp}.csv"
    summary_csv = outdir / f"nas_vs_local_summary_{stamp}.csv"
    txt_path = outdir / f"nas_vs_local_summary_{stamp}.txt"

    print("=" * 90)
    print("GOLDEN EYE - NAS VS LOCAL IMAGE STORAGE AUDIT (READ ONLY)")
    print("=" * 90)
    print(f"database={settings.pg_database} workers={workers} limit={args.limit or 'ALL'}")
    print(f"source_group={','.join(source_groups) if source_groups else 'ALL'}")
    if roots:
        for root in roots:
            print(f"nas_root={root}")
    else:
        print("nas_root=<not restricted; stored DB paths will be checked as-is>")

    rows: list[dict[str, Any]] = []
    panen_by_key: dict[tuple[str, str], str] = {}
    transport_by_uid: dict[tuple[str, str], str] = {}
    transport_by_name: dict[tuple[str, str], str] = {}

    with connection(settings) as conn:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout='15min'")

            # Exact panen registry (currently PWP/DIN in Golden Eye).
            if table_exists(cur, "processing.panen_nas_path_index"):
                cur.execute(
                    """
                    SELECT source_group, file_name_upper, nas_path
                    FROM processing.panen_nas_path_index
                    WHERE is_active=true
                      AND nullif(trim(nas_path),'') IS NOT NULL
                    """
                )
                for sg, fn, path in cur:
                    panen_by_key[(norm_source(sg), str(fn or "").upper().strip())] = str(path)

            # Exact Harmony KT registry.
            if table_exists(cur, "processing.transport_kt_before_context"):
                cur.execute(
                    """
                    SELECT source_group, file_uid::text, evidence_file_name, nas_path
                    FROM processing.transport_kt_before_context
                    WHERE is_active=true
                      AND load_status='READY'
                      AND nullif(trim(coalesce(nas_path,'')),'') IS NOT NULL
                    """
                )
                for sg, uid, fn, path in cur:
                    s = norm_source(sg)
                    if uid:
                        transport_by_uid[(s, str(uid))] = str(path)
                    if fn:
                        transport_by_name[(s, norm_name(fn))] = str(path)

            silver_schema = settings.silver_schema
            silver_table = settings.image_table
            if not table_exists(cur, f"{silver_schema}.{silver_table}"):
                raise RuntimeError(f"{silver_schema}.{silver_table} tidak ditemukan")

            cols = {
                c
                for c in (
                    "file_uid", "source_group", "file_name", "file_name_upper",
                    "file_path", "source_root", "relative_path", "is_transport",
                    "file_size_bytes", "file_sha256", "image_bytes", "bytea_len",
                    "file_exists", "zero_byte",
                )
                if column_exists(cur, silver_schema, silver_table, c)
            }
            required = {"file_uid", "source_group", "file_name", "image_bytes"}
            missing = required - cols
            if missing:
                raise RuntimeError(f"Silver columns missing: {sorted(missing)}")

            def expr(col: str, fallback: str = "NULL") -> str:
                return qident(col) if col in cols else fallback

            sql = f"""
                SELECT
                    {expr('file_uid')}::text AS file_uid,
                    {expr('source_group')}::text AS source_group,
                    {expr('file_name')}::text AS file_name,
                    {expr('file_name_upper')}::text AS file_name_upper,
                    {expr('file_path')}::text AS file_path,
                    {expr('source_root')}::text AS source_root,
                    {expr('relative_path')}::text AS relative_path,
                    coalesce({expr('is_transport', 'false')}, false) AS is_transport,
                    {expr('file_size_bytes')}::bigint AS file_size_bytes,
                    {expr('file_sha256')}::text AS file_sha256,
                    ({expr('image_bytes')} IS NOT NULL) AS has_bytea,
                    {expr('bytea_len')}::bigint AS bytea_len,
                    coalesce({expr('file_exists', 'true')}, true) AS db_file_exists,
                    coalesce({expr('zero_byte', 'false')}, false) AS db_zero_byte
                FROM {qname(silver_schema, silver_table)}
            """
            params = []
            if source_groups:
                aliases = []
                for sg in source_groups:
                    if sg == "DAN":
                        aliases += ["DAN", "ELHP_DAN"]
                    elif sg == "DWT":
                        aliases += ["DWT", "ELHP_DWT"]
                    elif sg == "DIN":
                        aliases += ["DIN", "ELHP_DIN"]
                    else:
                        aliases.append(sg)
                sql += " WHERE upper(trim(source_group::text)) = ANY(%s)"
                params.append([x.upper() for x in aliases])
            sql += " ORDER BY source_group, file_uid"
            if args.limit > 0:
                sql += " LIMIT %s"
                params.append(args.limit)
            cur.execute(sql, tuple(params))
            colnames = [d.name if hasattr(d, "name") else d[0] for d in cur.description]
            for raw in cur.fetchall():
                row = dict(zip(colnames, raw))
                row["source_group"] = norm_source(row.get("source_group"))
                rows.append(row)

    print(f"silver_rows_loaded={len(rows):,}")
    print(f"panen_registry_paths={len(panen_by_key):,} transport_registry_uids={len(transport_by_uid):,}")

    def candidate_for(row: dict[str, Any]) -> tuple[str, str]:
        sg = row["source_group"]
        uid = str(row.get("file_uid") or "")
        name = norm_name(row.get("file_name_upper") or row.get("file_name"))
        if row.get("is_transport"):
            p = transport_by_uid.get((sg, uid)) or transport_by_name.get((sg, name))
            if p:
                return "TRANSPORT_REGISTRY", p
        else:
            p = panen_by_key.get((sg, name))
            if p:
                return "PANEN_REGISTRY", p
        fp = str(row.get("file_path") or "").strip()
        if fp:
            return "SILVER_FILE_PATH", fp
        root = str(row.get("source_root") or "").strip()
        rel = str(row.get("relative_path") or "").strip()
        if root and rel:
            return "SILVER_ROOT_RELATIVE", str(Path(root) / rel)
        return "NONE", ""

    tasks = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for idx, row in enumerate(rows):
            path_source, candidate = candidate_for(row)
            row["path_source"] = path_source
            row["nas_candidate"] = candidate
            expected_size = row.get("file_size_bytes") or row.get("bytea_len")
            fut = pool.submit(verify_path, candidate, expected_size, roots)
            tasks.append((idx, fut))
        done = 0
        for idx, fut in tasks:
            status, actual_size, detail = fut.result()
            row = rows[idx]
            row["nas_status"] = status
            row["nas_actual_size"] = actual_size
            row["nas_detail"] = detail
            has_bytea = bool(row.get("has_bytea"))
            if status == "NAS_VERIFIED":
                row["storage_class"] = (
                    "NAS_OK_BYTEA_REDUNDANT" if has_bytea else "NAS_OK_METADATA_ONLY"
                )
            elif has_bytea:
                row["storage_class"] = "LOCAL_BYTEA_KEEP_UNTIL_NAS_FIXED"
            else:
                row["storage_class"] = "MISSING_IMAGE_NO_LOCAL_FALLBACK"
            done += 1
            if done % 25000 == 0 or done == len(rows):
                print(f"path_checked={done:,}/{len(rows):,}", flush=True)

    fields = [
        "source_group", "file_uid", "file_name", "is_transport",
        "path_source", "nas_candidate", "nas_status", "storage_class",
        "file_size_bytes", "nas_actual_size", "has_bytea", "bytea_len",
        "file_sha256", "db_file_exists", "db_zero_byte", "nas_detail",
    ]
    with detail_csv.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k) for k in fields})

    summary: dict[tuple[str, str, bool], dict[str, int]] = defaultdict(lambda: {"rows": 0, "bytea_bytes": 0})
    for row in rows:
        key = (row["source_group"], row["storage_class"], bool(row.get("is_transport")))
        summary[key]["rows"] += 1
        if row.get("has_bytea"):
            summary[key]["bytea_bytes"] += int(row.get("bytea_len") or row.get("file_size_bytes") or 0)

    with summary_csv.open("w", newline="", encoding="utf-8-sig") as f:
        fields2 = ["source_group", "is_transport", "storage_class", "rows", "bytea_bytes", "bytea_gb"]
        w = csv.DictWriter(f, fieldnames=fields2)
        w.writeheader()
        for (sg, cls, is_tr), val in sorted(summary.items()):
            w.writerow({
                "source_group": sg,
                "is_transport": is_tr,
                "storage_class": cls,
                "rows": val["rows"],
                "bytea_bytes": val["bytea_bytes"],
                "bytea_gb": f"{val['bytea_bytes'] / (1024**3):.3f}",
            })

    total = len(rows)
    counters = Counter(r["storage_class"] for r in rows)
    bytes_redundant = sum(
        int(r.get("bytea_len") or r.get("file_size_bytes") or 0)
        for r in rows
        if r["storage_class"] == "NAS_OK_BYTEA_REDUNDANT"
    )
    bytes_keep = sum(
        int(r.get("bytea_len") or r.get("file_size_bytes") or 0)
        for r in rows
        if r["storage_class"] == "LOCAL_BYTEA_KEEP_UNTIL_NAS_FIXED"
    )

    lines = [
        "=" * 90,
        "GOLDEN EYE - NAS VS LOCAL STORAGE SUMMARY",
        "=" * 90,
        f"rows={total:,}",
        f"NAS_OK_BYTEA_REDUNDANT={counters['NAS_OK_BYTEA_REDUNDANT']:,}",
        f"NAS_OK_METADATA_ONLY={counters['NAS_OK_METADATA_ONLY']:,}",
        f"LOCAL_BYTEA_KEEP_UNTIL_NAS_FIXED={counters['LOCAL_BYTEA_KEEP_UNTIL_NAS_FIXED']:,}",
        f"MISSING_IMAGE_NO_LOCAL_FALLBACK={counters['MISSING_IMAGE_NO_LOCAL_FALLBACK']:,}",
        f"redundant_local_image_payload_gb={bytes_redundant/(1024**3):,.3f}",
        f"local_payload_that_must_be_kept_gb={bytes_keep/(1024**3):,.3f}",
        "",
        "Interpretation:",
        "- NAS_OK_BYTEA_REDUNDANT: NAS file exists and size matches; Silver BYTEA is cleanup candidate.",
        "- NAS_OK_METADATA_ONLY: desired final Silver state.",
        "- LOCAL_BYTEA_KEEP_UNTIL_NAS_FIXED: do NOT delete BYTEA yet.",
        "- MISSING_IMAGE_NO_LOCAL_FALLBACK: investigate immediately; neither verified NAS nor local BYTEA.",
        "",
        "This audit never updates/deletes database rows.",
        f"detail_csv={detail_csv}",
        f"summary_csv={summary_csv}",
        "=" * 90,
    ]
    text = "\n".join(lines)
    txt_path.write_text(text, encoding="utf-8")
    print(text)
    print(f"summary_txt={txt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
