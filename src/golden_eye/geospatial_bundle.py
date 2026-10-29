from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Settings
from .db import connection
from .geospatial import import_boundaries, sync_tph_points


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUNDLED_ROOT = PROJECT_ROOT / "data" / "geospatial" / "whu_reused"
BUNDLED_BOUNDARIES = {
    "DAN": BUNDLED_ROOT / "DAN" / "DAN_blok_reused_4326.shp",
    "DIN": BUNDLED_ROOT / "DIN" / "DIN_blok_reused_4326.shp",
    "DWT": BUNDLED_ROOT / "DWT" / "DWT_blok_reused_4326.shp",
}


def ensure_geospatial_schema(settings: Settings) -> None:
    """Create the geospatial reference tables if this project predates sql/020."""
    sql_path = PROJECT_ROOT / "sql" / "020_geospatial_reference.sql"
    if not sql_path.exists():
        raise FileNotFoundError(f"Schema SQL geospatial tidak ditemukan: {sql_path}")
    sql_text = sql_path.read_text(encoding="utf-8")
    with connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(sql_text)
        conn.commit()


def _active_counts(settings: Settings) -> dict[str, int]:
    with connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT company_norm, count(*)::bigint
                FROM reference.geo_block_boundary
                WHERE is_active
                  AND company_norm IN ('DAN','DIN','DWT')
                GROUP BY company_norm
                ORDER BY company_norm
                """
            )
            return {str(company): int(count) for company, count in cur.fetchall()}


def sync_bundled_whu_boundaries(
    settings: Settings,
    *,
    companies: list[str] | None = None,
    simplify_tolerance: float = 0.00003,
    apply: bool = False,
    sync_tph: bool = False,
) -> dict[str, Any]:
    """Import bundled WHU block boundaries for DAN/DIN/DWT.

    The bundle is deliberately separate from PWP: PWP keeps its existing spatial source.
    Business-key mapping is explicit: DEPARTEMEN -> estate, AFD -> afdeling, BLOK -> blok.
    """
    selected = [str(c).upper().strip() for c in (companies or ["DAN", "DIN", "DWT"])]
    invalid = [c for c in selected if c not in BUNDLED_BOUNDARIES]
    if invalid:
        raise ValueError(
            "Bundled WHU hanya tersedia untuk DAN/DIN/DWT. Invalid=" + ",".join(invalid)
        )

    print("=" * 88, flush=True)
    print("GOLDEN EYE - BUNDLED WHU GEOSPATIAL SYNC", flush=True)
    print("=" * 88, flush=True)
    print(f"companies={','.join(selected)} apply={apply} sync_tph={sync_tph}", flush=True)
    print(f"bundle_root={BUNDLED_ROOT}", flush=True)

    for company in selected:
        shp = BUNDLED_BOUNDARIES[company]
        if not shp.exists():
            raise FileNotFoundError(
                f"Bundled boundary {company} tidak ditemukan: {shp}. "
                "Pastikan ZIP full replacement diextract lengkap."
            )
        missing = [str(shp.with_suffix(ext)) for ext in (".dbf", ".shx") if not shp.with_suffix(ext).exists()]
        if missing:
            raise FileNotFoundError(
                f"Bundled shapefile {company} tidak lengkap. Missing: {', '.join(missing)}"
            )
        print(f"[WHU GEO] {company} source={shp}", flush=True)

    if apply:
        ensure_geospatial_schema(settings)

    results: dict[str, Any] = {}
    for company in selected:
        result = import_boundaries(
            settings,
            company,
            source_files=[str(BUNDLED_BOUNDARIES[company])],
            estate_column="DEPARTEMEN",
            afdeling_column="AFD",
            block_column="BLOK",
            simplify_tolerance=simplify_tolerance,
            replace_company=True,
            apply=apply,
        )
        results[company] = result
        if apply and sync_tph:
            sync_tph_points(settings, company, apply=True)

    if apply:
        counts = _active_counts(settings)
        print("-" * 88, flush=True)
        print("ACTIVE BOUNDARY COUNTS", flush=True)
        for company in selected:
            print(f"{company}: {counts.get(company, 0):,}", flush=True)
        print("PWP: tidak disentuh; tetap memakai source geospatial existing.", flush=True)
        print("Restart Dashboard agar cache boundary dimuat ulang.", flush=True)
        results["active_counts"] = counts
    else:
        print("DRY-RUN selesai. Tambahkan --apply untuk menyimpan ke PostgreSQL.", flush=True)

    return results
