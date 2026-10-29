from __future__ import annotations

import ast
import importlib.util
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from golden_eye.utils import (
    UnionFind,
    filename_has_excluded_token,
    group_signature,
    normalize_oph,
    normalized_filename,
    parse_date_from_filename,
)
from golden_eye.transport_sync import (
    extract_evidence_file_names,
    transport_file_uid,
)
from golden_eye.raw_monthly_sync import parse_month, source_fingerprint
from golden_eye.config import Settings


def _load_dashboard_db_module():
    path = ROOT / "dashboard" / "db.py"
    spec = importlib.util.spec_from_file_location("golden_eye_dashboard_db_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_dashboard_diagnose_module():
    path = ROOT / "dashboard_diagnose.py"
    spec = importlib.util.spec_from_file_location(
        "golden_eye_dashboard_diagnose_test", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_dashboard_queries_module():
    dashboard_dir = str(ROOT / "dashboard")
    sys.path.insert(0, dashboard_dir)
    try:
        path = ROOT / "dashboard" / "queries.py"
        spec = importlib.util.spec_from_file_location(
            "golden_eye_dashboard_queries_test", path
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(dashboard_dir)


def test_filename_normalization_removes_old_pipeline_prefixes() -> None:
    assert normalized_filename(r"C:\foto\before_after_before_IMG_01.jpg") == "IMG_01.jpg"
    assert normalized_filename("after-before-IMG_02.jpg") == "IMG_02.jpg"


def test_oph_normalization_is_format_tolerant() -> None:
    assert normalize_oph("260129039001.003.I0442.896") == "260129039001003I0442896"


def test_transport_kt_evidence_supports_windows_path_url_and_key() -> None:
    assert extract_evidence_file_names(
        r"I:\Transport_PWP\KT_260101001.jpg"
    ) == ["KT_260101001.jpg"]
    assert extract_evidence_file_names(
        "https://internal/images/KT_260101002.jpg?token=x"
    ) == ["KT_260101002.jpg"]
    assert extract_evidence_file_names("KT_260101003") == ["KT_260101003.jpg"]


def test_transport_kt_hot_uid_deduplicates_same_file_across_oph() -> None:
    assert transport_file_uid("PWP", "same.jpg") == transport_file_uid(
        "pwp", "SAME.JPG"
    )


def test_raw_month_parser_and_fingerprint_are_stable() -> None:
    year, month, start, end = parse_month("2026-07")
    assert (year, month, start.isoformat(), end.isoformat()) == (
        2026,
        7,
        "2026-07-01",
        "2026-08-01",
    )
    left = source_fingerprint(
        "dbo.PRODUCTION_PLANTATION",
        ["COMPANY_CODE", "HARVEST_TICKET", "PERIOD_DATE"],
        ["PWP", "260701001", start],
    )
    right = source_fingerprint(
        "dbo.PRODUCTION_PLANTATION",
        ["COMPANY_CODE", "HARVEST_TICKET", "PERIOD_DATE"],
        ["PWP", "260701001", start],
    )
    assert left == right
    assert len(left) == 64



def test_pwp_sap_scope_hard_guard_excludes_kemitraan() -> None:
    settings = Settings(
        sap_prfnr_map=(
            "PWP=BUKIT PENDULANGAN 1|BUKIT PENDULANGAN 2|"
            "BUKIT PENDULANGAN 3|KEMITRAAN"
        ),
        sap_estate_scope_map="",
    )
    assert settings.sap_prfnr_values("PWP") == (
        "BUKIT PENDULANGAN 1",
        "BUKIT PENDULANGAN 2",
        "BUKIT PENDULANGAN 3",
    )
    assert "KEMITRAAN" not in settings.sap_prfnr_values("PWP")


def test_sap_estate_scope_map_is_authoritative_when_configured() -> None:
    settings = Settings(
        sap_prfnr_map="DIN=LONG KEJIAK 1|LONG KEJIAK 2|OUTSIDE UNIT",
        sap_estate_scope_map="DIN=LONG KEJIAK 1|LONG KEJIAK 2",
    )
    assert settings.sap_prfnr_values("DIN") == (
        "LONG KEJIAK 1",
        "LONG KEJIAK 2",
    )


def test_all_golden_eye_companies_have_hard_safe_sap_scope() -> None:
    settings = Settings(
        sap_prfnr_map=(
            "PWP=BUKIT PENDULANGAN 1|KEMITRAAN;"
            "DIN=LONG KEJIAK 1|OUTSIDE DIN;"
            "DAN=PUHUS 1|KEMITRAAN DAN;"
            "DWT=MELENYU 1|KEMITRAAN DWT"
        ),
        # Simulasi .env lama: hanya PWP yang sudah pernah dikunci.
        sap_estate_scope_map="PWP=BUKIT PENDULANGAN 1|BUKIT PENDULANGAN 2|BUKIT PENDULANGAN 3",
    )
    assert settings.sap_prfnr_values("PWP") == (
        "BUKIT PENDULANGAN 1",
        "BUKIT PENDULANGAN 2",
        "BUKIT PENDULANGAN 3",
    )
    assert settings.sap_prfnr_values("DIN") == (
        "LONG KEJIAK 1",
        "LONG KEJIAK 2",
        "LONG KEJIAK 3",
    )
    assert settings.sap_prfnr_values("DAN") == ("PU*",)
    assert settings.sap_prfnr_values("DWT") == ("ME*",)
    assert settings.sap_prfnr_values("ELHP_DAN") == ("PU*",)
    assert settings.sap_prfnr_values("ELHP_DWT") == ("ME*",)


def test_sap_prfnr_map_remains_fallback_for_non_golden_eye_company() -> None:
    settings = Settings(
        sap_prfnr_map="XYZ=ESTATE A|ESTATE B",
        sap_estate_scope_map="PWP=BUKIT PENDULANGAN 1",
    )
    assert settings.sap_prfnr_values("XYZ") == ("ESTATE A", "ESTATE B")

def test_group_signature_is_stable() -> None:
    assert group_signature(["b", "a", "a"]) == group_signature(["a", "b"])


def test_pdf_image_lookup_does_not_fallback_to_large_silver() -> None:
    module = _load_dashboard_db_module()
    module._unreadable_image_relations.clear()
    module._image_storage_warnings.clear()
    module.table_exists = lambda table, schema=None: table in {
        module.TABLE_BYTEA_RECOVERY,
        module.TABLE_BYTEA_EXCEPTION,
        module.TABLE_IMAGE_ALL,
    }
    module.get_columns = lambda table, schema=None: [
        "file_uid",
        "file_name",
        "image_bytes",
        "mime_type",
    ]

    healthy_bytes = b"healthy-image"

    def fake_read_one(sql, params=None):
        if 'FROM "processing"."gambar_reused"' in sql:
            raise RuntimeError(
                'could not open file "base/16388/3245644.5": '
                "previous segment is only 0 blocks"
            )
        if 'FROM "processing"."source_bytea_exception"' in sql:
            return None
        if 'FROM "silver"."image_store_all_files"' in sql:
            return {
                "image_bytes": healthy_bytes,
                "mime_type": "image/jpeg",
                "file_name": "PHOTO.JPG",
            }
        return None

    module.read_one = fake_read_one
    result = module.fetch_image_bytes("PHOTO.JPG")
    assert result is None
    assert "processing.gambar_reused" in module.get_image_storage_warnings()


def test_dashboard_image_reader_prefers_recovery_overlay_before_silver() -> None:
    module = _load_dashboard_db_module()
    module._unreadable_image_relations.clear()
    module._image_storage_warnings.clear()
    module.table_exists = lambda table, schema=None: table in {
        module.TABLE_BYTEA_RECOVERY,
        module.TABLE_BYTEA_EXCEPTION,
        module.TABLE_IMAGE_ALL,
    }
    module.get_columns = lambda table, schema=None: [
        "file_uid",
        "file_name",
        "image_bytes",
        "mime_type",
    ]
    calls: list[str] = []

    def fake_read_one(sql, params=None):
        calls.append(sql)
        if 'FROM "processing"."gambar_reused"' in sql:
            return {
                "image_bytes": b"recovered-overlay",
                "mime_type": "image/jpeg",
                "file_name": "DIN_PHOTO.JPG",
            }
        if 'FROM "silver".' in sql:
            raise AssertionError("Silver tidak boleh dibaca jika overlay ditemukan")
        return None

    module.read_one = fake_read_one
    result = module.fetch_image_bytes("DIN_PHOTO.JPG")
    assert result == (b"recovered-overlay", "image/jpeg", "DIN_PHOTO.JPG")
    assert any(
        'FROM "processing"."gambar_reused"' in sql for sql in calls
    )
    assert not any('FROM "silver".' in sql for sql in calls)


def test_dashboard_nas_first_reader_uses_exact_database_path(tmp_path) -> None:
    module = _load_dashboard_db_module()
    image = tmp_path / "PANEN.JPG"
    image.write_bytes(b"nas-image")
    module.NAS_IMAGE_FIRST = True
    module.table_exists = lambda table, schema=None: table == module.TABLE_IMAGE_ALL
    module.get_columns = lambda table, schema=None: [
        "file_uid", "file_name", "file_path"
    ]

    def fake_read_one(sql, params=None):
        if 'FROM "silver"."image_store_all_files"' in sql:
            return {"nas_path": str(image), "file_name": image.name}
        return None

    module.read_one = fake_read_one
    result = module.fetch_image_bytes("PANEN-UID")
    assert result == (b"nas-image", "image/jpeg", "PANEN.JPG")


def test_dashboard_nas_first_resolves_feature_filename_when_silver_uid_changed(
    tmp_path,
) -> None:
    module = _load_dashboard_db_module()
    image = tmp_path / "0084734_20260630_SAMPLE.jpg"
    image.write_bytes(b"panen-from-nas")
    module.NAS_IMAGE_FIRST = True
    module.table_exists = lambda table, schema=None: table in {
        "image_feature", module.TABLE_IMAGE_ALL
    }
    module.get_columns = lambda table, schema=None: [
        "file_uid", "file_name", "file_name_upper", "file_path"
    ]

    def fake_read_one(sql, params=None):
        if 'FROM "processing"."image_feature"' in sql:
            return {"file_name": image.name}
        if 'FROM "silver"."image_store_all_files"' in sql:
            assert image.name.upper() in params[-1]
            return {"nas_path": str(image), "file_name": image.name}
        return None

    module.read_one = fake_read_one
    result = module.fetch_corpus_image_bytes("OLD-PANEN-UID")
    assert result == (b"panen-from-nas", "image/jpeg", image.name)


def test_transport_corpus_forces_nas_lookup_when_legacy_flag_is_false() -> None:
    module = _load_dashboard_db_module()
    module.NAS_IMAGE_FIRST = False
    expected = (b"panen-from-nas", "image/jpeg", "PANEN.JPG")
    calls: list[str] = []

    def fake_nas(candidates, requested, base):
        calls.append(requested)
        return expected

    module._fetch_nas_image_bytes = fake_nas
    module.fetch_image_bytes = lambda _key: (_ for _ in ()).throw(
        AssertionError("Hot store tidak boleh mendahului NAS untuk corpus")
    )

    result = module.fetch_corpus_image_bytes("PANEN-UID")
    assert result == expected
    assert calls == ["PANEN-UID"]


def test_pdf_uses_colored_bulletin_and_corrupt_image_placeholder() -> None:
    source = (ROOT / "dashboard" / "generate_pdf_reports.py").read_text(
        encoding="utf-8"
    )
    assert 'NAVY = colors.HexColor("#003B7A")' in source
    assert 'LABEL_BG = NAVY' in source
    assert 'RED = LABEL_BG' in source
    assert "def generate_combined_report" in source
    assert '("HARMONY", "DIN")' in source
    assert 'choices=["ALL", "PWP", "DIN", "DAN", "DWT"]' in source
    assert 'placeholder_image(demo_color, "Gambar perlu recovery")' in source
    assert '"image_recovery_needed.csv"' in source
    assert "Gunakan grid 2 x 2" in source
    assert "row = idx // 2" in source
    assert "col_idx = idx % 2" in source
    assert "gap * 2) / 3" not in source
    assert "show_scope_badges: bool = True" in source
    assert "show_scope_badges=False" in source
    assert "load_sap_kpis," in source
    assert "gold.sap_oph_kpi_summary" in source
    assert "% Tiket = total Usage Golden Eye / total tiket SAP x 100." in source
    assert "load_sap_kpis(system, company, groups, months)" in source
    assert "min(100.0, pct_usage)" not in source
    assert "reference_total_tickets" not in source
    assert "total usage Golden Eye / total tiket SAP, sama dengan dashboard" in source
    assert "def personnel_usage_summary" in source
    assert "def company_personnel_page" in source
    assert "Top {top_n} Kerani Panen berdasarkan Usage" in source
    assert "Top {top_n} Mandor berdasarkan Usage" in source
    assert "--top-personnel" in source
    assert source.count("company_personnel_page(") >= 3


def test_dashboard_and_pdf_use_same_recovery_first_image_reader() -> None:
    db_source = (ROOT / "dashboard" / "db.py").read_text(encoding="utf-8")
    app_source = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
    pdf_source = (ROOT / "dashboard" / "generate_pdf_reports.py").read_text(
        encoding="utf-8"
    )
    recovery_pos = db_source.index("TABLE_BYTEA_RECOVERY")
    silver_loop_pos = db_source.index(
        "sources = [TABLE_IMAGE_STORE, TABLE_IMAGE_VIEW, TABLE_IMAGE_ALL]"
    )
    assert recovery_pos < silver_loop_pos
    assert "from db import fetch_image_bytes, ping" in app_source
    assert "fetch_image_bytes," in pdf_source


def test_similarity_transport_does_not_keep_legacy_missing_label() -> None:
    app_source = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
    body = app_source[
        app_source.index("def build_similarity_transport_rows") :
        app_source.index("def similarity_page_info")
    ]
    assert '"transport_similarity_category"' in body
    assert '"similarity_transport"' in body
    assert 'sim = "Tidak Ada Foto Transport"' in body
    assert 'sim = "Perlu Verifikasi"' in body
    assert '"Ada Foto Transport KT Before"' in body
    assert 'or "tidak ada" in status.lower()' in body


def test_transport_kt_similarity_uses_harmony_transaction_threshold() -> None:
    scorer = (ROOT / "src" / "golden_eye" / "transport_similarity.py").read_text(
        encoding="utf-8"
    )
    migration = (ROOT / "sql" / "016_transport_kt_similarity.sql").read_text(
        encoding="utf-8"
    )
    cli = (ROOT / "src" / "golden_eye" / "cli.py").read_text(encoding="utf-8")
    queries = (ROOT / "dashboard" / "queries.py").read_text(encoding="utf-8")
    diagnose = (ROOT / "dashboard_diagnose.py").read_text(encoding="utf-8")
    config = (ROOT / "src" / "golden_eye" / "config.py").read_text(
        encoding="utf-8"
    )
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "np.dot(left, right) * 100.0" in scorer
    assert "settings.harmony_transaction_same_threshold" in scorer
    assert 'return f"SAMA ≥ {threshold:g}%", True, False' in scorer
    assert 'return f"BERBEDA < {threshold:g}%", False, False' in scorer
    assert "harmony_transaction_same_threshold" in config
    assert "HARMONY_TRANSACTION_SAME_THRESHOLD=83.0" in env_example
    assert "processing.transport_kt_similarity" in migration
    assert '"score-transport-kt-before"' in cli
    assert "transport_similarity_score" in queries
    assert "nas_file_missing" in diagnose
    assert "tidak dianggap BYTEA dashboard rusak" in diagnose


def test_dashboard_image_routes_and_diagnostic_use_gambar_reused() -> None:
    db_source = (ROOT / "dashboard" / "db.py").read_text(encoding="utf-8")
    app_source = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
    js_source = (ROOT / "dashboard" / "assets" / "lazy_images.js").read_text(
        encoding="utf-8"
    )
    diagnose_source = (ROOT / "dashboard_diagnose.py").read_text(encoding="utf-8")

    assert "def get_image_recovery_status" in db_source
    assert "gambar_reused" in db_source
    assert "ALLOW_SILVER_IMAGE_FALLBACK" in db_source
    assert app_source.count("row = fetch_image_bytes(key)") == 2
    assert app_source.count('"Cache-Control"] = "no-store, max-age=0"') == 2
    assert "Gambar perlu recovery" in js_source
    assert "markImageFailed" in js_source
    assert "--input-report" in diagnose_source
    assert "DASHBOARD IMAGE CHECK" in diagnose_source
    assert "db.get_image_recovery_status" in diagnose_source
    assert "db.fetch_image_bytes" in diagnose_source
    assert "sap_kpi.get('total_sap_oph')" in diagnose_source
    assert "sap_kpi['visual_oph']" not in diagnose_source
    assert "--audit-all-images" in diagnose_source
    assert "DASHBOARD IMAGE AUDIT ALL" in diagnose_source
    assert "dashboard_image_recovery_needed_all.csv" in diagnose_source
    assert "FROM gold.reused_photo_detail" in diagnose_source
    assert "FROM processing.gambar_reused" in diagnose_source
    assert "i.image_bytes" not in diagnose_source


def test_dashboard_all_image_audit_writes_pending_csv_per_source() -> None:
    module = _load_dashboard_diagnose_module()

    class FakeDb:
        @staticmethod
        def read_df(sql, params=None):
            assert "gold.reused_photo_detail" in sql
            assert "processing.gambar_reused" in sql
            assert "image_bytes" not in sql
            return pd.DataFrame(
                [
                    {
                        "source_group": "DIN",
                        "file_uid": "uid-1",
                        "image_key": "healthy.jpg",
                        "image_role": "PANEN",
                        "tersedia_hot": True,
                    },
                    {
                        "source_group": "DIN",
                        "file_uid": "uid-2",
                        "image_key": "pending.jpg",
                        "image_role": "PANEN",
                        "tersedia_hot": False,
                    },
                ]
            )

    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp)
        result = module._audit_all_dashboard_images(FakeDb(), "DIN", output_dir)
        pending = pd.read_csv(
            output_dir / "dashboard_image_recovery_needed_din.csv"
        )
        assert result["total"] == 2
        assert result["gambar_reused"] == 1
        assert result["missing"] == ["pending.jpg"]
        assert pending["image_key"].tolist() == ["pending.jpg"]


def test_dbeaver_repair_script_creates_fresh_v2_without_dropping_v1() -> None:
    source = (ROOT / "docs" / "REPAIR_BYTEA_RECOVERY_DBEAVER.sql").read_text(
        encoding="utf-8"
    )
    assert "pg_filenode_relation(0, 3245644)" in source
    assert "pg_filenode_relation(0, 566629)" in source
    assert "CREATE TABLE IF NOT EXISTS processing.source_bytea_recovery_v2" in source
    assert "pg_relation_filepath" in source
    assert "3245644" in source
    assert "INSERT INTO processing.source_bytea_recovery_v2" not in source
    assert "DROP TABLE processing.source_bytea_recovery" not in source
    assert "DROP TABLE silver.image_store_all_files" not in source


def test_recovery_can_bypass_corrupt_silver_from_report_or_active_gold() -> None:
    source = (ROOT / "src" / "golden_eye" / "recovery.py").read_text(
        encoding="utf-8"
    )
    start = source.index("def recover_gold_evidence_from_folders")
    end = source.index("def recover_corrupt_bytea", start)
    gold_mode = source[start:end]
    assert "--input-report" in source
    assert "--active-gold" in source
    assert "def _gold_evidence_targets" in source
    assert "FROM gold.reused_photo_detail" in source
    assert "NAS_{mode}_EXACT_NAME_DECODED" in gold_mode
    assert "settings.image_table" not in gold_mode
    assert "processing.gambar_reused" in source
    assert "current_row.file_sha256 IS DISTINCT FROM excluded.file_sha256" in source


def test_transport_only_recovery_is_targeted_and_nas_scan_stops_early() -> None:
    recovery = (ROOT / "src" / "golden_eye" / "recovery.py").read_text(
        encoding="utf-8"
    )
    diagnose = (ROOT / "dashboard_diagnose.py").read_text(encoding="utf-8")

    assert '"--transport-only"' in recovery
    assert "ACTIVE_GOLD_TRANSPORT" in recovery
    assert "image_role IN ('TRANSPORT_DT', 'TRANSPORT_FT')" in recovery
    assert "logical_name if transport_only" in recovery
    assert "if not remaining:" in recovery
    assert "seluruh target ditemukan; scan dihentikan" in recovery
    assert '"--transport-only"' in diagnose
    assert "dashboard_transport_recovery_needed" in diagnose
    assert "processing.gambar_reused" in diagnose


def test_panen_only_audit_and_recovery_are_targeted() -> None:
    recovery = (ROOT / "src" / "golden_eye" / "recovery.py").read_text(
        encoding="utf-8"
    )
    diagnose = (ROOT / "dashboard_diagnose.py").read_text(encoding="utf-8")

    assert '"--panen-only"' in recovery
    assert '"ACTIVE_GOLD_PANEN"' in recovery
    assert "AND image_role='PANEN'" in recovery
    assert '"--panen-only"' in diagnose
    assert "dashboard_panen_recovery_needed" in diagnose
    assert "PANEN_IMAGES/" in diagnose
    assert "if requested_keys is not None and not upper_keys:" in recovery
    assert "return [], [], 0" in recovery


def test_targeted_recovery_can_force_rewrite_same_hash_corrupt_toast() -> None:
    recovery = (ROOT / "src" / "golden_eye" / "recovery.py").read_text(
        encoding="utf-8"
    )
    transport = (
        ROOT / "src" / "golden_eye" / "transport_sync.py"
    ).read_text(encoding="utf-8")
    diagnose = (ROOT / "dashboard_diagnose.py").read_text(encoding="utf-8")

    assert "force_replace=input_report is not None" in recovery
    assert 'conflict_guard = "" if force_replace' in recovery
    assert "force_replace=refresh_existing" in transport
    assert 'conflict_guard = "" if force_replace' in transport
    assert "STORAGE_RELATION_ERROR" in diagnose
    assert "jumlah file individual belum dapat ditentukan" in diagnose


def test_hot_store_rebuild_preserves_healthy_rows_and_quarantines_source() -> None:
    rebuild = (
        ROOT / "src" / "golden_eye" / "hot_store_rebuild.py"
    ).read_text(encoding="utf-8")
    cli = (ROOT / "src" / "golden_eye" / "cli.py").read_text(encoding="utf-8")
    dashboard_db = (ROOT / "dashboard" / "db.py").read_text(encoding="utf-8")

    assert "rebuild-gambar-reused-storage" in cli
    assert "LIKE processing.gambar_reused INCLUDING ALL" in rebuild
    assert "ROLLBACK TO SAVEPOINT copy_hot_store_batch" in rebuild
    assert "BYTEA_COPY_FAILED" in rebuild
    assert "LOCK TABLE processing.gambar_reused IN ACCESS EXCLUSIVE MODE" in rebuild
    assert "gambar_reused_corrupt_" in rebuild
    assert "Jangan DROP backup" in rebuild
    assert "and table != TABLE_BYTEA_RECOVERY" in dashboard_db


def test_mssql_kt_before_sync_covers_all_harmony_context_and_hot_transport() -> None:
    sync = (ROOT / "src" / "golden_eye" / "transport_sync.py").read_text(
        encoding="utf-8"
    )
    cli = (ROOT / "src" / "golden_eye" / "cli.py").read_text(encoding="utf-8")
    migration = (ROOT / "sql" / "014_transport_kt_before.sql").read_text(
        encoding="utf-8"
    )
    queries = (ROOT / "dashboard" / "queries.py").read_text(encoding="utf-8")

    assert "FFB_EVIDENCE_KT_BEFORE" in sync
    assert "def _all_harmony_panens_oph" in sync
    assert "FROM processing.image_context c" in sync
    assert "FROM gold.reused_photo_detail" not in sync
    assert "all_harmony_panen_distinct_oph" in sync
    assert 'source_group not in {"PWP", "DIN"}' in sync
    assert "processing.gambar_reused AS current_row" in sync
    assert "ONE_PASS_WALK" in sync
    assert "def _persist_image_batch" in sync
    assert "image_batch.clear()" in sync
    assert "[TRANSPORT LOAD] processed=" in sync
    assert "sync-transport-kt-before" in cli
    assert 'choices=["PWP", "DIN"]' in cli
    assert '"--batch-size"' in cli
    assert "batch_size=args.batch_size" in cli
    assert "transport_kt_before_context" in migration
    assert "image_bytes" not in migration
    assert "_attach_kt_before_transport" in queries
    assert 'source_company.isin({"PWP", "DIN"})' in queries
    assert "(str(row.source_group).upper(), str(row.oph_key))" in queries
    assert "foto_kt_before" in queries


def test_transport_similarity_supports_din_without_cross_company_oph() -> None:
    scorer = (
        ROOT / "src" / "golden_eye" / "transport_similarity.py"
    ).read_text(encoding="utf-8")
    queries = (ROOT / "dashboard" / "queries.py").read_text(encoding="utf-8")
    diagnose = (ROOT / "dashboard_diagnose.py").read_text(encoding="utf-8")

    assert 'source_group not in {"PWP", "DIN"}' in scorer
    assert "WHERE c.source_group=%s" in queries
    assert "s.source_group=c.source_group" in queries
    assert "for company in (\"PWP\", \"DIN\")" in diagnose
    assert "source_group=args.source_group" in diagnose


def test_transport_detail_retries_without_corrupt_index() -> None:
    dashboard_db = (ROOT / "dashboard" / "db.py").read_text(encoding="utf-8")
    queries = (ROOT / "dashboard" / "queries.py").read_text(encoding="utf-8")

    assert "def read_df_resilient(" in dashboard_db
    assert "SET LOCAL enable_indexscan=off" in dashboard_db
    assert "SET LOCAL enable_indexonlyscan=off" in dashboard_db
    assert "SET LOCAL enable_bitmapscan=off" in dashboard_db
    assert "frame = read_df_resilient(" in queries
    assert "attachment dilewati" in queries


def test_union_find_components_drop_singletons() -> None:
    uf = UnionFind(6)
    uf.union(0, 1)
    uf.union(1, 2)
    uf.union(4, 5)
    assert sorted(sorted(x) for x in uf.components()) == [[0, 1, 2], [4, 5]]


def test_union_find_can_reject_oversize_component() -> None:
    uf = UnionFind(5)
    assert uf.union(0, 1, max_size=2)
    assert not uf.union(1, 2, max_size=2)
    assert uf.component_size(0) == 2
    assert uf.component_size(2) == 1


def test_sql_contains_gold_compatibility_views() -> None:
    sql = (ROOT / "sql" / "002_gold_views.sql").read_text(encoding="utf-8")
    assert "gold.reused_photo_group_summary_cleaned" in sql
    assert "gold.reused_photo_detail_transaction" in sql
    assert "gold.elhp_gt_group_summary_cleaned" in sql
    monthly_sql = (ROOT / "sql" / "006_dashboard_aggregates.sql").read_text(encoding="utf-8")
    assert "gold.reused_photo_dashboard_monthly" in monthly_sql
    assert "date_trunc('month', tanggal)::date" in monthly_sql
    assert "WHERE is_active=true" in monthly_sql


def test_pipeline_queries_do_not_use_like_escape_sequences() -> None:
    context = (ROOT / "src" / "golden_eye" / "context.py").read_text(encoding="utf-8")
    features = (ROOT / "src" / "golden_eye" / "features.py").read_text(encoding="utf-8")
    assert " ESCAPE " not in context
    assert " ESCAPE " not in features
    assert "left(lower(i.file_name::text), 7) <> 'before_'" in features


def test_ai_exclusion_is_token_based() -> None:
    assert filename_has_excluded_token("AI_photo.jpg")
    assert filename_has_excluded_token("photo_AI_001.jpg")
    assert filename_has_excluded_token("photo-ai.jpg")
    assert not filename_has_excluded_token("chair_photo.jpg")
    assert not filename_has_excluded_token("ID_ACTUAL_photo.jpg")


def test_reconciliation_defaults_match_legacy_flow() -> None:
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "EMBED_SOURCE_READ_ORDER=physical" in env
    assert "EMBED_DB_PREFETCH_BATCHES=2" in env
    assert "MATCH_MODE=knn" in env
    assert "MATCH_ENGINE=auto" in env
    assert "DEDUP_LOGICAL_FILENAME=true" in env
    assert "MAX_MATCH_EDGES=5000000" in env
    assert "TORCH_FALLBACK_MAX_IMAGES=100000" in env
    assert "FAISS_INDEX_MODE=auto" in env
    assert "FAISS_FLAT_MAX_IMAGES=100000" in env
    assert "MATCH_AUTO_CAP_TOP_K=true" in env
    assert "MATCH_REQUIRE_MUTUAL_KNN=true" in env
    assert "MATCH_COMPONENT_MAX_SIZE=500" in env
    assert "VALIDATION_FULL_PAIR_MAX_GROUP=40" in env
    assert "VALIDATION_DEVICE=auto" in env
    assert "VALIDATION_GPU_PAIR_BATCH=65536" in env
    assert "VALIDATION_CPU_WORKERS=8" in env
    assert "VALIDATION_CPU_PAIR_BATCH=512" in env
    assert "VALIDATION_DB_MEMBER_BATCH=2000" in env
    assert "VALIDATION_DB_CANDIDATE_BATCH=500" in env
    assert "VALIDATION_SOURCE_READ_ORDER=physical" in env
    assert "VALIDATION_MAX_AUDIT_PAIRS_PER_GROUP=500" in env
    assert "VALIDATION_GROUP_MEAN_SIMILARITY=90.0" in env
    assert "VALIDATION_MEMBER_MEAN_SIMILARITY=93.0" in env
    assert "VALIDATION_REPRESENTATIVE_SIMILARITY=93.0" in env
    assert "EXCLUDE_FILENAME_TOKENS=AI" in env
    assert "ACCOUNT_STATUS_VALUES=ACTUAL,ACTUAL APPROVE" in env
    assert "ACCOUNT_STATUS_UNFILTERED_SOURCES=DAN,DWT" in env
    assert "REQUIRE_COMPLETE_MONTH=true" in env
    assert "FILENAME_TIMESTAMP_UTC_OFFSET_HOURS=7" in env
    assert "RAW_BUSINESS_SCHEMA=raw" in env
    assert "RAW_BUSINESS_TABLE=production_plantation" in env


def test_monthly_incremental_merge_reuses_embeddings_and_guards_month() -> None:
    cli = (ROOT / "src" / "golden_eye" / "cli.py").read_text(encoding="utf-8")
    context = (ROOT / "src" / "golden_eye" / "context.py").read_text(
        encoding="utf-8"
    )
    loader = (
        ROOT / "src" / "golden_eye" / "silver_loader.py"
    ).read_text(encoding="utf-8")

    assert '"merge-month"' in cli
    assert 'expected_month=month' in cli
    assert 'sync_raw_production_month(' in cli
    assert '"sync-raw-month"' in cli
    assert '"--skip-raw-sync"' in cli
    assert '"--resume-after-silver"' in cli
    audit = (ROOT / "src" / "golden_eye" / "audit.py").read_text(
        encoding="utf-8"
    )
    assert "), context_base AS (" in audit
    context_date_section = audit[
        audit.index("'CONTEXT_DATE'::text") : audit.index("'EMBED_VALID'::text")
    ]
    assert "FROM context_base" in context_date_section
    assert "FROM feature_base" not in context_date_section
    assert "RESUME_AFTER_SILVER" in cli
    assert "skip_silver_business_lookup=True" in cli
    assert "mode=MONTH_RAW_CANONICAL" in context
    assert "if current_period_date == parsed_date" in context
    assert 'from_stage=args.from_stage' in cli
    assert 'active_gold=True' in cli
    assert 'panen_only=True' in cli
    assert "publishable_context_months" in cli
    assert "MONTH_OUT_OF_SCOPE_SKIPPED" in loader
    assert "MONTH_UNPARSED_SKIPPED" in loader
    assert "CONFLICT_EXISTING_NAME_DIFFERENT_HASH" in loader
    assert "reject other months before file stat/read" in loader
    assert "actual_month != month" in loader


def test_match_staging_storage_can_be_reset_without_touching_business_results() -> None:
    cli = (ROOT / "src" / "golden_eye" / "cli.py").read_text(encoding="utf-8")
    source = (ROOT / "src" / "golden_eye" / "match_storage.py").read_text(
        encoding="utf-8"
    )
    assert '"reset-match-staging"' in cli
    assert "def reset_match_staging(" in source
    assert "TRUNCATE TABLE" in source
    for table in (
        "processing.candidate_edge",
        "processing.candidate_group",
        "processing.candidate_group_member",
    ):
        assert table in source
    for protected in (
        "processing.image_feature",
        "processing.image_context",
        "processing.validated_group",
        "gold.reused_photo",
        "processing.gambar_reused",
    ):
        assert f"TRUNCATE {protected}" not in source
        assert f"DELETE FROM {protected}" not in source
    assert "match_staging_resume_from" in source


def test_silver_storage_rebuild_is_resumable_and_keeps_corrupt_backup() -> None:
    source = (ROOT / "src" / "golden_eye" / "silver_storage_rebuild.py").read_text(
        encoding="utf-8"
    )
    cli = (ROOT / "src" / "golden_eye" / "cli.py").read_text(encoding="utf-8")
    assert '"rebuild-silver-storage"' in cli
    assert "SAVEPOINT copy_silver_storage_batch" in source
    assert "ON CONFLICT (file_uid) DO NOTHING" in source
    assert "SET LOCAL enable_indexscan=off" in source
    assert 'active_table + "_corrupt_"' in source
    assert "ALTER TABLE {} RENAME TO {}" in source
    assert "Jangan DROP backup" in source
    loader = (ROOT / "src" / "golden_eye" / "silver_loader.py").read_text(
        encoding="utf-8"
    )
    assert '"--input-report"' in cli
    assert "TARGET_FILE_NOT_FOUND" in loader
    assert "target_names is not None" in loader


def test_raw_monthly_sync_is_batch_resumable_and_anti_duplicate() -> None:
    source = (ROOT / "src" / "golden_eye" / "raw_monthly_sync.py").read_text(
        encoding="utf-8"
    )
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "_load_existing_batch_keys" in source
    assert "existing_month_rows_scanned" in source
    assert "FAST MODE: tidak menjalankan COUNT DISTINCT/full scan RAW" in source
    assert "RAW_LOOKUP_INDEX_MISSING" in source
    assert "pg_before = _pg_metrics" not in source
    assert "fetchmany(int(batch_size))" in source
    assert "source_duplicate_keys" in source
    assert "ACCOUNT_STATUS" in source
    assert "CREATE UNIQUE INDEX" not in source
    assert "MSSQL_RAW_TABLE=dbo.PRODUCTION_PLANTATION" in env
    assert "MSSQL_RAW_KEY_COLUMNS=" in env


def test_matcher_canonicalizes_duplicate_logical_filenames() -> None:
    matcher = (ROOT / "src" / "golden_eye" / "matcher.py").read_text(encoding="utf-8")
    assert "PARTITION BY upper(trim(f.file_name))" in matcher
    assert "MAX_MATCH_EDGES" in matcher
    assert "IndexHNSWFlat" in matcher
    assert "top_k otomatis dibatasi" in matcher
    assert "FAISS search=" in matcher
    assert "CUDA siap" in matcher
    assert "Torch search=" in matcher
    assert "torch.cuda.get_device_name" in matcher
    assert "became_mutual" in matcher
    assert "max_size=settings.match_component_max_size" in matcher
    assert "one_way_dropped=" in matcher
    assert "component guard OK" in matcher
    assert "max_candidate_group_size" in matcher


def test_validator_uses_sparse_edges_above_full_pair_limit() -> None:
    validator = (ROOT / "src" / "golden_eye" / "validator.py").read_text(encoding="utf-8")
    assert "settings.validation_full_pair_max_group" in validator
    assert "mode={'SPARSE_EDGE' if sparse_validation else 'FULL_PAIR'}" in validator
    assert "pairs={len(pairs):,}" in validator


def test_validator_has_hybrid_cuda_numeric_path() -> None:
    validator = (ROOT / "src" / "golden_eye" / "validator.py").read_text(encoding="utf-8")
    assert "import torch" in validator
    assert "@torch.inference_mode()" in validator
    assert "def _gpu_numeric_pair_metrics" in validator
    assert "full_sim = (full[left_idx] * full[right_idx]).sum(dim=1) * 100.0" in validator
    assert "center_sim = (center[left_idx] * center[right_idx]).sum(dim=1) * 100.0" in validator
    assert "torch.minimum(hist_left, hist_right)" in validator
    assert "numeric_device={runtime.numeric_device}" in validator
    assert '"validation_gpu_pairs": gpu_pairs' in validator
    assert "ThreadPoolExecutor" in validator
    assert "threading.local()" in validator


def test_validator_enforces_exact_all_pair_similarity_not_composite_score() -> None:
    source = (ROOT / "src" / "golden_eye" / "validator.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    wanted = {
        "GroupEmbeddingQuality",
        "_embedding_quality",
        "_similarity_components",
        "_solidify_components",
    }
    body = [
        node
        for node in module.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in wanted
    ]
    namespace: dict[str, object] = {
        "dataclass": dataclass,
        "np": np,
        "UnionFind": UnionFind,
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), "validator.py", "exec"), namespace)
    embedding_quality = namespace["_embedding_quality"]
    solidify_components = namespace["_solidify_components"]
    matrix = np.array(
        [
            [100.0, 95.0, 70.0],
            [95.0, 100.0, 95.0],
            [70.0, 95.0, 100.0],
        ],
        dtype=np.float32,
    )
    positions = {"a": 0, "b": 1, "c": 2}
    quality = embedding_quality(["a", "b", "c"], positions, matrix)
    assert quality.mean_similarity < 90.0
    assert quality.min_member_mean_similarity < 90.0

    accepted, rejected, qualities = solidify_components(
        [["a", "b", "c"]], positions, matrix, 90.0, 90.0
    )
    assert accepted == [["b", "c"]]
    assert rejected == {"a"}
    assert qualities[("b", "c")].mean_similarity == 95.0
    assert qualities[("b", "c")].min_member_mean_similarity == 95.0


def test_exact_similarity_uses_minimum_of_full_and_center_on_numeric_device() -> None:
    validator = (ROOT / "src" / "golden_eye" / "validator.py").read_text(
        encoding="utf-8"
    )
    assert "def _exact_embedding_similarity_matrix" in validator
    assert "full_similarity = full @ full.T" in validator
    assert "center_similarity = center @ center.T" in validator
    assert "torch.minimum(full_similarity, center_similarity) * 100.0" in validator
    assert "solid_similarity.fill_diagonal_(100.0)" in validator


def test_validator_batches_db_and_preserves_legacy_group_rules() -> None:
    validator = (ROOT / "src" / "golden_eye" / "validator.py").read_text(encoding="utf-8")
    sql = (ROOT / "sql" / "003_source_indexes.sql").read_text(encoding="utf-8")
    assert "def _candidate_batches" in validator
    assert "VALIDATE] preload batch=" in validator
    assert "autocommit=True" in validator
    assert "write_conn.commit()" in validator
    assert "_COMPONENT_EDGE_SCORE = 0.52" in validator
    assert "_COMPONENT_EDGE_ALT_CENTER = 87.5" in validator
    assert "_PAIR_SCORE_GROUP2_MIN = 0.66" in validator
    assert "validation_max_audit_pairs_per_group" in validator
    assert "def _image_uid_join_condition" in validator
    assert "i.source_group=%s" in validator
    assert "i.is_transport=false" in validator
    assert "ix_image_all_source_transport_uid" in sql
    assert "JOIN LATERAL" not in validator[validator.index("def validate_source(") :]
    assert "AND e.file_uid_a=ANY(%s)" in validator
    edge_lookup = validator[
        validator.index("raw_edge_rows = fetch_all_dict") :
        validator.index("members_by_candidate:")
    ]
    assert "JOIN processing.candidate_group_member" not in edge_lookup
    assert "batch_uid_to_candidate" in edge_lookup
    assert "ORDER BY {validation_member_order}" in validator
    assert '"coalesce(r.ctid, i.ctid)"' in validator
    assert "source_read_order=" in validator


def test_strict_validator_does_not_let_one_strong_edge_rescue_low_mean_group() -> None:
    validator = (ROOT / "src" / "golden_eye" / "validator.py").read_text(encoding="utf-8")
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "STRICT_GROUP_VALIDATION=true" in env
    assert "mean_score >= minimum_mean\n            if strict_validation" in validator
    assert "else mean_score >= minimum_mean or strong_edges >= 1" in validator
    assert "strict_mean={'ON' if settings.strict_group_validation else 'OFF'}" in validator


def test_low_light_guard_requires_structure_without_rejecting_all_night_photos() -> None:
    validator = (ROOT / "src" / "golden_eye" / "validator.py").read_text(encoding="utf-8")
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    sql = (ROOT / "sql" / "007_low_light_validation.sql").read_text(encoding="utf-8")
    assert "LOW_LIGHT_GUARD=true" in env
    assert "near_exact_evidence" in validator
    assert "geometric_evidence" in validator
    assert "not low_light_pair or near_exact_evidence or geometric_evidence" in validator
    assert "pair_score = 0.0 if low_light_guard_rejected else raw_pair_score" in validator
    assert "raw_pair_score" in sql
    assert "low_light_guard_pass" in sql


def test_low_light_guard_rejects_texture_but_keeps_exact_and_geometric_evidence() -> None:
    source = (ROOT / "src" / "golden_eye" / "validator.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    wanted = {
        "Descriptor", "PairResult", "NumericPairMetrics",
        "_is_low_light", "compare_pair",
    }
    body = [
        node
        for node in module.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in wanted
    ]
    namespace: dict[str, object] = {
        "dataclass": dataclass,
        "Any": Any,
        "np": np,
        "Settings": object,
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), "validator.py", "exec"), namespace)
    descriptor_cls = namespace["Descriptor"]
    numeric_cls = namespace["NumericPairMetrics"]
    compare_pair = namespace["compare_pair"]

    class Hash:
        def __init__(self, value: int):
            self.value = value

        def __sub__(self, other) -> int:
            return abs(self.value - other.value)

    def descriptor(uid: str, phash: int, edgehash: int):
        return descriptor_cls(
            uid, f"{uid}.jpg", np.array([1.0], dtype=np.float32),
            np.array([1.0], dtype=np.float32), Hash(phash), Hash(edgehash),
            np.array([1.0], dtype=np.float32), np.array([1.0], dtype=np.float32),
            [], None, 30.0, 0.80,
        )

    settings = SimpleNamespace(
        low_light_guard=True,
        low_light_max_mean=68.0,
        low_light_min_dark_fraction=0.55,
        low_light_near_exact_similarity=95.0,
        low_light_max_phash_distance=8,
        low_light_max_edgehash_distance=10,
        low_light_min_edgevec_similarity=0.72,
        low_light_min_orb_inliers=8,
        low_light_min_orb_ratio=0.12,
        review_threshold=90.0,
        pair_score_edge=0.54,
        pair_score_strong=0.77,
        min_modal_support=2,
    )

    namespace["_orb_stats"] = lambda _left, _right: (3, 0, 0.0)
    rejected = compare_pair(
        descriptor("a", 0, 0), descriptor("b", 12, 12), settings,
        numeric_cls(94.0, 94.0, 0.65, 0.90),
    )
    assert rejected.low_light_pair
    assert not rejected.low_light_guard_pass
    assert rejected.raw_pair_score > 0.54
    assert rejected.pair_score == 0.0
    assert not rejected.same_object

    near_exact = compare_pair(
        descriptor("c", 0, 0), descriptor("d", 4, 5), settings,
        numeric_cls(97.0, 97.0, 0.66, 0.90),
    )
    assert near_exact.low_light_guard_pass
    assert near_exact.same_object

    namespace["_orb_stats"] = lambda _left, _right: (24, 14, 0.58)
    geometric = compare_pair(
        descriptor("e", 0, 0), descriptor("f", 15, 15), settings,
        numeric_cls(94.0, 94.0, 0.82, 0.65),
    )
    assert geometric.low_light_guard_pass
    assert geometric.same_object


def test_publish_has_quality_uniqueness_and_growth_guards() -> None:
    publisher = (ROOT / "src" / "golden_eye" / "publisher.py").read_text(encoding="utf-8")
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "MIN_PUBLISH_MEAN_CONFIRMED=0.50" in env
    assert "MIN_PUBLISH_MEAN_REVIEW=0.52" in env
    assert "MAX_GOLD_GROUP_GROWTH_FACTOR=2.0" in env
    assert "MAX_GOLD_USAGE_GROWTH_FACTOR=3.0" in env
    assert "duplicate_file_uid=" in publisher
    assert "duplicate_logical_name=" in publisher
    assert "Gold group" in publisher
    assert "Gold usage melonjak" in publisher
    assert "coverage_comparable" in publisher
    assert "GROWTH WARNING (non-blocking)" in publisher
    assert "gold_month_coverage_expanded" in publisher
    assert "Atomic publication dilakukan hanya setelah semua quality gate lulus" in publisher
    assert "Run ini belum divalidasi dengan LOW_LIGHT_GUARD" in publisher
    assert "validation_exact_group_similarity" in publisher
    assert "mean_embedding_similarity" in publisher
    assert "min_member_mean_similarity" in publisher
    assert "skip exact-invalid groups=" in publisher
    assert "skip legacy pair_score-invalid groups=" in publisher
    assert "gold_exact_invalid_groups_skipped" in publisher
    assert "gold_legacy_pair_score_groups_skipped" in publisher
    assert "gold_total_quality_groups_skipped" in publisher
    strict_sql = (ROOT / "sql" / "009_strict_group_similarity.sql").read_text(
        encoding="utf-8"
    )
    assert "processing.validated_group" in strict_sql
    assert "gold.reused_photo_group" in strict_sql
    assert "CREATE OR REPLACE VIEW gold.reused_photo_group_summary_cleaned" in strict_sql
    assert "CREATE OR REPLACE VIEW gold.elhp_gt_group_summary_cleaned" in strict_sql


def test_large_source_top_k_is_bounded_by_edge_limit() -> None:
    source = (ROOT / "src" / "golden_eye" / "matcher.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    function = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_bounded_top_k"
    )
    namespace: dict[str, object] = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "matcher.py", "exec"), namespace)
    bounded_top_k = namespace["_bounded_top_k"]

    assert bounded_top_k(80, 5_000_000, 329_623) == 15
    assert bounded_top_k(80, 5_000_000, 8_201) == 80


def test_packed_edge_preserves_score_and_mutual_direction() -> None:
    source = (ROOT / "src" / "golden_eye" / "matcher.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    wanted = {"_pack_edge", "_edge_score", "_edge_direction_mask"}
    body = [
        node
        for node in module.body
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "_EDGE_SCORE_SCALE" for target in node.targets)
        )
        or (isinstance(node, ast.FunctionDef) and node.name in wanted)
    ]
    namespace: dict[str, object] = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), "matcher.py", "exec"), namespace)
    pack = namespace["_pack_edge"]
    score = namespace["_edge_score"]
    mask = namespace["_edge_direction_mask"]

    one_way = pack(94.1234, 0b01)
    mutual = pack(score(one_way), mask(one_way) | 0b10)
    assert score(mutual) == 94.1234
    assert mask(mutual) == 0b11


def test_date_fallback_from_filename_is_calendar_validated() -> None:
    assert str(parse_date_from_filename("0102132_20260328_175524.jpg")) == "2026-03-28"
    assert str(parse_date_from_filename("prefix_20260414_suffix.jpg")) == "2026-04-14"
    assert str(parse_date_from_filename("20260418045309_photo.jpg")) == "2026-04-18"
    assert str(parse_date_from_filename("JPEG_1782472739654.jpg")) == "2026-06-26"
    assert str(parse_date_from_filename("IMG-1775127053825.jpeg")) == "2026-04-02"
    assert parse_date_from_filename("ORDER_1782472739654.jpg") is None
    assert parse_date_from_filename("prefix_20260231_suffix.jpg") is None
    assert parse_date_from_filename("file_without_date.jpg") is None


def test_filename_date_fallback_insert_has_matching_arity() -> None:
    context = (ROOT / "src" / "golden_eye" / "context.py").read_text(encoding="utf-8")
    assert "match_method, updated_at)\n                    VALUES %s" not in context
    assert "match_method)\n                    VALUES %s" in context


def test_raw_context_enrichment_is_limited_to_usage() -> None:
    enrichment = (ROOT / "src" / "golden_eye" / "enrichment.py").read_text(encoding="utf-8")
    context = (ROOT / "src" / "golden_eye" / "context.py").read_text(encoding="utf-8")
    assert "processing.validated_group_member" in enrichment
    assert "gold.reused_photo_detail" in enrichment
    assert "evidence_method" in enrichment
    assert "processing.basename_upper" in context
    assert "_evidence_match_sql" in enrichment
    assert "mulai lookup" in enrichment
    assert "WHEN 'ACTUAL APPROVE' THEN 0" in enrichment
    assert "WHEN 'ACTUAL' THEN 1" in enrichment
    assert "remaining_missing_samples" in enrichment
    assert "MATCH_BY_RAW_ACTOR_DATE_UNIQUE_OPH" in enrichment
    assert "actor+period_date" in enrichment
    assert "WHERE estate_count=1" in enrichment
    assert '["department_code", "department_code_sap", "estnr", "estate"]' in enrichment
    assert "TRUNCATE" not in enrichment.upper()
    assert "DELETE FROM" not in enrichment.upper()


def test_verify_checks_missing_estate() -> None:
    cli = (ROOT / "src" / "golden_eye" / "cli.py").read_text(encoding="utf-8")
    assert "active_detail_missing_estate_excluded" in cli
    assert "active_group_missing_estate_excluded" in cli
    assert "DASHBOARD {row['source_group']}" in cli


def test_dashboard_excludes_missing_estate_without_deleting_gold() -> None:
    queries = (ROOT / "dashboard" / "queries.py").read_text(encoding="utf-8")
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "DASHBOARD_REQUIRE_ESTATE=true" in env
    assert "MIN_VISIBLE_GROUP_USAGE=2" in env
    assert "reconcile_groups_with_visible_details" in queries
    assert 'df = df[df["estate"].apply(is_valid)].copy()' in queries
    assert "DELETE FROM" not in queries.upper()


def test_dashboard_overview_uses_gold_summary_and_loads_detail_lazily() -> None:
    queries = (ROOT / "dashboard" / "queries.py").read_text(encoding="utf-8")
    app = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
    preload_body = queries[queries.index("def preload_all_data") : queries.index("def _filter_elhp_valid")]
    assert "_load_group_table(TABLE_GROUP" in preload_body
    assert "_load_detail_table" not in preload_body
    assert "_load_dashboard_monthly_table" in preload_body
    assert 'needs_all_detail = active_page_name == "similarity"' in app
    assert "load_details(system, company) if needs_all_detail else pd.DataFrame()" in app
    assert "load_group_details(system, company, gid)" in app
    assert "load_dashboard_monthly(system, company)" in app
    assert 'd.groupby(["group_key", "month_label"], dropna=False)["usage_weight"]' in app


def test_dashboard_company_filter_has_one_owner_callback() -> None:
    app = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
    company_callback = app[
        app.index("def update_company_filter") : app.index("def cascade_filters")
    ]
    cascade_callback = app[
        app.index("def cascade_filters") : app.index("def pick_group")
    ]
    assert 'State("company-filter", "value")' in app
    assert 'Output("company-filter", "value")' not in cascade_callback
    assert "return options, value" in company_callback
    assert "pilihan manual dapat tertimpa" in cascade_callback
    assert 'id="company-filter"' in app
    company_layout = app[app.index('id="company-filter"') : app.index('id="estate-filter"')]
    assert "multi=True" in company_layout
    assert "clearable=True" in company_layout


def test_dashboard_sap_kpi_and_targeted_group_detail_contract() -> None:
    queries = (ROOT / "dashboard" / "queries.py").read_text(encoding="utf-8")
    app = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
    sql = (ROOT / "sql" / "008_dashboard_sap.sql").read_text(encoding="utf-8")
    assert "def load_sap_kpis" in queries
    sap_body = queries[queries.index("def load_sap_kpis") :]
    assert "_load_sap_audit_rows()" not in sap_body
    assert "settings.sap_table" not in sap_body
    assert '"Total Transaksi SAP"' in app
    assert '"Temuan Usage / Transaksi SAP"' in app
    assert "load_sap_kpis(system, company, g, selected_months)" in app
    assert '"company & bulan terpilih"' in app
    assert 'kpi_card("OPH Dievaluasi SAP"' not in app
    assert 'kpi_card("Transaksi Ada di SAP"' not in app
    assert '"Transaksi Cocok ke SAP"' not in app
    assert '"SAP Muncul di Transaksi"' not in app
    assert '100.0 * float(k["total_usage"]) / float(sap_total)' in app
    assert "TABLE_SAP_KPI_SUMMARY" in queries
    assert "import numpy as np" in queries
    assert "def load_group_details" in queries
    assert "load_group_details(system, company, gid)" in app
    assert "ix_gold_oph_audit_active_source_key" in sql


def test_dashboard_sap_kpi_accepts_cached_legacy_schema_without_total_column() -> None:
    module = _load_dashboard_queries_module()
    module.DATA_CACHE_SECONDS = 60
    cache_key = f"sap-kpi-summary:{module.TABLE_SAP_KPI_SUMMARY}"
    legacy = pd.DataFrame(
        {
            "source_group": ["PWP"],
            "source_system": ["HARMONY"],
            "source_company": ["PWP"],
            "period_year": [2026],
            "sap_evaluated": [True],
            "monthly_sap_oph": [{"2026-06": 20, "2026-07": 30}],
        }
    )
    module._norm_cache[cache_key] = (module.time.time(), legacy)

    july = module.load_sap_kpis("HARMONY", ["PWP"], months=["2026-07"])
    full_year = module.load_sap_kpis("HARMONY", ["PWP"])

    assert july["total_sap_oph"] == 30
    assert july["scope_ready"] is True
    assert full_year["total_sap_oph"] == 50
    assert full_year["scope_ready"] is True


def test_dashboard_sap_snapshot_contract() -> None:
    migration = (ROOT / "sql" / "010_dashboard_sap_kpi.sql").read_text(encoding="utf-8")
    publisher = (ROOT / "src" / "golden_eye" / "publisher.py").read_text(encoding="utf-8")
    config = (ROOT / "src" / "golden_eye" / "config.py").read_text(encoding="utf-8")
    assert "gold.sap_oph_kpi_summary" in migration
    assert "total_sap_oph" in migration
    assert "monthly_sap_oph jsonb" in migration
    assert "transaction_match_pct" in migration
    assert "_load_scoped_sap_keys" in publisher
    assert "_write_sap_kpi_summary" in publisher
    assert "def refresh_active_sap_kpi" in publisher
    assert '"refresh-sap-kpi"' in (ROOT / "src" / "golden_eye" / "cli.py").read_text(encoding="utf-8")
    assert "ON CONFLICT (run_id, source_group) DO UPDATE" in publisher
    assert "def _oph_aliases" in publisher
    assert "digit-only fallback" in publisher
    assert "SAP_PERIOD_YEAR" in config
    assert "SAP_PRFNR_MAP" in config
    assert 'os.getenv("SAP_PRFNR_MAP", "")' in config
    assert "LONG KEJIAK 1" in config
    assert '"DAN": ("PU*",)' in config
    assert '"DWT": ("ME*",)' in config
    assert "_resolve_sap_prfnr_scopes" in publisher
    assert 'SAP_SCOPE_MODE", "company"' in config
    queries = (ROOT / "dashboard" / "queries.py").read_text(encoding="utf-8")
    assert "int(total_values.sum())" in queries
    assert "selected_total += sum(" in queries
    assert "monthly.get(month, 0)" in queries
    assert 'months: Any = None' in queries
    assert 'lambda value: "*" in str(value)' in queries
    assert "def _load_sap_count_snapshots" in publisher
    assert "COUNT DISTINCT beberapa company sekaligus" in publisher
    assert "WITH transaction_key AS" not in publisher
    assert "SAP_COUNT_ONLY" in publisher
    assert 'AS sap_month' in publisher
    assert '"gold_sap_monthly_oph"' in publisher
    assert "denominator global/" in publisher


def test_context_prefers_filename_month_over_latest_business_row() -> None:
    context = (ROOT / "src" / "golden_eye" / "context.py").read_text(encoding="utf-8")
    assert "filename_date_priority" in context
    assert "to_char({values['period_date']}, 'YYYYMMDD')" in context
    assert "period_date=excluded.period_date" in context
    assert "AND c.period_date IS NULL\n                  AND (" not in context
    assert '"context_months": context_months' in context


def test_elhp_alias_consolidation_is_non_destructive() -> None:
    module = (ROOT / "src" / "golden_eye" / "source_consolidation.py").read_text(encoding="utf-8")
    cli = (ROOT / "src" / "golden_eye" / "cli.py").read_text(encoding="utf-8")
    assert '"DAN": "ELHP_DAN"' in module
    assert '"DWT": "ELHP_DWT"' in module
    assert "DELETE FROM" not in module.upper()
    assert "consolidate-elhp" in cli
    assert "--apply" in cli


def test_structural_duplicate_guard_blocks_semantic_only_edges() -> None:
    validator = (ROOT / "src" / "golden_eye" / "validator.py").read_text(encoding="utf-8")
    publisher = (ROOT / "src" / "golden_eye" / "publisher.py").read_text(encoding="utf-8")
    assert "near_exact_structural" in validator
    assert "geometric_structural" in validator
    assert "and structural_evidence" in validator
    assert "result.structural_evidence and (" in validator
    assert "validation_structural_duplicate_guard" in validator
    assert "def _representative_star_guard" in validator
    assert "settings.validation_representative_similarity" in validator
    assert "validation_representative_star_guard" in validator
    assert "DUPLICATE_REQUIRE_HASH_SUPPORT=true" in (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "STRUCTURAL_DUPLICATE_GUARD" in publisher
    assert "REPRESENTATIVE_STAR_GUARD" in publisher


def test_dashboard_photo_card_shows_all_janjang_fields() -> None:
    app = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
    css = (ROOT / "dashboard" / "assets" / "style.css").read_text(encoding="utf-8")
    assert 'html.Div("JJG Kerani Panen"' in app
    assert 'html.Div("JJG FT"' in app
    assert 'html.Div("JJG DT"' in app
    assert 'html.Div("Bunch Pay / OPH"' in app
    assert 'className="frame-jjg-grid"' in app
    assert ".frame-jjg-grid" in css
    assert '"jjg_ft", "kerani_dt", "jjg_dt"' in app


def test_dashboard_separates_reused_group_similarity_from_transport_similarity() -> None:
    queries = (ROOT / "dashboard" / "queries.py").read_text(encoding="utf-8")
    app = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
    assert '"mean_embedding_similarity"' in queries
    assert '"min_member_mean_similarity"' in queries
    assert 'kpi_card("Group Similarity"' in app
    assert 'kpi_card("Min Member Similarity"' in app
    assert "def fmt_similarity" in app


def test_dashboard_month_filter_uses_exact_gold_monthly_rows() -> None:
    queries = (ROOT / "dashboard" / "queries.py").read_text(encoding="utf-8")
    app = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
    assert "monthly_exact = load_dashboard_monthly(system, company)" in queries
    assert "using it here hid later/earlier months" in queries
    assert 'filters_without_month["months"] = []' in app
    assert 'monthly_base["month"].astype(str).isin(selected_months)' in app
    assert "high-volume May/June cannot hide January-April" in app


def test_top_reused_selection_keeps_every_available_month_visible() -> None:
    source = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    wanted = {"_month_label", "_month_sort_key", "_balanced_top_group_keys"}
    body = [
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    namespace: dict[str, object] = {"Any": Any, "pd": pd}
    exec(compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"), namespace)
    select = namespace["_balanced_top_group_keys"]
    base = pd.DataFrame(
        [
            {"group_key": f"ELHP|{idx}", "usage_count": 1000 - idx}
            for idx in range(1, 8)
        ]
    )
    monthly = pd.DataFrame(
        [
            {
                "group_key": f"ELHP|{idx}",
                "month": f"2026-{idx:02d}",
                "usage_weight": idx,
            }
            for idx in range(1, 7)
        ]
    )
    picked = select(base, monthly, None, 6)
    assert set(picked) == {f"ELHP|{idx}" for idx in range(1, 7)}


def test_month_audit_includes_elhp_aliases_and_all_layers() -> None:
    audit = (ROOT / "src" / "golden_eye" / "audit.py").read_text(encoding="utf-8")
    cli = (ROOT / "src" / "golden_eye" / "cli.py").read_text(encoding="utf-8")
    assert 'return "DAN", ["DAN", "ELHP_DAN"]' in audit
    assert 'return "DWT", ["DWT", "ELHP_DWT"]' in audit
    for layer in (
        "SILVER_FILENAME", "BYTEA_EFFECTIVE", "BYTEA_PENDING_RECOVERY",
        "CONTEXT_DATE", "CONTEXT_PUBLISHABLE", "EMBED_VALID",
        "VALIDATED_LATEST", "GOLD_DETAIL", "DASHBOARD_MONTHLY",
    ):
        assert layer in audit
    assert "c.estate AS context_estate" in audit
    assert "btrim(context_estate::text)" in audit
    assert "c.match_method,\n                    c.match_method" not in audit
    assert "CONTEXT LINEAGE (asal transaksi per bulan)" in audit
    assert '"context_sources": context_sources' in audit
    assert '"audit-months"' in cli
    assert '"rebuild-company"' in cli
    assert '"sync-raw-context"' in cli
    assert '"missing_publishable_context_months", []' in cli
    assert 'from_stage="embed"' in cli
    assert "GOLD HISTORY REFERENCE" in audit
    assert '"historical_months_missing_source"' in audit
    assert '"historical_months_missing_active"' in audit
    assert "Month history guard gagal" in cli


def test_publish_never_replaces_complete_gold_with_partial_months() -> None:
    publisher = (ROOT / "src" / "golden_eye" / "publisher.py").read_text(
        encoding="utf-8"
    )
    guard = publisher.index("missing_previous_months = sorted(")
    deactivate = publisher.index(
        'UPDATE gold.reused_photo_group SET is_active=false'
    )
    assert guard < deactivate
    assert "PUBLISH dibatalkan sebelum Gold lama dinonaktifkan" in publisher
    assert '"gold_missing_previous_months"' in publisher

    restore = (ROOT / "restore_gold_run.py").read_text(encoding="utf-8")
    assert "gold_month AS" in restore
    assert "months={','.join(row['months'] or []) or '-'}" in restore


def test_dashboard_sap_denominator_follows_active_gold_month_scope() -> None:
    app = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
    assert "sap_scope_months = set(selected_months)" in app
    assert "load_sap_kpis(system, company, g, sap_scope_months)" in app
    assert "company & cakupan bulan Gold aktif" in app


def test_raw_context_backfill_preserves_filename_date_and_supports_elhp_timestamps() -> None:
    enrichment = (ROOT / "src" / "golden_eye" / "enrichment.py").read_text(
        encoding="utf-8"
    )
    assert "all_missing: bool = False" in enrichment
    assert "safe_match AS" in enrichment
    assert "raw_matches AS MATERIALIZED" in enrichment
    assert "FROM raw_matches r" in enrichment
    assert "strategy=INDEX_BASENAME_SINGLE_JOIN" in enrichment
    assert "HAVING count(DISTINCT upper(trim({values['estate']})))=1" in enrichment
    assert "period_date=coalesce(" in enrichment
    assert "processing.image_context.period_date," in enrichment
    assert "r.period_date=r.current_period_date THEN 0" in enrichment
    assert "MATCH_BY_RAW_TIMESTAMP_ACTOR_UNIQUE_OPH" in enrichment
    assert "split_part(u.file_name, '_', 1) ~ '^[0-9]{14}$'" in enrichment
    assert "split_part(u.file_name, '_', 2) ~ '^20[0-9]{6}$'" in enrichment
    assert "left(split_part(u.file_name, '_', 3), 6)" in enrichment
    assert "MATCH_BY_SAP_OPH_UNIQUE_PRFNR" in enrichment
    assert "period_year" in enrichment
    assert "period_month" in enrichment
    assert "period_day" in enrichment
    assert "MATCH_BY_HISTORICAL_GOLD_FILE_UID" in enrichment
    assert "MATCH_BY_RAW_ACTOR_DATE_UNIQUE_OPH" in enrichment
    assert "resolved.get(\"nik_pemanen\")" in enrichment
    assert "resolved.get(\"nik_kerani\")" in enrichment
    assert "resolved.get(\"nik_mandor\")" in enrichment
    assert "usage_keys AS MATERIALIZED" in enrichment
    assert "requested_keys AS MATERIALIZED" in enrichment
    assert "raw_rows AS MATERIALIZED" in enrichment
    assert "raw_selected AS MATERIALIZED" in enrichment
    assert "JOIN raw_selected r" in enrichment


def test_heavy_pipeline_commands_use_one_global_advisory_lock() -> None:
    db = (ROOT / "src" / "golden_eye" / "db.py").read_text(encoding="utf-8")
    cli = (ROOT / "src" / "golden_eye" / "cli.py").read_text(encoding="utf-8")
    assert "def pipeline_job_lock" in db
    assert "pg_try_advisory_lock" in db
    assert "autocommit=True" in db
    assert "exclusive_pipeline" in db
    assert cli.count("with pipeline_job_lock(") >= 5


def test_harmony_transaction_module_and_scoped_jobs_are_available() -> None:
    app_source = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
    queries = (ROOT / "dashboard" / "queries.py").read_text(encoding="utf-8")
    db = (ROOT / "src" / "golden_eye" / "db.py").read_text(encoding="utf-8")
    cli = (ROOT / "src" / "golden_eye" / "cli.py").read_text(encoding="utf-8")
    scorer = (ROOT / "src" / "golden_eye" / "transport_similarity.py").read_text(
        encoding="utf-8"
    )
    assert 'id="module-tabs"' in app_source
    assert 'id="module-harmony-transaction"' in app_source
    assert 'id="transaction-table"' in app_source
    assert "def load_harmony_transaction_validation" in queries
    validation_body = queries[queries.index("def load_harmony_transaction_validation"):]
    assert 'qname("image_context", RECOVERY_SCHEMA)' in validation_body
    assert "Unit menu ini adalah satu ``(company, oph_key)``" in validation_body
    assert "PARTITION BY p.company, p.oph_key" in validation_body
    assert "WHERE panen_rank=1" in validation_body
    assert "panen_candidate_count" in validation_body
    assert "transport_candidate_count" in validation_body
    assert 'out["transaction_status"] = "SKOR TIDAK TERSEDIA"' in validation_body
    assert '"TIDAK ADA FOTO PANEN"' in validation_body
    assert '"TIDAK ADA FOTO TRANSPORT"' in validation_body
    assert '"transport_has_embedding"' in validation_body
    assert 'has_transport_photo = (' in validation_body
    assert 'out.loc[~has_transport & has_panen_photo, "decision"] = "TIDAK ADA FOTO TRANSPORT"' in validation_body
    assert 'out.loc[ready, "transaction_status"] = "SIAP TRANSAKSI"' in validation_body
    assert "AND d.file_uid=c.file_uid" not in validation_body
    assert "LIMIT {HARMONY_TRANSACTION_ROW_LIMIT}" not in validation_body
    assert '"Total OPH SAP"' in app_source
    assert 'load_sap_kpis(' in app_source
    assert '"similarity terendah lebih dulu"' in app_source
    assert 'na_position="last"' in app_source
    assert 'table_rows = rows if (selected_unavailable or needle) else rows[ready_mask].copy()' in app_source
    assert '"Foto Panen Tidak Tersedia"' in app_source
    assert '"Foto Transport Tidak Tersedia"' in app_source
    assert '"Skor Tidak Tersedia"' in app_source
    assert '"OPH Multi-Foto"' not in app_source
    assert '"Membandingkan foto KP dan KT pada OPH yang sama. "' not in app_source
    assert 'summary = summary[summary["is_active"]].copy()' in queries
    assert "def fetch_corpus_image_bytes" in db
    assert 'id="module-harmony-transaction"' in app_source
    assert "/api/corpus-thumb/" in app_source
    assert "FROM processing.image_context c" in scorer
    assert "FROM gold.reused_photo_detail" not in scorer
    assert "only_pending: bool = False" in scorer
    assert "existing.similarity_score AS existing_similarity_score" in scorer
    assert "PARTITION BY p.oph_key" in scorer
    assert "OR (pair_rank=1 AND existing_similarity_score IS NULL)" in scorer
    assert "if apply and not only_pending:" in scorer
    assert "if apply and only_pending:" in scorer
    assert "if score is not None or not only_pending:" in scorer
    assert 'f"missing_panen={missing_panen_features:,} "' in scorer
    assert 'f"missing_transport={missing_transport_features:,}"' in scorer
    assert "f.full_embedding IS NULL" in scorer
    assert "coalesce(f.embedding_dim, 0) > 0" in scorer
    features = (ROOT / "src" / "golden_eye" / "features.py").read_text(
        encoding="utf-8"
    )
    assert "f.full_embedding IS NULL" in features
    assert "f.embedding_dim <= 0" in features
    assert '"--only-pending"' in cli
    assert '"embed-panen-nas"' in cli
    assert "embed_panen_nas_missing" in cli
    panen_index = (ROOT / "src" / "golden_eye" / "panen_nas_index.py").read_text(
        encoding="utf-8"
    )
    assert "LEFT JOIN processing.image_feature f" in panen_index
    assert "LEFT JOIN processing.gambar_reused g" in panen_index
    assert "LEFT JOIN active_gold_name d" in panen_index
    assert '"unnamed_context_rows"' in panen_index
    assert '"oph_missing_from_hot_store_name"' in panen_index
    assert '"oph_missing_from_active_gold_name"' in panen_index
    assert '"oph_missing_unnamed"' in panen_index
    assert "i.file_name::text" in panen_index
    assert "LEFT JOIN processing.gambar_reused g" in features
    assert "LEFT JOIN active_gold_name d" in features
    assert "lock_scope: str | None = None" in db
    assert "harmony-transaction:{args.source_group.upper()}:{args.year}" in cli


def test_context_matches_every_legacy_evidence_column_not_only_first() -> None:
    context = (ROOT / "src" / "golden_eye" / "context.py").read_text(
        encoding="utf-8"
    )
    cli = (ROOT / "src" / "golden_eye" / "cli.py").read_text(encoding="utf-8")
    assert "def _find_all(" in context
    assert "def _evidence_match_sql(" in context
    assert '"ffb_evidence_kp", "output_file"' in context
    assert "MATCH_BY_OUTPUT_FILE" in context
    assert "evidence_condition" in context
    sync_branch = cli[cli.index('if args.command == "sync-raw-context"') :]
    assert "sync_context(settings, args.source_group)" in sync_branch
    assert "sync_raw_usage_context(settings, args.source_group, all_missing=True)" in sync_branch


def test_lazy_images_reload_when_dash_reuses_img_node_for_new_group() -> None:
    lazy = (ROOT / "dashboard" / "assets" / "lazy_images.js").read_text(
        encoding="utf-8"
    )
    assert "loadedSrc" in lazy
    assert 'attributeFilter: ["data-src"]' in lazy
    assert 'mutations[i].type === "attributes"' in lazy


def test_repository_is_v1_3_16_without_company_specific_validation() -> None:
    version = (ROOT / "src" / "golden_eye" / "__init__.py").read_text(encoding="utf-8")
    validator = (ROOT / "src" / "golden_eye" / "validator.py").read_text(encoding="utf-8")
    publisher = (ROOT / "src" / "golden_eye" / "publisher.py").read_text(encoding="utf-8")
    assert '__version__ = "1.3.16"' in version
    assert "foto DIN" not in validator
    assert "foto DIN" not in publisher


def test_read_only_validation_audit_explains_growth_before_publish() -> None:
    audit = (ROOT / "audit_match_validate_v1_3_16.py").read_text(
        encoding="utf-8"
    )
    assert "GROWTH DIAGNOSIS (READ-ONLY," in audit
    assert "RUN SUDAH ACTIVE" in audit
    assert "BASELINE run_id=" in audit
    assert "run_id<>%s::uuid" in audit
    assert "PUBLISH eligible_groups=" in audit
    assert "FRAGMENTATION source_candidates=" in audit
    assert "ELIGIBLE USAGE PER BULAN" in audit
    assert "size2=" in audit
    assert "added_months=" in audit


def test_date_lineage_audit_checks_filename_context_and_gold_without_bytea() -> None:
    audit = (ROOT / "src" / "golden_eye" / "audit.py").read_text(
        encoding="utf-8"
    )
    cli = (ROOT / "src" / "golden_eye" / "cli.py").read_text(
        encoding="utf-8"
    )
    date_audit = audit[audit.index("def audit_date_lineage(") :]
    assert "SOURCE/CONTEXT DATE CONSISTENCY" in date_audit
    assert "ACTIVE/TARGET GOLD DATE CONSISTENCY" in date_audit
    assert "MISMATCH_MONTH" in date_audit
    assert "image_bytes" not in date_audit
    assert '"audit-dates"' in cli
    assert "audit_date_lineage(" in cli


def test_date_lineage_proves_gold_date_against_unique_raw_production_oph() -> None:
    audit = (ROOT / "src" / "golden_eye" / "audit.py").read_text(
        encoding="utf-8"
    )
    date_audit = audit[audit.index("def _audit_raw_oph_date_evidence(") :]
    assert "RAW PRODUCTION OPH/DATE CROSS-CHECK" in date_audit
    assert "processing.normalize_oph" in date_audit
    assert "requested_oph AS MATERIALIZED" in date_audit
    assert "raw_dates AS MATERIALIZED" in date_audit
    assert "PROVEN_EXACT_DAY" in date_audit
    assert "RAW_AMBIGUOUS_DATES_INCLUDES_EXACT" in date_audit
    assert "RAW_MISMATCH_MONTH" in date_audit
    assert "GOLD USAGE / OPH MULTIPLICITY" in date_audit
    assert "image_bytes" not in date_audit


def test_oph_uniqueness_audit_separates_gold_and_raw_duplicate_meanings() -> None:
    audit = (ROOT / "src" / "golden_eye" / "audit.py").read_text(
        encoding="utf-8"
    )
    cli = (ROOT / "src" / "golden_eye" / "cli.py").read_text(
        encoding="utf-8"
    )
    section = audit[audit.index("def audit_oph_uniqueness(") :]
    assert "ONE-OPH-ONE-USAGE" in section
    assert "OPH BERULANG DI DALAM GROUP YANG SAMA" in section
    assert "OPH YANG SAMA MASUK KE BEBERAPA GROUP" in section
    assert "CROSS-MONTH" in section
    assert "RAW OPH ROW CARDINALITY" in section
    assert "VALIDATED OPH PUBLISH GATE" in section
    assert "RAW/DB45 EXACT FILENAME EVIDENCE PROOF" in section
    assert "RAW/DB45 EXACT TIMESTAMP+ACTOR PROOF" in section
    assert "RAW/DB45 ACTOR+DATE+ESTATE CAPACITY ONLY" in section
    assert "PROVEN_CONTEXT_OPH_EXACT" in section
    assert "PROVEN_RAW_OPH_REPLACEMENT" in section
    assert "RAW_EVIDENCE_FILENAME_NOT_FOUND" in section
    assert "RAW_EVIDENCE_DATE_MISMATCH" in section
    assert "projected_recoverable" in section
    assert "CAPACITY bukan bukti OPH milik foto" in section
    assert "duplicate_occurrences" in section
    assert "image_bytes" not in section
    assert '"audit-oph"' in cli
    assert "audit_oph_uniqueness(" in cli


def test_actor_date_fallback_requires_unique_estate_and_unique_oph() -> None:
    enrichment = (ROOT / "src" / "golden_eye" / "enrichment.py").read_text(
        encoding="utf-8"
    )
    assert "unique_estate_oph AS" in enrichment
    assert "count(DISTINCT nullif(btrim(oph_key), ''))=1" in enrichment
    assert "MATCH_BY_RAW_ACTOR_DATE_UNIQUE_OPH" in enrichment
    assert "MATCH_BY_RAW_TIMESTAMP_ACTOR_UNIQUE_OPH" in enrichment


def test_legacy_oph_recovery_keeps_business_months_and_allocates_raw_oph_once() -> None:
    enrichment = (ROOT / "src" / "golden_eye" / "enrichment.py").read_text(
        encoding="utf-8"
    )
    cleanup = (ROOT / "src" / "golden_eye" / "oph_cleanup.py").read_text(
        encoding="utf-8"
    )
    publisher = (ROOT / "src" / "golden_eye" / "publisher.py").read_text(
        encoding="utf-8"
    )
    assert "def repair_legacy_oph_context(" in enrichment
    repair_section = enrichment[
        enrichment.index("def repair_legacy_oph_context(") :
        enrichment.index("def sync_raw_usage_context(")
    ]
    assert "split_part(u.file_name" not in repair_section
    assert "split_part(m.file_name" in repair_section
    assert "recovery_months:" in enrichment
    assert "to_char(c.period_date, 'YYYY-MM')=ANY(%s)" in enrichment
    assert "tmp_legacy_oph_repair" in enrichment
    assert "tmp_legacy_raw_scope" in enrichment
    assert "tmp_legacy_raw_scope_timestamp_idx" in enrichment
    assert "tmp_legacy_raw_scope_bucket_idx" in enrichment
    assert "[OPH RECOVERY] RAW stage siap" in enrichment
    assert "[OPH RECOVERY] exact filename evidence RAW/DB45" in enrichment
    assert '"legacy_oph_exact_evidence"' in enrichment
    assert "[OPH RECOVERY] exact timestamp selesai" in enrichment
    assert "[OPH RECOVERY] alokasi one-to-one selesai" in enrichment
    assert "MATCH_BY_RAW_ACTOR_DATE_ESTATE_ONE_TO_ONE" in enrichment
    assert "allow_actor_date_allocation: bool = False" in enrichment
    assert "capacity tidak dipakai" in enrichment
    assert "requested.estate_key=p.estate_key" in enrichment
    assert "settings.source_company(source_group)" in enrichment
    assert "settings.account_status_values_for(source_group)" in enrichment
    assert "SELECT DISTINCT ON (r.oph_key)" in enrichment
    assert "repair_legacy_oph_context(" in cleanup
    assert "allow_actor_date_allocation=False" in cleanup
    assert '"planned_recovery_months"' in cleanup
    assert "unsafe_months = sorted(" in cleanup
    assert "planned_recovery_months = unsafe_months" in cleanup
    assert '"current_active_months"' in cleanup
    assert '"oph_clean_expected_months"' in cleanup
    assert "missing_clean_months" in publisher
    assert 'row.get("period_date") or date.max' in publisher
    assert "Publish dibatalkan sebelum Gold lama dinonaktifkan" in publisher


def test_publish_enforces_one_normalized_oph_and_drops_unsafe_legacy_fallback() -> None:
    publisher = (ROOT / "src" / "golden_eye" / "publisher.py").read_text(
        encoding="utf-8"
    )
    cli = (ROOT / "src" / "golden_eye" / "cli.py").read_text(
        encoding="utf-8"
    )
    assert "_UNTRUSTED_OPH_MATCH_METHODS" in publisher
    assert "MATCH_BY_RAW_ACTOR_DATE_UNIQUE_ESTATE" in publisher
    assert "winner_by_oph" in publisher
    assert "duplicate_oph_sanitized" in publisher
    assert "gold_groups_removed_after_oph_gate" in publisher
    assert "active_duplicate_normalized_oph" in cli
    assert "active_group_less_than_two_visual_members" in cli
    assert "visual evidence dengan OPH=NULL" in publisher
    assert "UNVERIFIED_OPH_" in publisher
    assert "gold_visual_usages_without_verified_oph" in publisher
    assert "visual evidence dipulihkan; OPH unverified tetap NULL" in publisher


def test_clean_oph_republish_clones_validate_and_preserves_old_gold_until_publish() -> None:
    cleanup = (ROOT / "src" / "golden_eye" / "oph_cleanup.py").read_text(
        encoding="utf-8"
    )
    cli = (ROOT / "src" / "golden_eye" / "cli.py").read_text(
        encoding="utf-8"
    )
    assert "def republish_clean_oph(" in cleanup
    assert "INSERT INTO processing.validated_group" in cleanup
    assert "INSERT INTO processing.validated_group_member" in cleanup
    assert "publish_gold(settings, new_run_id, source_group)" in cleanup
    assert "match_source(" not in cleanup
    assert "validate_source(" not in cleanup
    assert "VALIDATE are not repeated" in cleanup
    assert 'status="FAILED"' in cleanup
    assert "error_message=str(exc)[:4000]" in cleanup
    assert '"republish-clean-oph"' in cli
    assert "source-run-id" in cli
    assert '"--visual-only"' in cli
    assert "visual_only=args.visual_only" in cli
    assert "context_skipped_visual_only" in cleanup
    assert '"oph_clean_visual_only"' in cleanup
    assert "duplicate_verified_oph" in cleanup
    assert "invalid_visual_groups" in cleanup
    assert "pipeline_job_lock(" in cli


def test_dashboard_source_migration_repairs_elhp_visibility() -> None:
    migration = (ROOT / "sql" / "011_canonical_dashboard_sources.sql").read_text(
        encoding="utf-8"
    )
    assert "UPDATE gold.reused_photo_group_summary" in migration
    assert "UPDATE gold.reused_photo_detail" in migration
    assert "UPDATE gold.reused_photo_dashboard_monthly" in migration
    assert "UPDATE gold.sap_oph_kpi_summary" in migration
    assert "CREATE OR REPLACE VIEW gold.elhp_gt_group_summary_cleaned" in migration
    assert "CREATE OR REPLACE VIEW gold.elhp_gt_detail_transaction" in migration
    assert "CREATE OR REPLACE VIEW gold.elhp_dashboard_monthly_cleaned" in migration
    assert "'DAN', 'DWT', 'ELHP_DAN', 'ELHP_DWT'" in migration


def test_company_summary_is_compact_and_company_guide_exists() -> None:
    app = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
    css = (ROOT / "dashboard" / "assets" / "style.css").read_text(encoding="utf-8")
    cli = (ROOT / "src" / "golden_eye" / "cli.py").read_text(encoding="utf-8")
    assert 'compact=True' in app
    assert 'className="company-summary-graph"' in app
    assert "height: 260px !important" in css
    assert '"company-guide"' in cli
    assert "audit-source --source-group" in cli
    assert 'parser.add_argument("--version"' in cli


def test_optional_raw_index_matches_lookup_expression() -> None:
    context = (ROOT / "src" / "golden_eye" / "context.py").read_text(
        encoding="utf-8"
    )
    sql = (ROOT / "docs" / "OPTIONAL_RAW_CONTEXT_INDEX.sql").read_text(encoding="utf-8")
    assert "ix_raw_production_ffb_evidence_basename_v2" in sql
    assert "processing.basename_upper(btrim(ffb_evidence_kp::text))" in sql
    assert 'value = f"btrim({alias}.{ident(column)}::text)"' in context


def test_bytea_audit_forces_full_read_and_quarantines_only_corrupt_rows() -> None:
    audit = (ROOT / "src" / "golden_eye" / "audit.py").read_text(encoding="utf-8")
    features = (ROOT / "src" / "golden_eye" / "features.py").read_text(encoding="utf-8")
    matcher = (ROOT / "src" / "golden_eye" / "matcher.py").read_text(encoding="utf-8")
    cli = (ROOT / "src" / "golden_eye" / "cli.py").read_text(encoding="utf-8")
    sql = (ROOT / "sql" / "004_bytea_health.sql").read_text(encoding="utf-8")

    assert "md5(image_bytes)" in audit
    assert "def _is_physical_bytea_read_error" in audit
    assert 'sqlstate == "XX001"' in audit
    assert 'sqlstate != "XX000"' in audit
    assert '"previous segment is only"' in audit
    assert "rows[:middle]" in audit and "rows[middle:]" in audit
    assert "DB_BYTEA_DATA_CORRUPTED" in audit
    assert "processing.source_bytea_exception" in audit
    assert '"audit-bytea"' in cli
    assert "LEFT JOIN processing.source_bytea_exception bx" in features
    assert "LEFT JOIN processing.source_bytea_exception bx" in matcher
    assert "DELETE" not in sql.upper()
    assert "DROP" not in sql.upper()


def test_gambar_reused_is_hot_store_not_in_place_silver_update() -> None:
    recovery = (ROOT / "src" / "golden_eye" / "recovery.py").read_text(encoding="utf-8")
    features = (ROOT / "src" / "golden_eye" / "features.py").read_text(encoding="utf-8")
    matcher = (ROOT / "src" / "golden_eye" / "matcher.py").read_text(encoding="utf-8")
    validator = (ROOT / "src" / "golden_eye" / "validator.py").read_text(encoding="utf-8")
    dashboard = (ROOT / "dashboard" / "db.py").read_text(encoding="utf-8")
    sql = (ROOT / "sql" / "013_gambar_reused.sql").read_text(encoding="utf-8")

    assert "processing.gambar_reused" in recovery
    assert "pipeline_job_lock" in recovery
    assert "DB_EXACT_HASH" in recovery
    assert "DB_EXACT_NAME_SIZE" in recovery
    assert "NAS_EXACT_NAME_SIZE_HASH" in recovery
    assert "--apply" in recovery
    assert "UPDATE SILVER" not in recovery.upper()
    assert "DELETE FROM" not in recovery.upper()
    assert "coalesce(r.image_bytes, i.image_bytes)" in features
    assert "gambar_reused r" in matcher
    assert "coalesce(r.image_bytes, i.image_bytes)" in validator
    assert "TABLE_BYTEA_RECOVERY" in dashboard
    assert "CREATE TABLE IF NOT EXISTS processing.gambar_reused" in sql


def test_runtime_uses_gambar_reused_instead_of_legacy_recovery() -> None:
    runtime_paths = [
        ROOT / "src" / "golden_eye" / "audit.py",
        ROOT / "src" / "golden_eye" / "features.py",
        ROOT / "src" / "golden_eye" / "matcher.py",
        ROOT / "src" / "golden_eye" / "recovery.py",
        ROOT / "src" / "golden_eye" / "source_consolidation.py",
        ROOT / "src" / "golden_eye" / "validator.py",
        ROOT / "dashboard" / "db.py",
    ]
    for path in runtime_paths:
        source = path.read_text(encoding="utf-8")
        assert "gambar_reused" in source
    dashboard = (ROOT / "dashboard" / "db.py").read_text(encoding="utf-8")
    assert '"gambar_reused"' in dashboard
    assert '"source_bytea_recovery_v2"' in dashboard


def test_embed_uses_physical_scan_prefetch_and_persistent_writer() -> None:
    features = (ROOT / "src" / "golden_eye" / "features.py").read_text(encoding="utf-8")
    config = (ROOT / "src" / "golden_eye" / "config.py").read_text(encoding="utf-8")

    assert "def _prefetched_source_batches" in features
    assert 'settings.embed_source_read_order == "physical"' in features
    assert 'else "ORDER BY i.file_uid"' in features
    assert "with connection(settings) as write_conn:" in features
    assert "write_conn.commit()" in features
    assert "speed_now=" in features
    assert "db_wait_total=" in features
    assert "EMBED_SOURCE_READ_ORDER" in config
    assert "EMBED_DB_PREFETCH_BATCHES" in config


def test_v1_3_16_env_upgrade_is_universal_and_deduplicates() -> None:
    source = (ROOT / "upgrade_env_v1_3_16.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    namespace: dict[str, Any] = {}
    selected = [
        node
        for node in tree.body
        if isinstance(node, (ast.ImportFrom, ast.Import, ast.Assign, ast.FunctionDef))
        and not (
            isinstance(node, ast.FunctionDef)
            and node.name == "main"
        )
    ]
    exec(compile(ast.Module(body=selected, type_ignores=[]), "upgrade_env", "exec"), namespace)
    cleaned, duplicate_count, changed = namespace["build_clean_env"](
        [
            "PG_PASSWORD=secret\n",
            "VALIDATION_DEVICE=cpu\n",
            "VALIDATION_DEVICE=cuda\n",
            "VALIDATION_MEMBER_MEAN_SIMILARITY=90.0\n",
            "SAP_PRFNR_MAP=DIN=LONG KEJIAK 1\n",
        ]
    )
    rendered = "".join(cleaned)
    assert duplicate_count == 1
    assert rendered.count("VALIDATION_DEVICE=") == 1
    assert "VALIDATION_DEVICE=cuda" in rendered
    assert "PG_PASSWORD=secret" in rendered
    assert "VALIDATION_MEMBER_MEAN_SIMILARITY=93.0" in rendered
    assert "VALIDATION_REPRESENTATIVE_SIMILARITY=93.0" in rendered
    assert "SAP_PRFNR_MAP=DIN=LONG KEJIAK 1\n" in rendered
    assert "LONG KEJIAK" in rendered
    assert "SAP_PRFNR_MAP" not in changed
    assert "SAP_SCOPE_MODE=company" in rendered


def test_cli_exposes_resumable_silver_loader() -> None:
    cli = (ROOT / "src" / "golden_eye" / "cli.py").read_text(encoding="utf-8")
    loader = (
        ROOT / "src" / "golden_eye" / "silver_loader.py"
    ).read_text(encoding="utf-8")

    assert '"load-silver"' in cli
    assert "--source-root" in cli
    assert "--allow-low-disk" in cli
    assert "processing.image_feature" in loader
    assert "processing.gambar_reused" in loader
    assert "ON CONFLICT (file_uid) DO UPDATE" in loader
    assert "CONFLICT_SAME_NAME_DIFFERENT_HASH" in loader
    assert "_decode_image(payload)" in loader
    assert "mode=STREAM_SCAN" in loader
    assert "os.scandir" in loader


def test_elhp_estate_scope_is_strict_in_sql_dashboard_and_cli() -> None:
    queries = (ROOT / "dashboard" / "queries.py").read_text(encoding="utf-8")
    migration = (ROOT / "sql" / "015_elhp_estate_scope.sql").read_text(
        encoding="utf-8"
    )
    cli = (ROOT / "src" / "golden_eye" / "cli.py").read_text(encoding="utf-8")
    audit = (ROOT / "src" / "golden_eye" / "audit.py").read_text(
        encoding="utf-8"
    )

    assert "def estate_matches_elhp_company" in queries
    assert 'expected_prefix = "PU" if company_key == "DAN" else "ME"' in queries
    assert '_filter_elhp_estate_scope(df, "estate_list")' in queries
    assert queries.count('_filter_elhp_estate_scope(df, "estate")') >= 2
    assert "processing.elhp_estate_matches_company" in migration
    assert "DAN=PU*, DWT=ME*" in migration
    assert '"audit-estates"' in cli
    assert "def audit_elhp_estate_scope" in audit


def test_active_gold_image_recovery_honors_elhp_estate_ownership() -> None:
    recovery = (ROOT / "src" / "golden_eye" / "recovery.py").read_text(
        encoding="utf-8"
    )
    target_query = recovery[
        recovery.index("def _gold_evidence_targets") :
        recovery.index("def recover_gold_evidence_from_folders")
    ]
    assert target_query.count("processing.elhp_estate_matches_company") == 3
    assert "upper(source_group) NOT IN ('DAN', 'DWT')" in target_query
