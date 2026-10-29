from __future__ import annotations

import argparse
import csv
import hashlib
import io
import inspect
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from PIL import Image


ROOT = Path(__file__).resolve().parent
DASHBOARD = ROOT / "dashboard"
APP_FILE = DASHBOARD / "app.py"
EXPECTED_APP_SHA256 = "6042463726d0a4988efc5ec92244f27dafe6e167317a0cd516b4688b3b513c98"
DASHBOARD_IMAGE_RECOVERY_ALL = "dashboard_image_recovery_needed_all.csv"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diagnosis metadata, callback, dan jalur gambar dashboard Golden Eye."
    )
    parser.add_argument(
        "--input-report",
        default="",
        help="CSV image_recovery_needed.csv yang harus diuji melalui pembaca dashboard.",
    )
    parser.add_argument(
        "--image-check-limit",
        type=int,
        default=3,
        help="Jumlah representative image per company jika --input-report tidak diberikan.",
    )
    parser.add_argument(
        "--audit-all-images",
        action="store_true",
        help=(
            "Audit seluruh image aktif halaman Detail berdasarkan Gold vs gambar_reused "
            "tanpa membaca BYTEA Silver."
        ),
    )
    parser.add_argument(
        "--source-group",
        default="ALL",
        choices=["ALL", "PWP", "DIN", "DAN", "DWT"],
        help="Batasi --audit-all-images ke satu source group.",
    )
    image_role_mode = parser.add_mutually_exclusive_group()
    image_role_mode.add_argument(
        "--transport-only",
        action="store_true",
        help=(
            "Bersama --audit-all-images, audit hanya foto transport DT/FT. "
            "Tidak membaca foto panen atau Silver."
        ),
    )
    image_role_mode.add_argument(
        "--panen-only",
        action="store_true",
        help=(
            "Bersama --audit-all-images, audit hanya foto panen aktif. "
            "Tidak membaca foto transport atau Silver."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default="dashboard_image_audit",
        help="Folder CSV hasil --audit-all-images, relatif terhadap root project.",
    )
    parser.add_argument(
        "--deep-image-read",
        action="store_true",
        help=(
            "Baca dan decode seluruh BYTEA gambar_reused yang direferensikan "
            "Gold. Pakai setelah recovery sebelum menghapus tabel legacy."
        ),
    )
    parser.add_argument(
        "--audit-transport-kt-before",
        action="store_true",
        help=(
            "Audit seluruh OPH foto panen PWP/DIN terhadap MSSQL KT Before yang sudah "
            "disinkronkan dan BYTEA processing.gambar_reused."
        ),
    )
    return parser.parse_args()


def _audit_transport_kt_before(
    db: Any,
    output_dir: Path,
    *,
    deep: bool,
    source_group: str = "PWP",
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    source_group = str(source_group or "PWP").upper().strip()
    if source_group == "ALL":
        results = [
            _audit_transport_kt_before(
                db, output_dir, deep=deep, source_group=company
            )
            for company in ("PWP", "DIN")
        ]
        return {
            "label": "TRANSPORT_KT_BEFORE/ALL",
            "total": sum(result["total"] for result in results),
            "readable": sum(result["readable"] for result in results),
            "gambar_reused": sum(result["gambar_reused"] for result in results),
            "source_fallback": 0,
            "missing": [item for result in results for item in result["missing"]],
            "invalid": [item for result in results for item in result["invalid"]],
        }
    if source_group not in {"PWP", "DIN"}:
        raise ValueError("--audit-transport-kt-before hanya untuk PWP, DIN, atau ALL")
    if not db.table_exists(db.TABLE_TRANSPORT_KT_CONTEXT, db.RECOVERY_SCHEMA):
        print(
            "FAIL transport KT: processing.transport_kt_before_context belum ada. "
            "Jalankan py run_pipeline.py init-db"
        )
        return {
            "label": f"TRANSPORT_KT_BEFORE/{source_group}", "total": 0, "readable": 0,
            "gambar_reused": 0, "source_fallback": 0,
            "missing": ["TABLE_MISSING"], "invalid": [],
        }
    rows = db.read_df(
        f"""
        WITH active AS (
            SELECT c.oph_key,
                   max(c.oph::text) AS oph
            FROM processing.image_context c
            WHERE upper(c.source_group)=%s
              AND c.oph IS NOT NULL
              AND nullif(trim(c.oph_key), '') IS NOT NULL
              AND c.period_date IS NOT NULL
              AND extract(year FROM c.period_date)::integer=2026
            GROUP BY c.oph_key
        ), chosen AS (
            SELECT DISTINCT ON (oph_key)
                   oph_key, file_uid, evidence_file_name, load_status, last_error
            FROM {db.qname(db.TABLE_TRANSPORT_KT_CONTEXT, db.RECOVERY_SCHEMA)}
            WHERE source_group=%s AND period_year=2026 AND is_active=true
            ORDER BY oph_key,
                     (load_status='READY') DESC,
                     period_date DESC NULLS LAST,
                     evidence_key
        )
        SELECT a.oph, a.oph_key,
               c.file_uid, c.evidence_file_name AS image_key,
               coalesce(c.load_status, 'MSSQL_EVIDENCE_MISSING') AS load_status,
               c.last_error,
               (g.file_uid IS NOT NULL) AS tersedia_hot
        FROM active a
        LEFT JOIN chosen c ON c.oph_key=a.oph_key
        LEFT JOIN processing.gambar_reused g
          ON g.source_group=%s
         AND g.file_uid=c.file_uid
         AND g.is_active=true
        ORDER BY a.oph_key
        """,
        [source_group, source_group, source_group],
    )
    if rows.empty:
        mssql_missing_rows = rows
        nas_file_missing_rows = rows
        hot_missing_rows = rows
        missing_rows = rows
    else:
        has_evidence = rows["file_uid"].fillna("").astype(str).str.strip().ne("")
        mssql_missing_rows = rows[~has_evidence].copy()
        ready_status = rows["load_status"].fillna("").astype(str).str.upper().eq("READY")
        nas_file_missing_rows = rows[
            has_evidence & ~ready_status & ~rows["tersedia_hot"].astype(bool)
        ].copy()
        hot_missing_rows = rows[
            has_evidence & ready_status & ~rows["tersedia_hot"].astype(bool)
        ].copy()
        missing_rows = rows[~rows["tersedia_hot"].astype(bool)].copy()
    csv_path = output_dir / (
        f"dashboard_transport_kt_before_missing_{source_group.lower()}.csv"
    )
    missing_rows.to_csv(csv_path, index=False, encoding="utf-8-sig")
    invalid: list[str] = []
    if deep and not rows.empty:
        ready_keys = rows.loc[rows["tersedia_hot"].astype(bool), "image_key"].dropna().astype(str).unique().tolist()
        print(f"[KT BEFORE DEEP READ] decode {len(ready_keys):,} gambar", flush=True)
        for index, key in enumerate(ready_keys, start=1):
            result = db.fetch_image_bytes(key)
            try:
                if not result:
                    raise ValueError("BYTEA tidak ditemukan")
                payload = bytes(result[0])
                with Image.open(io.BytesIO(payload)) as image:
                    image.verify()
            except Exception as exc:
                invalid.append(f"{key}: {exc}")
            storage_warnings = db.get_image_storage_warnings()
            if storage_warnings:
                relation, detail = next(iter(storage_warnings.items()))
                invalid = [f"STORAGE_RELATION_ERROR {relation}: {detail}"]
                print(
                    "[KT BEFORE DEEP READ] dihentikan: relation/TOAST rusak; "
                    "jumlah file individual belum dapat ditentukan.",
                    flush=True,
                )
                break
            if index % 500 == 0 or index == len(ready_keys):
                print(
                    f"[KT BEFORE DEEP READ] checked={index:,}/{len(ready_keys):,} "
                    f"failed={len(invalid):,}",
                    flush=True,
                )
    total = len(rows)
    available = int(rows["tersedia_hot"].astype(bool).sum()) if total else 0
    similarity_text = "similarity_table=belum_ada"
    if db.table_exists(db.TABLE_TRANSPORT_KT_SIMILARITY, db.RECOVERY_SCHEMA):
        score_rows = db.read_df(
            f"""
            SELECT similarity_category, count(*)::bigint AS pairs
            FROM {db.qname(db.TABLE_TRANSPORT_KT_SIMILARITY, db.RECOVERY_SCHEMA)}
            WHERE source_group=%s AND period_year=2026 AND is_active=true
            GROUP BY similarity_category
            ORDER BY similarity_category
            """,
            [source_group],
        )
        similarity_text = ", ".join(
            f"{row['similarity_category']}={int(row['pairs']):,}"
            for row in score_rows.to_dict("records")
        ) or "similarity_pairs=0"
    print("-" * 78)
    print(f"DASHBOARD TRANSPORT KT_BEFORE AUDIT ({source_group} 2026)")
    print(
        f"all_panen_distinct_oph={total:,} ready_hot={available:,} "
        f"mssql_without_kt={len(mssql_missing_rows):,} "
        f"nas_file_missing={len(nas_file_missing_rows):,} "
        f"ready_hot_missing={len(hot_missing_rows):,} invalid={len(invalid):,} "
        f"csv={csv_path}"
    )
    print(f"TRANSPORT SIMILARITY: {similarity_text}")
    if len(nas_file_missing_rows):
        print(
            f"WARN {len(nas_file_missing_rows):,} evidence MSSQL belum mempunyai "
            "file NAS; tidak dianggap BYTEA dashboard rusak."
        )
    return {
        "label": f"TRANSPORT_KT_BEFORE/{source_group}",
        "total": total,
        "readable": available - len(invalid),
        "gambar_reused": available,
        "source_fallback": 0,
        "missing": hot_missing_rows.get("oph", []).astype(str).tolist() if total else [],
        "invalid": invalid,
    }


def _project_path(value: str) -> Path:
    path = Path(str(value or "").strip()).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def _report_image_keys(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"CSV pemeriksaan dashboard tidak ditemukan: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
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
        keys = [
            str(row.get(key_column) or "").strip()
            for row in reader
            if str(row.get(key_column) or "").strip()
        ]
    return list(dict.fromkeys(keys))


def _sample_company_keys(queries: Any, limit: int) -> dict[str, list[str]]:
    samples: dict[str, list[str]] = {}
    pairs = (
        ("HARMONY", "PWP"),
        ("HARMONY", "DIN"),
        ("ELHP", "DAN"),
        ("ELHP", "DWT"),
    )
    for system, company in pairs:
        groups = queries.load_groups(system, [company])
        keys: list[str] = []
        if groups is not None and not groups.empty:
            for column in ("representative_image", "image_key"):
                if column not in groups.columns:
                    continue
                for value in groups[column].tolist():
                    key = str(value or "").strip()
                    if key and key.lower() not in {"nan", "none", "null"} and key not in keys:
                        keys.append(key)
                    if len(keys) >= limit:
                        break
                if len(keys) >= limit:
                    break
        samples[f"{system}/{company}"] = keys
    return samples


def _probe_dashboard_images(db: Any, label: str, keys: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "label": label,
        "total": len(keys),
        "readable": 0,
        "gambar_reused": 0,
        "source_fallback": 0,
        "missing": [],
        "invalid": [],
    }
    for key in keys:
        recovery = db.get_image_recovery_status(key)
        row = db.fetch_image_bytes(key)
        if not row:
            result["missing"].append(key)
            continue
        try:
            payload = bytes(row[0])
            if not payload:
                raise ValueError("payload kosong")
            with Image.open(io.BytesIO(payload)) as image:
                image.verify()
        except Exception as exc:
            result["invalid"].append(f"{key}: {exc}")
            continue
        result["readable"] += 1
        if recovery:
            result["gambar_reused"] += 1
        else:
            result["source_fallback"] += 1

    print(
        f"DASHBOARD IMAGE CHECK {label}: total={result['total']:,} "
        f"readable={result['readable']:,} gambar_reused={result['gambar_reused']:,} "
        f"source_fallback={result['source_fallback']:,} "
        f"missing={len(result['missing']):,} invalid={len(result['invalid']):,}"
    )
    for key in result["missing"][:20]:
        print(f"  MISSING {key}")
    for message in result["invalid"][:20]:
        print(f"  INVALID {message}")
    return result


def _audit_all_dashboard_images(
    db: Any,
    source_group: str,
    output_dir: Path,
    *,
    deep_image_read: bool = False,
    transport_only: bool = False,
    panen_only: bool = False,
) -> dict[str, Any]:
    """Audit semua evidence aktif terhadap hot store tanpa membaca BYTEA Silver."""
    source_group = str(source_group or "ALL").upper().strip()
    source_filter = ""
    params: list[Any] = []
    if source_group != "ALL":
        source_filter = "AND upper(source_group)=%s"
        params.append(source_group)
    role_filter = ""
    if transport_only:
        role_filter = "AND image_role IN ('TRANSPORT_DT', 'TRANSPORT_FT')"
    elif panen_only:
        role_filter = "AND image_role='PANEN'"

    rows = db.read_df(
        f"""
        WITH active_detail AS (
            SELECT *
            FROM gold.reused_photo_detail
            WHERE is_active=true
              {source_filter}
        ),
        evidence AS (
            SELECT upper(source_group) AS source_group,
                   nullif(trim(file_uid::text), '') AS file_uid,
                   trim(image_name::text) AS file_name,
                   'PANEN'::text AS image_role
            FROM active_detail
            WHERE nullif(trim(image_name::text), '') IS NOT NULL

            UNION ALL

            SELECT upper(source_group) AS source_group,
                   nullif(trim(foto_dt_uid::text), '') AS file_uid,
                   trim(foto_dt::text) AS file_name,
                   'TRANSPORT_DT'::text AS image_role
            FROM active_detail
            WHERE nullif(trim(foto_dt::text), '') IS NOT NULL

            UNION ALL

            SELECT upper(source_group) AS source_group,
                   nullif(trim(foto_ft_uid::text), '') AS file_uid,
                   trim(foto_ft::text) AS file_name,
                   'TRANSPORT_FT'::text AS image_role
            FROM active_detail
            WHERE nullif(trim(foto_ft::text), '') IS NOT NULL
        ),
        normalized AS (
            SELECT source_group,
                   file_uid,
                   file_name,
                   string_agg(
                       DISTINCT image_role, ', ' ORDER BY image_role
                   ) AS image_role
            FROM evidence
            WHERE file_name ~* '\\.(jpg|jpeg|png|bmp|webp|tif|tiff)$'
              {role_filter}
            GROUP BY source_group, file_uid, file_name
        )
        SELECT e.source_group,
               e.file_uid,
               e.file_name AS image_key,
               e.image_role,
               EXISTS (
                   SELECT 1
                   FROM processing.gambar_reused r
                   WHERE r.is_active=true
                     AND r.source_group=e.source_group
                     AND (
                           (e.file_uid IS NOT NULL AND r.file_uid=e.file_uid)
                           OR upper(trim(r.file_name))=upper(trim(e.file_name))
                         )
               ) AS tersedia_hot
        FROM normalized e
        ORDER BY e.source_group, upper(e.file_name), e.file_uid
        """,
        params,
    )

    if rows.empty:
        print(f"DASHBOARD IMAGE AUDIT ALL: source={source_group} total=0")
        return {
            "label": f"ALL_IMAGES/{source_group}",
            "total": 0,
            "readable": 0,
            "gambar_reused": 0,
            "source_fallback": 0,
            "missing": [],
            "invalid": [],
        }

    rows["tersedia_hot"] = rows["tersedia_hot"].fillna(False).astype(bool)
    rows["deep_read_ok"] = rows["tersedia_hot"]
    deep_invalid: list[str] = []
    if deep_image_read:
        unique_keys = list(dict.fromkeys(rows["image_key"].astype(str).tolist()))
        read_status: dict[str, bool] = {}
        print(
            f"[DEEP IMAGE READ] mulai membaca/decode {len(unique_keys):,} "
            "gambar dari processing.gambar_reused"
        )
        for position, key in enumerate(unique_keys, start=1):
            row = db.fetch_image_bytes(key)
            ok = False
            if row:
                try:
                    payload = bytes(row[0])
                    if not payload:
                        raise ValueError("payload kosong")
                    with Image.open(io.BytesIO(payload)) as image:
                        image.verify()
                    ok = True
                except Exception as exc:
                    deep_invalid.append(f"{key}: {exc}")
            read_status[key] = ok
            if position % 500 == 0 or position == len(unique_keys):
                print(
                    f"[DEEP IMAGE READ] checked={position:,}/{len(unique_keys):,} "
                    f"failed={sum(not value for value in read_status.values()):,}"
                )
        rows["deep_read_ok"] = rows["image_key"].astype(str).map(read_status).fillna(False)

    rows["status"] = rows["tersedia_hot"].map(
        {True: "READY_GAMBAR_REUSED", False: "PENDING_GAMBAR_REUSED"}
    )
    if deep_image_read:
        rows.loc[
            rows["tersedia_hot"] & ~rows["deep_read_ok"],
            "status",
        ] = "BYTEA_READ_OR_DECODE_FAILED"
    pending = rows[~rows["tersedia_hot"] | ~rows["deep_read_ok"]].copy()

    output_dir.mkdir(parents=True, exist_ok=True)
    columns = [
        "source_group",
        "file_uid",
        "image_key",
        "image_role",
        "status",
    ]
    file_prefix = (
        "dashboard_transport_recovery_needed"
        if transport_only
        else "dashboard_panen_recovery_needed"
        if panen_only
        else "dashboard_image_recovery_needed"
    )
    all_path = output_dir / (
        f"{file_prefix}_all.csv"
        if transport_only or panen_only
        else DASHBOARD_IMAGE_RECOVERY_ALL
    )
    pending[columns].to_csv(all_path, index=False, encoding="utf-8-sig")

    print("-" * 78)
    print(
        f"DASHBOARD "
        f"{'TRANSPORT ' if transport_only else 'PANEN ' if panen_only else ''}"
        "IMAGE AUDIT ALL "
        "(Gold aktif vs processing.gambar_reused; tidak membaca BYTEA Silver)"
    )
    for group in sorted(rows["source_group"].astype(str).unique()):
        scoped = rows[rows["source_group"].astype(str).eq(group)]
        scoped_pending = pending[pending["source_group"].astype(str).eq(group)]
        scoped_recovered = int(scoped["tersedia_hot"].sum())
        group_path = output_dir / f"{file_prefix}_{group.lower()}.csv"
        scoped_pending[columns].to_csv(
            group_path,
            index=False,
            encoding="utf-8-sig",
        )
        print(
            f"  {group}: active_images={len(scoped):,} "
            f"gambar_reused={scoped_recovered:,} "
            f"pending_gambar_reused={len(scoped_pending):,} "
            f"csv={group_path}"
        )
        if transport_only or panen_only:
            role_counts = scoped["image_role"].value_counts().to_dict()
            print(
                "       roles="
                + ", ".join(
                    f"{role}={int(count):,}"
                    for role, count in sorted(role_counts.items())
                )
            )
    print(f"  ALL pending CSV: {all_path}")

    return {
        "label": (
            f"TRANSPORT_IMAGES/{source_group}"
            if transport_only
            else f"PANEN_IMAGES/{source_group}"
            if panen_only
            else f"ALL_IMAGES/{source_group}"
        ),
        "total": len(rows),
        "readable": int(rows["tersedia_hot"].sum()),
        "gambar_reused": int(rows["tersedia_hot"].sum()),
        "source_fallback": 0,
        "missing": rows.loc[
            ~rows["tersedia_hot"], "image_key"
        ].astype(str).tolist(),
        "invalid": deep_invalid,
    }


def main() -> int:
    args = parse_args()
    if args.transport_only and not args.audit_all_images:
        raise ValueError("--transport-only harus dipakai bersama --audit-all-images")
    if args.panen_only and not args.audit_all_images:
        raise ValueError("--panen-only harus dipakai bersama --audit-all-images")
    input_report_path = _project_path(args.input_report) if args.input_report else None
    audit_output_dir = _project_path(args.output_dir)
    print("=" * 78)
    print("GOLDEN EYE DASHBOARD DIAGNOSTIC")
    print("=" * 78)
    print(f"root          : {ROOT}")
    print(f"dashboard dir : {DASHBOARD}")
    print(f"app.py        : {APP_FILE}")

    if not APP_FILE.exists():
        print("FAIL: dashboard/app.py tidak ditemukan.")
        return 2

    actual_hash = file_sha256(APP_FILE)
    print(f"app sha256    : {actual_hash}")
    if actual_hash == EXPECTED_APP_SHA256:
        print("app version   : OK, app.py perbaikan sudah terpasang")
    else:
        print("app version   : WARNING, hash berbeda dari app.py perbaikan")

    load_dotenv(ROOT / ".env", override=True)
    os.chdir(DASHBOARD)
    sys.path.insert(0, str(DASHBOARD))

    try:
        import db
        import queries
        import app as dashboard_app

        system = str(os.getenv("DEFAULT_SYSTEM", "HARMONY") or "HARMONY").upper().strip()
        default_company = str(os.getenv("DEFAULT_COMPANY", "PWP") or "PWP").upper().strip()

        print("-" * 78)
        print(f"loaded app    : {Path(inspect.getfile(dashboard_app)).resolve()}")
        print(f"database      : {db.ping()}")
        print(f"schema        : {db.APP_SCHEMA}")
        print(f"group table   : {db.TABLE_GROUP}")
        print(f"detail table  : {db.TABLE_DETAIL}")
        print(f"monthly table : {db.TABLE_DASHBOARD_MONTHLY}")
        print(f"SAP KPI table : {db.TABLE_SAP_KPI_SUMMARY}")
        print(f"recovery table: {db.RECOVERY_SCHEMA}.{db.TABLE_BYTEA_RECOVERY}")
        print(f"transport KT  : {db.RECOVERY_SCHEMA}.{db.TABLE_TRANSPORT_KT_CONTEXT}")
        print(f"system        : {system}")
        print(f"default comp. : {default_company}")
        print(f"require estate: {queries.DASHBOARD_REQUIRE_ESTATE}")

        options = queries.get_company_options(system)
        option_values = [str(item.get("value")) for item in options]
        print(f"companies     : {option_values}")

        company = [default_company] if default_company in option_values else (
            [option_values[0]] if option_values else []
        )
        groups = queries.load_groups(system, company)
        monthly = queries.load_dashboard_monthly(system, company)
        print(f"selected comp.: {company}")
        print(f"query groups  : {len(groups):,}")
        print(f"monthly rows  : {len(monthly):,}")
        monthly_values = sorted(
            monthly.get("month", []).astype(str).unique().tolist()
        ) if not monthly.empty and "month" in monthly.columns else []
        print(f"monthly months: {monthly_values}")
        print(
            "monthly usage : "
            f"{int(monthly.get('usage_weight', 0).sum()) if not monthly.empty else 0:,}"
        )

        switch_results = {}
        for option_value in option_values:
            _, selected = dashboard_app.update_company_filter(system, [option_value])
            switch_results[option_value] = selected
        company_switch_ok = all(
            switch_results.get(value) == [value] for value in option_values
        )
        print(f"company switch: {switch_results}")

        sap_kpi = queries.load_sap_kpis(system, company, groups)
        print(
            "SAP KPI       : "
            f"total_sap={sap_kpi.get('total_sap_oph')} "
            f"scope_ready={bool(sap_kpi.get('scope_ready'))} "
            f"period_year={sap_kpi.get('period_year')} "
            f"prfnr_scope={sap_kpi.get('prfnr_scope', [])}"
        )

        render_result = dashboard_app.render_all(
            system,
            company,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "",
            "overview",
            None,
        )
        print(f"render outputs: {len(render_result)}")
        group_rows = (
            render_result[21]
            if len(render_result) > 21 and isinstance(render_result[21], list)
            else []
        )
        print(f"render rows   : {len(group_rows):,}")

        if db.TABLE_BYTEA_RECOVERY != "gambar_reused":
            print(
                "FAIL: dashboard belum diarahkan ke hot store final. "
                "Gunakan processing.gambar_reused."
            )
            return 11
        if not db.table_exists(db.TABLE_BYTEA_RECOVERY, db.RECOVERY_SCHEMA):
            print(
                "FAIL: processing.gambar_reused belum ada. "
                "Jalankan: py run_pipeline.py init-db"
            )
            return 12

        recovery_counts = db.read_df(
            f"""
            SELECT upper(source_group) AS source_group,
                   count(*)::bigint AS recovered_images,
                   coalesce(sum(bytea_len), 0)::bigint AS recovered_bytes
            FROM {db.qname(db.TABLE_BYTEA_RECOVERY, db.RECOVERY_SCHEMA)}
            WHERE is_active=true
            GROUP BY upper(source_group)
            ORDER BY upper(source_group)
            """,
            cache_key="dashboard-diagnose:gambar-reused-counts",
        )
        print("gambar reused :")
        if recovery_counts.empty:
            print("  (kosong)")
        else:
            for row in recovery_counts.to_dict("records"):
                print(
                    f"  {row['source_group']}: "
                    f"images={int(row['recovered_images']):,} "
                    f"bytes={int(row['recovered_bytes']):,}"
                )

        image_results: list[dict[str, Any]] = []
        if args.audit_all_images:
            image_results.append(
                _audit_all_dashboard_images(
                    db,
                    args.source_group,
                    audit_output_dir,
                    deep_image_read=args.deep_image_read,
                    transport_only=args.transport_only,
                    panen_only=args.panen_only,
                )
            )
        if args.audit_transport_kt_before:
            image_results.append(
                _audit_transport_kt_before(
                    db,
                    audit_output_dir,
                    deep=args.deep_image_read,
                    source_group=args.source_group,
                )
            )
        if (
            input_report_path is not None
            and not args.audit_all_images
            and not args.audit_transport_kt_before
        ):
            image_results.append(
                _probe_dashboard_images(
                    db,
                    f"REPORT={input_report_path.name}",
                    _report_image_keys(input_report_path),
                )
            )
        elif not args.audit_all_images and not args.audit_transport_kt_before:
            limit = max(1, int(args.image_check_limit))
            for label, keys in _sample_company_keys(queries, limit).items():
                image_results.append(_probe_dashboard_images(db, label, keys))

        image_failures = sum(
            len(result["missing"]) + len(result["invalid"])
            for result in image_results
        )
        image_checked = sum(result["total"] for result in image_results)
        storage_warnings = db.get_image_storage_warnings()
        if storage_warnings:
            print("DASHBOARD IMAGE STORAGE WARNINGS:")
            for relation, message in sorted(storage_warnings.items()):
                print(f"  {relation}: {message}")

        if len(render_result) != 27:
            print("FAIL: render_all tidak menghasilkan 27 output.")
            return 3
        if not option_values:
            print("FAIL: company options kosong.")
            return 4
        if groups.empty:
            print("FAIL: query dashboard menghasilkan 0 group.")
            return 5
        if monthly.empty:
            print("FAIL: aggregate monthly kosong. Jalankan: py run_pipeline.py init-db")
            return 7
        if not group_rows:
            print("FAIL: callback render menghasilkan 0 row. Lihat RENDER ERROR di atas.")
            return 6
        if not company_switch_ok:
            print("FAIL: pilihan Company masih ditimpa callback.")
            return 8
        if image_checked == 0:
            print("FAIL: tidak ada image key dashboard yang dapat diperiksa.")
            return 13
        if image_failures:
            if args.audit_all_images:
                print(
                    f"FAIL: {image_failures:,} dari {image_checked:,} gambar aktif "
                    "belum tersedia/sehat di gambar_reused. Gunakan CSV per "
                    "source-group yang dibuat di atas untuk recovery dari NAS."
                )
            else:
                print(
                    f"FAIL: {image_failures:,} dari {image_checked:,} gambar dashboard "
                    "belum terbaca/decode. Jalankan recovery untuk key MISSING/INVALID."
                )
            return 14

        print("-" * 78)
        print(
            "PASS: DB, company switch, SAP KPI, callback, dan jalur gambar "
            "processing.gambar_reused menghasilkan data."
        )
        print("Jika browser masih kosong, hentikan proses lama, jalankan ulang dashboard,")
        print("lalu buka Incognito atau lakukan Ctrl+F5.")
        return 0
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 10


if __name__ == "__main__":
    raise SystemExit(main())
