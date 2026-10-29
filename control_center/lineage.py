from __future__ import annotations

import os

from .config import CONFIG


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _table(schema_env: str, table_env: str, schema_default: str, table_default: str) -> str:
    schema = _env(schema_env, schema_default)
    table = _env(table_env, table_default)
    return f"{schema}.{table}"


def build_lineage_model() -> dict:
    business_table = _table("SILVER_SCHEMA", "BUSINESS_TABLE", "silver", "production_plantation_clean")
    sap_table = _table("SAP_KPI_SCHEMA", "SAP_KPI_TABLE", "silver", "v_zpay_harvesting_live")
    raw_zpay = _table("DB60_ZPAY_TARGET_SCHEMA", "DB60_ZPAY_TARGET_TABLE", "raw", "zpay_harvesting")
    image_table = _table("SILVER_SCHEMA", "IMAGE_TABLE", "silver", "image_store_all_files")
    raw_business = _table("RAW_BUSINESS_SCHEMA", "RAW_BUSINESS_TABLE", "raw", "production_plantation")
    geo_boundary = _table("REFERENCE_SCHEMA", "TABLE_GEO_BOUNDARY", "reference", "geo_block_boundary")
    mssql_transport = _env("MSSQL_TRANSPORT_TABLE", "dbo.PRODUCTION_PLANTATION")
    mssql_raw = _env("MSSQL_RAW_TABLE", "dbo.PRODUCTION_PLANTATION")

    panen_index = _table("RECOVERY_SCHEMA", "TABLE_PANEN_NAS_INDEX", "processing", "panen_nas_path_index")
    kt_context = _table("RECOVERY_SCHEMA", "TABLE_TRANSPORT_KT_CONTEXT", "processing", "transport_kt_before_context")
    kt_similarity = _table("RECOVERY_SCHEMA", "TABLE_TRANSPORT_KT_SIMILARITY", "processing", "transport_kt_similarity")
    gambar_reused = _table("RECOVERY_SCHEMA", "TABLE_BYTEA_RECOVERY", "processing", "gambar_reused")

    reused_group = _table("APP_SCHEMA", "TABLE_GROUP_CLEANED", "gold", "reused_photo_group_summary_cleaned")
    reused_detail = _table("APP_SCHEMA", "TABLE_DETAIL_TRANSACTION", "gold", "reused_photo_detail_transaction")
    reused_monthly = _table("APP_SCHEMA", "TABLE_DASHBOARD_MONTHLY", "gold", "reused_photo_dashboard_monthly_cleaned")
    sap_kpi = _table("APP_SCHEMA", "TABLE_SAP_KPI_SUMMARY", "gold", "sap_oph_kpi_summary")
    transport_status = _table("APP_SCHEMA", "TABLE_TRANSPORT_STATUS", "gold", "reused_photo_transport_status_summary")
    elhp_group = _table("APP_SCHEMA", "TABLE_ELHP_GROUP_CLEANED", "gold", "elhp_gt_group_summary_cleaned")
    elhp_detail = _table("APP_SCHEMA", "TABLE_ELHP_DETAIL_TRANSACTION", "gold", "elhp_gt_detail_transaction")
    elhp_monthly = _table("APP_SCHEMA", "TABLE_ELHP_DASHBOARD_MONTHLY", "gold", "elhp_dashboard_monthly_cleaned")
    elhp_transport = _table("APP_SCHEMA", "TABLE_ELHP_TRANSPORT_STATUS", "gold", "elhp_gt_transport_status_summary")

    systems = [
        {
            "id": "sqlserver",
            "title": "Microsoft SQL Server / DASHBOARD",
            "kind": "source-db",
            "engine": "SQL Server",
            "database": _env("MSSQL_DATABASE", "DASHBOARD"),
            "description": "Sumber transaksi bulanan dan metadata transport KT Before yang masih berada di SQL Server.",
            "tables": [
                {"name": mssql_raw, "role": "Mirror transaksi bulanan ke PostgreSQL raw"},
                {"name": mssql_transport, "role": "Metadata dan evidence Foto Transport (KT Before)"},
            ],
        },
        {
            "id": "sap_db60",
            "title": "SAP DB60 / ZPAY Harvesting",
            "kind": "source-db",
            "engine": "Microsoft SQL Server",
            "database": _env("DB60_ZPAY_DATABASE", "db_audit"),
            "description": "Sumber otoritatif SAP harvesting. Control Center melakukan catch-up ke PostgreSQL sebelum publish/KPI.",
            "tables": [
                {"name": _env("DB60_ZPAY_TABLE", "db_audit.bronze.v_zpay_harvesting"), "role": "Source OPH / PRFNR / BUDAT"},
            ],
        },
        {
            "id": "postgres_silver",
            "title": "PostgreSQL / golden_eye / Silver",
            "kind": "source-db",
            "engine": "PostgreSQL",
            "database": _env("PG_DATABASE", "golden_eye"),
            "description": "Landing zone utama Golden Eye untuk transaksi bisnis, SAP, dan katalog gambar.",
            "tables": [
                {"name": business_table, "role": "Transaksi bisnis / context utama panen"},
                {"name": sap_table, "role": "Baseline SAP untuk KPI denominator dan coverage"},
                {"name": image_table, "role": "Katalog file gambar / image store"},
            ],
        },
        {
            "id": "postgres_raw",
            "title": "PostgreSQL / golden_eye / Raw",
            "kind": "source-db",
            "engine": "PostgreSQL",
            "database": _env("PG_DATABASE", "golden_eye"),
            "description": "Fallback raw business table dan mirror hasil tarik bulanan dari SQL Server.",
            "tables": [
                {"name": raw_business, "role": "Fallback metadata & raw mirror bulanan"},
                {"name": raw_zpay, "role": "Mirror SAP ZPAY hasil catch-up dari DB60"},
            ],
        },
        {
            "id": "nas",
            "title": "NAS / Folder Evidence",
            "kind": "filesystem",
            "engine": "Filesystem",
            "database": "Network share",
            "description": "Sumber file fisik Foto Panen dan Foto Transport yang di-index tanpa scan ke dashboard.",
            "tables": [
                {"name": "PANEN ROOT", "role": "; ".join([f"{c}={', '.join(CONFIG.panen_roots(c)) or '-'}" for c in ("PWP", "DIN", "DAN", "DWT")])},
                {"name": "TRANSPORT ROOT", "role": "; ".join([f"{c}={', '.join(CONFIG.transport_roots(c)) or '-'}" for c in ("PWP", "DIN")])},
            ],
        },
        {
            "id": "postgres_reference",
            "title": "PostgreSQL / golden_eye / Geospatial Reference",
            "kind": "serve-db",
            "engine": "PostgreSQL + GeoJSON",
            "database": _env("PG_DATABASE", "golden_eye"),
            "description": "Canonical boundary Estate/Afdeling/Blok hasil import SHP/GeoJSON; dashboard tidak membaca SHP langsung.",
            "tables": [
                {"name": geo_boundary, "role": "Boundary canonical per block untuk Geospatial Audit"},
            ],
        },
        {
            "id": "postgres_processing",
            "title": "PostgreSQL / golden_eye / Processing",
            "kind": "working-db",
            "engine": "PostgreSQL",
            "database": _env("PG_DATABASE", "golden_eye"),
            "description": "Area kerja pipeline untuk index NAS, context transport, similarity, dan recovery gambar.",
            "tables": [
                {"name": panen_index, "role": "Index path Foto Panen dari NAS"},
                {"name": kt_context, "role": "Context Foto Transport KT Before"},
                {"name": kt_similarity, "role": "Skor similarity Panen vs Transport"},
                {"name": gambar_reused, "role": "Hot storage / recovery BYTEA untuk report"},
            ],
        },
        {
            "id": "postgres_gold",
            "title": "PostgreSQL / golden_eye / Gold & Dashboard",
            "kind": "serve-db",
            "engine": "PostgreSQL",
            "database": _env("PG_DATABASE", "golden_eye"),
            "description": "Output final yang dibaca dashboard analytics dan report PDF.",
            "tables": [
                {"name": reused_group, "role": "Summary reused Harmony"},
                {"name": reused_detail, "role": "Detail transaksi reused Harmony"},
                {"name": reused_monthly, "role": "Agregat monthly reused Harmony"},
                {"name": sap_kpi, "role": "Snapshot KPI SAP / Foto Panen / Similarity"},
                {"name": transport_status, "role": "Summary Panen vs Transport Harmony"},
                {"name": elhp_group, "role": "Summary reused ELHP"},
                {"name": elhp_detail, "role": "Detail transaksi reused ELHP"},
                {"name": elhp_monthly, "role": "Agregat monthly ELHP"},
                {"name": elhp_transport, "role": "Summary transport ELHP bila tersedia"},
            ],
        },
    ]

    pipelines = [
        {
            "id": "sap_catchup",
            "title": "0. SAP DB60 → Silver PostgreSQL",
            "description": "Catch-up watermark SAP sebelum pipeline bulanan agar denominator dan exact OPH gate tidak membaca snapshot lokal yang tertinggal.",
            "steps": [
                {
                    "name": "Catch-up SAP ZPAY",
                    "command": "py run_pipeline.py sync-sap-db60 --through-month YYYY-MM --source-group ALL --apply",
                    "reads": [_env("DB60_ZPAY_TABLE", "db_audit.bronze.v_zpay_harvesting")],
                    "writes": [raw_zpay, sap_table],
                },
            ],
        },
        {
            "id": "data_reused",
            "title": "1. Data Pull + Reused Detection",
            "description": "Untuk PWP / DIN / DAN / DWT. Menarik data bulanan, merge image evidence, deteksi reused, lalu publish Gold.",
            "steps": [
                {
                    "name": "Mirror / prepare monthly source",
                    "command": "py run_pipeline.py merge-month --source-group <COMPANY> --month YYYY-MM --from-stage embed --to-stage publish --apply",
                    "reads": ["SQL Server monthly source", business_table, raw_business, image_table, "NAS Panen Root"],
                    "writes": [gambar_reused, reused_group, reused_detail, reused_monthly, elhp_group, elhp_detail, elhp_monthly],
                },
            ],
        },
        {
            "id": "transport",
            "title": "2. Panen vs Transport Validation",
            "description": "Khusus Harmony PWP dan DIN. Mengaitkan Foto Panen terhadap KT Before / transport lalu menilai SAMA vs BERBEDA.",
            "steps": [
                {
                    "name": "Index Foto Panen NAS",
                    "command": "py run_pipeline.py index-panen-nas --source-group <PWP|DIN> --year YYYY --apply",
                    "reads": ["NAS Panen Root"],
                    "writes": [panen_index],
                },
                {
                    "name": "Sync Transport KT Before",
                    "command": "py run_pipeline.py sync-transport-kt-before --source-group <PWP|DIN> --year YYYY --apply",
                    "reads": [mssql_transport, "NAS Transport Root"],
                    "writes": [kt_context],
                },
                {
                    "name": "Prepare Embedding Panen",
                    "command": "py run_pipeline.py embed-panen-nas --source-group <PWP|DIN> --year YYYY --apply",
                    "reads": [panen_index],
                    "writes": ["embedding cache internal"],
                },
                {
                    "name": "Score Panen vs Transport",
                    "command": "py run_pipeline.py score-transport-kt-before --source-group <PWP|DIN> --year YYYY --only-pending --apply",
                    "reads": [business_table, panen_index, kt_context],
                    "writes": [kt_similarity, transport_status],
                },
            ],
        },
        {
            "id": "publish_report",
            "title": "3. KPI Snapshot + Verify + Report",
            "description": "Menutup siklus bulanan dengan refresh denominator SAP, verify gold, dan generate report PDF.",
            "steps": [
                {
                    "name": "Refresh SAP KPI",
                    "command": "py run_pipeline.py refresh-sap-kpi --source-group ALL",
                    "reads": [sap_table, reused_detail, transport_status],
                    "writes": [sap_kpi],
                },
                {
                    "name": "Verify Gold",
                    "command": "py run_pipeline.py verify",
                    "reads": [reused_group, reused_detail, transport_status],
                    "writes": ["verification status / log"],
                },
                {
                    "name": "Generate Report Transport",
                    "command": "py dashboard/generate_transport_validation_report.py --company <ALL|PWP|DIN> --year YYYY",
                    "reads": [sap_kpi, transport_status, kt_similarity, gambar_reused],
                    "writes": ["PDF Panen vs Transport"],
                },
                {
                    "name": "Generate Reused Bulletin",
                    "command": "py dashboard/generate_pdf_reports.py --system <ALL|HARMONY|ELHP> --company <COMPANY>",
                    "reads": [reused_group, reused_detail, reused_monthly, gambar_reused],
                    "writes": ["PDF Reused Bulletin"],
                },
            ],
        },
    ]

    graph = {
        "width": 1540,
        "height": 900,
        "lanes": [
            {"x": 20, "y": 20, "w": 300, "h": 840, "title": "UPSTREAM / SOURCE"},
            {"x": 340, "y": 20, "w": 500, "h": 840, "title": "1 · REUSED PHOTO - ALL PT"},
            {"x": 860, "y": 20, "w": 340, "h": 840, "title": "2 · PANEN VS TRANSPORT"},
            {"x": 1220, "y": 20, "w": 300, "h": 840, "title": "3 · ALIGN / GOLD / REPORT"},
        ],
        "nodes": [
            {"id": "scrapeops", "x": 55, "y": 85, "w": 230, "h": 78, "type": "external", "title": "ScrapeOps / Downloader", "subtitle": "External acquisition → NAS", "health": "scrapeops"},
            {"id": "mssql", "x": 55, "y": 195, "w": 230, "h": 74, "type": "db", "title": "SQL Server DASHBOARD", "subtitle": mssql_raw, "health": "mssql"},
            {"id": "sap_db60", "x": 55, "y": 300, "w": 230, "h": 82, "type": "db", "title": "SAP DB60 / ZPAY", "subtitle": _env("DB60_ZPAY_TABLE", "db_audit.bronze.v_zpay_harvesting"), "health": "sap_db60", "step_keys": ["sync_sap_db60"]},
            {"id": "nas", "x": 55, "y": 420, "w": 230, "h": 76, "type": "storage", "title": "NAS Evidence", "subtitle": "PWP · DIN · DAN · DWT", "health": "nas"},
            {"id": "pg_source", "x": 55, "y": 535, "w": 230, "h": 105, "type": "db", "title": "PostgreSQL RAW / SILVER", "subtitle": f"{raw_business} · {raw_zpay} · {image_table}", "health": "postgres"},
            {"id": "geo_master", "x": 55, "y": 690, "w": 230, "h": 82, "type": "gold", "title": "Geospatial Master", "subtitle": geo_boundary, "health": "geospatial"},

            {"id": "reused_pwp", "x": 380, "y": 90, "w": 190, "h": 76, "type": "process", "title": "Reused Photo PWP", "subtitle": "new image → embed → historical match", "step_keys": ["merge_month_pwp"]},
            {"id": "reused_din", "x": 380, "y": 205, "w": 190, "h": 76, "type": "process", "title": "Reused Photo DIN", "subtitle": "new image → embed → historical match", "step_keys": ["merge_month_din"]},
            {"id": "reused_dan", "x": 380, "y": 320, "w": 190, "h": 76, "type": "process", "title": "Reused Photo DAN", "subtitle": "new image → embed → historical match", "step_keys": ["merge_month_dan"]},
            {"id": "reused_dwt", "x": 380, "y": 435, "w": 190, "h": 76, "type": "process", "title": "Reused Photo DWT", "subtitle": "new image → embed → historical match", "step_keys": ["merge_month_dwt"]},
            {"id": "reused_engine", "x": 610, "y": 235, "w": 190, "h": 150, "type": "working", "title": "Reused Engine", "subtitle": "image_context → image_feature → MATCH historical → GPU VALIDATE", "table": "processing.image_context / processing.image_feature", "step_keys": ["merge_month_pwp", "merge_month_din", "merge_month_dan", "merge_month_dwt"]},
            {"id": "gold_reused", "x": 610, "y": 485, "w": 190, "h": 105, "type": "gold", "title": "Gold Reused", "subtitle": "Harmony + ELHP reused outputs", "table": reused_detail, "step_keys": ["merge_month_pwp", "merge_month_din", "merge_month_dan", "merge_month_dwt"]},

            {"id": "panen_index_pwp", "x": 895, "y": 105, "w": 130, "h": 74, "type": "working", "title": "PWP Panen Index", "subtitle": panen_index, "step_keys": ["panen_index_pwp"]},
            {"id": "transport_pwp", "x": 1045, "y": 105, "w": 130, "h": 74, "type": "working", "title": "PWP Transport", "subtitle": kt_context, "step_keys": ["transport_sync_pwp"]},
            {"id": "score_pwp", "x": 970, "y": 220, "w": 150, "h": 82, "type": "process", "title": "PWP Similarity", "subtitle": "embed + score SAMA / BERBEDA", "step_keys": ["panen_embed_pwp", "transport_score_pwp"]},

            {"id": "panen_index_din", "x": 895, "y": 390, "w": 130, "h": 74, "type": "working", "title": "DIN Panen Index", "subtitle": panen_index, "step_keys": ["panen_index_din"]},
            {"id": "transport_din", "x": 1045, "y": 390, "w": 130, "h": 74, "type": "working", "title": "DIN Transport", "subtitle": kt_context, "step_keys": ["transport_sync_din"]},
            {"id": "score_din", "x": 970, "y": 505, "w": 150, "h": 82, "type": "process", "title": "DIN Similarity", "subtitle": "mulai Mar 2026 · SAMA / BERBEDA", "step_keys": ["panen_embed_din", "transport_score_din"]},
            {"id": "transport_result", "x": 950, "y": 675, "w": 190, "h": 95, "type": "gold", "title": "Transport Similarity Result", "subtitle": kt_similarity, "table": kt_similarity, "step_keys": ["transport_score_pwp", "transport_score_din"]},

            {"id": "sap", "x": 1255, "y": 110, "w": 225, "h": 78, "type": "db", "title": "SAP Local Live", "subtitle": sap_table, "health": "postgres", "step_keys": ["sync_sap_db60", "refresh_sap_kpi"]},
            {"id": "align", "x": 1255, "y": 285, "w": 225, "h": 115, "type": "process", "title": "Align & Reconcile", "subtitle": "Reused + Panen/Transport + SAP scope", "step_keys": ["refresh_sap_kpi", "verify_gold"]},
            {"id": "gold_final", "x": 1255, "y": 500, "w": 225, "h": 108, "type": "gold", "title": "Gold Final / KPI", "subtitle": f"{sap_kpi} · {transport_status}"},
            {"id": "output", "x": 1255, "y": 710, "w": 225, "h": 92, "type": "output", "title": "Dashboard + PDF Report", "subtitle": "Analytics 8050 · Control 8060 · Reports", "step_keys": ["report_transport", "report_reused"]},
        ],
        "edges": [
            {"from": "scrapeops", "to": "nas", "label": "download images"},
            {"from": "mssql", "to": "pg_source", "label": "monthly metadata"},
            {"from": "sap_db60", "to": "pg_source", "label": "copy ZPAY"},
            {"from": "pg_source", "to": "sap", "label": "silver mirror → live view"},
            {"from": "nas", "to": "pg_source", "label": "register new month"},
            {"from": "pg_source", "to": "reused_pwp"},
            {"from": "pg_source", "to": "reused_din"},
            {"from": "pg_source", "to": "reused_dan"},
            {"from": "pg_source", "to": "reused_dwt"},
            {"from": "reused_pwp", "to": "reused_engine"},
            {"from": "reused_din", "to": "reused_engine"},
            {"from": "reused_dan", "to": "reused_engine"},
            {"from": "reused_dwt", "to": "reused_engine"},
            {"from": "reused_engine", "to": "gold_reused", "label": "publish reused"},
            {"from": "nas", "to": "panen_index_pwp"},
            {"from": "mssql", "to": "transport_pwp"},
            {"from": "panen_index_pwp", "to": "score_pwp"},
            {"from": "transport_pwp", "to": "score_pwp"},
            {"from": "nas", "to": "panen_index_din"},
            {"from": "mssql", "to": "transport_din"},
            {"from": "panen_index_din", "to": "score_din"},
            {"from": "transport_din", "to": "score_din"},
            {"from": "score_pwp", "to": "transport_result"},
            {"from": "geo_master", "to": "output", "label": "block boundary"},
            {"from": "score_din", "to": "transport_result"},
            {"from": "gold_reused", "to": "align"},
            {"from": "transport_result", "to": "align"},
            {"from": "sap", "to": "align"},
            {"from": "align", "to": "gold_final", "label": "reconcile / verify"},
            {"from": "gold_final", "to": "output", "label": "serve"},
        ],
    }

    trace_rules = [
        "Control Center hanya menampilkan lineage dan jalur eksekusi; bukan tempat memodifikasi nama tabel secara manual.",
        "Nama DB dan tabel diambil dari konfigurasi .env yang aktif, sehingga visual ini mengikuti environment sebenarnya.",
        "Setiap job Control Center tetap memakai command pipeline existing sebagai source of truth; visual ini hanya memperjelas trace input-output.",
        "Job Reused Photo menyimpan checkpoint RAW/Silver/run_id/stage. Jika worker terputus, Control Center melanjutkan run yang sama dan tidak mengulang load yang sudah committed.",
    ]

    return {
        "systems": systems,
        "pipelines": pipelines,
        "trace_rules": trace_rules,
        "graph": graph,
    }
