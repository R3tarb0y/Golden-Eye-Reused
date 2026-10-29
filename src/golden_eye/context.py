from __future__ import annotations

from typing import Iterable
import time

from psycopg2.extras import execute_values

from .config import Settings
from .db import connection, ident, qname, table_columns, table_exists
from .utils import parse_date_from_filename


ALIASES: dict[str, list[str]] = {
    "source_pk": ["id", "record_id", "row_id"],
    "company_code": ["company_code", "company", "bukrs"],
    "oph": ["harvest_ticket", "oph", "tiket_panen", "no_bcc"],
    "period_date": ["period_date", "report_date", "tanggal", "budat", "date"],
    "estate": ["department_code", "estate", "kebun", "department"],
    "afdeling": ["afdeling_code", "afdeling_id", "afdeling", "afd"],
    "blok": ["block", "blok", "block_code"],
    "tph": ["tph_no", "tph", "tph_code"],
    "pemanen": ["name", "pemanen", "nama_pemanen", "employee_name"],
    "kerani_panen": ["name_kerani", "nama_kerani_panen", "kerani_panen", "kerani"],
    "jjg_kerani_panen": ["bunch", "jjg_kerani_panen", "jjg", "qty_jjg", "bunch_ai_kp"],
    "mandor": ["name_mandor", "nama_mandor", "mandor", "ename_m"],
    "nik_pemanen": ["nik", "nik_pemanen"],
    "nik_kerani": ["nik_kerani", "nik_keran"],
    "nik_mandor": ["nik_mandor", "nik_mand"],
    "npb": ["npb_no", "npb"],
    "latitude": ["latitude", "lat"],
    "longitude": ["longitude", "long", "lng"],
    "brd": ["brd"],
    "bs": ["bs"],
    "bm": ["bm"],
    "bk": ["bk"],
    "tp": ["tp"],
    # Jangan hanya mengambil kandidat pertama. Download report lama memakai
    # kombinasi FFB_EVIDENCE_KP dan OUTPUT_FILE; salah satunya dapat kosong.
    "evidence": [
        "ffb_evidence_kp", "output_file", "file_name", "filename",
        "image_name", "nama_gambar", "photo_url",
    ],
    "account_status": ["account_status", "status_account"],
}


NUMERIC_FIELDS = {
    "jjg_kerani_panen",
    "latitude",
    "longitude",
    "brd",
    "bs",
    "bm",
    "bk",
    "tp",
}


def _find(columns: Iterable[str], candidates: Iterable[str]) -> str | None:
    mapping = {column.lower(): column for column in columns}
    for candidate in candidates:
        if candidate.lower() in mapping:
            return mapping[candidate.lower()]
    return None


def _find_all(columns: Iterable[str], candidates: Iterable[str]) -> list[str]:
    """Kembalikan seluruh kolom kandidat yang tersedia, tanpa duplikat.

    Flow Excel lama mencocokkan FFB_EVIDENCE_KP *dan* basename OUTPUT_FILE.
    `_find` hanya memilih kolom pertama sehingga transaksi historis dapat
    hilang walaupun basename yang benar masih ada di kolom alternatif.
    """
    mapping = {column.lower(): column for column in columns}
    found: list[str] = []
    for candidate in candidates:
        column = mapping.get(candidate.lower())
        if column and column not in found:
            found.append(column)
    return found


def _evidence_match_sql(
    columns: Iterable[str],
    image_name_sql: str,
    *,
    alias: str = "p",
) -> tuple[list[str], str, str, str]:
    """Bangun condition, priority dan label untuk semua kolom evidence.

    Hanya basename yang dibandingkan, sama seperti code GT Excel lama. Rule
    ini source-neutral dan digunakan oleh seluruh company.
    """
    evidence_columns = _find_all(columns, ALIASES["evidence"])
    image_key = f"processing.basename_upper({image_name_sql})"
    comparisons: list[str] = []
    priority_parts: list[str] = []
    method_parts: list[str] = []
    for priority, column in enumerate(evidence_columns):
        # Samakan ekspresi ini persis dengan
        # docs/OPTIONAL_RAW_CONTEXT_INDEX.sql. Bentuk NULLIF(trim(...))
        # setara secara bisnis, tetapi PostgreSQL tidak selalu dapat
        # mencocokkannya ke functional index basename_upper(btrim(...)).
        value = f"btrim({alias}.{ident(column)}::text)"
        comparison = f"processing.basename_upper({value})={image_key}"
        comparisons.append(comparison)
        priority_parts.append(f"WHEN {comparison} THEN {priority}")
        canonical = column.upper()
        if canonical == "FFB_EVIDENCE_KP":
            method = "MATCH_BY_FFB_EVIDENCE_KP"
        elif canonical == "OUTPUT_FILE":
            method = "MATCH_BY_OUTPUT_FILE"
        else:
            method = "MATCH_BY_ALTERNATE_EVIDENCE"
        method_parts.append(f"WHEN {comparison} THEN '{method}'")
    condition = "(" + " OR ".join(comparisons) + ")" if comparisons else "false"
    priority_sql = "CASE " + " ".join(priority_parts) + " ELSE 999 END"
    method_sql = (
        "CASE " + " ".join(method_parts)
        + " ELSE 'MATCH_BY_BUSINESS_EVIDENCE' END"
    )
    return evidence_columns, condition, priority_sql, method_sql


def _period_date_sql(columns: Iterable[str], *, alias: str = "p") -> str:
    """Tanggal bisnis dari period_date atau komponen year/month/day yang valid."""
    mapping = {column.lower(): column for column in columns}
    direct = _date(
        next(
            (mapping[name] for name in ALIASES["period_date"] if name in mapping),
            None,
        ),
        alias,
    )
    year_col = mapping.get("period_year")
    month_col = mapping.get("period_month")
    day_col = mapping.get("period_day")
    if not (year_col and month_col and day_col):
        return direct
    token = (
        f"lpad(btrim({alias}.{ident(year_col)}::text), 4, '0') || "
        f"lpad(btrim({alias}.{ident(month_col)}::text), 2, '0') || "
        f"lpad(btrim({alias}.{ident(day_col)}::text), 2, '0')"
    )
    component = f"""
        CASE
            WHEN ({token}) ~ '^20[0-9]{{6}}$'
             AND to_char(to_date(({token}), 'YYYYMMDD'), 'YYYYMMDD')=({token})
            THEN to_date(({token}), 'YYYYMMDD')
            ELSE NULL::date
        END
    """
    return f"coalesce({direct}, {component})"


def _text(column: str | None, alias: str = "p") -> str:
    return f"NULLIF(trim({alias}.{ident(column)}::text), '')" if column else "NULL::text"


def _numeric(column: str | None, alias: str = "p") -> str:
    if not column:
        return "NULL::numeric"
    raw = f"regexp_replace(coalesce({alias}.{ident(column)}::text, ''), '[^0-9.\\-]', '', 'g')"
    return f"NULLIF({raw}, '')::numeric"


def _date(column: str | None, alias: str = "p") -> str:
    if not column:
        return "NULL::date"
    return f"{alias}.{ident(column)}::date"


def _context_source_aliases(source_group: str) -> tuple[str, ...]:
    target = str(source_group or "").upper().strip()
    mapping = {
        "PWP": ("PWP",),
        "DIN": ("DIN", "ELHP_DIN"),
        "DAN": ("DAN", "ELHP_DAN"),
        "DWT": ("DWT", "ELHP_DWT"),
        "ELHP_DIN": ("DIN", "ELHP_DIN"),
        "ELHP_DAN": ("DAN", "ELHP_DAN"),
        "ELHP_DWT": ("DWT", "ELHP_DWT"),
    }
    return mapping.get(target, (target,))


def sync_context(
    settings: Settings,
    source_group: str,
    *,
    skip_silver_business_lookup: bool = False,
    target_month: str | None = None,
) -> dict[str, object]:
    """Sinkronkan metadata gambar ke ``processing.image_context``.

    ``skip_silver_business_lookup`` dipakai oleh merge bulanan setelah RAW
    DB45 bulan target selesai disinkronkan. Pada mode itu tabel bisnis RAW
    adalah sumber OPH/estate yang kanonik; join evidence ke snapshot bisnis
    Silver yang besar hanya mengulang pekerjaan dan dapat membuat proses
    terlihat macet sebelum masuk ke ``[RAW CONTEXT]``.

    ``target_month`` (YYYY-MM) membuat perbaikan tanggal filename benar-benar
    incremental. Tanpa scope ini versi lama dapat menyapu seluruh histori
    ``image_store_all_files`` setiap resume bulanan.
    """
    source_group = source_group.upper()
    source_aliases = _context_source_aliases(source_group)
    image_table = qname(settings.silver_schema, settings.image_table)
    business_table = qname(settings.silver_schema, settings.business_table)
    context_count = 0
    evidence_columns: list[str] = []

    with connection(settings) as conn:
        if not table_exists(conn, settings.silver_schema, settings.image_table):
            raise RuntimeError(f"Tabel sumber gambar tidak ada: {image_table}")

        if skip_silver_business_lookup:
            print(
                "[CONTEXT] mode=MONTH_RAW_CANONICAL; "
                "Silver business evidence lookup dilewati",
                flush=True,
            )
        elif table_exists(conn, settings.silver_schema, settings.business_table):
            columns = table_columns(conn, settings.silver_schema, settings.business_table)
            resolved = {key: _find(columns, aliases) for key, aliases in ALIASES.items()}
            (
                evidence_columns,
                evidence_condition,
                evidence_priority,
                evidence_method,
            ) = _evidence_match_sql(columns, "i.file_name::text")
            if not evidence_columns:
                raise RuntimeError(
                    f"Kolom bukti gambar tidak ditemukan di {business_table}. "
                    f"Kandidat: {ALIASES['evidence']}"
                )

            values = {
                key: (
                    _numeric(column)
                    if key in NUMERIC_FIELDS
                    else _date(column)
                    if key == "period_date"
                    else _text(column)
                )
                for key, column in resolved.items()
                if key not in {"evidence", "account_status"}
            }
            # Download report/RAW tertentu menyimpan tanggal hanya sebagai
            # period_year, period_month, period_day.
            values["period_date"] = _period_date_sql(columns)
            status_values = settings.account_status_values_for(source_group)
            account_filter = ""
            if resolved["account_status"] and status_values:
                account_filter = (
                    f"AND upper(trim(p.{ident(resolved['account_status'])}::text)) = ANY(%s)"
                )

            source_pk_expr = values["source_pk"] if resolved["source_pk"] else "NULL::text"
            # Evidence yang sama dapat muncul pada transaksi lintas bulan.
            # Memilih period_date terbaru secara buta menggeser foto lama ke
            # Mei/Juni. Utamakan tanggal/bulan kamera yang tertulis di filename.
            filename_date_priority = "2"
            if values["period_date"] != "NULL::date":
                filename_date_priority = f"""
                    CASE
                        WHEN {values['period_date']} IS NOT NULL
                         AND position(to_char({values['period_date']}, 'YYYYMMDD')
                                      in i.file_name::text) > 0 THEN 0
                        WHEN {values['period_date']} IS NOT NULL
                         AND position(to_char({values['period_date']}, 'YYYYMM')
                                      in i.file_name::text) > 0 THEN 1
                        ELSE 2
                    END
                """
            sql = f"""
                INSERT INTO processing.image_context (
                    file_uid, source_group, company_code, oph, oph_key, period_date,
                    estate, afdeling, blok, tph, pemanen, kerani_panen,
                    jjg_kerani_panen, mandor, nik_pemanen, nik_kerani, nik_mandor,
                    npb, latitude, longitude, brd, bs, bm, bk, tp,
                    source_table, source_pk, match_method, updated_at
                )
                SELECT DISTINCT ON (i.file_uid::text)
                    i.file_uid::text,
                    upper(i.source_group::text),
                    {values['company_code']},
                    {values['oph']},
                    processing.normalize_oph({values['oph']}),
                    {values['period_date']},
                    {values['estate']},
                    {values['afdeling']},
                    {values['blok']},
                    {values['tph']},
                    {values['pemanen']},
                    {values['kerani_panen']},
                    {values['jjg_kerani_panen']},
                    {values['mandor']},
                    {values['nik_pemanen']},
                    {values['nik_kerani']},
                    {values['nik_mandor']},
                    {values['npb']},
                    {values['latitude']},
                    {values['longitude']},
                    {values['brd']},
                    {values['bs']},
                    {values['bm']},
                    {values['bk']},
                    {values['tp']},
                    %s,
                    {source_pk_expr},
                    {evidence_method},
                    now()
                FROM {image_table} i
                JOIN {business_table} p
                  ON {evidence_condition}
                WHERE upper(i.source_group::text)=upper(%s)
                  AND coalesce(i.is_transport, false)=false
                  {account_filter}
                ORDER BY i.file_uid::text,
                         {evidence_priority},
                         {filename_date_priority},
                         {values['period_date']} DESC NULLS LAST
                ON CONFLICT (file_uid) DO UPDATE SET
                    source_group=excluded.source_group,
                    company_code=coalesce(excluded.company_code, processing.image_context.company_code),
                    oph=coalesce(excluded.oph, processing.image_context.oph),
                    oph_key=coalesce(excluded.oph_key, processing.image_context.oph_key),
                    period_date=coalesce(excluded.period_date, processing.image_context.period_date),
                    estate=coalesce(excluded.estate, processing.image_context.estate),
                    afdeling=coalesce(excluded.afdeling, processing.image_context.afdeling),
                    blok=coalesce(excluded.blok, processing.image_context.blok),
                    tph=coalesce(excluded.tph, processing.image_context.tph),
                    pemanen=coalesce(excluded.pemanen, processing.image_context.pemanen),
                    kerani_panen=coalesce(excluded.kerani_panen, processing.image_context.kerani_panen),
                    jjg_kerani_panen=coalesce(excluded.jjg_kerani_panen, processing.image_context.jjg_kerani_panen),
                    mandor=coalesce(excluded.mandor, processing.image_context.mandor),
                    nik_pemanen=coalesce(excluded.nik_pemanen, processing.image_context.nik_pemanen),
                    nik_kerani=coalesce(excluded.nik_kerani, processing.image_context.nik_kerani),
                    nik_mandor=coalesce(excluded.nik_mandor, processing.image_context.nik_mandor),
                    npb=coalesce(excluded.npb, processing.image_context.npb),
                    latitude=coalesce(excluded.latitude, processing.image_context.latitude),
                    longitude=coalesce(excluded.longitude, processing.image_context.longitude),
                    brd=coalesce(excluded.brd, processing.image_context.brd),
                    bs=coalesce(excluded.bs, processing.image_context.bs),
                    bm=coalesce(excluded.bm, processing.image_context.bm),
                    bk=coalesce(excluded.bk, processing.image_context.bk),
                    tp=coalesce(excluded.tp, processing.image_context.tp),
                    source_table=excluded.source_table,
                    source_pk=excluded.source_pk,
                    match_method=excluded.match_method,
                    updated_at=now()
            """
            with conn.cursor() as cur:
                params: list[object] = [
                    f"{settings.silver_schema}.{settings.business_table}",
                    source_group,
                ]
                if resolved["account_status"] and status_values:
                    params.append(list(status_values))
                cur.execute(sql, params)
                context_count = cur.rowcount
        else:
            print(f"[CONTEXT] warning: {business_table} tidak ada; metadata GT akan kosong")

        if skip_silver_business_lookup:
            # merge-month hanya memuat foto panen; transport memiliki command
            # sync tersendiri dan tidak perlu melakukan scan filename di sini.
            transport_count = 0
        else:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO processing.transport_context
                        (file_uid, source_group, oph, oph_key, transport_kind,
                         blok, tph, file_name, updated_at)
                    SELECT
                        file_uid::text,
                        upper(source_group::text),
                        split_part(regexp_replace(file_name::text, '\\.[^.]+$', ''), '_', 3),
                        processing.normalize_oph(split_part(regexp_replace(file_name::text, '\\.[^.]+$', ''), '_', 3)),
                        upper(split_part(regexp_replace(file_name::text, '\\.[^.]+$', ''), '_', 2)),
                        split_part(regexp_replace(file_name::text, '\\.[^.]+$', ''), '_', 4),
                        split_part(regexp_replace(file_name::text, '\\.[^.]+$', ''), '_', 5),
                        file_name::text,
                        now()
                    FROM {image_table}
                    WHERE upper(source_group::text)=upper(%s)
                      AND upper(split_part(regexp_replace(file_name::text, '\\.[^.]+$', ''), '_', 1))='TRANSPORT'
                      AND upper(split_part(regexp_replace(file_name::text, '\\.[^.]+$', ''), '_', 2)) IN ('DT', 'FT')
                    ON CONFLICT (file_uid) DO UPDATE SET
                        source_group=excluded.source_group,
                        oph=excluded.oph,
                        oph_key=excluded.oph_key,
                        transport_kind=excluded.transport_kind,
                        blok=excluded.blok,
                        tph=excluded.tph,
                        file_name=excluded.file_name,
                        updated_at=now()
                    """,
                    (source_group,),
                )
                transport_count = cur.rowcount

        # Tanggal kamera pada filename adalah otoritas distribusi bulan image.
        # V183: merge bulanan WAJIB membatasi pekerjaan ke bulan target.
        # Metadata Silver tetap dapat dipakai walaupun BYTEA sudah dipindahkan
        # ke NAS, jadi jangan lagi menjadikan image_bytes IS NOT NULL sebagai
        # syarat untuk memperbaiki context tanggal.
        print(
            f"[CONTEXT DATE] source={source_group} "
            f"month={target_month or 'ALL'} status=FETCH",
            flush=True,
        )
        date_fetch_started = time.perf_counter()
        month_token = (target_month or "").replace("-", "")
        monthly_scope_sql = ""
        monthly_scope_params: list[object] = []
        if target_month:
            if len(month_token) != 6 or not month_token.isdigit():
                raise ValueError(
                    f"target_month tidak valid: {target_month!r}; expected YYYY-MM"
                )
            # Format Harmony/ELHP normal membawa YYYYMMDD pada filename.
            # Filename Android berbasis epoch tetap diikutkan dan disaring lagi
            # oleh parse_date_from_filename di Python.
            monthly_scope_sql = """
              AND (
                    position(%s in i.file_name::text) > 0
                    OR i.file_name::text ~* '^(JPEG|IMG)[_-][0-9]{13}([^0-9]|$)'
                  )
            """
            monthly_scope_params.append(month_token)

        with conn.cursor() as cur:
            # Safety guard: monthly Context tidak boleh lagi menggantung berjam-jam.
            cur.execute("SET LOCAL statement_timeout = '300s'")
            # Batasi planner ke source_group terlebih dahulu. Bentuk equality
            # ini dapat menggunakan prefix index (source_group, ...), berbeda
            # dengan upper(source_group)=upper(%s) yang cenderung memicu scan.
            cur.execute(
                f"""
                WITH target_files AS MATERIALIZED (
                    SELECT
                        i.file_uid::text AS file_uid,
                        i.source_group::text AS source_group,
                        i.file_name::text AS file_name
                    FROM {image_table} i
                    WHERE i.source_group = ANY(%s)
                      AND coalesce(i.is_transport, false)=false
                      AND coalesce(i.file_exists, true)=true
                      AND coalesce(i.zero_byte, false)=false
                      AND (
                          i.file_name::text ~ '20[0-9]{{6}}'
                          OR i.file_name::text ~* '^(JPEG|IMG)[_-][0-9]{{13}}([^0-9]|$)'
                      )
                      {monthly_scope_sql}
                )
                SELECT
                    t.file_uid,
                    upper(t.source_group),
                    t.file_name,
                    c.period_date
                FROM target_files t
                LEFT JOIN processing.image_context c
                  ON c.file_uid=t.file_uid
                """,
                tuple([list(source_aliases)] + monthly_scope_params),
            )
            missing_date_rows = cur.fetchall()

        print(
            f"[CONTEXT DATE] source={source_group} month={target_month or 'ALL'} "
            f"fetched={len(missing_date_rows):,} "
            f"elapsed={time.perf_counter()-date_fetch_started:.1f}s status=PARSE",
            flush=True,
        )

        date_rows = []
        date_parse_failed = 0
        for idx, (file_uid, row_source_group, file_name, current_period_date) in enumerate(
            missing_date_rows, start=1
        ):
            parsed_date = parse_date_from_filename(
                file_name,
                settings.filename_timestamp_utc_offset_hours,
            )
            if parsed_date is None:
                date_parse_failed += 1
                continue
            if target_month and parsed_date.strftime("%Y-%m") != target_month:
                continue
            if current_period_date == parsed_date:
                continue
            date_rows.append(
                (
                    file_uid,
                    row_source_group,
                    parsed_date,
                    f"{settings.silver_schema}.{settings.image_table}",
                    "MATCH_DATE_FROM_FILENAME",
                )
            )
            if idx % 25000 == 0:
                print(
                    f"[CONTEXT DATE] parsed={idx:,}/{len(missing_date_rows):,} "
                    f"changes={len(date_rows):,}",
                    flush=True,
                )

        if date_rows:
            upsert_started = time.perf_counter()
            with conn.cursor() as cur:
                for batch_start in range(0, len(date_rows), 5000):
                    batch = date_rows[batch_start:batch_start + 5000]
                    execute_values(
                        cur,
                        """
                        INSERT INTO processing.image_context
                            (file_uid, source_group, period_date, source_table,
                             match_method)
                        VALUES %s
                        ON CONFLICT (file_uid) DO UPDATE SET
                            period_date=excluded.period_date,
                            source_table=coalesce(
                                processing.image_context.source_table,
                                excluded.source_table
                            ),
                            match_method=CASE
                                WHEN processing.image_context.period_date IS DISTINCT FROM excluded.period_date
                                THEN excluded.match_method
                                WHEN nullif(trim(coalesce(processing.image_context.match_method, '')), '') IS NULL
                                THEN excluded.match_method
                                ELSE processing.image_context.match_method
                            END,
                            updated_at=now()
                        """,
                        batch,
                        page_size=5000,
                    )
                    print(
                        f"[CONTEXT DATE] upserted="
                        f"{min(batch_start + len(batch), len(date_rows)):,}/"
                        f"{len(date_rows):,}",
                        flush=True,
                    )
                date_from_filename_upserted = len(date_rows)
            print(
                f"[CONTEXT DATE] status=DONE changed={date_from_filename_upserted:,} "
                f"elapsed={time.perf_counter()-upsert_started:.1f}s",
                flush=True,
            )
        else:
            date_from_filename_upserted = 0
            print(
                f"[CONTEXT DATE] status=DONE changed=0",
                flush=True,
            )

        # Remaining-without-date dipakai sebagai diagnostic. Pada monthly merge
        # cukup hitung scope bulan yang sama; jangan kembali menyapu histori.
        remaining_scope_sql = monthly_scope_sql
        remaining_params = [list(source_aliases)] + monthly_scope_params
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT count(*)::bigint
                FROM {image_table} i
                LEFT JOIN processing.image_context c ON c.file_uid=i.file_uid::text
                WHERE i.source_group = ANY(%s)
                  AND coalesce(i.is_transport, false)=false
                  AND coalesce(i.file_exists, true)=true
                  AND coalesce(i.zero_byte, false)=false
                  AND c.period_date IS NULL
                  {remaining_scope_sql}
                """,
                tuple(remaining_params),
            )
            remaining_without_date = int(cur.fetchone()[0])

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT to_char(period_date, 'YYYY-MM') AS month,
                       count(*)::bigint AS rows
                FROM processing.image_context
                WHERE upper(source_group)=upper(%s)
                  AND period_date IS NOT NULL
                GROUP BY to_char(period_date, 'YYYY-MM')
                ORDER BY month
                """,
                (source_group,),
            )
            context_months = {
                str(month): int(rows) for month, rows in cur.fetchall()
            }

    metrics = {
        "context_upserted": context_count,
        "context_evidence_columns": evidence_columns,
        "transport_upserted": transport_count,
        "date_from_filename_upserted": date_from_filename_upserted,
        "date_parse_failed": date_parse_failed,
        "remaining_without_date": remaining_without_date,
        "context_months": context_months,
    }
    print(f"[CONTEXT] {metrics}")
    return metrics
