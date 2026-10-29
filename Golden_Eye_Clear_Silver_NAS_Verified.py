from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Iterable

try:
    from src.golden_eye.config import Settings
    from src.golden_eye.db import connection
except Exception as exc:
    raise SystemExit(
        "Run this file from C:\\golden_eye_db_first so src.golden_eye can be imported. "
        f"Import error: {exc}"
    )

SAFE_CLASS = "NAS_OK_BYTEA_REDUNDANT"
DEFAULT_GROUPS = ("PWP", "DIN", "DAN", "DWT")


def latest_detail_csv(root: Path, group: str) -> Path:
    folder = root / group
    files = sorted(folder.glob("nas_vs_local_detail_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise FileNotFoundError(f"No nas_vs_local_detail_*.csv found under {folder}")
    return files[0]


def load_safe_rows(path: Path) -> tuple[list[str], int, int]:
    safe_uids: list[str] = []
    safe_bytes = 0
    non_safe = 0
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"file_uid", "storage_class"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(f"{path}: missing CSV columns {sorted(missing)}")
        for row in reader:
            uid = str(row.get("file_uid") or "").strip()
            cls = str(row.get("storage_class") or "").strip()
            if cls == SAFE_CLASS and uid:
                safe_uids.append(uid)
                raw = row.get("bytea_len") or row.get("file_size_bytes") or "0"
                try:
                    safe_bytes += int(float(str(raw).strip() or "0"))
                except Exception:
                    pass
            else:
                non_safe += 1
    # preserve order, remove duplicates
    safe_uids = list(dict.fromkeys(safe_uids))
    return safe_uids, safe_bytes, non_safe


def chunks(values: list[str], n: int) -> Iterable[list[str]]:
    for i in range(0, len(values), n):
        yield values[i:i+n]


def column_exists(cur, schema: str, table: str, col: str) -> bool:
    cur.execute(
        """
        SELECT EXISTS (
          SELECT 1 FROM information_schema.columns
          WHERE table_schema=%s AND table_name=%s AND column_name=%s
        )
        """,
        (schema, table, col),
    )
    return bool(cur.fetchone()[0])


def get_uid_udt(cur, schema: str, table: str) -> str:
    cur.execute(
        """
        SELECT udt_name
        FROM information_schema.columns
        WHERE table_schema=%s AND table_name=%s AND column_name='file_uid'
        """,
        (schema, table),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"{schema}.{table}.file_uid not found")
    return str(row[0])


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Make NAS-verified Silver image rows metadata-only. DRY RUN by default. "
            "Only file_uid values classified NAS_OK_BYTEA_REDUNDANT in the latest audit CSV are eligible."
        )
    )
    ap.add_argument("--audit-root", default="storage_audit_nas")
    ap.add_argument("--source-group", action="append", default=[])
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument("--apply", action="store_true", help="Actually NULL image_bytes. Without this flag, read-only dry run.")
    args = ap.parse_args()

    root = Path(args.audit_root).resolve()
    groups = [str(x).upper().strip() for x in (args.source_group or DEFAULT_GROUPS)]
    batch_size = max(100, min(5000, int(args.batch_size)))

    print("=" * 92)
    print("GOLDEN EYE - CLEAR NAS-VERIFIED SILVER BYTEA")
    print("=" * 92)
    print(f"mode={'APPLY' if args.apply else 'DRY-RUN'} batch_size={batch_size}")
    print(f"audit_root={root}")

    all_uids: list[str] = []
    expected_bytes = 0
    for group in groups:
        p = latest_detail_csv(root, group)
        uids, b, non_safe = load_safe_rows(p)
        all_uids.extend(uids)
        expected_bytes += b
        print(
            f"{group}: audit={p.name} safe_rows={len(uids):,} "
            f"keep_rows={non_safe:,} safe_payload~={b/(1024**3):.3f} GB"
        )

    all_uids = list(dict.fromkeys(all_uids))
    print("-" * 92)
    print(f"TOTAL safe unique file_uid={len(all_uids):,}")
    print(f"TOTAL logical JPEG payload eligible~={expected_bytes/(1024**3):.3f} GB")

    if not args.apply:
        print("\nDRY RUN ONLY: no database rows changed.")
        print("If these counts match the completed NAS audits, rerun with --apply.")
        return 0

    settings = Settings()
    schema = settings.silver_schema
    table = settings.image_table

    with connection(settings) as conn:
        with conn.cursor() as cur:
            uid_udt = get_uid_udt(cur, schema, table)
            has_bytea_len = column_exists(cur, schema, table, "bytea_len")
            print(f"target={schema}.{table} file_uid_type={uid_udt} bytea_len_column={has_bytea_len}")

            if uid_udt == "uuid":
                predicate = "file_uid = ANY(%s::uuid[])"
            elif uid_udt in {"text", "varchar", "bpchar", "citext"}:
                predicate = "file_uid = ANY(%s::text[])"
            else:
                # Safe fallback for unusual UID types. It may be less index-friendly.
                predicate = "file_uid::text = ANY(%s::text[])"

            sets = ["image_bytes = NULL"]
            if has_bytea_len:
                sets.append("bytea_len = NULL")
            set_sql = ", ".join(sets)
            sql = f"""
                UPDATE {schema}.{table}
                   SET {set_sql}
                 WHERE {predicate}
                   AND image_bytes IS NOT NULL
            """

            updated_total = 0
            batches = (len(all_uids) + batch_size - 1) // batch_size
            for idx, batch in enumerate(chunks(all_uids, batch_size), 1):
                try:
                    cur.execute("SET LOCAL lock_timeout='15s'")
                    cur.execute("SET LOCAL statement_timeout='10min'")
                    cur.execute(sql, (batch,))
                    changed = int(cur.rowcount or 0)
                    conn.commit()
                    updated_total += changed
                    if idx == 1 or idx % 10 == 0 or idx == batches:
                        print(
                            f"batch={idx:,}/{batches:,} requested={len(batch):,} "
                            f"updated={changed:,} updated_total={updated_total:,}",
                            flush=True,
                        )
                except Exception:
                    conn.rollback()
                    raise

            with conn.cursor() as vcur:
                # Count remaining BYTEA among the audited-safe UID list in manageable chunks.
                remaining = 0
                for batch in chunks(all_uids, batch_size):
                    if uid_udt == "uuid":
                        pred = "file_uid = ANY(%s::uuid[])"
                    elif uid_udt in {"text", "varchar", "bpchar", "citext"}:
                        pred = "file_uid = ANY(%s::text[])"
                    else:
                        pred = "file_uid::text = ANY(%s::text[])"
                    vcur.execute(
                        f"SELECT count(*) FROM {schema}.{table} WHERE {pred} AND image_bytes IS NOT NULL",
                        (batch,),
                    )
                    remaining += int(vcur.fetchone()[0])

    print("=" * 92)
    print("DONE")
    print(f"audited_safe_rows={len(all_uids):,}")
    print(f"rows_changed_this_run={updated_total:,}")
    print(f"safe_rows_still_with_bytea={remaining:,}")
    print("No rows were deleted. Metadata/path/hash remain in Silver.")
    print("Physical PostgreSQL files will NOT shrink automatically; do not VACUUM FULL yet.")
    print("=" * 92)
    return 0 if remaining == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
