from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
load_dotenv()


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _mapping(name: str, default: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in os.getenv(name, default).split(","):
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        if key.strip() and value.strip():
            out[key.strip().upper()] = value.strip().upper()
    return out


def _csv(name: str, default: str, *, upper: bool = False) -> tuple[str, ...]:
    values = []
    for item in os.getenv(name, default).split(","):
        value = item.strip()
        if value:
            values.append(value.upper() if upper else value.lower())
    return tuple(dict.fromkeys(values))


@dataclass(frozen=True)
class Settings:
    pg_host: str = os.getenv("PG_HOST", "localhost")
    pg_port: int = int(os.getenv("PG_PORT", "5432"))
    pg_database: str = os.getenv("PG_DATABASE", "golden_eye")
    pg_user: str = os.getenv("PG_USER", "postgres")
    pg_password: str = os.getenv("PG_PASSWORD", "")

    silver_schema: str = os.getenv("SILVER_SCHEMA", "silver")
    image_table: str = os.getenv("IMAGE_TABLE", "image_store_all_files")
    business_table: str = os.getenv("BUSINESS_TABLE", "production_plantation_clean")
    # Canonical SAP KPI source. v153 deliberately separates the runtime KPI
    # relation from the legacy SAP_TABLE setting so an old .env cannot keep
    # pointing dashboard/KPI to a stale snapshot. sync-sap-db60 maintains this
    # lightweight live view over silver.zpay_harvesting_db60.
    sap_schema: str = os.getenv("SAP_KPI_SCHEMA", "silver")
    sap_table: str = os.getenv("SAP_KPI_TABLE", "v_zpay_harvesting_live")
    sap_period_year: int = int(os.getenv("SAP_PERIOD_YEAR", "2026"))
    sap_scope_mode: str = os.getenv("SAP_SCOPE_MODE", "company").strip().lower()
    # Legacy OPH provenance TIDAK dinormalkan otomatis. Semua visual finding
    # tetap dapat dilihat pada Visual Audit, sedangkan KPI resmi hanya memakai
    # bukti OPH yang dipercaya. Jika suatu saat perlu eksperimen normalization,
    # aktifkan eksplisit lewat SAP_NORMALIZE_LEGACY_OPH_SOURCES.
    legacy_oph_sap_normalize_sources: tuple[str, ...] = _csv(
        "SAP_NORMALIZE_LEGACY_OPH_SOURCES", "", upper=True
    )
    # Month-scoped exception untuk legacy OPH yang tetap wajib dibuktikan oleh
    # exact SAP key pada bulan yang sama. Default sengaja hanya DAN Jan-2026;
    # bulan DAN lain dan seluruh DWT tetap strict untuk mencegah spike ELHP.
    legacy_oph_sap_normalize_months_raw: str = os.getenv(
        "SAP_NORMALIZE_LEGACY_OPH_MONTHS", "DAN:2026-01"
    )
    # Mapping data PRFNR per company. Ini bukan rule algoritme khusus company:
    # semua company memakai query COUNT DISTINCT yang sama dengan scope datanya.
    sap_prfnr_map: str = os.getenv("SAP_PRFNR_MAP", "")
    # Allowlist estate/PRFNR yang sah untuk denominator SAP per company.
    # Jika sebuah company ada di mapping ini, nilainya menjadi scope otoritatif
    # dan SAP_PRFNR_MAP tidak boleh memperluas denominator ke estate lain.
    # Hard-safe estate scope berlaku untuk SEMUA company Golden Eye. Exact
    # estate dipakai untuk Harmony; wildcard dipakai untuk keluarga estate
    # ELHP lalu di-resolve ke PRFNR SAP aktual saat snapshot dibangun.
    sap_estate_scope_map: str = os.getenv(
        "SAP_ESTATE_SCOPE_MAP",
        (
            "PWP=BUKIT PENDULANGAN 1|BUKIT PENDULANGAN 2|BUKIT PENDULANGAN 3;"
            "DIN=LONG KEJIAK 1|LONG KEJIAK 2|LONG KEJIAK 3;"
            "DAN=PU*;DWT=ME*"
        ),
    )
    raw_business_schema: str = os.getenv("RAW_BUSINESS_SCHEMA", "raw")
    raw_business_table: str = os.getenv("RAW_BUSINESS_TABLE", "production_plantation")

    # Microsoft SQL Server hanya dipakai sebagai sumber metadata evidence
    # transport. Password tidak pernah ditulis ke source/repository.
    mssql_server: str = os.getenv("MSSQL_SERVER", "")
    mssql_port: int = int(os.getenv("MSSQL_PORT", "1433"))
    mssql_database: str = os.getenv("MSSQL_DATABASE", "DASHBOARD")
    mssql_user: str = os.getenv("MSSQL_USER", "audit")
    mssql_password: str = os.getenv("MSSQL_PASSWORD", "")
    mssql_driver: str = os.getenv("MSSQL_DRIVER", "SQL Server")
    mssql_transport_table: str = os.getenv(
        "MSSQL_TRANSPORT_TABLE", "dbo.PRODUCTION_PLANTATION"
    )
    mssql_transport_oph_column: str = os.getenv(
        "MSSQL_TRANSPORT_OPH_COLUMN", "HARVEST_TICKET"
    )
    mssql_transport_evidence_column: str = os.getenv(
        "MSSQL_TRANSPORT_EVIDENCE_COLUMN", "FFB_EVIDENCE_KT_BEFORE"
    )
    mssql_transport_company_column: str = os.getenv(
        "MSSQL_TRANSPORT_COMPANY_COLUMN", "COMPANY_CODE"
    )
    mssql_transport_year_column: str = os.getenv(
        "MSSQL_TRANSPORT_YEAR_COLUMN", "PERIOD_YEAR"
    )
    mssql_transport_date_column: str = os.getenv(
        "MSSQL_TRANSPORT_DATE_COLUMN", "PERIOD_DATE"
    )
    # Monthly mirror DB45 -> PostgreSQL RAW.  Nilai dipisahkan dari konfigurasi
    # transport supaya pergantian source/view transport tidak ikut mengubah
    # sumber transaksi produksi utama.
    mssql_raw_table: str = os.getenv(
        "MSSQL_RAW_TABLE", "dbo.PRODUCTION_PLANTATION"
    )
    mssql_raw_company_column: str = os.getenv(
        "MSSQL_RAW_COMPANY_COLUMN", "COMPANY_CODE"
    )
    mssql_raw_year_column: str = os.getenv(
        "MSSQL_RAW_YEAR_COLUMN", "PERIOD_YEAR"
    )
    mssql_raw_month_column: str = os.getenv(
        "MSSQL_RAW_MONTH_COLUMN", "PERIOD_MONTH"
    )
    mssql_raw_date_column: str = os.getenv(
        "MSSQL_RAW_DATE_COLUMN", "PERIOD_DATE"
    )
    mssql_raw_status_column: str = os.getenv(
        "MSSQL_RAW_STATUS_COLUMN", "ACCOUNT_STATUS"
    )
    mssql_raw_oph_column: str = os.getenv(
        "MSSQL_RAW_OPH_COLUMN", "HARVEST_TICKET"
    )
    mssql_raw_key_columns: tuple[str, ...] | None = None

    model_name: str = os.getenv("MODEL_NAME", "resnet18")
    model_version: str = os.getenv("MODEL_VERSION", "resnet18_imagenet_v1")
    model_weights_path: str = os.getenv("MODEL_WEIGHTS_PATH", "")
    use_gpu: bool = _bool("USE_GPU", True)
    embed_batch_size: int = int(os.getenv("EMBED_BATCH_SIZE", "128"))
    db_fetch_size: int = int(os.getenv("DB_FETCH_SIZE", "256"))
    embed_source_read_order: str = os.getenv(
        "EMBED_SOURCE_READ_ORDER", "physical"
    ).strip().lower()
    embed_db_prefetch_batches: int = int(os.getenv("EMBED_DB_PREFETCH_BATCHES", "2"))
    valid_extensions: tuple[str, ...] | None = None
    exclude_filename_tokens: tuple[str, ...] | None = None
    account_status_values: tuple[str, ...] | None = None
    account_status_unfiltered_sources: tuple[str, ...] | None = None

    review_threshold: float = float(os.getenv("REVIEW_THRESHOLD", "90.0"))
    reused_threshold: float = float(os.getenv("REUSED_THRESHOLD", "93.0"))
    match_top_k: int = int(os.getenv("MATCH_TOP_K", "80"))
    match_query_block: int = int(os.getenv("MATCH_QUERY_BLOCK", "2048"))
    match_reference_block: int = int(os.getenv("MATCH_REFERENCE_BLOCK", "8192"))
    match_mode: str = os.getenv("MATCH_MODE", "knn").strip().lower()
    match_engine: str = os.getenv("MATCH_ENGINE", "auto").strip().lower()
    faiss_index_mode: str = os.getenv("FAISS_INDEX_MODE", "auto").strip().lower()
    faiss_flat_max_images: int = int(os.getenv("FAISS_FLAT_MAX_IMAGES", "100000"))
    faiss_hnsw_m: int = int(os.getenv("FAISS_HNSW_M", "32"))
    faiss_hnsw_ef_construction: int = int(os.getenv("FAISS_HNSW_EF_CONSTRUCTION", "100"))
    faiss_hnsw_ef_search: int = int(os.getenv("FAISS_HNSW_EF_SEARCH", "128"))
    match_auto_cap_top_k: bool = _bool("MATCH_AUTO_CAP_TOP_K", True)
    match_require_mutual_knn: bool = _bool("MATCH_REQUIRE_MUTUAL_KNN", True)
    match_component_max_size: int = int(os.getenv("MATCH_COMPONENT_MAX_SIZE", "500"))
    deduplicate_logical_filename: bool = _bool("DEDUP_LOGICAL_FILENAME", True)
    max_match_edges: int = int(os.getenv("MAX_MATCH_EDGES", "5000000"))
    torch_fallback_max_images: int = int(os.getenv("TORCH_FALLBACK_MAX_IMAGES", "100000"))

    max_accepted_group_size: int = int(os.getenv("MAX_ACCEPTED_GROUP_SIZE", "500"))
    max_validation_group_read: int = int(os.getenv("MAX_VALIDATION_GROUP_READ", "5000"))
    center_crop_ratio: float = float(os.getenv("CENTER_CROP_RATIO", "0.72"))
    pair_score_edge: float = float(os.getenv("PAIR_SCORE_EDGE", "0.54"))
    pair_score_strong: float = float(os.getenv("PAIR_SCORE_STRONG", "0.77"))
    min_modal_support: int = int(os.getenv("MIN_MODAL_SUPPORT", "2"))
    validation_top_k_oversize: int = int(os.getenv("VALIDATION_TOP_K_OVERSIZE", "80"))
    validation_full_pair_max_group: int = int(
        os.getenv("VALIDATION_FULL_PAIR_MAX_GROUP", "40")
    )
    validation_device: str = os.getenv("VALIDATION_DEVICE", "auto").strip().lower()
    validation_gpu_pair_batch: int = int(os.getenv("VALIDATION_GPU_PAIR_BATCH", "65536"))
    validation_cpu_workers: int = int(os.getenv("VALIDATION_CPU_WORKERS", "8"))
    validation_nas_io_workers: int = int(
        os.getenv("VALIDATION_NAS_IO_WORKERS", "24")
    )
    validation_cpu_pair_batch: int = int(os.getenv("VALIDATION_CPU_PAIR_BATCH", "512"))
    validation_db_member_batch: int = int(os.getenv("VALIDATION_DB_MEMBER_BATCH", "2000"))
    validation_db_candidate_batch: int = int(os.getenv("VALIDATION_DB_CANDIDATE_BATCH", "500"))
    validation_source_read_order: str = os.getenv(
        "VALIDATION_SOURCE_READ_ORDER", "physical"
    ).strip().lower()
    validation_max_audit_pairs_per_group: int = int(
        os.getenv("VALIDATION_MAX_AUDIT_PAIRS_PER_GROUP", "500")
    )
    strict_group_validation: bool = _bool("STRICT_GROUP_VALIDATION", True)
    validation_group_mean_similarity: float = float(
        os.getenv("VALIDATION_GROUP_MEAN_SIMILARITY", "90.0")
    )
    validation_member_mean_similarity: float = float(
        os.getenv("VALIDATION_MEMBER_MEAN_SIMILARITY", "93.0")
    )
    validation_representative_similarity: float = float(
        os.getenv("VALIDATION_REPRESENTATIVE_SIMILARITY", "93.0")
    )
    low_light_guard: bool = _bool("LOW_LIGHT_GUARD", True)
    low_light_max_mean: float = float(os.getenv("LOW_LIGHT_MAX_MEAN", "68.0"))
    low_light_dark_pixel_threshold: int = int(
        os.getenv("LOW_LIGHT_DARK_PIXEL_THRESHOLD", "55")
    )
    low_light_min_dark_fraction: float = float(
        os.getenv("LOW_LIGHT_MIN_DARK_FRACTION", "0.55")
    )
    low_light_near_exact_similarity: float = float(
        os.getenv("LOW_LIGHT_NEAR_EXACT_SIMILARITY", "95.0")
    )
    low_light_max_phash_distance: int = int(
        os.getenv("LOW_LIGHT_MAX_PHASH_DISTANCE", "8")
    )
    low_light_max_edgehash_distance: int = int(
        os.getenv("LOW_LIGHT_MAX_EDGEHASH_DISTANCE", "10")
    )
    low_light_min_edgevec_similarity: float = float(
        os.getenv("LOW_LIGHT_MIN_EDGEVEC_SIMILARITY", "0.72")
    )
    low_light_min_orb_inliers: int = int(
        os.getenv("LOW_LIGHT_MIN_ORB_INLIERS", "8")
    )
    low_light_min_orb_ratio: float = float(
        os.getenv("LOW_LIGHT_MIN_ORB_RATIO", "0.12")
    )
    duplicate_require_structural_evidence: bool = _bool(
        "DUPLICATE_REQUIRE_STRUCTURAL_EVIDENCE", True
    )
    duplicate_near_exact_similarity: float = float(
        os.getenv("DUPLICATE_NEAR_EXACT_SIMILARITY", "96.0")
    )
    duplicate_max_phash_distance: int = int(
        os.getenv("DUPLICATE_MAX_PHASH_DISTANCE", "10")
    )
    duplicate_max_edgehash_distance: int = int(
        os.getenv("DUPLICATE_MAX_EDGEHASH_DISTANCE", "12")
    )
    duplicate_min_edgevec_similarity: float = float(
        os.getenv("DUPLICATE_MIN_EDGEVEC_SIMILARITY", "0.80")
    )
    duplicate_min_orb_inliers: int = int(
        os.getenv("DUPLICATE_MIN_ORB_INLIERS", "12")
    )
    duplicate_min_orb_ratio: float = float(
        os.getenv("DUPLICATE_MIN_ORB_RATIO", "0.18")
    )
    duplicate_geometric_similarity: float = float(
        os.getenv("DUPLICATE_GEOMETRIC_SIMILARITY", "93.0")
    )
    duplicate_require_hash_support: bool = _bool(
        "DUPLICATE_REQUIRE_HASH_SUPPORT", True
    )
    duplicate_geometric_max_hash_distance: int = int(
        os.getenv("DUPLICATE_GEOMETRIC_MAX_HASH_DISTANCE", "18")
    )

    transport_review_threshold: float = float(os.getenv("TRANSPORT_REVIEW_THRESHOLD", "90.0"))
    transport_reused_threshold: float = float(os.getenv("TRANSPORT_REUSED_THRESHOLD", "93.0"))
    harmony_transaction_same_threshold: float = float(
        os.getenv("HARMONY_TRANSACTION_SAME_THRESHOLD", "83.0")
    )
    min_context_match_rate: float = float(os.getenv("MIN_CONTEXT_MATCH_RATE", "0.80"))
    require_complete_month: bool = _bool("REQUIRE_COMPLETE_MONTH", True)
    filename_timestamp_utc_offset_hours: int = int(
        os.getenv("FILENAME_TIMESTAMP_UTC_OFFSET_HOURS", "7")
    )
    publish_require_estate: bool = _bool("PUBLISH_REQUIRE_ESTATE", True)
    min_publish_mean_confirmed: float = float(
        os.getenv("MIN_PUBLISH_MEAN_CONFIRMED", "0.50")
    )
    min_publish_mean_review: float = float(
        os.getenv("MIN_PUBLISH_MEAN_REVIEW", "0.52")
    )
    max_gold_group_growth_factor: float = float(
        os.getenv("MAX_GOLD_GROUP_GROWTH_FACTOR", "2.0")
    )
    max_gold_usage_growth_factor: float = float(
        os.getenv("MAX_GOLD_USAGE_GROWTH_FACTOR", "3.0")
    )

    source_system_map: dict[str, str] | None = None
    source_company_map: dict[str, str] | None = None
    # SAP BUKRS is not the same identifier as Golden Eye company/source labels.
    # Keep this mapping separate so DB45/raw logic can continue using PWP/DIN/DAN/DWT
    # while SAP is guarded by its actual company codes (PW/DI/DA/DW).
    sap_company_code_map: dict[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "valid_extensions",
            _csv("VALID_EXTENSIONS", ".jpg,.jpeg,.png,.bmp,.webp"),
        )
        object.__setattr__(
            self,
            "exclude_filename_tokens",
            _csv("EXCLUDE_FILENAME_TOKENS", "AI", upper=True),
        )
        object.__setattr__(
            self,
            "account_status_values",
            _csv("ACCOUNT_STATUS_VALUES", "ACTUAL,ACTUAL APPROVE", upper=True),
        )
        object.__setattr__(
            self,
            "account_status_unfiltered_sources",
            _csv("ACCOUNT_STATUS_UNFILTERED_SOURCES", "DAN,DWT", upper=True),
        )
        object.__setattr__(
            self,
            "mssql_raw_key_columns",
            _csv("MSSQL_RAW_KEY_COLUMNS", "", upper=True),
        )
        object.__setattr__(
            self,
            "source_system_map",
            _mapping(
                "SOURCE_SYSTEM_MAP",
                "PWP:HARMONY,DIN:HARMONY,DAN:ELHP,DWT:ELHP,"
                "ELHP_DIN:HARMONY,ELHP_DAN:ELHP,ELHP_DWT:ELHP,TRANSPORT:HARMONY",
            ),
        )
        object.__setattr__(
            self,
            "source_company_map",
            _mapping(
                "SOURCE_COMPANY_MAP",
                "PWP:PWP,DIN:DIN,DAN:DAN,DWT:DWT,"
                "ELHP_DIN:DIN,ELHP_DAN:DAN,ELHP_DWT:DWT,TRANSPORT:TRANSPORT",
            ),
        )
        object.__setattr__(
            self,
            "sap_company_code_map",
            _mapping(
                "SAP_COMPANY_CODE_MAP",
                "PWP:PW,DIN:DI,DAN:DA,DWT:DW,ELHP_DIN:DI,ELHP_DAN:DA,ELHP_DWT:DW",
            ),
        )

    def connection_kwargs(self) -> dict[str, object]:
        if not self.pg_password:
            raise RuntimeError("PG_PASSWORD masih kosong. Salin .env.example menjadi .env lalu isi password.")
        return {
            "host": self.pg_host,
            "port": self.pg_port,
            "dbname": self.pg_database,
            "user": self.pg_user,
            "password": self.pg_password,
            "connect_timeout": 30,
            "application_name": "golden_eye_db_first",
        }

    def mssql_connection_string(self, driver: str | None = None) -> str:
        if not self.mssql_password:
            raise RuntimeError(
                "MSSQL_PASSWORD masih kosong. Isi credential SQL Server di .env."
            )
        # ODBC Driver 18 mengenkripsi secara default, sedangkan instalasi lama
        # sering hanya mempunyai driver bernama 'SQL Server'. Opsi ini bekerja
        # untuk koneksi internal dengan sertifikat server yang belum trusted.
        effective_driver = str(driver or self.mssql_driver).strip()
        modern_tls = (
            "Encrypt=no;TrustServerCertificate=yes;"
            if effective_driver.upper().startswith("ODBC DRIVER")
            else ""
        )
        return (
            f"DRIVER={{{effective_driver}}};"
            f"SERVER={self.mssql_server},{self.mssql_port};"
            f"DATABASE={self.mssql_database};"
            f"UID={self.mssql_user};PWD={self.mssql_password};"
            f"{modern_tls}"
            "APP=golden_eye_transport_sync;"
        )

    def source_system(self, source_group: str) -> str:
        return (self.source_system_map or {}).get(source_group.upper(), "HARMONY")

    def source_company(self, source_group: str) -> str:
        return (self.source_company_map or {}).get(source_group.upper(), source_group.upper())

    def sap_company_code(self, source_group: str) -> str:
        """Return the SAP company code (BUKRS) for one Golden Eye source.

        Golden Eye labels are PWP/DIN/DAN/DWT, while SAP ZPAY stores the
        corresponding company codes as PW/DI/DA/DW.  This mapping must remain
        separate from ``source_company()`` because that method is also used by
        DB45/raw lineage where the Golden Eye labels are correct.
        """
        canonical = self._sap_scope_company(source_group)
        return (self.sap_company_code_map or {}).get(canonical, canonical)

    def account_status_values_for(self, source_group: str) -> tuple[str, ...]:
        """Return the optional production status scope for one source.

        ELHP DAN/DWT download all non-empty panen evidence by default.  Keeping
        this decision in configuration prevents RAW, Context, and audit from
        silently applying different populations.  Set
        ``ACCOUNT_STATUS_UNFILTERED_SOURCES=`` to restore the global filter for
        every source, or add another canonical source when its downloader also
        has no status filter.
        """
        target = str(source_group or "").upper().strip().removeprefix("ELHP_")
        unfiltered = {
            str(value).upper().strip().removeprefix("ELHP_")
            for value in (self.account_status_unfiltered_sources or ())
        }
        if target in unfiltered:
            return ()
        return tuple(self.account_status_values or ())

    @staticmethod
    def _sap_scope_company(source_group: str) -> str:
        """Normalize alias source-group ke company pemilik estate SAP."""
        target = str(source_group or "").upper().strip()
        return {
            "ELHP_DAN": "DAN",
            "ELHP_DWT": "DWT",
        }.get(target, target)

    @classmethod
    def _sap_scope_values(cls, mapping: str, source_group: str) -> tuple[str, ...]:
        """Parse ``COMPANY=A|B;OTHER=C`` mapping secara konsisten.

        Nilai boleh berupa exact PRFNR atau wildcard ``*``. Wildcard baru
        di-resolve terhadap PRFNR yang benar-benar ada di tabel SAP saat
        snapshot KPI dibangun.
        """
        target = cls._sap_scope_company(source_group)
        for item in str(mapping or "").split(";"):
            if "=" not in item:
                continue
            key, raw_values = item.split("=", 1)
            if cls._sap_scope_company(key) != target:
                continue
            values = [
                value.upper().strip()
                for value in raw_values.split("|")
                if value.strip()
            ]
            return tuple(dict.fromkeys(values))
        return ()

    def sap_estate_scope_values(self, source_group: str) -> tuple[str, ...]:
        """Return hard-safe estate/PRFNR scope untuk satu company.

        Aturan berlaku ke PWP, DIN, DAN, dan DWT. Jika file ``.env`` lama hanya
        mendefinisikan sebagian company, company yang hilang tetap mendapat
        hard-safe default dan tidak fallback ke ``SAP_PRFNR_MAP`` yang mungkin
        berisi estate eksternal. DAN/DWT memakai prefix wildcard agar nama PRFNR
        panjang tidak perlu ditebak; publisher akan menyimpannya kembali sebagai
        daftar PRFNR exact setelah resolve dari SAP.
        """
        target = self._sap_scope_company(source_group)
        configured = self._sap_scope_values(self.sap_estate_scope_map, target)
        if configured:
            return configured
        defaults: dict[str, tuple[str, ...]] = {
            "PWP": (
                "BUKIT PENDULANGAN 1",
                "BUKIT PENDULANGAN 2",
                "BUKIT PENDULANGAN 3",
            ),
            "DIN": (
                "LONG KEJIAK 1",
                "LONG KEJIAK 2",
                "LONG KEJIAK 3",
            ),
            "DAN": ("PU*",),
            "DWT": ("ME*",),
        }
        return defaults.get(target, ())

    def sap_configured_prfnr_values(self, source_group: str) -> tuple[str, ...]:
        """Return legacy/raw PRFNR map sebelum estate hard-guard diterapkan."""
        return self._sap_scope_values(self.sap_prfnr_map, source_group)

    def sap_prfnr_values(self, source_group: str) -> tuple[str, ...]:
        """Return effective SAP PRFNR scope untuk canonical company.

        Jika ``SAP_ESTATE_SCOPE_MAP`` mendefinisikan company tersebut, allowlist
        estate menjadi sumber kebenaran. Jika tidak, fallback ke mapping PRFNR
        lama agar company lain tetap backward-compatible.
        """
        estate_scope = self.sap_estate_scope_values(source_group)
        if estate_scope:
            return estate_scope
        return self.sap_configured_prfnr_values(source_group)

    def legacy_oph_sap_normalize_month_pairs(self) -> set[tuple[str, str]]:
        out: set[tuple[str, str]] = set()
        raw = str(self.legacy_oph_sap_normalize_months_raw or "")
        for token in re.split(r"[;,]", raw):
            value = token.strip()
            if not value or ":" not in value:
                continue
            source, month = value.split(":", 1)
            source = source.strip().upper()
            month = month.strip()
            if source and re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", month):
                out.add((source, month))
        return out

    def excluded_filename_regex(self) -> str:
        tokens = "|".join(re.escape(value) for value in (self.exclude_filename_tokens or ()))
        return rf"(^|[^A-Z0-9])({tokens})([^A-Z0-9]|$)" if tokens else r"a^"

    def valid_extension_regex(self) -> str:
        extensions = [re.escape(value.lstrip(".")) for value in (self.valid_extensions or ())]
        return rf"\.({'|'.join(extensions)})$" if extensions else r"\.[^.]+$"

    def validate(self) -> None:
        if not 2000 <= self.sap_period_year <= 2100:
            raise ValueError("SAP_PERIOD_YEAR harus antara 2000 dan 2100")
        if self.sap_scope_mode not in {"global", "company"}:
            raise ValueError("SAP_SCOPE_MODE harus global atau company")
        if self.review_threshold > self.reused_threshold:
            raise ValueError("REVIEW_THRESHOLD tidak boleh lebih tinggi dari REUSED_THRESHOLD")
        if not 0.0 <= self.harmony_transaction_same_threshold <= 100.0:
            raise ValueError(
                "HARMONY_TRANSACTION_SAME_THRESHOLD harus antara 0 dan 100"
            )
        if self.embed_source_read_order not in {"physical", "file_uid"}:
            raise ValueError("EMBED_SOURCE_READ_ORDER harus physical atau file_uid")
        if not 1 <= self.embed_db_prefetch_batches <= 8:
            raise ValueError("EMBED_DB_PREFETCH_BATCHES harus 1 sampai 8")
        if self.max_accepted_group_size < 2:
            raise ValueError("MAX_ACCEPTED_GROUP_SIZE minimal 2")
        if self.max_validation_group_read < self.max_accepted_group_size:
            raise ValueError("MAX_VALIDATION_GROUP_READ tidak boleh di bawah MAX_ACCEPTED_GROUP_SIZE")
        if self.match_component_max_size < 2:
            raise ValueError("MATCH_COMPONENT_MAX_SIZE minimal 2")
        if self.match_component_max_size > self.max_accepted_group_size:
            raise ValueError(
                "MATCH_COMPONENT_MAX_SIZE tidak boleh melebihi MAX_ACCEPTED_GROUP_SIZE"
            )
        if not 2 <= self.validation_full_pair_max_group <= self.max_accepted_group_size:
            raise ValueError(
                "VALIDATION_FULL_PAIR_MAX_GROUP harus 2 sampai MAX_ACCEPTED_GROUP_SIZE"
            )
        if self.validation_device not in {"auto", "cuda", "cpu"}:
            raise ValueError("VALIDATION_DEVICE harus auto, cuda, atau cpu")
        if self.validation_gpu_pair_batch < 1:
            raise ValueError("VALIDATION_GPU_PAIR_BATCH minimal 1")
        if not 1 <= self.validation_cpu_workers <= 64:
            raise ValueError("VALIDATION_CPU_WORKERS harus 1 sampai 64")
        if not 1 <= self.validation_nas_io_workers <= 64:
            raise ValueError("VALIDATION_NAS_IO_WORKERS harus 1 sampai 64")
        if self.validation_cpu_pair_batch < 1:
            raise ValueError("VALIDATION_CPU_PAIR_BATCH minimal 1")
        if self.validation_db_member_batch < 2:
            raise ValueError("VALIDATION_DB_MEMBER_BATCH minimal 2")
        if self.validation_db_candidate_batch < 1:
            raise ValueError("VALIDATION_DB_CANDIDATE_BATCH minimal 1")
        if self.validation_source_read_order not in {"physical", "file_uid"}:
            raise ValueError("VALIDATION_SOURCE_READ_ORDER harus physical atau file_uid")
        if self.validation_max_audit_pairs_per_group < 0:
            raise ValueError("VALIDATION_MAX_AUDIT_PAIRS_PER_GROUP minimal 0")
        if not self.review_threshold <= self.validation_group_mean_similarity <= 100.0:
            raise ValueError(
                "VALIDATION_GROUP_MEAN_SIMILARITY harus antara REVIEW_THRESHOLD dan 100"
            )
        if not self.review_threshold <= self.validation_member_mean_similarity <= 100.0:
            raise ValueError(
                "VALIDATION_MEMBER_MEAN_SIMILARITY harus antara REVIEW_THRESHOLD dan 100"
            )
        if not self.reused_threshold <= self.validation_representative_similarity <= 100.0:
            raise ValueError(
                "VALIDATION_REPRESENTATIVE_SIMILARITY harus antara REUSED_THRESHOLD dan 100"
            )
        if not 0.0 <= self.low_light_max_mean <= 255.0:
            raise ValueError("LOW_LIGHT_MAX_MEAN harus 0 sampai 255")
        if not 0 <= self.low_light_dark_pixel_threshold <= 255:
            raise ValueError("LOW_LIGHT_DARK_PIXEL_THRESHOLD harus 0 sampai 255")
        if not 0.0 <= self.low_light_min_dark_fraction <= 1.0:
            raise ValueError("LOW_LIGHT_MIN_DARK_FRACTION harus 0 sampai 1")
        if not 0.0 <= self.low_light_near_exact_similarity <= 100.0:
            raise ValueError("LOW_LIGHT_NEAR_EXACT_SIMILARITY harus 0 sampai 100")
        if not 0 <= self.low_light_max_phash_distance <= 64:
            raise ValueError("LOW_LIGHT_MAX_PHASH_DISTANCE harus 0 sampai 64")
        if not 0 <= self.low_light_max_edgehash_distance <= 64:
            raise ValueError("LOW_LIGHT_MAX_EDGEHASH_DISTANCE harus 0 sampai 64")
        if not 0.0 <= self.low_light_min_edgevec_similarity <= 1.0:
            raise ValueError("LOW_LIGHT_MIN_EDGEVEC_SIMILARITY harus 0 sampai 1")
        if self.low_light_min_orb_inliers < 0:
            raise ValueError("LOW_LIGHT_MIN_ORB_INLIERS minimal 0")
        if not 0.0 <= self.low_light_min_orb_ratio <= 1.0:
            raise ValueError("LOW_LIGHT_MIN_ORB_RATIO harus 0 sampai 1")
        if not 0.0 <= self.duplicate_near_exact_similarity <= 100.0:
            raise ValueError("DUPLICATE_NEAR_EXACT_SIMILARITY harus 0 sampai 100")
        if not 0 <= self.duplicate_max_phash_distance <= 64:
            raise ValueError("DUPLICATE_MAX_PHASH_DISTANCE harus 0 sampai 64")
        if not 0 <= self.duplicate_max_edgehash_distance <= 64:
            raise ValueError("DUPLICATE_MAX_EDGEHASH_DISTANCE harus 0 sampai 64")
        if not 0.0 <= self.duplicate_min_edgevec_similarity <= 1.0:
            raise ValueError("DUPLICATE_MIN_EDGEVEC_SIMILARITY harus 0 sampai 1")
        if self.duplicate_min_orb_inliers < 0:
            raise ValueError("DUPLICATE_MIN_ORB_INLIERS minimal 0")
        if not 0.0 <= self.duplicate_min_orb_ratio <= 1.0:
            raise ValueError("DUPLICATE_MIN_ORB_RATIO harus 0 sampai 1")
        if not self.reused_threshold <= self.duplicate_geometric_similarity <= 100.0:
            raise ValueError(
                "DUPLICATE_GEOMETRIC_SIMILARITY harus antara REUSED_THRESHOLD dan 100"
            )
        if not 0 <= self.duplicate_geometric_max_hash_distance <= 64:
            raise ValueError("DUPLICATE_GEOMETRIC_MAX_HASH_DISTANCE harus 0 sampai 64")
        if not 0.0 <= self.min_publish_mean_confirmed <= 1.0:
            raise ValueError("MIN_PUBLISH_MEAN_CONFIRMED harus 0 sampai 1")
        if not 0.0 <= self.min_publish_mean_review <= 1.0:
            raise ValueError("MIN_PUBLISH_MEAN_REVIEW harus 0 sampai 1")
        if self.max_gold_group_growth_factor != 0 and self.max_gold_group_growth_factor < 1.0:
            raise ValueError("MAX_GOLD_GROUP_GROWTH_FACTOR harus 0 atau minimal 1")
        if self.max_gold_usage_growth_factor != 0 and self.max_gold_usage_growth_factor < 1.0:
            raise ValueError("MAX_GOLD_USAGE_GROWTH_FACTOR harus 0 atau minimal 1")
        if self.match_mode not in {"knn", "exact"}:
            raise ValueError("MATCH_MODE harus knn atau exact")
        if self.match_engine not in {"auto", "faiss", "torch"}:
            raise ValueError("MATCH_ENGINE harus auto, faiss, atau torch")
        if self.faiss_index_mode not in {"auto", "flat", "hnsw"}:
            raise ValueError("FAISS_INDEX_MODE harus auto, flat, atau hnsw")
        if self.faiss_flat_max_images < 1000:
            raise ValueError("FAISS_FLAT_MAX_IMAGES minimal 1000")
        if self.faiss_hnsw_m < 4:
            raise ValueError("FAISS_HNSW_M minimal 4")
        if self.faiss_hnsw_ef_construction < 8:
            raise ValueError("FAISS_HNSW_EF_CONSTRUCTION minimal 8")
        if self.faiss_hnsw_ef_search < 8:
            raise ValueError("FAISS_HNSW_EF_SEARCH minimal 8")
        if self.max_match_edges < 1000:
            raise ValueError("MAX_MATCH_EDGES minimal 1000")
        if self.torch_fallback_max_images < 1000:
            raise ValueError("TORCH_FALLBACK_MAX_IMAGES minimal 1000")
        if not 0.0 <= self.min_context_match_rate <= 1.0:
            raise ValueError("MIN_CONTEXT_MATCH_RATE harus 0 sampai 1")
        if not -12 <= self.filename_timestamp_utc_offset_hours <= 14:
            raise ValueError("FILENAME_TIMESTAMP_UTC_OFFSET_HOURS harus -12 sampai 14")
