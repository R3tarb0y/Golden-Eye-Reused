from __future__ import annotations

import json
import uuid
import re
from collections import Counter, defaultdict
from datetime import date
from typing import Any, Iterable

import numpy as np
from psycopg2.extras import execute_values

from .config import Settings
from .db import connection, fetch_all_dict, ident, qname, table_columns, table_exists, update_run
from .enrichment import backfill_gold_from_context, sync_raw_usage_context
from .utils import unpack_vector


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"", "none", "null", "nan", "nat"} else text


_UNTRUSTED_OPH_MATCH_METHODS = {
    # Legacy fallback only proved a unique estate, then DISTINCT ON selected an
    # arbitrary OPH for actor+date. It is the confirmed cause of OPH explosion.
    "MATCH_BY_RAW_ACTOR_DATE_UNIQUE_ESTATE",
    "MATCH_BY_RAW_ACTOR_DATE_UNIQUE_ESTATE_NO_OPH",
}


def _oph_match_priority(value: Any) -> int:
    """Deterministic source priority for the one-OPH-one-usage winner."""
    method = _clean(value).upper()
    priorities = {
        "MATCH_BY_FFB_EVIDENCE_KP": 0,
        "MATCH_BY_OUTPUT_FILE": 1,
        "MATCH_BY_ALTERNATE_EVIDENCE": 2,
        "MATCH_BY_RAW_TIMESTAMP_ACTOR_UNIQUE_OPH": 3,
        "MATCH_BY_RAW_ACTOR_DATE_UNIQUE_OPH": 4,
        "MATCH_BY_RAW_ACTOR_DATE_ESTATE_ONE_TO_ONE": 5,
        "MATCH_BY_HISTORICAL_GOLD_FILE_UID": 6,
        "MATCH_BY_SAP_OPH_UNIQUE_PRFNR": 7,
    }
    # SAP-normalized legacy OPH tetap diberi prioritas lebih rendah daripada
    # semua exact transaction proof. Ia hanya mencegah historical DIN hilang
    # dari dashboard ketika OPH tersebut memang ada pada SAP bulan yang sama.
    if method.startswith("SAP_NORMALIZED_LEGACY_"):
        return 8
    return priorities.get(method, 20)


def _oph_aliases(value: Any) -> set[str]:
    """Match the legacy SAP rule: compact alnum plus digit-only fallback."""
    text = _clean(value).upper()
    if not text:
        return set()
    compact = re.sub(r"[^A-Z0-9]", "", text)
    digits = re.sub(r"[^0-9]", "", text)
    return {alias for alias in (compact, digits) if alias}


def _oph_in_sap(sap_aliases: set[str], value: Any) -> bool:
    return bool(_oph_aliases(value) & sap_aliases)


def _scope_pattern_to_sql_like(value: str) -> str:
    """Convert business wildcard ``*`` to a safe PostgreSQL LIKE pattern."""
    text = str(value or "").upper().strip()
    text = text.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return text.replace("*", "%")


def _sap_company_column(sap_map: dict[str, str]) -> str | None:
    """Resolve SAP company-code column used to prevent cross-company leakage."""
    return next(
        (
            sap_map[name]
            for name in (
                "company_code",
                "company_code_sap",
                "company",
                "bukrs",
                "companycode",
                "co_code",
            )
            if name in sap_map
        ),
        None,
    )


def _resolve_sap_prfnr_scopes(
    conn,
    settings: Settings,
    scopes_by_source: dict[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    """Resolve wildcard estate rules inside the canonical SAP company only.

    v102 already constrained PRFNR/estate, but wildcard resolution could still
    discover a matching PRFNR from another company when SAP contains a company
    code column. v103 binds wildcard discovery to both company (BUKRS/company)
    and PRFNR so an estate from another company cannot enter the denominator.
    """
    requested = {
        str(source).upper(): tuple(
            dict.fromkeys(
                str(value).upper().strip()
                for value in values
                if str(value).strip()
            )
        )
        for source, values in scopes_by_source.items()
    }
    mutable: dict[str, list[str]] = {
        source: [value for value in values if "*" not in value]
        for source, values in requested.items()
    }
    wildcard_rows = [
        (
            source,
            str(settings.sap_company_code(source)).upper().strip(),
            value,
            _scope_pattern_to_sql_like(value),
        )
        for source, values in requested.items()
        for value in values
        if "*" in value
    ]
    if not wildcard_rows:
        return {source: tuple(dict.fromkeys(values)) for source, values in mutable.items()}
    if not table_exists(conn, settings.sap_schema, settings.sap_table):
        return {source: tuple(dict.fromkeys(values)) for source, values in mutable.items()}

    sap_columns = table_columns(conn, settings.sap_schema, settings.sap_table)
    sap_map = {column.lower(): column for column in sap_columns}
    prfnr_col = next(
        (sap_map[name] for name in ("prfnr", "estate", "estate_name") if name in sap_map),
        None,
    )
    company_col = _sap_company_column(sap_map)
    date_col = next(
        (sap_map[name] for name in ("budat", "posting_date", "period_date") if name in sap_map),
        None,
    )
    if not prfnr_col:
        return {source: tuple(dict.fromkeys(values)) for source, values in mutable.items()}

    values_sql = ",".join(["(%s,%s,%s,%s)"] * len(wildcard_rows))
    params: list[Any] = [item for row in wildcard_rows for item in row]
    where = [
        f"s.{ident(prfnr_col)} IS NOT NULL",
        f"btrim(s.{ident(prfnr_col)}::text) <> ''",
    ]
    if date_col and settings.sap_period_year:
        where.append(f"left(btrim(s.{ident(date_col)}::text), 4)=%s")
        params.append(str(settings.sap_period_year))

    company_join = ""
    if company_col:
        company_join = (
            f" AND upper(btrim(s.{ident(company_col)}::text))="
            "r.source_company"
        )

    rows = fetch_all_dict(
        conn,
        f"""
        WITH requested_scope(source_group, source_company, scope_rule, like_rule) AS (
            VALUES {values_sql}
        )
        SELECT DISTINCT
               r.source_group,
               upper(btrim(s.{ident(prfnr_col)}::text)) AS prfnr
        FROM {qname(settings.sap_schema, settings.sap_table)} s
        JOIN requested_scope r
          ON upper(btrim(s.{ident(prfnr_col)}::text))
             LIKE r.like_rule ESCAPE '!'
             {company_join}
        WHERE {' AND '.join(where)}
        ORDER BY r.source_group, prfnr
        """,
        tuple(params),
    )
    for row in rows:
        source = str(row.get("source_group") or "").upper()
        prfnr = _clean(row.get("prfnr")).upper()
        if source in mutable and prfnr:
            mutable[source].append(prfnr)
    return {
        source: tuple(dict.fromkeys(values))
        for source, values in mutable.items()
    }



def _load_image_month_cutoffs(
    conn,
    settings: Settings,
    source_groups: Iterable[str],
) -> dict[str, str]:
    """Return the latest YYYY-MM that actually has image context per company.

    SAP denominator must never run ahead of the image population being compared.
    ELHP legacy source names are canonicalized to DAN/DWT. If a company has no
    image month for the active year, no cutoff is returned and SAP KPI is left
    unevaluated instead of silently counting a wider SAP period.
    """
    requested_map = {
        str(value).upper().strip(): settings._sap_scope_company(str(value))
        for value in source_groups
        if str(value).strip()
    }
    requested = {value for value in requested_map.values() if value}
    if not requested or not table_exists(conn, "processing", "image_context"):
        return {}
    rows = fetch_all_dict(
        conn,
        """
        WITH image_month AS (
            SELECT CASE upper(btrim(source_group))
                     WHEN 'ELHP_DAN' THEN 'DAN'
                     WHEN 'ELHP_DWT' THEN 'DWT'
                     ELSE upper(btrim(source_group))
                   END AS source_group,
                   to_char(period_date, 'YYYY-MM') AS image_month
            FROM processing.image_context
            WHERE period_date IS NOT NULL
              AND extract(year FROM period_date)::integer=%s
        )
        SELECT source_group, max(image_month) AS max_image_month
        FROM image_month
        WHERE source_group=ANY(%s)
        GROUP BY source_group
        ORDER BY source_group
        """,
        (int(settings.sap_period_year), sorted(requested)),
    )
    canonical_result: dict[str, str] = {}
    for row in rows:
        source = _clean(row.get("source_group")).upper()
        month = _clean(row.get("max_image_month"))
        if source and re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", month):
            canonical_result[source] = month
    result = dict(canonical_result)
    for original, canonical in requested_map.items():
        if canonical in canonical_result:
            result[original] = canonical_result[canonical]
    return result

def _load_sap_count_snapshots(
    conn,
    settings: Settings,
    scopes_by_source: dict[str, tuple[str, ...]],
    image_cutoff_by_source: dict[str, str] | None = None,
) -> dict[
    str,
    tuple[
        int | None,
        dict[str, int],
        dict[str, int],
        dict[str, dict[str, int]],
        bool,
    ],
]:
    """COUNT DISTINCT normalized OPH by canonical company + estate + month.

    Snapshot is deliberately hierarchical so the dashboard denominator can
    follow an explicit Estate filter instead of falling back to the whole
    company. Guards: company code (when available), exact PRFNR, period year,
    normalized distinct OPH, and the latest month that actually exists in the
    image context. SAP is therefore never allowed to run ahead of image data.
    """
    image_cutoff_by_source = {
        str(source).upper(): str(month)
        for source, month in (image_cutoff_by_source or {}).items()
        if re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", str(month))
    }
    normalized_scopes = {
        str(source).upper(): tuple(
            sorted({str(value).upper().strip() for value in values if str(value).strip()})
        )
        for source, values in scopes_by_source.items()
        if values
    }
    result = {
        str(source).upper(): (None, {}, {}, {}, False)
        for source in scopes_by_source
    }
    bounded_scopes: dict[str, tuple[str, ...]] = {}
    for source, scope in normalized_scopes.items():
        cutoff = image_cutoff_by_source.get(source)
        if not cutoff:
            print(
                f"[SAP KPI IMAGE CUTOFF] {source} tidak memiliki bulan gambar "
                f"untuk {settings.sap_period_year}; SAP denominator=N/A",
                flush=True,
            )
            continue
        bounded_scopes[source] = scope
        print(
            f"[SAP KPI IMAGE CUTOFF] {source} max_image_month={cutoff}; "
            f"SAP dihitung hanya sampai {cutoff}",
            flush=True,
        )
    normalized_scopes = bounded_scopes
    if not normalized_scopes:
        return result
    if not table_exists(conn, settings.sap_schema, settings.sap_table):
        return result

    sap_columns = table_columns(conn, settings.sap_schema, settings.sap_table)
    sap_map = {column.lower(): column for column in sap_columns}
    oph_col = next(
        (
            sap_map[name]
            for name in ("oph", "harvest_ticket", "tiket_panen", "no_bcc")
            if name in sap_map
        ),
        None,
    )
    prfnr_col = next(
        (
            sap_map[name]
            for name in ("prfnr", "estate", "estate_name")
            if name in sap_map
        ),
        None,
    )
    company_col = _sap_company_column(sap_map)
    date_col = next(
        (
            sap_map[name]
            for name in ("budat", "posting_date", "period_date")
            if name in sap_map
        ),
        None,
    )
    if not oph_col or not prfnr_col:
        return result

    scope_rows = [
        (
            source,
            str(settings.sap_company_code(source)).upper().strip(),
            prfnr,
            image_cutoff_by_source[source],
        )
        for source, values in normalized_scopes.items()
        for prfnr in values
    ]
    values_sql = ",".join(["(%s,%s,%s,%s)"] * len(scope_rows))
    params: list[Any] = [item for row in scope_rows for item in row]
    oph_norm = f"processing.normalize_oph(s.{ident(oph_col)}::text)"
    where = [f"nullif({oph_norm}, '') IS NOT NULL"]
    if date_col and settings.sap_period_year:
        where.append(f"left(btrim(s.{ident(date_col)}::text), 4)=%s")
        params.append(str(settings.sap_period_year))
    month_expr = "NULL::text"
    if date_col:
        date_digits = (
            f"regexp_replace(btrim(s.{ident(date_col)}::text), "
            "'[^0-9]', '', 'g')"
        )
        month_expr = (
            f"CASE WHEN length({date_digits}) >= 6 "
            f"THEN substring({date_digits} from 1 for 4) || '-' || "
            f"substring({date_digits} from 5 for 2) END"
        )

    company_join = ""
    if company_col:
        company_join = (
            f" AND upper(btrim(s.{ident(company_col)}::text))="
            "cs.source_company"
        )

    print(
        "[SAP KPI FILTER] company_column="
        + (company_col or "NOT_FOUND")
        + f"; estate_column={prfnr_col}; oph_column={oph_col}; "
          "oph_count=COUNT(DISTINCT processing.normalize_oph(OPH))",
        flush=True,
    )
    if not company_col:
        print(
            "[SAP KPI WARNING] kolom company SAP tidak ditemukan; filter tetap "
            "dikunci exact PRFNR, tetapi BUKRS/COMPANY_CODE guard tidak tersedia.",
            flush=True,
        )

    rows = fetch_all_dict(
        conn,
        f"""
        WITH company_scope(source_group, source_company, prfnr, max_image_month) AS (
            VALUES {values_sql}
        ), scoped_sap AS (
            SELECT cs.source_group,
                   cs.source_company,
                   cs.prfnr,
                   {oph_norm} AS oph_key,
                   {month_expr} AS sap_month
            FROM {qname(settings.sap_schema, settings.sap_table)} s
            JOIN company_scope cs
              ON upper(btrim(s.{ident(prfnr_col)}::text))=cs.prfnr
             {company_join}
            WHERE {' AND '.join(where)}
              AND {month_expr} IS NOT NULL
              AND {month_expr} <= cs.max_image_month
        )
        SELECT source_group, prfnr, sap_month,
               grouping(prfnr) AS prfnr_grouped,
               grouping(sap_month) AS month_grouped,
               count(DISTINCT oph_key)::bigint AS oph_count
        FROM scoped_sap
        GROUP BY GROUPING SETS (
            (source_group),
            (source_group, sap_month),
            (source_group, prfnr),
            (source_group, prfnr, sap_month)
        )
        ORDER BY source_group, prfnr_grouped DESC, month_grouped DESC,
                 prfnr NULLS FIRST, sap_month NULLS FIRST
        """,
        tuple(params),
    )
    mutable = {
        source: {"total": 0, "monthly": {}, "estate": {}, "estate_monthly": {}}
        for source in normalized_scopes
    }
    for row in rows:
        source = str(row.get("source_group") or "").upper()
        if source not in mutable:
            continue
        count = int(row.get("oph_count") or 0)
        prfnr_grouped = int(row.get("prfnr_grouped") or 0)
        month_grouped = int(row.get("month_grouped") or 0)
        prfnr = _clean(row.get("prfnr")).upper()
        sap_month = _clean(row.get("sap_month"))
        if prfnr_grouped == 1 and month_grouped == 1:
            mutable[source]["total"] = count
            continue
        if prfnr_grouped == 1 and month_grouped == 0:
            if re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", sap_month):
                mutable[source]["monthly"][sap_month] = count
                print(
                    f"[SAP KPI MONTH] {source} {sap_month} distinct_oph={count}",
                    flush=True,
                )
            continue
        if prfnr_grouped == 0 and month_grouped == 1:
            if prfnr:
                mutable[source]["estate"][prfnr] = count
                print(
                    f"[SAP KPI ESTATE] {source} {prfnr} distinct_oph={count}",
                    flush=True,
                )
            continue
        if prfnr_grouped == 0 and month_grouped == 0:
            if prfnr and re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", sap_month):
                mutable[source]["estate_monthly"].setdefault(prfnr, {})[sap_month] = count
    for source, snapshot in mutable.items():
        result[source] = (
            int(snapshot["total"]),
            dict(snapshot["monthly"]),
            dict(snapshot["estate"]),
            {key: dict(value) for key, value in snapshot["estate_monthly"].items()},
            True,
        )
    return result


def _refresh_sap_scope_keys(
    conn,
    settings: Settings,
    scopes_by_source: dict[str, tuple[str, ...]],
    image_cutoff_by_source: dict[str, str] | None = None,
) -> dict[str, int]:
    """Refresh exact SAP OPH keys used by dashboard visibility.

    The dashboard contract is stricter than the visual-evidence Gold layer:
    a reused usage is visible only when the same normalized OPH exists in the
    canonical SAP estate scope *and* the SAP month equals the transaction
    month.  Keeping this as a compact key table lets dashboard/report views
    enforce that contract without re-running image similarity.
    """
    image_cutoff_by_source = {
        str(source).upper(): str(month)
        for source, month in (image_cutoff_by_source or {}).items()
        if re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", str(month))
    }
    normalized_scopes = {
        str(source).upper(): tuple(
            sorted({str(value).upper().strip() for value in values if str(value).strip()})
        )
        for source, values in scopes_by_source.items()
        if values and image_cutoff_by_source.get(str(source).upper())
    }
    if not normalized_scopes:
        return {}
    if not table_exists(conn, "gold", "sap_oph_scope_key"):
        return {}
    if not table_exists(conn, settings.sap_schema, settings.sap_table):
        return {}

    sap_columns = table_columns(conn, settings.sap_schema, settings.sap_table)
    sap_map = {column.lower(): column for column in sap_columns}
    oph_col = next(
        (sap_map[name] for name in ("oph", "harvest_ticket", "tiket_panen", "no_bcc") if name in sap_map),
        None,
    )
    prfnr_col = next(
        (sap_map[name] for name in ("prfnr", "estate", "estate_name") if name in sap_map),
        None,
    )
    company_col = _sap_company_column(sap_map)
    date_col = next(
        (sap_map[name] for name in ("budat", "posting_date", "period_date") if name in sap_map),
        None,
    )
    # Exact monthly visibility cannot be proven without OPH + estate + date.
    if not oph_col or not prfnr_col or not date_col:
        return {}

    scope_rows = [
        (
            source,
            str(settings.sap_company_code(source)).upper().strip(),
            prfnr,
            image_cutoff_by_source[source],
        )
        for source, values in normalized_scopes.items()
        for prfnr in values
    ]
    values_sql = ",".join(["(%s,%s,%s,%s)"] * len(scope_rows))
    params: list[Any] = [item for row in scope_rows for item in row]
    oph_norm = f"processing.normalize_oph(s.{ident(oph_col)}::text)"
    date_digits = (
        f"regexp_replace(btrim(s.{ident(date_col)}::text), "
        "'[^0-9]', '', 'g')"
    )
    month_expr = (
        f"CASE WHEN length({date_digits}) >= 6 "
        f"THEN substring({date_digits} from 1 for 4) || '-' || "
        f"substring({date_digits} from 5 for 2) END"
    )
    company_join = ""
    if company_col:
        company_join = (
            f" AND upper(btrim(s.{ident(company_col)}::text))=cs.source_company"
        )

    current_sources = sorted(normalized_scopes)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM gold.sap_oph_scope_key "
            "WHERE period_year=%s AND source_group=ANY(%s)",
            (int(settings.sap_period_year), current_sources),
        )
        cur.execute(
            f"""
            WITH company_scope(source_group, source_company, prfnr, max_image_month) AS (
                VALUES {values_sql}
            ), scoped_sap AS (
                SELECT cs.source_group,
                       %s::integer AS period_year,
                       {oph_norm} AS oph_key,
                       upper(btrim(s.{ident(prfnr_col)}::text)) AS prfnr,
                       {month_expr} AS sap_month
                FROM {qname(settings.sap_schema, settings.sap_table)} s
                JOIN company_scope cs
                  ON upper(btrim(s.{ident(prfnr_col)}::text))=cs.prfnr
                 {company_join}
                WHERE nullif({oph_norm}, '') IS NOT NULL
                  AND left(btrim(s.{ident(date_col)}::text), 4)=%s
                  AND {month_expr} IS NOT NULL
                  AND {month_expr} <= cs.max_image_month
            )
            INSERT INTO gold.sap_oph_scope_key
                (source_group, period_year, oph_key, prfnr, sap_month, refreshed_at)
            SELECT DISTINCT source_group, period_year, oph_key, prfnr, sap_month, now()
            FROM scoped_sap
            ON CONFLICT (source_group, period_year, oph_key, prfnr, sap_month)
            DO UPDATE SET refreshed_at=excluded.refreshed_at
            """,
            tuple(params) + (int(settings.sap_period_year), str(settings.sap_period_year)),
        )
        cur.execute(
            """
            SELECT source_group, count(DISTINCT oph_key)::bigint AS key_count
            FROM gold.sap_oph_scope_key
            WHERE period_year=%s AND source_group=ANY(%s)
            GROUP BY source_group
            ORDER BY source_group
            """,
            (int(settings.sap_period_year), current_sources),
        )
        counts = {str(source).upper(): int(count) for source, count in cur.fetchall()}
    for source in current_sources:
        print(
            f"[SAP VISIBILITY KEY] {source} exact_oph={counts.get(source, 0):,} "
            f"cutoff={image_cutoff_by_source.get(source, '')}",
            flush=True,
        )
    return counts


def _load_scoped_sap_keys(
    conn,
    settings: Settings,
    source_group: str,
    transaction_keys: Iterable[str] | None = None,
) -> tuple[
    set[str],
    int | None,
    dict[str, int],
    dict[str, int],
    dict[str, dict[str, int]],
    bool,
    tuple[str, ...],
]:
    """Count SAP OPH and refresh exact keys for one company.

    The returned key set is only the intersection with ``transaction_keys`` so
    memory stays bounded even when SAP contains hundreds of thousands of OPH.
    """
    raw_configured_scope = settings.sap_configured_prfnr_values(source_group)
    estate_allowlist = settings.sap_estate_scope_values(source_group)
    configured_patterns = settings.sap_prfnr_values(source_group)
    resolved = _resolve_sap_prfnr_scopes(
        conn,
        settings,
        {str(source_group).upper(): configured_patterns},
    )
    scope = tuple(resolved.get(str(source_group).upper()) or ())
    if estate_allowlist:
        print(
            f"[SAP KPI SCOPE] {str(source_group).upper()} rules={list(estate_allowlist)} "
            f"=> resolved_prfnr={list(scope)}",
            flush=True,
        )
        if raw_configured_scope:
            print(
                f"[SAP KPI SCOPE] {str(source_group).upper()} legacy_map_ignored="
                f"{list(raw_configured_scope)}",
                flush=True,
            )
    if not scope:
        print(
            f"[SAP KPI] {source_group} dilewati: estate scope tidak menemukan "
            "PRFNR SAP pada tahun aktif. Denominator global/legacy tidak dipakai "
            "sebagai fallback karena dapat menarik estate luar company.",
            flush=True,
        )
        return set(), None, {}, {}, {}, False, ()
    canonical_source = str(source_group).upper()
    image_cutoff = _load_image_month_cutoffs(conn, settings, [canonical_source])
    total, monthly, estate, estate_monthly, evaluated = _load_sap_count_snapshots(
        conn,
        settings,
        {canonical_source: scope},
        image_cutoff_by_source=image_cutoff,
    )[canonical_source]
    _refresh_sap_scope_keys(
        conn,
        settings,
        {canonical_source: scope},
        image_cutoff_by_source=image_cutoff,
    )

    matched_keys: set[str] = set()
    tx_keys = sorted({
        processing_key
        for value in (transaction_keys or ())
        if (processing_key := re.sub(r"[^A-Z0-9]", "", str(value or "").upper()))
    })
    if tx_keys and table_exists(conn, "gold", "sap_oph_scope_key"):
        rows = fetch_all_dict(
            conn,
            """
            SELECT DISTINCT oph_key
            FROM gold.sap_oph_scope_key
            WHERE source_group=%s
              AND period_year=%s
              AND oph_key=ANY(%s)
            """,
            (canonical_source, int(settings.sap_period_year), tx_keys),
        )
        matched_keys = {str(row.get("oph_key") or "") for row in rows if row.get("oph_key")}
    return matched_keys, total, monthly, estate, estate_monthly, evaluated, scope


def _write_sap_kpi_summary(
    cur,
    settings: Settings,
    run_id: uuid.UUID,
    source_group: str,
    source_system: str,
    source_company: str,
    sap_aliases: set[str],
    total_sap_oph: int | None,
    monthly_sap_oph: dict[str, int],
    estate_sap_oph: dict[str, int],
    estate_monthly_sap_oph: dict[str, dict[str, int]],
    sap_evaluated: bool,
    sap_prfnr_scope: tuple[str, ...],
    transaction_keys: Iterable[str],
) -> dict[str, Any]:
    keys = {str(value or "").strip() for value in transaction_keys if str(value or "").strip()}
    transaction_oph = len(keys)
    # Exact key membership is kept for audit/visibility.  The denominator is
    # still the scoped DISTINCT SAP OPH snapshot, but dashboard rows can now be
    # proven against SAP instead of comparing a visual-usage numerator with a
    # count-only denominator.
    sap_keys = {str(value or "").strip() for value in sap_aliases if str(value or "").strip()}
    transaction_oph_in_sap = len(keys & sap_keys)
    transaction_oph_not_in_sap = len(keys - sap_keys) if sap_evaluated else 0
    transaction_match_pct = (
        round(transaction_oph_in_sap * 100.0 / transaction_oph, 4)
        if sap_evaluated and transaction_oph
        else None
    )
    sap_used_by_transaction_pct = (
        round(transaction_oph_in_sap * 100.0 / total_sap_oph, 4)
        if sap_evaluated and total_sap_oph
        else None
    )
    cur.execute(
        "UPDATE gold.sap_oph_kpi_summary SET is_active=false "
        "WHERE source_group=%s AND is_active=true",
        (source_group,),
    )
    cur.execute(
        """
        INSERT INTO gold.sap_oph_kpi_summary
            (run_id, source_group, source_system, source_company,
             period_year, prfnr_scope, sap_evaluated, total_sap_oph,
             monthly_sap_oph, estate_sap_oph, estate_monthly_sap_oph,
             transaction_oph, transaction_oph_in_sap,
             transaction_oph_not_in_sap, transaction_match_pct,
             sap_used_by_transaction_pct, is_active)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,true)
        ON CONFLICT (run_id, source_group) DO UPDATE SET
            source_system=EXCLUDED.source_system,
            source_company=EXCLUDED.source_company,
            period_year=EXCLUDED.period_year,
            prfnr_scope=EXCLUDED.prfnr_scope,
            sap_evaluated=EXCLUDED.sap_evaluated,
            total_sap_oph=EXCLUDED.total_sap_oph,
            monthly_sap_oph=EXCLUDED.monthly_sap_oph,
            estate_sap_oph=EXCLUDED.estate_sap_oph,
            estate_monthly_sap_oph=EXCLUDED.estate_monthly_sap_oph,
            transaction_oph=EXCLUDED.transaction_oph,
            transaction_oph_in_sap=EXCLUDED.transaction_oph_in_sap,
            transaction_oph_not_in_sap=EXCLUDED.transaction_oph_not_in_sap,
            transaction_match_pct=EXCLUDED.transaction_match_pct,
            sap_used_by_transaction_pct=EXCLUDED.sap_used_by_transaction_pct,
            is_active=true,
            published_at=now()
        """,
        (
            run_id,
            source_group,
            source_system,
            source_company,
            settings.sap_period_year,
            list(sap_prfnr_scope),
            sap_evaluated,
            total_sap_oph if sap_evaluated else None,
            json.dumps(monthly_sap_oph if sap_evaluated else {}, sort_keys=True),
            json.dumps(estate_sap_oph if sap_evaluated else {}, sort_keys=True),
            json.dumps(estate_monthly_sap_oph if sap_evaluated else {}, sort_keys=True),
            transaction_oph,
            transaction_oph_in_sap,
            transaction_oph_not_in_sap,
            transaction_match_pct,
            sap_used_by_transaction_pct,
        ),
    )
    return {
        "gold_sap_total_oph": total_sap_oph if sap_evaluated else None,
        "gold_sap_monthly_oph": monthly_sap_oph if sap_evaluated else {},
        "gold_sap_estate_oph": estate_sap_oph if sap_evaluated else {},
        "gold_sap_estate_monthly_oph": estate_monthly_sap_oph if sap_evaluated else {},
        "gold_sap_transaction_oph": transaction_oph,
        "gold_sap_transaction_oph_in_sap": transaction_oph_in_sap,
        "gold_sap_transaction_oph_not_in_sap": transaction_oph_not_in_sap,
        "gold_sap_transaction_match_pct": transaction_match_pct,
        "gold_sap_used_by_transaction_pct": sap_used_by_transaction_pct,
        "gold_sap_prfnr_scope": list(sap_prfnr_scope),
    }


def refresh_active_sap_kpi(
    settings: Settings,
    source_group: str = "ALL",
) -> dict[str, Any]:
    """Refresh snapshot COUNT DISTINCT OPH SAP tanpa rerun similarity.

    Tidak ada join OPH Gold -> SAP dan tidak ada update audit per OPH. Hanya
    snapshot kecil ``gold.sap_oph_kpi_summary`` yang diubah; active Gold dan
    seluruh Silver/BYTEA tetap tidak disentuh.
    """
    requested = str(source_group or "ALL").upper().strip()
    with connection(settings) as conn:
        active = fetch_all_dict(
            conn,
            """
            SELECT DISTINCT ON (upper(source_group))
                   run_id, upper(source_group) AS source_group,
                   source_system, source_company, published_at
            FROM gold.reused_photo_group
            WHERE is_active=true
              AND (%s='ALL' OR upper(source_group)=%s)
            ORDER BY upper(source_group), published_at DESC
            """,
            (requested, requested),
        )
        if not active:
            raise RuntimeError(
                f"Tidak ada active Gold untuk source_group={requested}."
            )

        scope_rules_by_source = {
            str(item["source_group"]).upper(): settings.sap_prfnr_values(
                str(item["source_group"]).upper()
            )
            for item in active
        }
        for current_source in sorted(scope_rules_by_source):
            print(
                f"[SAP COMPANY MAP] {current_source} -> "
                f"BUKRS={settings.sap_company_code(current_source)}",
                flush=True,
            )
        # Resolve exact/wildcard allowlist ke PRFNR yang benar-benar ada di SAP.
        # Hasil inilah yang disimpan ke gold.sap_oph_kpi_summary sehingga seluruh
        # dashboard/audit downstream tetap bekerja dengan exact PRFNR.
        scopes_by_source = _resolve_sap_prfnr_scopes(
            conn, settings, scope_rules_by_source
        )
        for current_source, scope in scopes_by_source.items():
            estate_allowlist = settings.sap_estate_scope_values(current_source)
            raw_configured = settings.sap_configured_prfnr_values(current_source)
            print(
                f"[SAP KPI SCOPE] {current_source} rules={list(scope_rules_by_source.get(current_source) or ())} "
                f"=> resolved_prfnr={list(scope)}",
                flush=True,
            )
            if estate_allowlist and raw_configured:
                print(
                    f"[SAP KPI SCOPE] {current_source} legacy_map_ignored="
                    f"{list(raw_configured)}",
                    flush=True,
                )
            if not scope:
                print(
                    f"[SAP KPI] {current_source} dilewati: estate scope tidak "
                    "menemukan PRFNR SAP pada tahun aktif; denominator global/"
                    "legacy tidak dipakai agar estate luar tidak ikut.",
                    flush=True,
                )
        # SAP denominator wajib mengikuti horizon data gambar. Jangan pernah
        # membandingkan image Jan-Jul terhadap SAP Jan-Aug.
        image_cutoff_by_source = _load_image_month_cutoffs(
            conn, settings, scopes_by_source.keys()
        )
        active_sources = sorted({str(item["source_group"]).upper() for item in active})
        if active_sources and table_exists(conn, "gold", "sap_oph_scope_key"):
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM gold.sap_oph_scope_key "
                    "WHERE period_year=%s AND source_group=ANY(%s)",
                    (int(settings.sap_period_year), active_sources),
                )
        visibility_key_counts = _refresh_sap_scope_keys(
            conn,
            settings,
            scopes_by_source,
            image_cutoff_by_source=image_cutoff_by_source,
        )

        # Satu scan agregat untuk semua company yang direfresh, hanya exact PRFNR
        # hasil hard-safe resolve di atas dan period <= max month gambar.
        snapshots = _load_sap_count_snapshots(
            conn,
            settings,
            scopes_by_source,
            image_cutoff_by_source=image_cutoff_by_source,
        )
        output: dict[str, Any] = {
            "source_groups": [],
            "sources": {},
            "sap_visibility_key_counts": visibility_key_counts,
        }
        for item in active:
            current_source = str(item["source_group"]).upper()
            sap_scope = tuple(scopes_by_source.get(current_source) or ())
            (
                total_sap_oph,
                monthly_sap_oph,
                estate_sap_oph,
                estate_monthly_sap_oph,
                sap_evaluated,
            ) = snapshots.get(
                current_source,
                (None, {}, {}, {}, False),
            )

            with conn.cursor() as cur:
                metrics = _write_sap_kpi_summary(
                    cur,
                    settings,
                    item["run_id"],
                    current_source,
                    str(item.get("source_system") or ""),
                    str(item.get("source_company") or current_source),
                    set(),
                    total_sap_oph,
                    monthly_sap_oph,
                    estate_sap_oph,
                    estate_monthly_sap_oph,
                    sap_evaluated,
                    sap_scope,
                    (),
                )
            output["source_groups"].append(current_source)
            output["sources"][current_source] = metrics
            print(
                f"[SAP KPI] {current_source} total_sap={metrics['gold_sap_total_oph']} "
                f"prfnr_scope={metrics['gold_sap_prfnr_scope']}",
                flush=True,
            )
    return output


def _joined(values: Iterable[Any]) -> str:
    unique = sorted({_clean(value) for value in values if _clean(value)})
    return ", ".join(unique)


def _reuse_category(rows: list[dict[str, Any]]) -> str:
    estates = {_clean(row.get("estate")) for row in rows if _clean(row.get("estate"))}
    afdelings = {_clean(row.get("afdeling")) for row in rows if _clean(row.get("afdeling"))}
    blocks = {_clean(row.get("blok")) for row in rows if _clean(row.get("blok"))}
    tphs = {_clean(row.get("tph")) for row in rows if _clean(row.get("tph"))}
    if len(estates) > 1:
        return "Estate Berbeda"
    if len(afdelings) > 1:
        return "Afdeling Berbeda"
    if len(blocks) > 1:
        return "Blok Berbeda"
    if len(tphs) > 1:
        return "Blok Sama / TPH Berbeda"
    return "TPH Sama"


def _date_category(rows: list[dict[str, Any]]) -> str:
    dates = {row.get("tanggal") for row in rows if row.get("tanggal")}
    if len(dates) <= 1:
        return "Tanggal Sama"
    return "Tanggal Berbeda"


def _transport_status(dt: list[dict[str, Any]], ft: list[dict[str, Any]]) -> str:
    if dt and ft:
        return "Ada DT dan FT"
    if dt:
        return "Ada DT"
    if ft:
        return "Ada FT"
    return "Tidak Ada FT/DT"


def _similarity_label(score: float | None, settings: Settings) -> str:
    if score is None:
        return "Tidak Ada Foto Transport"
    if score >= settings.transport_reused_threshold:
        return "Reused ≥ 93%"
    if score >= settings.transport_review_threshold:
        return "Review 90–<93%"
    return "Tidak Mirip < 90%"


def _best_transport(
    harvest_vector: np.ndarray,
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, float | None]:
    best: dict[str, Any] | None = None
    best_score: float | None = None
    for item in candidates:
        vector = item.get("vector")
        if vector is None:
            continue
        score = float(np.dot(harvest_vector, vector) * 100.0)
        if best_score is None or score > best_score:
            best = item
            best_score = score
    return best, best_score


def publish_gold(settings: Settings, run_id: uuid.UUID, source_group: str) -> dict[str, int]:
    update_run(settings, run_id, stage="PUBLISH")
    source_group = source_group.upper()
    source_system = settings.source_system(source_group)
    source_company = settings.source_company(source_group)
    with connection(settings) as conn:
        run_guard = fetch_all_dict(
            conn,
            """
            SELECT coalesce(
                (metrics->>'validation_low_light_guard')::boolean,
                false
            ) AS low_light_applied,
            coalesce(
                (metrics->>'validation_exact_group_similarity')::boolean,
                false
            ) AS exact_similarity_applied,
            coalesce(
                (metrics->>'validation_structural_duplicate_guard')::boolean,
                false
            ) AS structural_duplicate_applied,
            coalesce(
                (metrics->>'validation_representative_star_guard')::boolean,
                false
            ) AS representative_star_applied,
            coalesce(
                (metrics->>'oph_clean_republish')::boolean,
                false
            ) AS oph_clean_republish,
            coalesce(
                metrics->'oph_clean_expected_months',
                '[]'::jsonb
            ) AS oph_clean_expected_months
            FROM processing.pipeline_run
            WHERE run_id=%s
            """,
            (run_id,),
        )
    if settings.low_light_guard:
        if not run_guard or not bool(run_guard[0]["low_light_applied"]):
            raise RuntimeError(
                "Run ini belum divalidasi dengan LOW_LIGHT_GUARD. Publish "
                "dibatalkan agar false positive foto malam tidak menggantikan "
                "Gold aktif. Buat run_id baru dari MATCH -> VALIDATE."
            )
    if not run_guard or not bool(run_guard[0]["exact_similarity_applied"]):
        raise RuntimeError(
            "Run ini belum divalidasi dengan exact all-pair group similarity. "
            "Publish dibatalkan agar group dengan similarity di bawah kontrak "
            "aktif tidak menggantikan Gold. Buat run_id baru dari "
            "MATCH -> VALIDATE."
        )
    if settings.duplicate_require_structural_evidence:
        if not run_guard or not bool(run_guard[0]["structural_duplicate_applied"]):
            raise RuntimeError(
                "Run ini belum divalidasi dengan STRUCTURAL_DUPLICATE_GUARD. "
                "Publish dibatalkan agar foto yang hanya mirip secara "
                "semantik tidak masuk satu group. Buat run_id baru mulai "
                "MATCH -> VALIDATE."
            )
    if not run_guard or not bool(run_guard[0]["representative_star_applied"]):
        raise RuntimeError(
            "Run ini belum divalidasi dengan REPRESENTATIVE_STAR_GUARD v1.3.16. "
            "Setiap anggota final harus cocok langsung dengan satu foto "
            "representative. Buat run_id baru mulai MATCH -> VALIDATE."
        )
    raw_context_metrics = sync_raw_usage_context(settings, source_group, run_id=run_id)

    sap_keys: set[str] = set()
    total_sap_oph: int | None = None
    monthly_sap_oph: dict[str, int] = {}
    sap_evaluated = False
    sap_prfnr_scope: tuple[str, ...] = ()
    with connection(settings) as conn:
        existing = fetch_all_dict(
            conn,
            "SELECT count(*) AS n FROM gold.reused_photo_group WHERE run_id=%s",
            (run_id,),
        )
        if int(existing[0]["n"]) > 0:
            resume_quality = fetch_all_dict(
                conn,
                """
                SELECT count(*) FILTER (
                           WHERE mean_pair_score IS NULL
                              OR (upper(validation_status) LIKE '%%REVIEW%%'
                                  AND mean_pair_score < %s)
                              OR (upper(validation_status) NOT LIKE '%%REVIEW%%'
                                  AND mean_pair_score < %s)
                       )::bigint AS low_mean_groups,
                       count(*) FILTER (
                           WHERE mean_embedding_similarity IS NULL
                              OR min_member_mean_similarity IS NULL
                              OR mean_embedding_similarity < %s
                              OR min_member_mean_similarity < %s
                       )::bigint AS low_embedding_groups,
                       count(DISTINCT representative_file_uid)::bigint
                           AS distinct_representatives,
                       count(*)::bigint AS groups
                FROM gold.reused_photo_group
                WHERE run_id=%s
                """,
                (
                    settings.min_publish_mean_review,
                    settings.min_publish_mean_confirmed,
                    settings.validation_group_mean_similarity,
                    settings.validation_member_mean_similarity,
                    run_id,
                ),
            )[0]
            if (
                int(resume_quality["low_mean_groups"]) > 0
                or int(resume_quality["low_embedding_groups"]) > 0
                or int(resume_quality["distinct_representatives"])
                != int(resume_quality["groups"])
            ):
                raise RuntimeError(
                    "Run Gold ini tidak lulus pair/exact similarity atau "
                    "representative gate. "
                    "Jangan resume PUBLISH run lama; buat run_id baru mulai "
                    "MATCH -> VALIDATE -> PUBLISH. "
                    f"low_mean_groups={resume_quality['low_mean_groups']} "
                    f"low_embedding_groups={resume_quality['low_embedding_groups']} "
                    f"groups={resume_quality['groups']} "
                    f"distinct_representatives={resume_quality['distinct_representatives']}"
                )
            (
                sap_keys,
                total_sap_oph,
                monthly_sap_oph,
                estate_sap_oph,
                estate_monthly_sap_oph,
                sap_evaluated,
                sap_prfnr_scope,
            ) = _load_scoped_sap_keys(
                conn,
                settings,
                source_group,
            )
            with conn.cursor() as cur:
                sap_metrics = _write_sap_kpi_summary(
                    cur,
                    settings,
                    run_id,
                    source_group,
                    source_system,
                    source_company,
                    sap_keys,
                    total_sap_oph,
                    monthly_sap_oph,
                    estate_sap_oph,
                    estate_monthly_sap_oph,
                    sap_evaluated,
                    sap_prfnr_scope,
                    (),
                )
            gold_context_metrics = backfill_gold_from_context(
                settings,
                source_group,
                run_id=run_id,
            )
            metrics = {
                "gold_groups": int(existing[0]["n"]),
                "publish_resumed": 1,
                **sap_metrics,
                **raw_context_metrics,
                **gold_context_metrics,
            }
            update_run(settings, run_id, stage="PUBLISH", status="SUCCEEDED", metrics=metrics)
            return metrics

        groups = fetch_all_dict(
            conn,
            """
            SELECT validated_group_id, group_signature, representative_file_uid,
                   usage_count, validation_status, mean_pair_score,
                   mean_embedding_similarity, min_member_mean_similarity
            FROM processing.validated_group
            WHERE run_id=%s
            ORDER BY validated_group_id
            """,
            (run_id,),
        )
        if not groups:
            raise RuntimeError("Tidak ada validated group. Gold lama tidak dinonaktifkan.")

        exact_invalid_groups = []
        legacy_invalid_groups = []
        for group in groups:
            status = str(group.get("validation_status") or "").upper()
            minimum = (
                settings.min_publish_mean_review
                if "REVIEW" in status
                else settings.min_publish_mean_confirmed
            )
            score = group.get("mean_pair_score")
            embedding_mean = group.get("mean_embedding_similarity")
            member_mean = group.get("min_member_mean_similarity")
            if (
                score is None
                or float(score) < minimum
            ):
                legacy_invalid_groups.append(
                    (
                        group.get("validated_group_id"),
                        score,
                        embedding_mean,
                        member_mean,
                        status,
                    )
                )
            if (
                embedding_mean is None
                or float(embedding_mean) < settings.validation_group_mean_similarity
                or member_mean is None
                or float(member_mean) < settings.validation_member_mean_similarity
            ):
                exact_invalid_groups.append(
                    (
                        group.get("validated_group_id"),
                        score,
                        embedding_mean,
                        member_mean,
                        status,
                    )
                )
        exact_invalid_group_ids = {
            int(row[0]) for row in exact_invalid_groups if row[0] is not None
        }
        legacy_invalid_group_ids = {
            int(row[0]) for row in legacy_invalid_groups if row[0] is not None
        }
        if exact_invalid_group_ids:
            print(
                f"[PUBLISH] skip exact-invalid groups="
                f"{len(exact_invalid_group_ids):,}; "
                f"threshold group/member="
                f"{settings.validation_group_mean_similarity:.1f}/"
                f"{settings.validation_member_mean_similarity:.1f}; "
                f"samples={exact_invalid_groups[:10]}",
                flush=True,
            )
        if legacy_invalid_group_ids:
            print(
                f"[PUBLISH] skip legacy pair_score-invalid groups="
                f"{len(legacy_invalid_group_ids):,}; NULL/di bawah minimum "
                f"confirmed={settings.min_publish_mean_confirmed:.2f} "
                f"review={settings.min_publish_mean_review:.2f}; "
                f"samples={legacy_invalid_groups[:10]}",
                flush=True,
            )
        skipped_group_ids = exact_invalid_group_ids | legacy_invalid_group_ids
        groups = [
            group
            for group in groups
            if int(group["validated_group_id"]) not in skipped_group_ids
        ]
        if not groups:
            raise RuntimeError(
                "Semua validated group gagal pair/exact similarity gate. "
                "Gold lama tidak dinonaktifkan."
            )

        representative_counts = Counter(
            str(group.get("representative_file_uid") or "") for group in groups
        )
        duplicate_representatives = [
            uid for uid, count in representative_counts.items() if uid and count > 1
        ]
        if duplicate_representatives:
            raise RuntimeError(
                f"Ada {len(duplicate_representatives):,} representative_file_uid "
                "dipakai lebih dari satu validated group. Publish dibatalkan. "
                f"Contoh={duplicate_representatives[:10]}"
            )

        members = fetch_all_dict(
            conn,
            """
            SELECT
                m.validated_group_id,
                m.file_uid,
                m.file_name,
                m.is_representative,
                m.member_validity,
                m.member_mean_similarity,
                c.company_code,
                c.oph,
                c.oph_key,
                c.period_date,
                c.estate,
                c.afdeling,
                c.blok,
                c.tph,
                c.pemanen,
                c.kerani_panen,
                c.jjg_kerani_panen,
                c.mandor,
                c.latitude,
                c.longitude,
                c.match_method,
                f.embedding_dim,
                f.full_embedding
            FROM processing.validated_group_member m
            LEFT JOIN processing.image_context c ON c.file_uid=m.file_uid
            JOIN processing.image_feature f
              ON f.file_uid=m.file_uid AND f.model_version=%s AND f.is_valid=true
            WHERE m.run_id=%s
              AND m.validated_group_id=ANY(%s)
            ORDER BY m.validated_group_id, m.is_representative DESC, m.file_uid
            """,
            (
                settings.model_version,
                run_id,
                [int(group["validated_group_id"]) for group in groups],
            ),
        )

        groups_by_uid: dict[str, set[int]] = defaultdict(set)
        groups_by_name: dict[str, set[int]] = defaultdict(set)
        for row in members:
            validated_id = int(row["validated_group_id"])
            groups_by_uid[str(row["file_uid"])].add(validated_id)
            groups_by_name[str(row.get("file_name") or "").strip().upper()].add(validated_id)
        duplicate_member_uids = [uid for uid, ids in groups_by_uid.items() if len(ids) > 1]
        duplicate_member_names = [
            name for name, ids in groups_by_name.items() if name and len(ids) > 1
        ]
        if duplicate_member_uids or duplicate_member_names:
            raise RuntimeError(
                "Validated member tidak unik lintas group. Publish dibatalkan. "
                f"duplicate_file_uid={len(duplicate_member_uids):,} "
                f"duplicate_logical_name={len(duplicate_member_names):,} "
                f"sample_uid={duplicate_member_uids[:5]} "
                f"sample_name={duplicate_member_names[:5]}"
            )

        member_count_before_estate = len(members)
        if settings.publish_require_estate:
            members = [row for row in members if _clean(row.get("estate"))]
        excluded_missing_estate = member_count_before_estate - len(members)
        if not members:
            raise RuntimeError(
                "Semua validated member tidak mempunyai estate. Gold lama tidak dinonaktifkan."
            )

        # Pisahkan kontrak visual dan transaksi. Group reuse adalah hasil
        # similarity gambar dan tidak boleh hilang hanya karena OPH bisnis
        # belum dapat dibuktikan. OPH unsafe/kosong tetap dipublish sebagai
        # visual evidence dengan OPH=NULL sehingga tidak ikut KPI/SAP.
        # OPH terpercaya tetap mengikuti one-normalized-OPH-one-usage.
        #
        # v158: DIN historical pernah melewati Harmony + legacy ELHP path.
        # Sebagian Context lama membawa OPH nyata tetapi provenance-nya adalah
        # MATCH_BY_RAW_ACTOR_DATE_UNIQUE_ESTATE, sehingga v150+ menyanitasi OPH
        # tersebut. Exact RAW evidence sudah tidak selalu tersedia lagi. Alih-
        # alih mempercayai seluruh legacy OPH secara buta, normalisasi hanya
        # dilakukan bila normalized OPH tersebut tercatat pada exact SAP scope
        # key source yang sama DAN bulan SAP sama dengan period_date Context.
        # Provenance tetap diberi label SAP_NORMALIZED_LEGACY_* agar tidak
        # disamakan dengan exact filename/timestamp proof.
        sap_normalizable_month_keys: set[tuple[str, str]] = set()
        normalize_sources = {
            str(value).upper().strip()
            for value in settings.legacy_oph_sap_normalize_sources
            if str(value).strip()
        }
        normalize_month_pairs = settings.legacy_oph_sap_normalize_month_pairs()
        canonical_source = source_group.upper()

        def _legacy_normalize_policy_allows(row: dict[str, Any]) -> bool:
            period_value = row.get("period_date")
            period_month = str(period_value)[:7] if period_value else ""
            if canonical_source in normalize_sources:
                return True
            if (canonical_source, period_month) not in normalize_month_pairs:
                return False
            # ELHP month exception tetap harus ownership-safe. Ini mencegah
            # DAN Jan menarik ME1-4 / DWT walau OPH kebetulan overlap.
            estate = _clean(row.get("estate")).upper()
            if canonical_source == "DAN":
                return estate.startswith("PU")
            if canonical_source == "DWT":
                return estate.startswith("ME")
            return True

        if (
            (canonical_source in normalize_sources
             or any(src == canonical_source for src, _ in normalize_month_pairs))
            and table_exists(conn, "gold", "sap_oph_scope_key")
        ):
            unsafe_candidate_keys: set[str] = set()
            for candidate in members:
                method = _clean(candidate.get("match_method")).upper()
                if method not in _UNTRUSTED_OPH_MATCH_METHODS:
                    continue
                if not _legacy_normalize_policy_allows(candidate):
                    continue
                candidate_key = _clean(candidate.get("oph_key")).upper()
                if not candidate_key:
                    candidate_key = re.sub(
                        r"[^A-Z0-9]", "", _clean(candidate.get("oph")).upper()
                    )
                if candidate_key and candidate.get("period_date"):
                    unsafe_candidate_keys.add(candidate_key)
            if unsafe_candidate_keys:
                sap_rows = fetch_all_dict(
                    conn,
                    """
                    SELECT DISTINCT oph_key, sap_month
                    FROM gold.sap_oph_scope_key
                    WHERE source_group=%s
                      AND period_year=%s
                      AND oph_key=ANY(%s)
                    """,
                    (
                        source_group.upper(),
                        int(settings.sap_period_year),
                        sorted(unsafe_candidate_keys),
                    ),
                )
                sap_normalizable_month_keys = {
                    (str(row.get("oph_key") or "").upper(), str(row.get("sap_month") or ""))
                    for row in sap_rows
                    if row.get("oph_key") and row.get("sap_month")
                }

        member_count_before_oph = len(members)
        visual_members: list[dict[str, Any]] = []
        normalized_members: list[dict[str, Any]] = []
        excluded_untrusted_oph = 0
        excluded_missing_oph = 0
        for original in members:
            row = dict(original)
            method = _clean(row.get("match_method")).upper()
            if method in _UNTRUSTED_OPH_MATCH_METHODS:
                candidate_key = _clean(row.get("oph_key")).upper()
                if not candidate_key:
                    candidate_key = re.sub(
                        r"[^A-Z0-9]", "", _clean(row.get("oph")).upper()
                    )
                period_value = row.get("period_date")
                period_month = str(period_value)[:7] if period_value else ""
                if (
                    _legacy_normalize_policy_allows(row)
                    and candidate_key
                    and period_month
                    and (candidate_key, period_month) in sap_normalizable_month_keys
                ):
                    row["oph_key"] = candidate_key
                    row["match_method"] = f"SAP_NORMALIZED_LEGACY_{method}"
                    normalized_members.append(row)
                    visual_members.append(row)
                    continue
                excluded_untrusted_oph += 1
                row["oph"] = None
                row["oph_key"] = ""
                row["match_method"] = f"UNVERIFIED_OPH_{method}"
                visual_members.append(row)
                continue
            oph_key = _clean(row.get("oph_key")).upper()
            if not oph_key:
                oph_key = re.sub(
                    r"[^A-Z0-9]", "", _clean(row.get("oph")).upper()
                )
            if not oph_key:
                excluded_missing_oph += 1
                row["oph"] = None
                row["oph_key"] = ""
                row["match_method"] = (
                    f"UNVERIFIED_OPH_{method}" if method
                    else "UNVERIFIED_OPH_MISSING"
                )
                visual_members.append(row)
                continue
            row["oph_key"] = oph_key
            normalized_members.append(row)
            visual_members.append(row)

        group_quality = {
            int(group["validated_group_id"]): group for group in groups
        }

        def oph_winner_key(row: dict[str, Any]) -> tuple[Any, ...]:
            quality = group_quality[int(row["validated_group_id"])]
            return (
                # Bila satu OPH yang sama secara tidak wajar muncul lintas
                # bulan, pertahankan occurrence bisnis paling awal. Ini
                # mencegah ledakan bulan baru (misalnya Mei) menggantikan
                # bukti Januari-April.
                row.get("period_date") or date.max,
                _oph_match_priority(row.get("match_method")),
                -float(row.get("member_mean_similarity") or 0.0),
                -float(quality.get("min_member_mean_similarity") or 0.0),
                -float(quality.get("mean_embedding_similarity") or 0.0),
                -float(quality.get("mean_pair_score") or 0.0),
                0 if bool(row.get("is_representative")) else 1,
                str(row.get("file_uid") or ""),
            )

        winner_by_oph: dict[str, dict[str, Any]] = {}
        for row in normalized_members:
            oph_key = str(row["oph_key"])
            current = winner_by_oph.get(oph_key)
            if current is None or oph_winner_key(row) < oph_winner_key(current):
                winner_by_oph[oph_key] = row
        winner_file_uids = {
            str(row.get("file_uid") or "")
            for row in winner_by_oph.values()
        }
        for row in normalized_members:
            if str(row.get("file_uid") or "") in winner_file_uids:
                continue
            original_method = _clean(row.get("match_method")).upper()
            row["oph"] = None
            row["oph_key"] = ""
            row["match_method"] = (
                f"UNVERIFIED_DUPLICATE_OPH_{original_method}"
            )

        members = sorted(
            visual_members,
            key=lambda row: (
                int(row["validated_group_id"]),
                0 if bool(row.get("is_representative")) else 1,
                str(row.get("file_uid") or ""),
            ),
        )
        # Semua loser OPH tetap ada sebagai visual, sehingga jumlah sanitasi
        # dihitung dari kandidat OPH terpercaya dikurangi satu winner per key.
        deduplicated_oph_members = (
            len(normalized_members) - len(winner_by_oph)
        )
        sap_normalized_legacy_members = sum(
            1
            for row in normalized_members
            if _clean(row.get("match_method")).upper().startswith(
                "SAP_NORMALIZED_LEGACY_"
            )
        )
        print(
            "[PUBLISH VISUAL/OPH] "
            f"input_after_estate={member_count_before_oph:,} "
            f"visual_members={len(members):,} "
            f"sap_normalized_legacy={sap_normalized_legacy_members:,} "
            f"untrusted_oph_sanitized={excluded_untrusted_oph:,} "
            f"missing_oph_sanitized={excluded_missing_oph:,} "
            f"duplicate_oph_sanitized={deduplicated_oph_members:,} "
            f"verified_unique_oph={len(winner_by_oph):,}",
            flush=True,
        )
        if len(members) < 2:
            raise RuntimeError(
                "Kurang dari dua visual member setelah estate quality gate. "
                "Gold lama tidak dinonaktifkan."
            )

        matched_context = sum(
            1
            for row in members
            if row.get("match_method")
            and row.get("match_method") != "MATCH_DATE_FROM_FILENAME"
        )
        context_rate = matched_context / len(members) if members else 0.0
        if context_rate < settings.min_context_match_rate:
            raise RuntimeError(
                f"Context match rate {context_rate:.2%} di bawah batas "
                f"{settings.min_context_match_rate:.2%}. Gold lama tidak dinonaktifkan."
            )
        dated_members = sum(1 for row in members if row.get("period_date") is not None)
        date_rate = dated_members / len(members) if members else 0.0
        if settings.require_complete_month and dated_members != len(members):
            samples = [
                row.get("file_name", "")
                for row in members
                if row.get("period_date") is None
            ][:10]
            raise RuntimeError(
                f"Ada {len(members) - dated_members:,} dari {len(members):,} validated image "
                "tanpa period_date/bulan. Publish dibatalkan agar dashboard tidak "
                f"menampilkan bulan kosong. Contoh: {samples}"
            )

        transports = fetch_all_dict(
            conn,
            """
            SELECT
                t.file_uid, t.oph_key, t.transport_kind, t.file_name,
                f.embedding_dim, f.full_embedding
            FROM processing.transport_context t
            LEFT JOIN processing.image_feature f
              ON f.file_uid=t.file_uid AND f.model_version=%s AND f.is_valid=true
            WHERE upper(t.source_group)=upper(%s)
            ORDER BY t.oph_key, t.transport_kind, t.file_name
            """,
            (settings.model_version, source_group),
        )

    transport_map: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in transports:
        if row.get("full_embedding") is not None:
            row["vector"] = unpack_vector(row["full_embedding"], int(row["embedding_dim"]))
        else:
            row["vector"] = None
        transport_map[(row["oph_key"], row["transport_kind"])].append(row)

    by_validated_group: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in members:
        row["vector"] = unpack_vector(row["full_embedding"], int(row["embedding_dim"]))
        by_validated_group[int(row["validated_group_id"])].append(row)
    for rows in by_validated_group.values():
        # Bila representative asli termasuk member tanpa estate, promosikan
        # satu member valid supaya Gold tetap mempunyai tepat satu representative.
        if rows and not any(bool(row.get("is_representative")) for row in rows):
            rows[0]["is_representative"] = True

    with connection(settings) as conn, conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO gold.group_registry (source_group, group_signature)
            VALUES %s
            ON CONFLICT (source_group, group_signature) DO UPDATE SET last_seen_at=now()
            """,
            [(source_group, group["group_signature"]) for group in groups],
        )
        cur.execute(
            """
            SELECT group_id, group_signature
            FROM gold.group_registry
            WHERE source_group=%s AND group_signature=ANY(%s)
            """,
            (source_group, [group["group_signature"] for group in groups]),
        )
        registry = {signature: int(group_id) for group_id, signature in cur.fetchall()}

        group_rows = []
        detail_rows = []
        summary_rows = []
        dashboard_monthly_counter: Counter[tuple[Any, ...]] = Counter()
        audit_by_oph: dict[str, dict[str, Any]] = {}

        for group in groups:
            validated_id = int(group["validated_group_id"])
            rows = by_validated_group.get(validated_id, [])
            if len(rows) < 2:
                continue
            group_id = registry[group["group_signature"]]
            representative = next((row for row in rows if row["is_representative"]), rows[0])
            group_rows.append(
                (
                    run_id,
                    group_id,
                    source_group,
                    source_system,
                    source_company,
                    representative["file_uid"],
                    representative["file_name"],
                    len(rows),
                    group["validation_status"],
                    group["mean_pair_score"],
                    group["mean_embedding_similarity"],
                    group["min_member_mean_similarity"],
                    True,
                )
            )

            prepared_rows: list[dict[str, Any]] = []
            for row in rows:
                oph_key = _clean(row.get("oph_key"))
                dt_candidates = transport_map.get((oph_key, "DT"), [])
                ft_candidates = transport_map.get((oph_key, "FT"), [])
                best_dt, score_dt = _best_transport(row["vector"], dt_candidates)
                best_ft, score_ft = _best_transport(row["vector"], ft_candidates)
                scores = [score for score in (score_dt, score_ft) if score is not None]
                score = max(scores) if scores else None
                transport_status = _transport_status(dt_candidates, ft_candidates)
                similarity = _similarity_label(score, settings)
                match_source = _clean(row.get("match_method")) or "NOT_MATCH_DB_CONTEXT"
                company = _clean(row.get("company_code")) or source_company

                prepared = {
                    **row,
                    "group_id": group_id,
                    "source_company": company,
                    "foto_dt": best_dt["file_name"] if best_dt else "",
                    "foto_dt_uid": best_dt["file_uid"] if best_dt else "",
                    "foto_ft": best_ft["file_name"] if best_ft else "",
                    "foto_ft_uid": best_ft["file_uid"] if best_ft else "",
                    "transport_status": transport_status,
                    "similarity": similarity,
                    "similarity_score": score,
                    "match_source": match_source,
                    "tanggal": row.get("period_date"),
                    "bunch_pay": row.get("jjg_kerani_panen") or 0,
                }
                prepared_rows.append(prepared)
                tanggal = prepared.get("tanggal")
                if tanggal is not None:
                    month = tanggal.replace(day=1)
                    dashboard_monthly_counter[
                        (
                            run_id,
                            group_id,
                            source_group,
                            source_system,
                            company,
                            month,
                            _clean(row.get("estate")),
                            _clean(row.get("afdeling")),
                            _clean(row.get("kerani_panen")),
                            _clean(row.get("mandor")),
                            _clean(row.get("pemanen")),
                        )
                    ] += 1
                detail_rows.append(
                    (
                        run_id,
                        group_id,
                        row["file_uid"],
                        row["file_name"],
                        1 if row["is_representative"] else 0,
                        source_group,
                        source_system,
                        company,
                        row.get("estate"),
                        row.get("afdeling"),
                        row.get("blok"),
                        row.get("tph"),
                        row.get("oph"),
                        row.get("period_date"),
                        row.get("pemanen"),
                        row.get("kerani_panen"),
                        row.get("jjg_kerani_panen"),
                        row.get("mandor"),
                        None,
                        None,
                        None,
                        None,
                        similarity,
                        score,
                        row.get("jjg_kerani_panen") or 0,
                        best_dt["file_name"] if best_dt else None,
                        best_dt["file_uid"] if best_dt else None,
                        best_ft["file_name"] if best_ft else None,
                        best_ft["file_uid"] if best_ft else None,
                        transport_status,
                        match_source,
                        row.get("latitude"),
                        row.get("longitude"),
                        True,
                    )
                )
                if oph_key:
                    audit_by_oph[oph_key] = {
                        "oph": row.get("oph"),
                        "has_image": True,
                    }

            reuse_category = _reuse_category(prepared_rows)
            date_category = _date_category(prepared_rows)
            summary_rows.append(
                (
                    run_id,
                    group_id,
                    source_group,
                    source_system,
                    source_company,
                    representative["file_name"],
                    representative["file_uid"],
                    len(prepared_rows),
                    sum(1 for row in prepared_rows if row["match_source"] != "NOT_MATCH_DB_CONTEXT"),
                    group["mean_embedding_similarity"],
                    group["min_member_mean_similarity"],
                    _joined(row.get("estate") for row in prepared_rows),
                    _joined(row.get("afdeling") for row in prepared_rows),
                    _joined(row.get("blok") for row in prepared_rows),
                    _joined(row.get("tph") for row in prepared_rows),
                    _joined(row.get("oph") for row in prepared_rows),
                    _joined(row.get("tanggal") for row in prepared_rows),
                    _joined(row.get("pemanen") for row in prepared_rows),
                    _joined(row.get("kerani_panen") for row in prepared_rows),
                    _joined(row.get("mandor") for row in prepared_rows),
                    "",
                    "",
                    _joined(row.get("transport_status") for row in prepared_rows),
                    _joined(row.get("similarity") for row in prepared_rows),
                    sum(float(row.get("bunch_pay") or 0) for row in prepared_rows),
                    reuse_category,
                    date_category,
                    f"{reuse_category} | {date_category}",
                    True,
                )
            )

        if not group_rows:
            raise RuntimeError("Validated group ada, tetapi tidak ada group beranggota minimal dua yang dapat dipublish")

        dashboard_monthly_rows = [
            (*key, count, True)
            for key, count in dashboard_monthly_counter.items()
        ]

        cur.execute(
            """
            SELECT count(*)::bigint, coalesce(sum(usage_count), 0)::bigint
            FROM gold.reused_photo_group
            WHERE source_group=%s AND is_active=true
            """,
            (source_group,),
        )
        previous_group_count, previous_usage_count = map(int, cur.fetchone())
        cur.execute(
            """
            SELECT DISTINCT left(month::text, 7) AS period_month
            FROM gold.reused_photo_dashboard_monthly
            WHERE source_group=%s AND is_active=true AND month IS NOT NULL
            ORDER BY 1
            """,
            (source_group,),
        )
        previous_months = sorted(
            str(row[0]) for row in cur.fetchall() if row and row[0]
        )
        new_months = sorted(
            {
                str(key[5])[:7]
                for key in dashboard_monthly_counter
                if len(key) > 5 and key[5]
            }
        )
        new_usage_by_month: Counter[str] = Counter()
        for key, count in dashboard_monthly_counter.items():
            if len(key) > 5 and key[5]:
                new_usage_by_month[str(key[5])[:7]] += int(count)
        print(
            "[PUBLISH MONTH] "
            + ", ".join(
                f"{month}={new_usage_by_month[month]:,}"
                for month in sorted(new_usage_by_month)
            ),
            flush=True,
        )
        previous_month_set = set(previous_months)
        new_month_set = set(new_months)
        clean_expected_month_set = {
            str(value)
            for value in (
                run_guard[0].get("oph_clean_expected_months", [])
                if run_guard
                else []
            )
            if value
        }
        visual_oph_republish = bool(
            run_guard
            and bool(run_guard[0].get("oph_clean_republish"))
        )
        missing_clean_months = sorted(clean_expected_month_set - new_month_set)
        if (
            run_guard
            and bool(run_guard[0].get("oph_clean_republish"))
            and missing_clean_months
        ):
            raise RuntimeError(
                "Clean OPH akan menghilangkan bulan validated yang sebelumnya "
                "tersedia. Publish dibatalkan sebelum Gold lama dinonaktifkan. "
                f"expected={sorted(clean_expected_month_set)} "
                f"new={new_months} missing={missing_clean_months}. "
                "Periksa [OPH RECOVERY MONTH]."
            )
        missing_previous_months = sorted(previous_month_set - new_month_set)
        if missing_previous_months:
            raise RuntimeError(
                "Snapshot baru akan menghilangkan bulan Gold aktif: "
                + ", ".join(missing_previous_months)
                + f". old={previous_months} new={new_months}. "
                "PUBLISH dibatalkan sebelum Gold lama dinonaktifkan. Pulihkan "
                "Embedding/Context historis lalu ulangi MATCH -> VALIDATE dari "
                "NAS -> PUBLISH."
            )
        coverage_expanded = bool(new_month_set - previous_month_set)
        coverage_comparable = bool(previous_month_set) and (
            previous_month_set == new_month_set
        )
        new_group_count = len(group_rows)
        new_usage_count = len(detail_rows)
        group_growth_exceeded = bool(
            settings.max_gold_group_growth_factor > 0
            and previous_group_count > 0
            and new_group_count
            > previous_group_count * settings.max_gold_group_growth_factor
        )
        usage_growth_exceeded = bool(
            settings.max_gold_usage_growth_factor > 0
            and previous_usage_count > 0
            and new_usage_count
            > previous_usage_count * settings.max_gold_usage_growth_factor
        )
        growth_warnings: list[str] = []

        # Jumlah group dapat naik karena group lama dipecah oleh validator yang
        # lebih ketat. Itu indikator audit, bukan alasan membuang snapshot yang
        # seluruh group-nya sudah lulus quality gate. Lonjakan usage pada
        # cakupan bulan yang sama tetap menjadi blocking guard utama.
        if group_growth_exceeded:
            growth_warnings.append(
                f"Gold group {previous_group_count:,}->{new_group_count:,} "
                f"(>{settings.max_gold_group_growth_factor:.2f}x)"
            )
        if (
            usage_growth_exceeded
            and coverage_comparable
            and not visual_oph_republish
        ):
            raise RuntimeError(
                f"Gold usage melonjak {previous_usage_count:,} -> {new_usage_count:,} "
                f"(>{settings.max_gold_usage_growth_factor:.2f}x). Publish dibatalkan "
                "karena cakupan bulan lama dan baru sama "
                f"({','.join(new_months)}). Gold lama tetap aktif; audit "
                "MATCH/VALIDATE terlebih dahulu."
            )
        if usage_growth_exceeded:
            growth_warnings.append(
                f"Gold usage {previous_usage_count:,}->{new_usage_count:,} "
                f"(>{settings.max_gold_usage_growth_factor:.2f}x)"
            )
            if visual_oph_republish:
                growth_warnings.append(
                    "visual evidence dipulihkan; OPH unverified tetap NULL"
                )
        if growth_warnings:
            coverage_reason = (
                "month coverage expanded"
                if coverage_expanded
                else "month coverage not comparable"
            )
            print(
                "[PUBLISH] GROWTH WARNING (non-blocking): "
                + "; ".join(growth_warnings)
                + f" | {coverage_reason} old={previous_months} new={new_months}. "
                  "Semua group yang dipublish tetap sudah lulus quality gate.",
                flush=True,
            )

        # Atomic publication dilakukan hanya setelah semua quality gate lulus.
        cur.execute("UPDATE gold.reused_photo_group SET is_active=false WHERE source_group=%s AND is_active=true", (source_group,))
        cur.execute("UPDATE gold.reused_photo_detail SET is_active=false WHERE source_group=%s AND is_active=true", (source_group,))
        cur.execute("UPDATE gold.reused_photo_group_summary SET is_active=false WHERE source_group=%s AND is_active=true", (source_group,))
        cur.execute("UPDATE gold.reused_photo_dashboard_monthly SET is_active=false WHERE source_group=%s AND is_active=true", (source_group,))
        cur.execute("UPDATE gold.oph_audit SET is_active=false WHERE source_group=%s AND is_active=true", (source_group,))
        cur.execute("UPDATE gold.sap_oph_kpi_summary SET is_active=false WHERE source_group=%s AND is_active=true", (source_group,))

        execute_values(
            cur,
            """
            INSERT INTO gold.reused_photo_group
                (run_id, group_id, source_group, source_system, source_company,
                 representative_file_uid, representative_image, usage_count,
                 validation_status, mean_pair_score, mean_embedding_similarity,
                 min_member_mean_similarity, is_active)
            VALUES %s
            """,
            group_rows,
            page_size=1000,
        )
        execute_values(
            cur,
            """
            INSERT INTO gold.reused_photo_detail
                (run_id, group_id, file_uid, image_name, is_representative,
                 source_group, source_system, source_company, estate, afdeling,
                 blok, tph, oph, tanggal, pemanen, kerani_panen, jjg_kerani_panen,
                 mandor, kerani_ft, jjg_ft, kerani_dt, jjg_dt, similarity,
                 similarity_score, bunch_pay, foto_dt, foto_dt_uid, foto_ft,
                 foto_ft_uid, transport_status, match_source, latitude, longitude,
                 is_active)
            VALUES %s
            """,
            detail_rows,
            page_size=2000,
        )
        execute_values(
            cur,
            """
            INSERT INTO gold.reused_photo_group_summary
                (run_id, group_id, source_group, source_system, source_company,
                 representative_image, representative_file_uid, usage_count,
                 matched_image_count, mean_embedding_similarity,
                 min_member_mean_similarity, estate_list, afdeling_list, blok_list,
                 tph_list, oph_list, sql_date_list, pemanen_list, kerani_panen_list,
                 mandor_list, kerani_ft_list, kerani_dt_list, status_transport_list,
                 similarity_list, total_bunch_pay_group, reuse_category,
                 date_category, reuse_date_category, is_active)
            VALUES %s
            """,
            summary_rows,
            page_size=1000,
        )
        if dashboard_monthly_rows:
            execute_values(
                cur,
                """
                INSERT INTO gold.reused_photo_dashboard_monthly
                    (run_id, group_id, source_group, source_system,
                     source_company, month, estate, afdeling, kerani_panen,
                     mandor, pemanen, usage_count, is_active)
                VALUES %s
                """,
                dashboard_monthly_rows,
                page_size=2000,
            )

        # Hitung langsung DISTINCT OPH SAP pada PRFNR company. Tidak ada join
        # OPH Gold -> tabel SAP dan tidak ada denominator global lintas company.
        (
            sap_keys,
            total_sap_oph,
            monthly_sap_oph,
            estate_sap_oph,
            estate_monthly_sap_oph,
            sap_evaluated,
            sap_prfnr_scope,
        ) = _load_scoped_sap_keys(
            conn,
            settings,
            source_group,
            audit_by_oph.keys(),
        )

        # Audit OPH tetap menjadi daftar temuan Gold, tetapi tidak lagi dipaksa
        # melakukan lookup per OPH ke SAP. KPI memakai agregat count-only.
        audit_rows = [
            (
                run_id,
                source_group,
                item["oph"],
                oph_key,
                True,
                bool(oph_key in sap_keys) if sap_evaluated else False,
                item["has_image"],
                (
                    "SAP_VERIFIED"
                    if sap_evaluated and oph_key in sap_keys
                    else ("NOT_IN_SCOPED_SAP" if sap_evaluated else "SAP_NOT_EVALUATED")
                ),
                True,
            )
            for oph_key, item in audit_by_oph.items()
        ]
        if audit_rows:
            execute_values(
                cur,
                """
                INSERT INTO gold.oph_audit
                    (run_id, source_group, oph, oph_key, in_business_source,
                     in_sap, has_image, status, is_active)
                VALUES %s
                """,
                audit_rows,
            )

        sap_metrics = _write_sap_kpi_summary(
            cur,
            settings,
            run_id,
            source_group,
            source_system,
            source_company,
            sap_keys,
            total_sap_oph,
            monthly_sap_oph,
            estate_sap_oph,
            estate_monthly_sap_oph,
            sap_evaluated,
            sap_prfnr_scope,
            audit_by_oph.keys(),
        )

    metrics = {
        "gold_groups": len(group_rows),
        "gold_detail_rows": len(detail_rows),
        "gold_oph_audit_rows": len(audit_rows),
        **sap_metrics,
        "gold_dashboard_monthly_rows": len(dashboard_monthly_rows),
        "gold_excluded_missing_estate": excluded_missing_estate,
        "gold_excluded_untrusted_oph": excluded_untrusted_oph,
        "gold_excluded_missing_oph": excluded_missing_oph,
        "gold_untrusted_oph_sanitized": excluded_untrusted_oph,
        "gold_missing_oph_visual_members": excluded_missing_oph,
        "gold_duplicate_oph_occurrences_removed": deduplicated_oph_members,
        "gold_duplicate_oph_sanitized": deduplicated_oph_members,
        "gold_unique_oph_usages": len(audit_by_oph),
        "gold_visual_usages": len(detail_rows),
        "gold_visual_usages_without_verified_oph": (
            len(detail_rows) - len(audit_by_oph)
        ),
        "gold_groups_removed_after_oph_gate": len(groups) - len(group_rows),
        "gold_groups_removed_after_visual_gate": len(groups) - len(group_rows),
        "gold_previous_groups": previous_group_count,
        "gold_previous_usages": previous_usage_count,
        "gold_previous_months": previous_months,
        "gold_new_months": new_months,
        "gold_new_usages_by_month": dict(sorted(new_usage_by_month.items())),
        "gold_clean_expected_months": sorted(clean_expected_month_set),
        "gold_clean_missing_months": missing_clean_months,
        "gold_missing_previous_months": missing_previous_months,
        "gold_month_coverage_expanded": coverage_expanded,
        "gold_growth_warnings": growth_warnings,
        "gold_group_mean_similarity_threshold": settings.validation_group_mean_similarity,
        "gold_member_mean_similarity_threshold": settings.validation_member_mean_similarity,
        "gold_exact_invalid_groups_skipped": len(exact_invalid_group_ids),
        "gold_legacy_pair_score_groups_skipped": len(legacy_invalid_group_ids),
        "gold_total_quality_groups_skipped": len(skipped_group_ids),
        "context_match_rate_pct": round(context_rate * 100.0, 2),
        "date_match_rate_pct": round(date_rate * 100.0, 2),
        **raw_context_metrics,
    }
    update_run(settings, run_id, stage="PUBLISH", status="SUCCEEDED", metrics=metrics)
    return metrics
