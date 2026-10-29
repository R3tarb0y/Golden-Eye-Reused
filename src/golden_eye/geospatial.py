from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Iterable

from .config import Settings
from .db import connection, ident, qname, table_columns, table_exists


COMPANY_CHOICES = ("PWP", "DIN", "DAN", "DWT")


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"", "nan", "none", "null", "-"}:
        return ""
    return text


def _norm(value: Any) -> str:
    text = _clean(value).upper()
    return re.sub(r"[^A-Z0-9]", "", text)


def _first_column(columns: Iterable[str], candidates: list[str]) -> str | None:
    lookup = {str(c).upper(): str(c) for c in columns}
    for candidate in candidates:
        hit = lookup.get(candidate.upper())
        if hit:
            return hit
    return None


def _json_scalar(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def configured_boundary_files(company: str) -> list[str]:
    raw = os.getenv(f"GEO_{company.upper()}_BOUNDARY_FILE", "").strip()
    return [v.strip() for v in raw.split(";") if v.strip()]


def import_boundaries(
    settings: Settings,
    company: str,
    source_files: list[str] | None = None,
    *,
    estate_column: str | None = None,
    afdeling_column: str | None = None,
    block_column: str | None = None,
    simplify_tolerance: float = 0.00003,
    replace_company: bool = True,
    apply: bool = False,
) -> dict[str, Any]:
    company = str(company or "").upper().strip()
    if company not in COMPANY_CHOICES:
        raise ValueError(f"Company harus salah satu {', '.join(COMPANY_CHOICES)}")
    files = source_files or configured_boundary_files(company)
    if not files:
        raise ValueError(
            f"Belum ada boundary file untuk {company}. Isi GEO_{company}_BOUNDARY_FILE "
            "di .env atau gunakan --source-file."
        )

    try:
        import geopandas as gpd
        from shapely.geometry import mapping
    except Exception as exc:
        raise RuntimeError(
            "Import SHP/GeoJSON membutuhkan geopandas, pyogrio, shapely, dan pyproj. "
            "Jalankan: py -m pip install geopandas pyogrio shapely pyproj"
        ) from exc

    prepared: list[dict[str, Any]] = []
    source_summaries: list[dict[str, Any]] = []
    for raw_path in files:
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Boundary file tidak ditemukan: {path}")
        if path.suffix.lower() == ".shp":
            missing = [
                str(path.with_suffix(ext))
                for ext in (".shx", ".dbf")
                if not path.with_suffix(ext).exists()
            ]
            if missing:
                raise FileNotFoundError(
                    "Shapefile tidak lengkap. File wajib hilang: " + ", ".join(missing)
                )

        gdf = gpd.read_file(path)
        if gdf.empty:
            source_summaries.append({"path": str(path), "features": 0, "status": "EMPTY"})
            continue
        gdf = gdf[gdf.geometry.notna()].copy()
        if gdf.empty:
            source_summaries.append({"path": str(path), "features": 0, "status": "NO_GEOMETRY"})
            continue
        if gdf.crs is not None:
            gdf = gdf.to_crs(epsg=4326)
        else:
            print(
                f"[GEO] WARNING {path.name} tidak memiliki CRS/.prj; geometry diasumsikan EPSG:4326.",
                flush=True,
            )
        if simplify_tolerance and simplify_tolerance > 0:
            gdf["geometry"] = gdf.geometry.simplify(
                simplify_tolerance, preserve_topology=True
            )

        columns = [str(c) for c in gdf.columns if str(c) != "geometry"]
        estate_col = estate_column or _first_column(
            columns,
            ["ESTATE", "ESTATE_1", "DEPARTEMEN", "DEPARTMENT", "DEPARTMENT_CODE", "KEBUN"],
        )
        afdeling_col = afdeling_column or _first_column(
            columns,
            ["AFDELING", "AFDELING_1", "AFD_1", "AFD", "DIVISION", "DIVISI", "KDAF_1"],
        )
        blok_col = block_column or _first_column(
            columns,
            ["BLOK_1", "BLOK", "BLOCK", "BLOCK_CODE", "BLOK_CODE", "BLOCK_1"],
        )
        if not blok_col or blok_col not in gdf.columns:
            raise ValueError(
                f"Kolom blok tidak ditemukan pada {path.name}. Columns={columns}. "
                "Gunakan --block-column bila nama kolom berbeda."
            )

        area_col = _first_column(columns, ["LUAS_1", "LUAS", "AREA_HA", "HECTARE", "HECTARES"])
        mtime = path.stat().st_mtime
        loaded = 0
        for idx, row in gdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            estate = _clean(row.get(estate_col)) if estate_col else ""
            afdeling = _clean(row.get(afdeling_col)) if afdeling_col else ""
            blok = _clean(row.get(blok_col))
            if not blok:
                continue
            geom_json = mapping(geom)
            rep = geom.representative_point()
            area_ha = None
            if area_col:
                try:
                    area_ha = float(row.get(area_col))
                    if math.isnan(area_ha):
                        area_ha = None
                except Exception:
                    area_ha = None
            attrs = {c: _json_scalar(row.get(c)) for c in columns[:80]}
            digest_source = "|".join(
                [
                    company,
                    str(path).upper(),
                    str(idx),
                    _norm(estate),
                    _norm(afdeling),
                    _norm(blok),
                ]
            )
            boundary_id = hashlib.sha1(digest_source.encode("utf-8")).hexdigest()
            prepared.append(
                {
                    "boundary_id": boundary_id,
                    "company": company,
                    "estate": estate,
                    "afdeling": afdeling,
                    "blok": blok,
                    "company_norm": _norm(company),
                    "estate_norm": _norm(estate),
                    "afdeling_norm": _norm(afdeling),
                    "blok_norm": _norm(blok),
                    "geometry_geojson": json.dumps(geom_json, ensure_ascii=False),
                    "centroid_lat": float(rep.y),
                    "centroid_lon": float(rep.x),
                    "area_ha": area_ha,
                    "source_path": str(path),
                    "source_feature_index": int(idx) if isinstance(idx, int) else loaded,
                    "source_mtime": float(mtime),
                    "source_attributes": json.dumps(attrs, ensure_ascii=False),
                }
            )
            loaded += 1
        source_summaries.append(
            {
                "path": str(path),
                "features": loaded,
                "status": "READY",
                "estate_column": estate_col,
                "afdeling_column": afdeling_col,
                "block_column": blok_col,
            }
        )

    print(f"[GEO] company={company} prepared_features={len(prepared):,}", flush=True)
    for summary in source_summaries:
        print("[GEO] source=" + json.dumps(summary, ensure_ascii=False), flush=True)
    if not apply:
        print("[GEO] DRY-RUN; tambahkan --apply untuk menyimpan ke PostgreSQL.", flush=True)
        return {"company": company, "prepared": len(prepared), "sources": source_summaries, "applied": False}

    with connection(settings) as conn:
        with conn.cursor() as cur:
            if replace_company:
                cur.execute(
                    "UPDATE reference.geo_block_boundary SET is_active=false WHERE company_norm=%s AND is_active",
                    (_norm(company),),
                )
            insert_sql = """
                INSERT INTO reference.geo_block_boundary (
                    boundary_id, company, estate, afdeling, blok,
                    company_norm, estate_norm, afdeling_norm, blok_norm,
                    geometry_geojson, centroid_lat, centroid_lon, area_ha,
                    source_path, source_feature_index, source_mtime,
                    source_attributes, is_active, imported_at
                ) VALUES (
                    %(boundary_id)s, %(company)s, %(estate)s, %(afdeling)s, %(blok)s,
                    %(company_norm)s, %(estate_norm)s, %(afdeling_norm)s, %(blok_norm)s,
                    %(geometry_geojson)s::jsonb, %(centroid_lat)s, %(centroid_lon)s, %(area_ha)s,
                    %(source_path)s, %(source_feature_index)s, %(source_mtime)s,
                    %(source_attributes)s::jsonb, true, now()
                )
                ON CONFLICT (boundary_id) DO UPDATE SET
                    company=EXCLUDED.company,
                    estate=EXCLUDED.estate,
                    afdeling=EXCLUDED.afdeling,
                    blok=EXCLUDED.blok,
                    company_norm=EXCLUDED.company_norm,
                    estate_norm=EXCLUDED.estate_norm,
                    afdeling_norm=EXCLUDED.afdeling_norm,
                    blok_norm=EXCLUDED.blok_norm,
                    geometry_geojson=EXCLUDED.geometry_geojson,
                    centroid_lat=EXCLUDED.centroid_lat,
                    centroid_lon=EXCLUDED.centroid_lon,
                    area_ha=EXCLUDED.area_ha,
                    source_path=EXCLUDED.source_path,
                    source_feature_index=EXCLUDED.source_feature_index,
                    source_mtime=EXCLUDED.source_mtime,
                    source_attributes=EXCLUDED.source_attributes,
                    is_active=true,
                    imported_at=now()
            """
            for row in prepared:
                cur.execute(insert_sql, row)
            for summary in source_summaries:
                cur.execute(
                    """
                    INSERT INTO reference.geo_import_log
                        (company, source_path, feature_count, active_count, status, message)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        company,
                        summary["path"],
                        int(summary.get("features") or 0),
                        int(summary.get("features") or 0),
                        summary.get("status") or "READY",
                        json.dumps(summary, ensure_ascii=False),
                    ),
                )
        conn.commit()
    print(f"[GEO] committed company={company} active_features={len(prepared):,}", flush=True)
    return {"company": company, "prepared": len(prepared), "sources": source_summaries, "applied": True}


RAW_TPH_ALIASES: dict[str, list[str]] = {
    "company": ["company_code", "company", "bukrs"],
    "estate": ["department_code", "estate", "kebun", "department"],
    "afdeling": ["afdeling_code", "afdeling_id", "afdeling", "afd"],
    "blok": ["block", "blok", "block_code"],
    "tph": ["tph_no", "tph", "tph_code"],
    "latitude": ["latitude", "lat"],
    "longitude": ["longitude", "long", "lng"],
    "period_date": ["period_date", "report_date", "tanggal", "budat", "date"],
}


def _column_ci(columns: Iterable[str], candidates: Iterable[str]) -> str | None:
    lookup = {str(c).lower(): str(c) for c in columns}
    for candidate in candidates:
        hit = lookup.get(str(candidate).lower())
        if hit:
            return hit
    return None


def _safe_numeric_expr(column: str) -> str:
    col = ident(column)
    # RAW mirror normally preserves numeric SQL Server types, but this also
    # tolerates text columns without letting one bad value abort the refresh.
    return (
        f"CASE WHEN trim({col}::text) ~ '^[+-]?[0-9]+([.][0-9]+)?$' "
        f"THEN trim({col}::text)::double precision END"
    )


def sync_tph_points(
    settings: Settings,
    company: str,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Build a canonical TPH reference point from historical transaction GPS.

    PRODUCTION_PLANTATION contains TPH_NO together with LATITUDE/LONGITUDE, but
    those coordinates are transaction observations rather than a guaranteed
    master TPH coordinate.  We therefore use the median of all valid GPS
    observations for the same Company/Estate/Afdeling/Block/TPH and retain a
    P90 dispersion metric so the dashboard can show how trustworthy the
    inferred anchor is.
    """
    company = str(company or "").upper().strip()
    if company not in COMPANY_CHOICES:
        raise ValueError(f"Company harus salah satu {', '.join(COMPANY_CHOICES)}")

    schema = settings.raw_business_schema
    table = settings.raw_business_table
    with connection(settings) as conn:
        if not table_exists(conn, schema, table):
            print(f"[GEO TPH] SKIP {schema}.{table} tidak tersedia", flush=True)
            return {"company": company, "prepared": 0, "applied": False, "status": "NO_RAW_TABLE"}
        cols = table_columns(conn, schema, table)

        resolved = {key: _column_ci(cols, aliases) for key, aliases in RAW_TPH_ALIASES.items()}
        required = ["company", "blok", "tph", "latitude", "longitude"]
        missing = [key for key in required if not resolved.get(key)]
        if missing:
            print(f"[GEO TPH] SKIP kolom RAW tidak lengkap missing={missing}", flush=True)
            return {"company": company, "prepared": 0, "applied": False, "status": "MISSING_COLUMNS", "missing": missing}

        company_col = ident(resolved["company"])
        estate_sql = f"coalesce({ident(resolved['estate'])}::text,'')" if resolved.get("estate") else "''::text"
        afdeling_sql = f"coalesce({ident(resolved['afdeling'])}::text,'')" if resolved.get("afdeling") else "''::text"
        blok_sql = f"coalesce({ident(resolved['blok'])}::text,'')"
        tph_sql = f"coalesce({ident(resolved['tph'])}::text,'')"
        lat_sql = _safe_numeric_expr(resolved["latitude"])
        lon_sql = _safe_numeric_expr(resolved["longitude"])
        if resolved.get("period_date"):
            date_sql = (
                f"CASE WHEN trim({ident(resolved['period_date'])}::text) ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}' "
                f"THEN left(trim({ident(resolved['period_date'])}::text),10)::date END"
            )
        else:
            date_sql = "NULL::date"

        sql = f"""
            WITH base AS (
                SELECT
                    trim({company_col}::text) AS company,
                    trim({estate_sql}) AS estate,
                    trim({afdeling_sql}) AS afdeling,
                    trim({blok_sql}) AS blok,
                    trim({tph_sql}) AS tph,
                    {lat_sql} AS latitude,
                    {lon_sql} AS longitude,
                    {date_sql} AS observed_date
                FROM {qname(schema, table)}
                WHERE upper(regexp_replace(coalesce({company_col}::text,''), '[^A-Za-z0-9]', '', 'g')) = %s
                  AND trim({tph_sql}) <> ''
            ), valid AS (
                SELECT * FROM base
                WHERE latitude BETWEEN -90 AND 90
                  AND longitude BETWEEN -180 AND 180
                  AND NOT (abs(latitude) < 0.000001 AND abs(longitude) < 0.000001)
            ), med AS (
                SELECT company, estate, afdeling, blok, tph,
                       count(*)::integer AS observation_count,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY latitude) AS latitude,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY longitude) AS longitude,
                       min(observed_date) AS first_observed_date,
                       max(observed_date) AS last_observed_date
                FROM valid
                GROUP BY company, estate, afdeling, blok, tph
            ), dist AS (
                SELECT v.company, v.estate, v.afdeling, v.blok, v.tph,
                       m.observation_count, m.latitude, m.longitude,
                       m.first_observed_date, m.last_observed_date,
                       sqrt(
                           power((v.latitude - m.latitude) * 111320.0, 2) +
                           power((v.longitude - m.longitude) * 111320.0 * cos(radians(m.latitude)), 2)
                       ) AS distance_m
                FROM valid v
                JOIN med m USING (company, estate, afdeling, blok, tph)
            )
            SELECT company, estate, afdeling, blok, tph,
                   max(observation_count)::integer AS observation_count,
                   max(latitude)::double precision AS latitude,
                   max(longitude)::double precision AS longitude,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY distance_m)::double precision AS dispersion_p90_m,
                   max(first_observed_date) AS first_observed_date,
                   max(last_observed_date) AS last_observed_date
            FROM dist
            GROUP BY company, estate, afdeling, blok, tph
            ORDER BY estate, afdeling, blok, tph
        """
        with conn.cursor() as cur:
            cur.execute(sql, (_norm(company),))
            rows = cur.fetchall()

    prepared: list[dict[str, Any]] = []
    for row in rows:
        company_v, estate, afdeling, blok, tph, obs, lat, lon, p90, first_date, last_date = row
        obs = int(obs or 0)
        p90_v = float(p90) if p90 is not None else None
        confidence = (
            "HIGH" if obs >= 5 and p90_v is not None and p90_v <= 40.0
            else "MEDIUM" if obs >= 2 and p90_v is not None and p90_v <= 75.0
            else "LOW"
        )
        norm_key = "|".join([_norm(company_v), _norm(estate), _norm(afdeling), _norm(blok), _norm(tph)])
        prepared.append({
            "tph_point_id": hashlib.sha1(norm_key.encode("utf-8")).hexdigest(),
            "company": _clean(company_v) or company,
            "estate": _clean(estate),
            "afdeling": _clean(afdeling),
            "blok": _clean(blok),
            "tph": _clean(tph),
            "company_norm": _norm(company_v) or _norm(company),
            "estate_norm": _norm(estate),
            "afdeling_norm": _norm(afdeling),
            "blok_norm": _norm(blok),
            "tph_norm": _norm(tph),
            "latitude": float(lat),
            "longitude": float(lon),
            "observation_count": obs,
            "dispersion_p90_m": p90_v,
            "confidence": confidence,
            "first_observed_date": first_date,
            "last_observed_date": last_date,
            "source_relation": f"{schema}.{table}",
        })

    print(f"[GEO TPH] company={company} prepared_points={len(prepared):,}", flush=True)
    if not apply:
        print("[GEO TPH] DRY-RUN; belum menyimpan reference.geo_tph_point", flush=True)
        return {"company": company, "prepared": len(prepared), "applied": False, "status": "READY"}

    with connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reference.geo_tph_point SET is_active=false WHERE company_norm=%s AND is_active",
                (_norm(company),),
            )
            upsert = """
                INSERT INTO reference.geo_tph_point (
                    tph_point_id, company, estate, afdeling, blok, tph,
                    company_norm, estate_norm, afdeling_norm, blok_norm, tph_norm,
                    latitude, longitude, observation_count, dispersion_p90_m,
                    confidence, first_observed_date, last_observed_date,
                    source_relation, is_active, refreshed_at
                ) VALUES (
                    %(tph_point_id)s, %(company)s, %(estate)s, %(afdeling)s, %(blok)s, %(tph)s,
                    %(company_norm)s, %(estate_norm)s, %(afdeling_norm)s, %(blok_norm)s, %(tph_norm)s,
                    %(latitude)s, %(longitude)s, %(observation_count)s, %(dispersion_p90_m)s,
                    %(confidence)s, %(first_observed_date)s, %(last_observed_date)s,
                    %(source_relation)s, true, now()
                )
                ON CONFLICT (tph_point_id) DO UPDATE SET
                    company=EXCLUDED.company, estate=EXCLUDED.estate,
                    afdeling=EXCLUDED.afdeling, blok=EXCLUDED.blok, tph=EXCLUDED.tph,
                    company_norm=EXCLUDED.company_norm, estate_norm=EXCLUDED.estate_norm,
                    afdeling_norm=EXCLUDED.afdeling_norm, blok_norm=EXCLUDED.blok_norm,
                    tph_norm=EXCLUDED.tph_norm, latitude=EXCLUDED.latitude,
                    longitude=EXCLUDED.longitude, observation_count=EXCLUDED.observation_count,
                    dispersion_p90_m=EXCLUDED.dispersion_p90_m,
                    confidence=EXCLUDED.confidence,
                    first_observed_date=EXCLUDED.first_observed_date,
                    last_observed_date=EXCLUDED.last_observed_date,
                    source_relation=EXCLUDED.source_relation, is_active=true, refreshed_at=now()
            """
            for row in prepared:
                cur.execute(upsert, row)
        conn.commit()
    print(f"[GEO TPH] committed company={company} active_points={len(prepared):,}", flush=True)
    return {"company": company, "prepared": len(prepared), "applied": True, "status": "READY"}
