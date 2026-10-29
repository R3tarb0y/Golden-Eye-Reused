from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from . import __version__
from .config import Settings
from .context import sync_context
from .audit import (
    audit_bytea_health,
    audit_date_lineage,
    audit_elhp_estate_scope,
    audit_month_coverage,
    audit_oph_uniqueness,
    audit_source,
    print_source_audit,
)
from .db import (
    connection,
    create_run,
    fetch_all_dict,
    init_database,
    pipeline_job_lock,
    table_exists,
    update_run,
)
from .features import embed_panen_nas_missing, embed_source
from .enrichment import enrich_active_gold_context, recover_elhp_personnel_month, sync_raw_usage_context
from .matcher import match_source
from .oph_cleanup import republish_clean_oph
from .publisher import publish_gold, refresh_active_sap_kpi
from .validator import validate_source
from .source_consolidation import consolidate_elhp_source
from .recovery import cleanup_legacy_recovery, recover_gold_evidence_from_folders
from .silver_loader import load_silver_from_folders
from .transport_sync import sync_transport_kt_before
from .transport_similarity import score_transport_kt_before
from .panen_nas_index import index_panen_nas_paths
from .hot_store_rebuild import rebuild_gambar_reused_storage
from .image_storage_repair import repair_gambar_reused_indexes
from .raw_monthly_sync import sync_raw_production_month
from .silver_storage_rebuild import rebuild_silver_storage
from .match_storage import reset_match_staging
from .geospatial import import_boundaries, sync_tph_points
from .geospatial_bundle import sync_bundled_whu_boundaries
from .sap_db60_sync import sync_db60_zpay
from .monthly_trend_export import export_monthly_trend_png


STAGES = ["context", "embed", "match", "validate", "publish"]


def _run_pipeline(args, settings: Settings) -> int:
    source_group = args.source_group.upper()
    run_id = uuid.UUID(args.run_id) if args.run_id else create_run(settings, source_group)
    start_idx = STAGES.index(args.from_stage)
    stop_idx = STAGES.index(args.to_stage)
    if start_idx > stop_idx:
        raise ValueError("--from-stage harus sebelum atau sama dengan --to-stage")

    print(f"run_id={run_id} source_group={source_group}")
    print(
        "effective_config="
        f"mode={settings.match_mode} "
        f"engine={settings.match_engine} "
        f"review={settings.review_threshold:.1f} "
        f"reused={settings.reused_threshold:.1f} "
        f"top_k={settings.match_top_k} "
        f"faiss_index={settings.faiss_index_mode} "
        f"faiss_flat_max={settings.faiss_flat_max_images:,} "
        f"auto_cap_top_k={settings.match_auto_cap_top_k} "
        f"mutual_knn={settings.match_require_mutual_knn} "
        f"component_cap={settings.match_component_max_size:,} "
        f"full_pair_max={settings.validation_full_pair_max_group:,} "
        f"validation_device={settings.validation_device} "
        f"validation_workers={settings.validation_cpu_workers} "
        f"validation_nas_io_workers={settings.validation_nas_io_workers} "
        f"validation_db_members={settings.validation_db_member_batch:,} "
        f"validation_db_candidates={settings.validation_db_candidate_batch:,} "
        f"validation_read_order={settings.validation_source_read_order} "
        f"validation_audit_cap={settings.validation_max_audit_pairs_per_group:,} "
        f"group_mean_similarity={settings.validation_group_mean_similarity:.1f} "
        f"member_mean_similarity={settings.validation_member_mean_similarity:.1f} "
        f"representative_similarity={settings.validation_representative_similarity:.1f} "
        f"low_light_guard={settings.low_light_guard} "
        f"structural_duplicate_guard={settings.duplicate_require_structural_evidence} "
        "sap_kpi_scope=auto_company_month_distinct_oph "
        f"dedup_logical={settings.deduplicate_logical_filename} "
        f"max_edges={settings.max_match_edges:,} "
        f"torch_max_images={settings.torch_fallback_max_images:,} "
        f"exclude_tokens={list(settings.exclude_filename_tokens or ())}"
    )
    try:
        for stage in STAGES[start_idx : stop_idx + 1]:
            print(f"\n{'=' * 78}\nSTAGE {stage.upper()}\n{'=' * 78}")
            if stage == "context":
                metrics = sync_context(settings, source_group)
                update_run(settings, run_id, stage="CONTEXT", metrics=metrics)
            elif stage == "embed":
                embed_source(settings, run_id, source_group)
            elif stage == "match":
                match_source(
                    settings,
                    run_id,
                    source_group,
                    allow_nas_validation=bool(
                        getattr(args, "validation_nas_roots", None)
                    ),
                )
            elif stage == "validate":
                validate_source(
                    settings,
                    run_id,
                    source_group,
                    nas_roots=getattr(args, "validation_nas_roots", None),
                )
            elif stage == "publish":
                publish_gold(settings, run_id, source_group)
        if args.to_stage != "publish":
            update_run(settings, run_id, stage=args.to_stage.upper(), status="PAUSED")
        print(f"\nDONE run_id={run_id}")
        return 0
    except KeyboardInterrupt:
        update_run(
            settings,
            run_id,
            stage="INTERRUPTED",
            status="FAILED",
            error_message="Dihentikan user dengan Ctrl+C",
        )
        print(f"INTERRUPTED run_id={run_id}", file=sys.stderr)
        return 130
    except Exception as exc:
        update_run(
            settings,
            run_id,
            stage="FAILED",
            status="FAILED",
            error_message=str(exc)[:4000],
        )
        print(f"FAILED run_id={run_id}: {exc}", file=sys.stderr)
        raise


def _status(settings: Settings, limit: int) -> int:
    with connection(settings) as conn:
        rows = fetch_all_dict(
            conn,
            """
            SELECT run_id, source_group, stage, status, started_at, finished_at,
                   metrics, error_message
            FROM processing.pipeline_run
            ORDER BY started_at DESC
            LIMIT %s
            """,
            (limit,),
        )
    for row in rows:
        print(
            f"{row['started_at']} | {row['source_group']:<8} | "
            f"{row['status']:<9} | {row['stage']:<10} | {row['run_id']}"
        )
        if row.get("metrics"):
            print("  metrics:", json.dumps(row["metrics"], ensure_ascii=False, default=str))
        if row.get("error_message"):
            print("  error  :", row["error_message"])
    return 0


def _merge_month(args, settings: Settings) -> int:
    """Load one NAS month, reuse old embeddings, rebuild the all-history snapshot."""
    source_group = args.source_group.upper().strip()
    month = args.month.strip()
    if not 1 <= args.batch_size <= 256:
        raise ValueError("--batch-size merge-month harus 1..256")
    prefix = Path(
        args.report_prefix
        or f"merge_{source_group.lower()}_{month.replace('-', '_')}"
    ).resolve()
    silver_report = str(prefix.with_name(prefix.name + "_silver.csv"))
    hot_report = str(prefix.with_name(prefix.name + "_gambar_reused.csv"))
    raw_report = str(prefix.with_name(prefix.name + "_raw.csv"))

    print("=" * 78)
    print(f"MONTHLY INCREMENTAL MERGE: {source_group} | month={month}")
    print("=" * 78)
    print(
        "SCOPE root boleh campuran banyak bulan. Tanggal filename disaring sebelum "
        "stat/hash/decode sehingga payload bulan lama tidak dibaca ulang dari NAS. "
        "MATCH tetap lintas seluruh histori agar reuse antarbulan ada."
    )
    if args.resume_after_silver and not args.apply:
        raise ValueError("--resume-after-silver hanya boleh dipakai bersama --apply")

    if args.resume_after_silver:
        print(
            "[MONTH MERGE] RESUME_AFTER_SILVER: RAW sync dan scan/load Silver "
            "dilewati; lanjut Context bulan yang sudah tersimpan.",
            flush=True,
        )
    elif args.skip_raw_sync:
        print(
            "[MONTH MERGE] RAW sync dilewati secara eksplisit. Pastikan "
            f"raw.production_plantation {source_group} {month} sudah lengkap.",
            flush=True,
        )
    else:
        print(
            "[MONTH MERGE] tahap 1/6: sinkronkan transaksi DB45 ke RAW ...",
            flush=True,
        )
        raw_metrics = sync_raw_production_month(
            settings,
            source_group,
            month,
            batch_size=args.raw_batch_size,
            apply=args.apply,
            report_path=raw_report,
        )
        if args.apply and int(raw_metrics.get("source_rows") or 0) <= 0:
            raise RuntimeError(
                f"Month guard RAW gagal: DB45 tidak mempunyai {source_group} {month}."
            )
    if args.resume_after_silver:
        load_metrics = {
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "resume_after_silver": True,
        }
    else:
        load_metrics = load_silver_from_folders(
            settings,
            source_group,
            args.source_root,
            batch_size=args.batch_size,
            report_path=silver_report,
            apply=args.apply,
            max_files=args.max_files,
            allow_low_disk=args.allow_low_disk,
            expected_month=month,
        )
    if not args.apply:
        print("DRY-RUN merge selesai; Context/EMBED/MATCH/Gold belum dijalankan.")
        return 0

    print("[MONTH MERGE] sinkronisasi Context bisnis ...", flush=True)
    # RAW bulan target baru saja disinkronkan di atas dan menjadi sumber
    # bisnis kanonik. Jangan ulangi join seluruh image bulan ke snapshot
    # business Silver; filename tetap diproses oleh sync_context dan
    # OPH/estate dilengkapi oleh sync_raw_usage_context sesudahnya.
    sync_context(
        settings,
        source_group,
        skip_silver_business_lookup=True,
        target_month=month,
    )
    sync_raw_usage_context(settings, source_group, all_missing=True, target_month=month)
    month_audit = audit_month_coverage(settings, source_group)
    source_months = set(month_audit.get("source_months") or [])
    publishable_months = set(
        month_audit.get("publishable_context_months") or []
    )
    historical_missing_source = list(
        month_audit.get("historical_months_missing_source") or []
    )
    if month not in source_months:
        raise RuntimeError(
            f"Month guard gagal: {month} belum terlihat pada source setelah load. "
            f"Periksa {silver_report}."
        )
    if month not in publishable_months:
        raise RuntimeError(
            f"Month guard gagal: Context {month} belum publishable (tanggal+estate). "
            "MATCH/VALIDATE belum dijalankan agar GPU dan waktu tidak terbuang."
        )
    if historical_missing_source:
        raise RuntimeError(
            "Month history guard gagal: bulan yang pernah ada di Gold hilang "
            "dari Embedding/Context=" + ",".join(historical_missing_source) + ". "
            "Pulihkan metadata/embedding historis dahulu; BYTEA penuh tidak "
            "perlu dimuat ulang ke Silver. PUBLISH parsial "
            "tidak diizinkan menggantikan snapshot lengkap."
        )

    if args.from_stage in {"validate", "publish"} and not args.run_id:
        raise ValueError(
            f"--from-stage {args.from_stage} memerlukan --run-id dari percobaan sebelumnya"
        )
    run_id = uuid.UUID(args.run_id) if args.run_id else create_run(
        settings, source_group
    )
    run_args = argparse.Namespace(
        source_group=source_group,
        run_id=str(run_id),
        from_stage=args.from_stage,
        to_stage=args.to_stage,
        validation_nas_roots=[Path(value) for value in args.source_root],
    )
    print(
        f"[MONTH MERGE] run_id={run_id}; load inserted={load_metrics.get('inserted', 0):,} "
        f"updated={load_metrics.get('updated', 0):,} "
        f"skipped={load_metrics.get('skipped', 0):,}",
        flush=True,
    )
    result = _run_pipeline(run_args, settings)
    if result != 0 or args.to_stage != "publish":
        return result

    print(
        "[MONTH MERGE] isi processing.gambar_reused hanya untuk evidence Gold "
        "yang masih belum tersedia ...",
        flush=True,
    )
    hot_metrics = recover_gold_evidence_from_folders(
        settings,
        source_group,
        nas_roots=[Path(value) for value in args.source_root],
        input_report=None,
        active_gold=True,
        panen_only=True,
        batch_size=args.batch_size,
        apply=True,
        report_path=hot_report,
    )
    audit_month_coverage(settings, source_group)
    if source_group in {"DAN", "DWT"}:
        estate_result = audit_elhp_estate_scope(settings, source_group)
        if not estate_result.get("passed"):
            return 3
    remaining = int(hot_metrics.get("remaining") or 0)
    if remaining:
        print(
            f"WARN Gold sudah aktif, tetapi {remaining:,} gambar belum ditemukan pada "
            f"root bulan {month}. Lihat {hot_report}; tambahkan root historis hanya "
            "untuk nama yang tercantum di report.",
            flush=True,
        )
        return 4
    print(
        f"PASS MONTH MERGE {source_group} {month}: run_id={run_id}; "
        "Gold aktif dan gambar reused baru tersedia.",
        flush=True,
    )
    return 0


def _company_guide(settings: Settings, source_group: str | None = None) -> int:
    known = sorted(
        set((settings.source_system_map or {}).keys())
        | set((settings.source_company_map or {}).keys())
    )
    if source_group is None:
        print("=" * 78)
        print("COMPANY / SOURCE GROUP TERKONFIGURASI")
        print("=" * 78)
        for code in known:
            print(
                f"{code:<8} system={settings.source_system(code):<8} "
                f"company={settings.source_company(code)}"
            )
        print("\nPilih satu, contoh:")
        print("  py run_pipeline.py company-guide --source-group DIN")
        return 0

    code = source_group.upper().strip()
    print("=" * 78)
    print(
        f"RUN COMPANY {code} | system={settings.source_system(code)} "
        f"company={settings.source_company(code)}"
    )
    print("=" * 78)
    print("1. Periksa source dan coverage:")
    print(f"   py run_pipeline.py audit-source --source-group {code}")
    print(f"   py run_pipeline.py audit-months --source-group {code}")
    print("   Jika pernah ada error DataCorrupted/TOAST, lakukan deep audit:")
    print(f"   py run_pipeline.py audit-bytea --source-group {code}")
    print("2. Jalankan seluruh pipeline (jangan bersamaan untuk company yang sama):")
    print(f"   py run_pipeline.py run --source-group {code}")
    print("3. Setelah selesai:")
    print("   py run_pipeline.py verify")
    print(f"   py run_pipeline.py audit-months --source-group {code}")
    print("   py run_dashboard.py")
    print("\nJika proses berhenti, lihat UUID:")
    print("   py run_pipeline.py status --limit 5")
    print("Lalu lanjutkan tanpa membuat run baru:")
    print(
        f"   py run_pipeline.py run --source-group {code} --run-id UUID "
        "--from-stage NAMA_STAGE"
    )
    print("Stage tersedia: context, embed, match, validate, publish")
    return 0



def _dashboard_contract_status(settings: Settings) -> dict[str, object]:
    """Inspect the persistent dashboard views that are actually consumed by Dash.

    Code replacement does not mutate PostgreSQL views.  This check makes a stale
    v161/v026 ELHP visual-primary view explicit instead of silently calling its
    result "DASHBOARD".
    """
    required = {
        "gold.reused_photo_group_summary_cleaned": "reused_photo_group_summary_visual_clean",
        "gold.reused_photo_detail_transaction": "reused_photo_detail_visual_clean",
        "gold.elhp_gt_group_summary_cleaned": "reused_photo_group_summary_sap_verified",
        "gold.elhp_gt_detail_transaction": "reused_photo_detail_sap_verified",
        "gold.elhp_dashboard_monthly_cleaned": "reused_photo_dashboard_monthly_sap_verified",
    }
    rows: list[dict[str, object]] = []
    ok = True
    with connection(settings) as conn:
        for relation, must_contain in required.items():
            schema, name = relation.split(".", 1)
            found = fetch_all_dict(
                conn,
                """
                SELECT c.relkind,
                       pg_get_viewdef(c.oid, true) AS definition
                FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=%s AND c.relname=%s
                """,
                (schema, name),
            )
            definition = str(found[0].get("definition") or "") if found else ""
            contract_ok = bool(found) and must_contain.lower() in definition.lower()
            rows.append({
                "relation": relation,
                "required_source": must_contain,
                "exists": bool(found),
                "contract_ok": contract_ok,
                "definition": definition,
            })
            ok = ok and contract_ok
    return {"ok": ok, "views": rows}


def _ensure_dashboard_contract(settings: Settings, *, apply: bool) -> int:
    """Ensure the final v162+ dashboard visibility contract is installed.

    Only the final view contract (sql/027) is reapplied.  It does not delete or
    rewrite Gold rows; it changes which already-published rows are visible in the
    dashboard/report compatibility views.
    """
    before = _dashboard_contract_status(settings)
    print("=" * 78)
    print("DASHBOARD VIEW CONTRACT")
    print("=" * 78)
    for row in before["views"]:
        status = "OK" if row["contract_ok"] else "STALE"
        print(f"{status:<6} {row['relation']} -> {row['required_source']}")
    if before["ok"]:
        print("OK dashboard view contract sudah terbaru.")
        return 0
    if not apply:
        print("STALE dashboard view ditemukan. Jalankan ensure-dashboard-contract --apply.")
        return 1

    project_root = Path(__file__).resolve().parents[2]
    sql_path = project_root / "sql" / "027_elhp_sap_lock_harmony_visual.sql"
    if not sql_path.exists():
        raise FileNotFoundError(f"SQL contract tidak ditemukan: {sql_path}")
    sql_text = sql_path.read_text(encoding="utf-8")
    try:
        with connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '10s'")
                cur.execute("SET LOCAL statement_timeout = '120s'")
                cur.execute(sql_text)
            conn.commit()
    except Exception as exc:
        raise RuntimeError(
            "Gagal memasang final dashboard contract. Jika dependency view lama belum ada, "
            "jalankan sekali: python run_pipeline.py init-db. "
            f"Detail: {exc}"
        ) from exc

    after = _dashboard_contract_status(settings)
    for row in after["views"]:
        status = "OK" if row["contract_ok"] else "STALE"
        print(f"{status:<6} {row['relation']} -> {row['required_source']}")
    if not after["ok"]:
        print("FAIL dashboard contract masih stale setelah apply.")
        return 1
    print("OK final dashboard view contract terpasang. Base Gold tidak dihapus/diubah.")
    return 0


def _verify_dashboard(settings: Settings) -> int:
    """Verify the exact relations consumed by the dashboard, not raw active Gold."""
    contract = _dashboard_contract_status(settings)
    print("=" * 78)
    print("VERIFY DASHBOARD CONSUMPTION LAYER")
    print("=" * 78)
    if not contract["ok"]:
        for row in contract["views"]:
            if not row["contract_ok"]:
                print(
                    f"FAIL stale_view_contract: {row['relation']} "
                    f"expected_source={row['required_source']}"
                )
        print("ACTION: python run_pipeline.py ensure-dashboard-contract --apply")
        return 1

    failed = False
    with connection(settings) as conn:
        # Harmony and ELHP deliberately use different compatibility views.
        harmony = fetch_all_dict(
            conn,
            """
            SELECT processing.canonical_source_group(source_group) AS source_group,
                   count(*)::bigint AS groups,
                   coalesce(sum(usage_count),0)::bigint AS usages,
                   coalesce(max(usage_count),0)::bigint AS max_group
            FROM gold.reused_photo_group_summary_cleaned
            GROUP BY processing.canonical_source_group(source_group)
            ORDER BY 1
            """,
        )
        elhp = fetch_all_dict(
            conn,
            """
            SELECT processing.canonical_source_group(source_group) AS source_group,
                   count(*)::bigint AS groups,
                   coalesce(sum(usage_count),0)::bigint AS usages,
                   coalesce(max(usage_count),0)::bigint AS max_group
            FROM gold.elhp_gt_group_summary_cleaned
            GROUP BY processing.canonical_source_group(source_group)
            ORDER BY 1
            """,
        )
        wrong_estate = fetch_all_dict(
            conn,
            """
            SELECT count(*)::bigint AS n
            FROM gold.elhp_gt_detail_transaction d
            WHERE (processing.canonical_source_group(d.source_group)='DAN'
                   AND NOT processing.elhp_estate_matches_company('DAN', d.estate))
               OR (processing.canonical_source_group(d.source_group)='DWT'
                   AND NOT processing.elhp_estate_matches_company('DWT', d.estate))
            """,
        )[0]["n"]
        small_groups = fetch_all_dict(
            conn,
            """
            SELECT count(*)::bigint AS n
            FROM gold.elhp_gt_group_summary_cleaned
            WHERE usage_count < 2
            """,
        )[0]["n"]
        duplicate_oph = fetch_all_dict(
            conn,
            """
            SELECT count(*)::bigint AS n
            FROM (
                SELECT processing.canonical_source_group(source_group) AS source_group,
                       processing.normalize_oph(oph) AS oph_key
                FROM gold.elhp_gt_detail_transaction
                WHERE nullif(processing.normalize_oph(oph), '') IS NOT NULL
                GROUP BY 1, 2
                HAVING count(*) > 1
            ) q
            """,
        )[0]["n"]
        monthly = fetch_all_dict(
            conn,
            """
            SELECT processing.canonical_source_group(source_group) AS source_group,
                   to_char(month, 'YYYY-MM') AS month,
                   count(DISTINCT (run_id, group_id))::bigint AS groups,
                   coalesce(sum(usage_count),0)::bigint AS usages
            FROM gold.elhp_dashboard_monthly_cleaned
            GROUP BY 1,2
            ORDER BY 1,2
            """,
        )

    for row in [*harmony, *elhp]:
        print(
            f"DASHBOARD CLEANED {row['source_group']}: groups={int(row['groups']):,} "
            f"usages={int(row['usages']):,} max_group={int(row['max_group']):,}"
        )
    print(f"{'OK' if int(wrong_estate)==0 else 'FAIL':<4} elhp_wrong_estate_visible: {int(wrong_estate):,}")
    print(f"{'OK' if int(small_groups)==0 else 'FAIL':<4} elhp_group_less_than_two_visible: {int(small_groups):,}")
    # Duplicate OPH is surfaced as a warning here.  It remains covered by the
    # deeper audit-oph command and should not turn a visually valid run into a
    # false Control Center failure.
    print(f"{'OK' if int(duplicate_oph)==0 else 'WARN':<4} elhp_duplicate_normalized_oph_visible: {int(duplicate_oph):,}")
    failed |= int(wrong_estate) != 0 or int(small_groups) != 0

    if monthly:
        print("-" * 78)
        print("ELHP DASHBOARD MONTHLY (CLEANED / SAP SAME-MONTH)")
        for row in monthly:
            print(
                f"{row['source_group']:<4} {row['month']}: "
                f"groups={int(row['groups']):,} usages={int(row['usages']):,}"
            )
    return 1 if failed else 0


def _verify(settings: Settings) -> int:
    checks = {
        "oversize_active_gold_group": """
            SELECT count(*) AS n FROM gold.reused_photo_group_summary
            WHERE is_active=true AND usage_count > %s
        """,
        "orphan_active_detail": """
            SELECT count(*) AS n
            FROM gold.reused_photo_detail d
            LEFT JOIN gold.reused_photo_group_summary g
              ON g.run_id=d.run_id AND g.group_id=d.group_id AND g.is_active=true
            WHERE d.is_active=true AND g.group_id IS NULL
        """,
        "representative_count_not_one": """
            SELECT count(*) AS n FROM (
                SELECT run_id, group_id
                FROM gold.reused_photo_detail
                WHERE is_active=true
                GROUP BY run_id, group_id
                HAVING sum(is_representative) <> 1
            ) q
        """,
        "active_detail_missing_month": """
            SELECT count(*) AS n
            FROM gold.reused_photo_detail
            WHERE is_active=true AND tanggal IS NULL
        """,
        "active_group_missing_month": """
            SELECT count(*) AS n
            FROM gold.reused_photo_group_summary
            WHERE is_active=true
              AND nullif(trim(coalesce(sql_date_list, '')), '') IS NULL
        """,
        "active_duplicate_normalized_oph": """
            SELECT count(*) AS n
            FROM (
                SELECT source_group, processing.normalize_oph(oph) AS oph_key
                FROM gold.reused_photo_detail
                WHERE is_active=true
                  AND nullif(processing.normalize_oph(oph), '') IS NOT NULL
                GROUP BY source_group, processing.normalize_oph(oph)
                HAVING count(*) > 1
            ) q
        """,
        "active_group_less_than_two_visual_members": """
            SELECT count(*) AS n
            FROM (
                SELECT run_id, group_id
                FROM gold.reused_photo_detail
                WHERE is_active=true
                GROUP BY run_id, group_id
                HAVING count(*) < 2
            ) q
        """,
    }
    dashboard_exclusions = {
        "active_detail_missing_estate_excluded": """
            SELECT count(*) AS n
            FROM gold.reused_photo_detail
            WHERE is_active=true
              AND nullif(trim(coalesce(estate, '')), '') IS NULL
        """,
        "active_group_missing_estate_excluded": """
            SELECT count(*) AS n
            FROM gold.reused_photo_group_summary
            WHERE is_active=true
              AND nullif(trim(coalesce(estate_list, '')), '') IS NULL
        """,
    }
    failed = False
    with connection(settings) as conn:
        for name, sql in checks.items():
            params = (settings.max_accepted_group_size,) if "%s" in sql else ()
            value = int(fetch_all_dict(conn, sql, params)[0]["n"])
            status = "OK" if value == 0 else "FAIL"
            print(f"{status:<4} {name}: {value:,}")
            failed |= value != 0
        for name, sql in dashboard_exclusions.items():
            value = int(fetch_all_dict(conn, sql)[0]["n"])
            status = "OK" if value == 0 else "WARN"
            print(f"{status:<4} {name}: {value:,}")
        counts = fetch_all_dict(
            conn,
            """
            SELECT source_group,
                   count(*) AS groups,
                   sum(usage_count) AS usages,
                   max(usage_count) AS max_group
            FROM gold.reused_photo_group_summary
            WHERE is_active=true
            GROUP BY source_group
            ORDER BY source_group
            """,
        )
        dashboard_counts = fetch_all_dict(
            conn,
            """
            WITH visible_group AS (
                SELECT source_group, run_id, group_id, count(*)::bigint AS usage_count
                FROM gold.reused_photo_detail
                WHERE is_active=true
                  AND nullif(trim(coalesce(estate, '')), '') IS NOT NULL
                GROUP BY source_group, run_id, group_id
                HAVING count(*) >= 2
            )
            SELECT source_group,
                   count(*)::bigint AS groups,
                   sum(usage_count)::bigint AS usages,
                   max(usage_count)::bigint AS max_group
            FROM visible_group
            GROUP BY source_group
            ORDER BY source_group
            """,
        )
    for row in counts:
        print(
            f"RAW ACTIVE GOLD {row['source_group']}: groups={row['groups']:,} "
            f"usages={row['usages']:,} max_group={row['max_group']:,}"
        )
    for row in dashboard_counts:
        print(
            f"BASE VISIBLE (NOT ELHP CLEANED) {row['source_group']}: groups={row['groups']:,} "
            f"usages={row['usages']:,} max_group={row['max_group']:,}"
        )
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Golden Eye DB-first pipeline: BYTEA -> similarity -> validation -> GT -> Gold"
    )
    parser.add_argument("--version", action="version", version=f"Golden Eye {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Buat schema processing/gold, tabel, index, dan compatibility view")

    run = sub.add_parser("run", help="Jalankan satu atau beberapa stage pipeline")
    run.add_argument("--source-group", required=True, help="Nilai source_group pada silver.image_store_all_files")
    run.add_argument("--run-id", help="Gunakan run_id lama untuk resume; kosong = run baru")
    run.add_argument("--from-stage", choices=STAGES, default="context")
    run.add_argument("--to-stage", choices=STAGES, default="publish")
    run.add_argument(
        "--validation-nas-root",
        action="append",
        dest="validation_nas_roots",
        help="Root NAS untuk resume VALIDATE tanpa reload BYTEA/Silver; dapat diulang.",
    )

    rebuild = sub.add_parser(
        "rebuild-company",
        help="Buat run baru Context sampai Gold lalu audit bulan otomatis",
    )
    rebuild.add_argument("--source-group", required=True)

    context = sub.add_parser("sync-context", help="Sinkronkan metadata bisnis dan transport tanpa menjalankan similarity")
    context.add_argument("--source-group", required=True)

    raw_context = sub.add_parser(
        "sync-raw-context",
        help="Isi semua Context tanpa estate dari raw dengan exact/unique-estate match",
    )
    raw_context.add_argument("--source-group", required=True)

    enrich = sub.add_parser(
        "enrich-gold-context",
        help="Isi metadata Gold usage yang kosong dari raw.production_plantation",
    )
    enrich.add_argument("--source-group", required=True)

    personnel = sub.add_parser(
        "recover-elhp-personnel",
        help="Recovery Kerani Panen/Mandor ELHP untuk satu bulan tanpa historical/global scan",
    )
    personnel.add_argument("--source-group", required=True, choices=["DAN", "DWT"])
    personnel.add_argument("--month", required=True, help="YYYY-MM, contoh 2026-08")

    export_trend = sub.add_parser(
        "export-monthly-trend-png",
        help="Export chart Monthly Trend report final ke PNG per folder bulan",
    )
    export_trend.add_argument(
        "--month",
        default="",
        help="YYYY-MM. Jika diisi, export satu folder bulan dengan trend YTD sampai bulan tersebut.",
    )
    export_trend.add_argument(
        "--year",
        type=int,
        default=2026,
        help="Tahun export jika --month kosong; semua bulan yang tersedia akan dibuat.",
    )
    export_trend.add_argument(
        "--output-dir",
        default="exports/monthly_trend_png",
        help="Root output; subfolder YYYY-MM dibuat otomatis.",
    )
    export_trend.add_argument(
        "--kind",
        choices=["all", "reused", "transport"],
        default="all",
        help="all = Reused PWP/DIN/DAN/DWT + Transport PWP/DIN.",
    )

    guide = sub.add_parser(
        "company-guide",
        help="Tampilkan company terkonfigurasi dan urutan perintah menjalankannya",
    )
    guide.add_argument("--source-group", help="Contoh: PWP, DIN, DAN, atau DWT")

    status = sub.add_parser("status", help="Lihat run terakhir")
    status.add_argument("--limit", type=int, default=10)

    sub.add_parser("verify", help="Validasi integritas hasil Gold aktif")

    ensure_dash = sub.add_parser(
        "ensure-dashboard-contract",
        help="Pastikan compatibility view Dashboard terbaru (ELHP SAP same-month lock)",
    )
    ensure_dash.add_argument("--apply", action="store_true")

    sub.add_parser(
        "verify-dashboard",
        help="Validasi relation yang benar-benar dibaca Dashboard Harmony/ELHP",
    )

    audit = sub.add_parser("audit-source", help="Bandingkan row sumber, eligible, embedding, context, dan Gold")
    audit.add_argument("--source-group", required=True)

    audit_months = sub.add_parser(
        "audit-months",
        help="Bandingkan bulan Silver/alias, Context, Gold, dan aggregate dashboard",
    )
    audit_months.add_argument("--source-group", required=True)

    audit_dates = sub.add_parser(
        "audit-dates",
        help=(
            "Bandingkan tanggal filename, Context, Gold, dan OPH production RAW "
            "tanpa membaca BYTEA"
        ),
    )
    audit_dates.add_argument("--source-group", required=True)
    audit_dates.add_argument(
        "--run-id",
        help="Run tertentu; default run Gold aktif untuk source-group",
    )

    audit_oph = sub.add_parser(
        "audit-oph",
        help=(
            "Periksa OPH berulang dalam group, lintas group, lintas bulan, dan RAW"
        ),
    )
    audit_oph.add_argument("--source-group", required=True)
    audit_oph.add_argument(
        "--run-id",
        help="Run tertentu; default run Gold aktif untuk source-group",
    )

    audit_estates = sub.add_parser(
        "audit-estates",
        help="Pastikan DAN hanya PU* dan DWT hanya ME* pada dashboard/report",
    )
    audit_estates.add_argument(
        "--source-group",
        required=True,
        choices=["DAN", "DWT"],
    )

    clean_oph = sub.add_parser(
        "republish-clean-oph",
        help=(
            "Clone hasil VALIDATE ke run baru dan publish satu OPH satu usage "
            "tanpa mengulang MATCH/VALIDATE"
        ),
    )
    clean_oph.add_argument("--source-group", required=True)
    clean_oph.add_argument("--source-run-id", required=True)
    clean_oph.add_argument(
        "--visual-only",
        action="store_true",
        help=(
            "Publish seluruh visual VALIDATE tanpa Context/RAW recovery berat; "
            "legacy OPH hanya dipertahankan bila lolos SAP normalization policy, "
            "sisanya dibuat NULL"
        ),
    )
    clean_oph.add_argument(
        "--skip-context-sync",
        action="store_true",
        help=(
            "Lewati sync_context sebelum legacy OPH recovery. Gunakan hanya bila "
            "log percobaan sebelumnya sudah menunjukkan [CONTEXT] selesai/commit."
        ),
    )
    clean_oph.add_argument(
        "--apply",
        action="store_true",
        help="Terapkan; tanpa flag ini hanya preflight read-only",
    )

    consolidate = sub.add_parser(
        "consolidate-elhp",
        help="Satukan ELHP_DAN/ELHP_DWT ke source canonical tanpa menghapus gambar",
    )
    consolidate.add_argument("--source-group", required=True, choices=["DAN", "DWT"])
    consolidate.add_argument("--apply", action="store_true")

    repair_report_images = sub.add_parser(
        "repair-report-images",
        help=(
            "Pulihkan hanya image key pada CSV report ke targeted overlay sehat; "
            "tidak menyentuh processing.gambar_reused utama"
        ),
    )
    repair_report_images.add_argument(
        "--source-group",
        required=True,
        choices=["PWP", "DIN", "DAN", "DWT"],
    )
    repair_report_images.add_argument("--input-report", required=True)
    repair_report_images.add_argument(
        "--nas-root",
        action="append",
        required=True,
        help="Root NAS source-group; dapat diulang bila evidence tersebar di beberapa folder",
    )
    repair_report_images.add_argument("--batch-size", type=int, default=32)
    repair_report_images.add_argument(
        "--apply",
        action="store_true",
        help="Simpan ke processing.gambar_reused_targeted_recovery",
    )

    geo = sub.add_parser(
        "sync-geospatial",
        help="Import/sinkronkan boundary SHP/GeoJSON per company ke reference.geo_block_boundary",
    )
    geo.add_argument("--company", required=True, choices=["PWP", "DIN", "DAN", "DWT"])
    geo.add_argument(
        "--source-file",
        action="append",
        help="Path .shp/.geojson; dapat diulang. Jika kosong memakai GEO_<COMPANY>_BOUNDARY_FILE dari .env",
    )
    geo.add_argument("--estate-column")
    geo.add_argument("--afdeling-column")
    geo.add_argument("--block-column")
    geo.add_argument("--simplify-tolerance", type=float, default=0.00003)
    geo.add_argument(
        "--append",
        action="store_true",
        help="Jangan nonaktifkan boundary aktif company sebelum import",
    )
    geo.add_argument("--apply", action="store_true")
    geo.add_argument(
        "--skip-tph-points",
        action="store_true",
        help="Hanya import boundary; jangan refresh titik referensi TPH dari RAW transaction GPS",
    )

    whu_geo = sub.add_parser(
        "sync-whu-geospatial",
        help=(
            "Import boundary blok bundled DAN/DIN/DWT ke reference.geo_block_boundary; "
            "PWP tidak disentuh"
        ),
    )
    whu_geo.add_argument(
        "--company",
        action="append",
        choices=["DAN", "DIN", "DWT"],
        help="Opsional; dapat diulang. Default mengimpor DAN, DIN, DWT sekaligus.",
    )
    whu_geo.add_argument("--simplify-tolerance", type=float, default=0.00003)
    whu_geo.add_argument(
        "--sync-tph-points",
        action="store_true",
        help="Setelah boundary, refresh juga TPH anchor dari RAW GPS. Default tidak.",
    )
    whu_geo.add_argument("--apply", action="store_true")

    sync_sap_db60 = sub.add_parser(
        "sync-sap-db60",
        help="Copy/append SAP ZPAY dari DB60 langsung ke PostgreSQL Silver lalu refresh live SAP source",
    )
    sync_sap_db60.add_argument(
        "--through-month",
        required=True,
        help="Bulan akhir YYYY-MM. Loader mengulang watermark month agar bulan parsial tertutup.",
    )
    sync_sap_db60.add_argument(
        "--start-month",
        help="Opsional bulan awal YYYY-MM; default otomatis dari local watermark.",
    )
    sync_sap_db60.add_argument(
        "--source-group",
        default="ALL",
        help="PWP, DIN, DAN, DWT, atau ALL. ALL = seluruh row DB60 pada rentang bulan (default).",
    )
    sync_sap_db60.add_argument("--batch-size", type=int, default=10000)
    sync_sap_db60.add_argument(
        "--apply", action="store_true",
        help="Tulis INSERT-NEW langsung ke silver.zpay_harvesting_db60; ID existing di-skip. Tanpa flag hanya preflight.",
    )

    refresh_sap = sub.add_parser(
        "refresh-sap-kpi",
        help="Refresh audit/KPI SAP active Gold tanpa mengulang MATCH/VALIDATE",
    )
    refresh_sap.add_argument(
        "--source-group",
        default="ALL",
        help="PWP, DIN, DAN, DWT, atau ALL (default)",
    )

    audit_bytea = sub.add_parser(
        "audit-bytea",
        help="Baca checksum penuh semua BYTEA, isolasi TOAST korup, dan aktifkan karantina",
    )
    audit_bytea.add_argument("--source-group", required=True)
    audit_bytea.add_argument("--batch-size", type=int, default=256)
    audit_bytea.add_argument(
        "--report",
        help="Path CSV masalah; default bytea_audit_<source_group>.csv",
    )

    cleanup_recovery = sub.add_parser(
        "cleanup-legacy-recovery",
        help=(
            "Audit full-read processing.gambar_reused lalu hapus recovery "
            "v1/v2 jika seluruh evidence Gold sudah tersedia"
        ),
    )
    cleanup_recovery.add_argument(
        "--apply",
        action="store_true",
        help="Hapus tabel legacy; tanpa flag ini hanya preflight",
    )

    repair_image_storage = sub.add_parser(
        "repair-image-storage-index",
        help=(
            "Audit dan REINDEX processing.gambar_reused termasuk TOAST index; "
            "gunakan ketika muncul unexpected zero page / pg_toast index corrupt"
        ),
    )
    repair_image_storage.add_argument(
        "--apply",
        action="store_true",
        help="Jalankan REINDEX; tanpa flag ini hanya diagnosis read-only",
    )

    rebuild_hot_store = sub.add_parser(
        "rebuild-gambar-reused-storage",
        help=(
            "Salin BYTEA gambar_reused yang masih terbaca ke relation/TOAST baru, "
            "karantina row korup, lalu swap atomik tanpa menghapus tabel lama"
        ),
    )
    rebuild_hot_store.add_argument("--batch-size", type=int, default=128)
    rebuild_hot_store.add_argument(
        "--report",
        default="gambar_reused_storage_rebuild_skipped.csv",
        help="CSV row yang tidak dapat disalin karena BYTEA/TOAST rusak",
    )
    rebuild_hot_store.add_argument(
        "--apply",
        action="store_true",
        help="Salin dan swap; tanpa flag ini hanya preflight ukuran/count",
    )

    rebuild_silver = sub.add_parser(
        "rebuild-silver-storage",
        help=(
            "Salin row Silver dengan BYTEA sehat ke heap/TOAST baru, karantina "
            "row korup, lalu swap atomik tanpa menghapus source lama"
        ),
    )
    rebuild_silver.add_argument("--batch-size", type=int, default=64)
    rebuild_silver.add_argument(
        "--report",
        default="silver_storage_rebuild_recovery_needed.csv",
        help="CSV row Silver yang perlu dipulihkan kembali dari NAS",
    )
    rebuild_silver.add_argument(
        "--apply",
        action="store_true",
        help="Salin dan swap; tanpa flag ini hanya preflight ukuran/count",
    )

    reset_match = sub.add_parser(
        "reset-match-staging",
        help=(
            "Buat ulang storage candidate MATCH yang korup tanpa menghapus "
            "embedding, Context, validated result, Gold, atau BYTEA"
        ),
    )
    reset_match.add_argument(
        "--run-id",
        required=True,
        help="Run gagal yang akan dilanjutkan kembali dari stage MATCH",
    )
    reset_match.add_argument(
        "--apply",
        action="store_true",
        help="TRUNCATE staging candidate; tanpa flag ini hanya katalog/preflight",
    )

    load_silver = sub.add_parser(
        "load-silver",
        help=(
            "Muat ulang metadata+BYTEA folder ke silver.image_store_all_files "
            "secara streaming, resumable, dan deduplicated"
        ),
    )
    load_silver.add_argument("--source-group", required=True)
    load_silver.add_argument(
        "--source-root",
        action="append",
        required=True,
        help="Root folder sumber; dapat diulang untuk source-group yang sama",
    )
    load_silver.add_argument("--batch-size", type=int, default=64)
    load_silver.add_argument("--report")
    load_silver.add_argument(
        "--max-files",
        type=int,
        help="Batasi jumlah file untuk pilot; kosong berarti seluruh folder",
    )
    load_silver.add_argument(
        "--allow-low-disk",
        action="store_true",
        help="Lewati disk-space guard setelah pemeriksaan manual",
    )
    load_silver.add_argument(
        "--expected-month",
        help=(
            "Guard incremental YYYY-MM; file bulan lain/tanpa tanggal filename "
            "dilaporkan dan tidak dimuat"
        ),
    )
    load_silver.add_argument(
        "--input-report",
        help=(
            "CSV rebuild-silver-storage; hanya nama file pada CSV yang dicari "
            "dan dipulihkan dari NAS"
        ),
    )
    load_silver.add_argument(
        "--apply",
        action="store_true",
        help="Baca/decode dan tulis BYTEA; tanpa flag ini hanya preflight",
    )

    merge_month = sub.add_parser(
        "merge-month",
        help=(
            "Gabungkan satu bulan NAS secara incremental: load terfilter, Context, "
            "embedding baru, snapshot MATCH/VALIDATE/Gold, lalu gambar_reused"
        ),
    )
    merge_month.add_argument("--source-group", required=True)
    merge_month.add_argument(
        "--month",
        required=True,
        help="Bulan bisnis yang harus cocok dengan filename, contoh 2026-07",
    )
    merge_month.add_argument(
        "--source-root",
        action="append",
        required=True,
        help=(
            "Root NAS; boleh berisi banyak bulan karena filename disaring oleh --month"
        ),
    )
    merge_month.add_argument("--batch-size", type=int, default=64)
    merge_month.add_argument(
        "--raw-batch-size",
        type=int,
        default=2000,
        help="Batch commit DB45 -> RAW; default 2000 dan aman di-resume",
    )
    merge_month.add_argument(
        "--report-prefix",
        help="Prefix dua CSV: _silver.csv dan _gambar_reused.csv",
    )
    merge_month.add_argument("--run-id", help="Run ID lama bila melanjutkan stage")
    merge_month.add_argument(
        "--from-stage",
        choices=["embed", "match", "validate", "publish"],
        default="embed",
    )
    merge_month.add_argument(
        "--to-stage",
        choices=["embed", "match", "validate", "publish"],
        default="publish",
    )
    merge_month.add_argument("--max-files", type=int, help="Pilot saja")
    merge_month.add_argument(
        "--allow-low-disk",
        action="store_true",
        help="Lewati disk guard setelah tablespace/WAL diperiksa manual",
    )
    merge_month.add_argument(
        "--apply",
        action="store_true",
        help="Terapkan; tanpa flag ini hanya scan dan guard bulan",
    )
    merge_month.add_argument(
        "--skip-raw-sync",
        action="store_true",
        help="Lewati DB45 -> RAW hanya jika bulan tersebut sudah diaudit lengkap",
    )
    merge_month.add_argument(
        "--resume-after-silver",
        action="store_true",
        help=(
            "Lanjut setelah output SILVER LOAD DONE; lewati RAW sync dan scan NAS "
            "Silver, lalu mulai dari Context"
        ),
    )

    raw_month = sub.add_parser(
        "sync-raw-month",
        help=(
            "Lengkapi satu bulan transaksi DB45 ke raw.production_plantation "
            "secara batch, resumable, dan anti-duplikasi"
        ),
    )
    raw_month.add_argument(
        "--source-group",
        required=True,
        choices=["PWP", "DIN", "DAN", "DWT", "ALL"],
    )
    raw_month.add_argument(
        "--month", required=True, help="Bulan sumber YYYY-MM, contoh 2026-07"
    )
    raw_month.add_argument("--batch-size", type=int, default=2000)
    raw_month.add_argument("--report")
    raw_month.add_argument(
        "--apply",
        action="store_true",
        help="Tambahkan key RAW yang belum ada; tanpa flag ini hanya preflight",
    )

    transport_kt = sub.add_parser(
        "sync-transport-kt-before",
        help=(
            "Ambil FFB_EVIDENCE_KT_BEFORE MSSQL untuk seluruh OPH foto panen "
            "Harmony PWP/DIN, lalu muat file NAS ke gambar_reused secara batch"
        ),
    )
    transport_kt.add_argument(
        "--source-group", required=True, choices=["PWP", "DIN"],
    )
    transport_kt.add_argument("--year", type=int, default=2026)
    transport_kt.add_argument(
        "--nas-root",
        action="append",
        required=True,
        help="Folder Transport_PWP/Transport_DIN; dapat diulang bila ada beberapa root",
    )
    transport_kt.add_argument(
        "--report",
    )
    transport_kt.add_argument(
        "--batch-size",
        type=int,
        default=64,
        help="Jumlah BYTEA per commit (default 64; aman untuk RAM dan resume)",
    )
    transport_kt.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Baca ulang file yang sudah sehat; default dilewati agar job harian cepat",
    )
    transport_kt.add_argument(
        "--apply",
        action="store_true",
        help="Tulis metadata+BYTEA; tanpa flag ini hanya preflight",
    )

    panen_nas = sub.add_parser(
        "index-panen-nas",
        help=(
            "Index exact filename foto panen ke path NAS tanpa menyimpan BYTEA; "
            "dipakai dashboard KP vs KT untuk foto reused dan non-reused"
        ),
    )
    panen_nas.add_argument(
        "--source-group", required=True, choices=["PWP", "DIN"],
    )
    panen_nas.add_argument("--year", type=int, default=2026)
    panen_nas.add_argument(
        "--nas-root",
        action="append",
        required=True,
        help="Root NAS foto panen; dapat diulang bila ada beberapa root",
    )
    panen_nas.add_argument("--report")
    panen_nas.add_argument(
        "--apply",
        action="store_true",
        help="Simpan metadata path; tanpa flag ini scan dan audit read-only",
    )

    panen_nas_embed = sub.add_parser(
        "embed-panen-nas",
        help=(
            "Index seluruh foto KP Context ke NAS lalu pulihkan embedding KP "
            "yang tidak mempunyai BYTEA Silver"
        ),
    )
    panen_nas_embed.add_argument(
        "--source-group", required=True, choices=["PWP", "DIN"],
    )
    panen_nas_embed.add_argument("--year", type=int, default=2026)
    panen_nas_embed.add_argument("--batch-size", type=int, default=64)
    panen_nas_embed.add_argument(
        "--nas-root",
        action="append",
        required=True,
        help="Root NAS foto panen; dapat diulang bila ada beberapa root",
    )
    panen_nas_embed.add_argument("--report")
    panen_nas_embed.add_argument(
        "--apply",
        action="store_true",
        help="Simpan index NAS dan embedding; tanpa flag hanya audit index",
    )

    transport_score = sub.add_parser(
        "score-transport-kt-before",
        help=(
            "Hitung cosine similarity seluruh foto panen Harmony vs KT Before "
            "pada OPH yang sama memakai embedding full corpus"
        ),
    )
    transport_score.add_argument(
        "--source-group", required=True, choices=["PWP", "DIN"],
    )
    transport_score.add_argument("--year", type=int, default=2026)
    transport_score.add_argument("--batch-size", type=int, default=64)
    transport_score.add_argument(
        "--only-pending",
        action="store_true",
        help=(
            "Proses hanya pasangan tanpa skor aktif; skor lama tetap aktif "
            "dan tidak dihitung ulang"
        ),
    )
    transport_score.add_argument(
        "--report",
    )
    transport_score.add_argument(
        "--apply",
        action="store_true",
        help="Buat embedding KT yang belum ada dan simpan hasil similarity",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = Settings()
    settings.validate()

    if args.command == "init-db":
        init_database(settings)
        return 0
    if args.command == "run":
        with pipeline_job_lock(settings, f"run:{args.source_group.upper()}"):
            return _run_pipeline(args, settings)
    if args.command == "rebuild-company":
        with pipeline_job_lock(settings, f"rebuild:{args.source_group.upper()}"):
            # Preflight universal: samakan Context dengan flow transaksi lama dan
            # jangan membakar waktu GPU bila ada bulan source yang belum punya
            # minimal satu Context publishable (tanggal+estate).
            sync_context(settings, args.source_group)
            sync_raw_usage_context(settings, args.source_group, all_missing=True)
            month_audit = audit_month_coverage(settings, args.source_group)
            missing_context_months = month_audit.get(
                "missing_publishable_context_months", []
            )
            historical_missing_source = month_audit.get(
                "historical_months_missing_source", []
            )
            if historical_missing_source:
                print(
                    "FAILED preflight History: bulan pernah ada di Gold tetapi "
                    "hilang dari Embedding/Context="
                    + ", ".join(str(value) for value in historical_missing_source)
                )
                print(
                    "MATCH/VALIDATE tidak dijalankan. Pulihkan metadata/embedding "
                    "historis; BYTEA penuh tidak perlu dimuat ke Silver."
                )
                return 2
            if missing_context_months:
                print(
                    "FAILED preflight Context: bulan belum publishable="
                    + ", ".join(str(value) for value in missing_context_months)
                )
                print(
                    "MATCH/VALIDATE tidak dijalankan. Periksa bagian CONTEXT LINEAGE "
                    "pada audit di atas."
                )
                return 2
            rebuild_args = argparse.Namespace(
                source_group=args.source_group,
                run_id=None,
                # Context telah diselesaikan oleh preflight di atas.
                from_stage="embed",
                to_stage="publish",
            )
            result = _run_pipeline(rebuild_args, settings)
            if result == 0:
                audit_month_coverage(settings, args.source_group)
            return result
    if args.command == "sync-context":
        with pipeline_job_lock(settings, f"context:{args.source_group.upper()}"):
            sync_context(settings, args.source_group)
        return 0
    if args.command == "sync-raw-context":
        # Mulai dari sumber transaksi utama (setara download_report lama),
        # termasuk seluruh kolom evidence seperti FFB_EVIDENCE_KP/OUTPUT_FILE.
        # RAW, historical Gold dan SAP hanya fallback setelah jalur utama ini.
        with pipeline_job_lock(settings, f"raw-context:{args.source_group.upper()}"):
            sync_context(settings, args.source_group)
            sync_raw_usage_context(settings, args.source_group, all_missing=True)
            audit_month_coverage(settings, args.source_group)
        return 0
    if args.command == "enrich-gold-context":
        with pipeline_job_lock(settings, f"enrich:{args.source_group.upper()}"):
            enrich_active_gold_context(settings, args.source_group)
        return 0
    if args.command == "recover-elhp-personnel":
        with pipeline_job_lock(settings, f"personnel:{args.source_group.upper()}:{args.month}"):
            recover_elhp_personnel_month(settings, args.source_group, args.month)
        return 0
    if args.command == "export-monthly-trend-png":
        export_monthly_trend_png(
            month=args.month or None,
            year=args.year,
            output_dir=args.output_dir,
            kind=args.kind,
        )
        return 0
    if args.command == "company-guide":
        return _company_guide(settings, args.source_group)
    if args.command == "status":
        return _status(settings, args.limit)
    if args.command == "verify":
        return _verify(settings)
    if args.command == "ensure-dashboard-contract":
        return _ensure_dashboard_contract(settings, apply=bool(args.apply))
    if args.command == "verify-dashboard":
        return _verify_dashboard(settings)
    if args.command == "audit-source":
        result = audit_source(settings, args.source_group)
        print_source_audit(args.source_group, result)
        return 0
    if args.command == "audit-months":
        audit_month_coverage(settings, args.source_group)
        return 0
    if args.command == "audit-dates":
        audit_date_lineage(
            settings,
            args.source_group,
            run_id=args.run_id,
        )
        return 0
    if args.command == "audit-oph":
        audit_oph_uniqueness(
            settings,
            args.source_group,
            run_id=args.run_id,
        )
        return 0
    if args.command == "audit-estates":
        result = audit_elhp_estate_scope(settings, args.source_group)
        return 0 if result["passed"] else 1
    if args.command == "republish-clean-oph":
        if args.apply:
            with pipeline_job_lock(
                settings, f"republish-clean-oph:{args.source_group.upper()}"
            ):
                republish_clean_oph(
                    settings,
                    args.source_group,
                    args.source_run_id,
                    visual_only=args.visual_only,
                    skip_context_sync=args.skip_context_sync,
                    apply=True,
                )
        else:
            republish_clean_oph(
                settings,
                args.source_group,
                args.source_run_id,
                visual_only=args.visual_only,
                skip_context_sync=args.skip_context_sync,
                apply=False,
            )
        return 0
    if args.command == "consolidate-elhp":
        consolidate_elhp_source(settings, args.source_group, apply=args.apply)
        return 0
    if args.command == "repair-report-images":
        source_group = args.source_group.upper()
        with pipeline_job_lock(settings, f"repair-report-images:{source_group}"):
            result = recover_gold_evidence_from_folders(
                settings,
                source_group,
                nas_roots=[Path(value) for value in args.nas_root],
                input_report=Path(args.input_report),
                active_gold=False,
                batch_size=args.batch_size,
                apply=bool(args.apply),
                report_path=None,
                targeted_overlay=True,
            )
        print(
            f"[TARGETED REPORT RECOVERY] source={source_group} "
            f"targets={int(result.get('targets') or 0):,} "
            f"recovered={int(result.get('recovered') or 0):,} "
            f"remaining={int(result.get('remaining') or 0):,} "
            f"overlay=processing.gambar_reused_targeted_recovery"
        )
        return 0 if int(result.get("remaining") or 0) == 0 else 4
    if args.command == "sync-geospatial":
        with pipeline_job_lock(settings, f"sync-geospatial:{args.company.upper()}"):
            import_boundaries(
                settings,
                args.company,
                source_files=args.source_file,
                estate_column=args.estate_column,
                afdeling_column=args.afdeling_column,
                block_column=args.block_column,
                simplify_tolerance=args.simplify_tolerance,
                replace_company=not args.append,
                apply=bool(args.apply),
            )
            if not bool(args.skip_tph_points):
                sync_tph_points(settings, args.company, apply=bool(args.apply))
        return 0
    if args.command == "sync-whu-geospatial":
        companies = args.company or ["DAN", "DIN", "DWT"]
        lock_name = "sync-whu-geospatial:" + ",".join(companies)
        with pipeline_job_lock(settings, lock_name):
            sync_bundled_whu_boundaries(
                settings,
                companies=companies,
                simplify_tolerance=args.simplify_tolerance,
                apply=bool(args.apply),
                sync_tph=bool(args.sync_tph_points),
            )
        return 0
    if args.command == "sync-sap-db60":
        with pipeline_job_lock(settings, f"sync-sap-db60:{args.source_group.upper()}"):
            sync_db60_zpay(
                settings,
                through_month=args.through_month,
                source_group=args.source_group,
                start_month=args.start_month,
                batch_size=args.batch_size,
                apply=bool(args.apply),
            )
        return 0
    if args.command == "refresh-sap-kpi":
        with connection(settings) as conn:
            if not table_exists(conn, settings.sap_schema, settings.sap_table):
                raise RuntimeError(
                    f"SAP live source {settings.sap_schema}.{settings.sap_table} belum tersedia. "
                    "Jalankan: py run_pipeline.py sync-sap-db60 --through-month YYYY-MM --source-group ALL --apply"
                )
        with pipeline_job_lock(settings, f"refresh-sap:{args.source_group.upper()}"):
            refresh_active_sap_kpi(settings, args.source_group)
        return 0
    if args.command == "audit-bytea":
        audit_bytea_health(
            settings,
            args.source_group,
            batch_size=args.batch_size,
            report_path=args.report,
        )
        return 0
    if args.command == "cleanup-legacy-recovery":
        if args.apply:
            with pipeline_job_lock(settings, "cleanup-legacy-recovery"):
                cleanup_legacy_recovery(settings, apply=True)
        else:
            cleanup_legacy_recovery(settings, apply=False)
        return 0
    if args.command == "repair-image-storage-index":
        if args.apply:
            with pipeline_job_lock(settings, "repair-image-storage-index"):
                repair_gambar_reused_indexes(settings, apply=True)
        else:
            repair_gambar_reused_indexes(settings, apply=False)
        return 0
    if args.command == "rebuild-gambar-reused-storage":
        if args.apply:
            with pipeline_job_lock(settings, "rebuild-gambar-reused-storage"):
                rebuild_gambar_reused_storage(
                    settings,
                    batch_size=args.batch_size,
                    report_path=args.report,
                    apply=True,
                )
        else:
            rebuild_gambar_reused_storage(
                settings,
                batch_size=args.batch_size,
                report_path=args.report,
                apply=False,
            )
        return 0
    if args.command == "rebuild-silver-storage":
        if args.apply:
            with pipeline_job_lock(settings, "rebuild-silver-storage"):
                rebuild_silver_storage(
                    settings,
                    batch_size=args.batch_size,
                    report_path=args.report,
                    apply=True,
                )
        else:
            rebuild_silver_storage(
                settings,
                batch_size=args.batch_size,
                report_path=args.report,
                apply=False,
            )
        return 0
    if args.command == "reset-match-staging":
        if args.apply:
            with pipeline_job_lock(settings, "reset-match-staging"):
                reset_match_staging(settings, args.run_id, apply=True)
        else:
            reset_match_staging(settings, args.run_id, apply=False)
        return 0
    if args.command == "merge-month":
        with pipeline_job_lock(
            settings,
            f"merge-month:{args.source_group.upper()}:{args.month}",
        ):
            return _merge_month(args, settings)
    if args.command == "sync-raw-month":
        if args.apply:
            with pipeline_job_lock(
                settings,
                f"sync-raw-month:{args.source_group.upper()}:{args.month}",
            ):
                sync_raw_production_month(
                    settings,
                    args.source_group,
                    args.month,
                    batch_size=args.batch_size,
                    apply=True,
                    report_path=args.report,
                )
        else:
            sync_raw_production_month(
                settings,
                args.source_group,
                args.month,
                batch_size=args.batch_size,
                apply=False,
                report_path=args.report,
            )
        return 0
    if args.command == "load-silver":
        with pipeline_job_lock(settings, f"load-silver:{args.source_group.upper()}"):
            load_silver_from_folders(
                settings,
                args.source_group,
                args.source_root,
                batch_size=args.batch_size,
                report_path=args.report,
                apply=args.apply,
                max_files=args.max_files,
                allow_low_disk=args.allow_low_disk,
                expected_month=args.expected_month,
                input_report_path=args.input_report,
            )
        return 0
    if args.command == "sync-transport-kt-before":
        if args.apply:
            with pipeline_job_lock(
                settings,
                f"sync-transport-kt-before:{args.source_group.upper()}",
                lock_scope=(
                    f"harmony-transaction:{args.source_group.upper()}:{args.year}"
                ),
            ):
                sync_transport_kt_before(
                    settings,
                    args.source_group,
                    args.year,
                    args.nas_root,
                    apply=True,
                    refresh_existing=args.refresh_existing,
                    report_path=args.report,
                    batch_size=args.batch_size,
                )
        else:
            sync_transport_kt_before(
                settings,
                args.source_group,
                args.year,
                args.nas_root,
                apply=False,
                refresh_existing=args.refresh_existing,
                report_path=args.report,
                batch_size=args.batch_size,
            )
        return 0
    if args.command == "index-panen-nas":
        with pipeline_job_lock(
            settings,
            f"index-panen-nas:{args.source_group.upper()}:{args.year}",
        ):
            index_panen_nas_paths(
                settings,
                args.source_group,
                args.year,
                args.nas_root,
                apply=args.apply,
                report_path=args.report,
            )
        return 0
    if args.command == "embed-panen-nas":
        with pipeline_job_lock(
            settings,
            f"embed-panen-nas:{args.source_group.upper()}:{args.year}",
            lock_scope=(
                f"harmony-transaction:{args.source_group.upper()}:{args.year}"
            ),
        ):
            index_metrics = index_panen_nas_paths(
                settings,
                args.source_group,
                args.year,
                args.nas_root,
                apply=args.apply,
                report_path=args.report,
            )
            if not args.apply:
                print(
                    "DRY-RUN: index diperiksa; tambahkan --apply untuk "
                    "membuat embedding KP dari NAS."
                )
                return 0
            embed_metrics = embed_panen_nas_missing(
                settings,
                args.source_group,
                args.year,
                batch_size=args.batch_size,
            )
            print(
                "[EMBED NAS SUMMARY] "
                f"index_ready={int(index_metrics.get('ready') or 0):,} "
                f"index_unresolved={int(index_metrics.get('unresolved') or 0):,} "
                f"unnamed_context="
                f"{int(index_metrics.get('unnamed_context_rows') or 0):,} "
                f"embedded_ok={int(embed_metrics.get('embedded_ok') or 0):,} "
                f"embedded_failed={int(embed_metrics.get('embedded_failed') or 0):,} "
                f"device={embed_metrics.get('device')}"
            )
        return 0
    if args.command == "score-transport-kt-before":
        if args.apply:
            with pipeline_job_lock(
                settings,
                f"score-transport-kt-before:{args.source_group.upper()}",
                lock_scope=(
                    f"harmony-transaction:{args.source_group.upper()}:{args.year}"
                ),
            ):
                score_transport_kt_before(
                    settings,
                    args.source_group,
                    args.year,
                    batch_size=args.batch_size,
                    apply=True,
                    only_pending=args.only_pending,
                    report_path=args.report,
                )
        else:
            score_transport_kt_before(
                settings,
                args.source_group,
                args.year,
                batch_size=args.batch_size,
                apply=False,
                only_pending=args.only_pending,
                report_path=args.report,
            )
        return 0
    parser.error("Perintah tidak dikenal")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
