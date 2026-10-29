from __future__ import annotations

import uuid
from time import perf_counter
from typing import Any

from .config import Settings
from .context import (
    ALIASES,
    NUMERIC_FIELDS,
    _date,
    _evidence_match_sql,
    _find,
    _numeric,
    _period_date_sql,
    _text,
)
from .db import connection, ident, qname, table_columns, table_exists


_LEGACY_UNSAFE_OPH_METHODS = (
    "MATCH_BY_RAW_ACTOR_DATE_UNIQUE_ESTATE",
    "MATCH_BY_RAW_ACTOR_DATE_UNIQUE_ESTATE_NO_OPH",
)


def _coalesce_text(columns: list[str], candidates: list[str]) -> str:
    mapping = {column.lower(): column for column in columns}
    expressions = [
        _text(mapping[candidate.lower()])
        for candidate in candidates
        if candidate.lower() in mapping
    ]
    if not expressions:
        return "NULL::text"
    if len(expressions) == 1:
        return expressions[0]
    return "coalesce(" + ", ".join(expressions) + ")"


def _raw_values(columns: list[str]) -> tuple[dict[str, str | None], dict[str, str]]:
    resolved = {key: _find(columns, aliases) for key, aliases in ALIASES.items()}
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
    values["company_code"] = _coalesce_text(
        columns,
        ["company_code", "company_code_sap", "company", "bukrs"],
    )
    values["estate"] = _coalesce_text(
        columns,
        ["department_code", "department_code_sap", "estnr", "estate"],
    )
    values["blok"] = _coalesce_text(
        columns,
        ["block", "block_sap", "blok", "block_code"],
    )
    values["period_date"] = _period_date_sql(columns)
    return resolved, values


def _context_source_aliases(settings: Settings, source_group: str) -> list[str]:
    """Semua source_group yang memetakan ke company canonical yang sama.

    Prefix ``ELHP_`` adalah konvensi source-system, bukan pengecualian aturan
    similarity untuk DAN/DWT. Dengan demikian donor historis tetap ditemukan
    walau `.env` lama belum mencantumkan alias tersebut.
    """
    canonical = source_group.upper().strip()
    company = settings.source_company(canonical)
    aliases = [
        key.upper()
        for key, value in (settings.source_company_map or {}).items()
        if str(value).upper() == company
    ]
    if canonical not in aliases:
        aliases.append(canonical)
    if settings.source_system(canonical).upper() == "ELHP":
        unprefixed = canonical.removeprefix("ELHP_")
        aliases.extend([unprefixed, f"ELHP_{unprefixed}"])
    return list(dict.fromkeys(aliases))


def _sync_historical_gold_context(
    conn,
    settings: Settings,
    source_group: str,
) -> int:
    """Pulihkan metadata transaksi yang sudah pernah benar di snapshot Gold.

    Publish bersifat snapshot-replacement. Metadata historis tidak boleh hilang
    hanya karena run baru sedang membentuk ulang group. Donor dipilih per
    file_uid dari publish terbaru dan hanya mengisi kolom Context yang kosong.
    Rule ini berlaku identik untuk semua company.
    """
    aliases = _context_source_aliases(settings, source_group)
    image_table = qname(settings.silver_schema, settings.image_table)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH wanted AS (
                SELECT i.file_uid::text AS file_uid
                FROM {image_table} i
                LEFT JOIN processing.image_context c
                  ON c.file_uid=i.file_uid::text
                WHERE upper(i.source_group::text)=upper(%s)
                  AND coalesce(i.is_transport, false)=false
                  AND (
                      nullif(btrim(coalesce(c.estate, '')), '') IS NULL
                      OR nullif(btrim(coalesce(c.kerani_panen, '')), '') IS NULL
                      OR nullif(btrim(coalesce(c.mandor, '')), '') IS NULL
                  )
            ), donor AS (
                SELECT DISTINCT ON (d.file_uid)
                    d.file_uid, d.source_company, d.oph, d.tanggal,
                    d.estate, d.afdeling, d.blok, d.tph,
                    d.pemanen, d.kerani_panen, d.jjg_kerani_panen,
                    d.mandor, d.latitude, d.longitude
                FROM gold.reused_photo_detail d
                JOIN wanted w ON w.file_uid=d.file_uid
                WHERE upper(d.source_group)=ANY(%s)
                  AND nullif(btrim(coalesce(d.estate, '')), '') IS NOT NULL
                ORDER BY d.file_uid, d.published_at DESC, d.is_active DESC
            )
            UPDATE processing.image_context c
            SET company_code=coalesce(nullif(btrim(c.company_code), ''), d.source_company),
                oph=coalesce(nullif(btrim(c.oph), ''), d.oph),
                oph_key=coalesce(
                    nullif(btrim(c.oph_key), ''),
                    processing.normalize_oph(d.oph)
                ),
                period_date=coalesce(c.period_date, d.tanggal),
                estate=d.estate,
                afdeling=coalesce(nullif(btrim(c.afdeling), ''), d.afdeling),
                blok=coalesce(nullif(btrim(c.blok), ''), d.blok),
                tph=coalesce(nullif(btrim(c.tph), ''), d.tph),
                pemanen=coalesce(nullif(btrim(c.pemanen), ''), d.pemanen),
                kerani_panen=coalesce(nullif(btrim(c.kerani_panen), ''), d.kerani_panen),
                jjg_kerani_panen=coalesce(c.jjg_kerani_panen, d.jjg_kerani_panen),
                mandor=coalesce(nullif(btrim(c.mandor), ''), d.mandor),
                latitude=coalesce(c.latitude, d.latitude),
                longitude=coalesce(c.longitude, d.longitude),
                source_table='gold.reused_photo_detail',
                match_method='MATCH_BY_HISTORICAL_GOLD_FILE_UID',
                updated_at=now()
            FROM donor d
            WHERE c.file_uid=d.file_uid
              AND (
                  nullif(btrim(coalesce(c.estate, '')), '') IS NULL
                  OR nullif(btrim(coalesce(c.kerani_panen, '')), '') IS NULL
                  OR nullif(btrim(coalesce(c.mandor, '')), '') IS NULL
              )
            """,
            (source_group, aliases),
        )
        return max(0, int(cur.rowcount))


def repair_legacy_oph_context(
    settings: Settings,
    source_group: str,
    run_id: uuid.UUID,
    *,
    recovery_months: tuple[str, ...] | list[str],
    allow_actor_date_allocation: bool = False,
) -> dict[str, Any]:
    """Pulihkan OPH legacy tanpa membuang bulan bisnis yang valid.

    Fallback lama membuktikan actor + tanggal + satu estate, tetapi kemudian
    memilih satu OPH arbitrer untuk seluruh bucket. Recovery dilakukan tiga
    tingkat:

    1. exact basename bukti gambar ke FFB_EVIDENCE_KP/OUTPUT_FILE RAW;
    2. exact generated_at + actor untuk dua pola filename ELHP;
    3. alokasi satu-ke-satu opsional atas OPH RAW yang tersisa dalam bucket
       company + estate + actor + period_date yang sama.

    Tingkat ketiga dinonaktifkan secara default karena kapasitas OPH tidak
    membuktikan OPH mana milik sebuah foto. Jika suatu workflow bisnis secara
    eksplisit mengizinkannya, setiap OPH RAW tetap hanya dapat dialokasikan
    sekali dalam satu run.
    """
    source_group = source_group.upper().strip()
    raw_table = qname(settings.raw_business_schema, settings.raw_business_table)
    empty_metrics: dict[str, Any] = {
        "legacy_oph_before": 0,
        "legacy_oph_exact_evidence": 0,
        "legacy_oph_timestamp_exact": 0,
        "legacy_oph_actor_date_allocated": 0,
        "legacy_oph_unresolved": 0,
        "legacy_oph_recovery_months": [],
        "legacy_oph_recovered_by_month": {},
        "legacy_oph_unresolved_by_month": {},
    }
    target_months = sorted(
        {
            str(value).strip()
            for value in recovery_months
            if str(value).strip()
        }
    )
    if not target_months:
        print(
            "[OPH RECOVERY] tidak ada bulan dengan OPH legacy unsafe; "
            "tidak ada yang diperiksa ulang",
            flush=True,
        )
        return empty_metrics

    with connection(settings) as conn:
        if not table_exists(
            conn, settings.raw_business_schema, settings.raw_business_table
        ):
            print(
                f"[OPH RECOVERY] warning: {raw_table} tidak ada; "
                "legacy Context tidak diubah",
                flush=True,
            )
            return empty_metrics

        columns = table_columns(
            conn, settings.raw_business_schema, settings.raw_business_table
        )
        column_map = {column.lower(): column for column in columns}
        resolved, values = _raw_values(columns)
        generated_at_col = column_map.get("generated_at")
        actor_columns = list(
            dict.fromkeys(
                column
                for column in (
                    column_map.get("generated_by"),
                    resolved.get("nik_pemanen"),
                    resolved.get("nik_kerani"),
                    resolved.get("nik_mandor"),
                )
                if column
            )
        )
        raw_has_period = bool(
            resolved.get("period_date")
            or all(
                name in column_map
                for name in ("period_year", "period_month", "period_day")
            )
        )
        if (
            not raw_has_period
            or not resolved.get("oph")
            or not resolved.get("estate")
        ):
            print(
                "[OPH RECOVERY] warning: kolom date/estate/OPH RAW "
                "tidak lengkap; legacy Context tidak diubah",
                flush=True,
            )
            return empty_metrics

        raw_actor_values = [
            "NULLIF(regexp_replace(regexp_replace(upper(btrim("
            f"p.{ident(column)}::text)), '[^A-Z0-9]', '', 'g'), '^0+', ''), '')"
            for column in actor_columns
        ]
        raw_actor_array_expr = (
            "array_remove(ARRAY["
            + ", ".join(raw_actor_values)
            + "]::text[], NULL)"
        )
        filename_actor_expr = """
            NULLIF(
                regexp_replace(
                    regexp_replace(
                        upper(
                            CASE
                                WHEN split_part(m.file_name, '_', 1)
                                     ~ '^[0-9]{14}$'
                                THEN split_part(m.file_name, '_', 2)
                                ELSE split_part(m.file_name, '_', 1)
                            END
                        ),
                        '[^A-Z0-9]', '', 'g'
                    ),
                    '^0+', ''
                ),
                ''
            )
        """
        # ELHP baru: YYYYMMDDHHMMSS_ACTOR_...
        # ELHP historis: ACTOR_YYYYMMDD_HHMMSS_...
        filename_timestamp_expr = """
            CASE
                WHEN split_part(m.file_name, '_', 1) ~ '^[0-9]{14}$'
                THEN split_part(m.file_name, '_', 1)
                WHEN split_part(m.file_name, '_', 2) ~ '^20[0-9]{6}$'
                 AND split_part(m.file_name, '_', 3) ~ '^[0-9]{6}'
                THEN split_part(m.file_name, '_', 2)
                     || left(split_part(m.file_name, '_', 3), 6)
                ELSE NULL::text
            END
        """
        source_pk_expr = (
            values["source_pk"] if resolved.get("source_pk") else "NULL::text"
        )
        account_priority = "2"
        if resolved.get("account_status"):
            account_priority = (
                f"CASE upper(btrim(p.{ident(resolved['account_status'])}::text)) "
                "WHEN 'ACTUAL APPROVE' THEN 0 "
                "WHEN 'ACTUAL' THEN 1 "
                "ELSE 2 END"
            )

        company_columns = {
            "company_code",
            "company_code_sap",
            "company",
            "bukrs",
        } & set(column_map)
        company_filter = ""
        company_params: list[Any] = []
        if company_columns:
            company_filter = (
                f"AND upper(btrim({values['company_code']}))=upper(%s)"
            )
            company_params.append(settings.source_company(source_group))

        account_filter = ""
        account_params: list[Any] = []
        status_values = settings.account_status_values_for(source_group)
        if resolved.get("account_status") and status_values:
            account_filter = (
                f"AND upper(btrim(p.{ident(resolved['account_status'])}::text)) "
                "=ANY(%s)"
            )
            account_params.append(list(status_values))

        (
            evidence_columns,
            evidence_condition,
            evidence_priority,
            evidence_method,
        ) = _evidence_match_sql(columns, "u.file_name", alias="p")

        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TEMP TABLE tmp_legacy_oph_repair
                ON COMMIT DROP
                AS
                SELECT DISTINCT m.file_uid, c.period_date
                FROM processing.validated_group_member m
                JOIN processing.image_context c ON c.file_uid=m.file_uid
                WHERE m.run_id=%s
                  AND upper(coalesce(c.match_method, ''))=ANY(%s)
                  AND to_char(c.period_date, 'YYYY-MM')=ANY(%s)
                """,
                (
                    run_id,
                    list(_LEGACY_UNSAFE_OPH_METHODS),
                    target_months,
                ),
            )
            cur.execute(
                "CREATE INDEX ON tmp_legacy_oph_repair (file_uid)"
            )
            cur.execute(
                "SELECT count(*)::bigint FROM tmp_legacy_oph_repair"
            )
            legacy_before = int(cur.fetchone()[0])

        if legacy_before < 1:
            return {
                **empty_metrics,
                "legacy_oph_before": 0,
            }

        # Bukti terkuat untuk Harmony/DIN adalah nama gambar yang sama persis
        # dengan FFB_EVIDENCE_KP/OUTPUT_FILE di RAW (snapshot DB45). Versi
        # sebelumnya langsung JOIN 1.000+ unsafe usage ke tabel RAW jutaan row
        # memakai OR beberapa expression basename. Pada PostgreSQL tertentu
        # planner memilih nested-loop berbiaya sangat besar sehingga proses
        # tampak macet pada "exact filename evidence RAW/DB45".
        #
        # Recovery v157 men-stage RAW SATU KALI untuk company+bulan recovery,
        # meng-unpivot seluruh evidence basename ke temp table terindeks, lalu
        # melakukan join kecil target -> stage. Semantik proof tetap sama:
        # terima hanya bila semua raw match menyepakati satu OPH, satu estate,
        # dan tanggal bisnis yang sama dengan Context.
        exact_evidence_count = 0
        if evidence_columns:
            evidence_values: list[str] = []
            for priority, column in enumerate(evidence_columns):
                canonical = column.upper()
                if canonical == "FFB_EVIDENCE_KP":
                    method = "MATCH_BY_FFB_EVIDENCE_KP"
                elif canonical == "OUTPUT_FILE":
                    method = "MATCH_BY_OUTPUT_FILE"
                else:
                    method = "MATCH_BY_ALTERNATE_EVIDENCE"
                evidence_values.append(
                    "(processing.basename_upper(btrim(p."
                    + ident(column)
                    + "::text)), "
                    + str(priority)
                    + ", '"
                    + method
                    + "'::text)"
                )
            evidence_values_sql = ",\n                    ".join(evidence_values)

            raw_evidence_stage_sql = f"""
                CREATE TEMP TABLE tmp_legacy_raw_evidence
                ON COMMIT DROP
                AS
                SELECT
                    ev.evidence_key,
                    ev.evidence_priority,
                    ev.match_method,
                    {values['company_code']} AS company_code,
                    {values['oph']} AS oph,
                    processing.normalize_oph({values['oph']}) AS oph_key,
                    {values['period_date']} AS period_date,
                    {values['estate']} AS estate,
                    {values['afdeling']} AS afdeling,
                    {values['blok']} AS blok,
                    {values['tph']} AS tph,
                    {values['pemanen']} AS pemanen,
                    {values['kerani_panen']} AS kerani_panen,
                    {values['jjg_kerani_panen']} AS jjg_kerani_panen,
                    {values['mandor']} AS mandor,
                    {values['nik_pemanen']} AS nik_pemanen,
                    {values['nik_kerani']} AS nik_kerani,
                    {values['nik_mandor']} AS nik_mandor,
                    {values['npb']} AS npb,
                    {values['latitude']} AS latitude,
                    {values['longitude']} AS longitude,
                    {values['brd']} AS brd,
                    {values['bs']} AS bs,
                    {values['bm']} AS bm,
                    {values['bk']} AS bk,
                    {values['tp']} AS tp,
                    {source_pk_expr} AS source_pk,
                    {account_priority}::integer AS account_priority
                FROM {raw_table} p
                CROSS JOIN LATERAL (
                    VALUES
                    {evidence_values_sql}
                ) AS ev(evidence_key, evidence_priority, match_method)
                WHERE ev.evidence_key IS NOT NULL
                  AND to_char({values['period_date']}, 'YYYY-MM')=ANY(%s)
                  AND nullif(
                        processing.normalize_oph({values['oph']}), ''
                      ) IS NOT NULL
                  AND nullif(btrim(coalesce({values['estate']}, '')), '')
                      IS NOT NULL
                  {company_filter}
                  {account_filter}
            """
            print(
                "[OPH RECOVERY] stage exact-evidence RAW terfilter "
                f"company={source_group} months={target_months} ...",
                flush=True,
            )
            evidence_stage_started = perf_counter()
            with conn.cursor() as cur:
                cur.execute(
                    raw_evidence_stage_sql,
                    [
                        target_months,
                        *company_params,
                        *account_params,
                    ],
                )
                raw_evidence_rows = max(0, int(cur.rowcount))
                print(
                    "[OPH RECOVERY] stage exact-evidence selesai "
                    f"rows={raw_evidence_rows:,}; membuat temp index ...",
                    flush=True,
                )
                cur.execute(
                    """
                    CREATE INDEX tmp_legacy_raw_evidence_key_idx
                    ON tmp_legacy_raw_evidence (period_date, evidence_key)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX tmp_legacy_raw_evidence_oph_idx
                    ON tmp_legacy_raw_evidence (oph_key)
                    """
                )
                cur.execute("ANALYZE tmp_legacy_raw_evidence")
            print(
                "[OPH RECOVERY] exact-evidence stage siap "
                f"rows={raw_evidence_rows:,} elapsed="
                f"{perf_counter() - evidence_stage_started:,.1f}s",
                flush=True,
            )

            exact_evidence_sql = f"""
                WITH unsafe_usage AS MATERIALIZED (
                    SELECT DISTINCT
                        m.file_uid,
                        processing.basename_upper(m.file_name) AS evidence_key,
                        c.period_date
                    FROM processing.validated_group_member m
                    JOIN processing.image_context c ON c.file_uid=m.file_uid
                    JOIN tmp_legacy_oph_repair target
                      ON target.file_uid=m.file_uid
                    WHERE m.run_id=%s
                      AND upper(coalesce(c.match_method, ''))=ANY(%s)
                      AND c.period_date IS NOT NULL
                ), raw_matches AS MATERIALIZED (
                    SELECT
                        u.file_uid,
                        r.company_code,
                        r.oph,
                        r.oph_key,
                        r.period_date,
                        r.estate,
                        r.afdeling,
                        r.blok,
                        r.tph,
                        r.pemanen,
                        r.kerani_panen,
                        r.jjg_kerani_panen,
                        r.mandor,
                        r.nik_pemanen,
                        r.nik_kerani,
                        r.nik_mandor,
                        r.npb,
                        r.latitude,
                        r.longitude,
                        r.brd,
                        r.bs,
                        r.bm,
                        r.bk,
                        r.tp,
                        r.source_pk,
                        r.match_method,
                        r.evidence_priority,
                        r.account_priority
                    FROM unsafe_usage u
                    JOIN tmp_legacy_raw_evidence r
                      ON r.period_date=u.period_date
                     AND r.evidence_key=u.evidence_key
                    WHERE u.evidence_key IS NOT NULL
                ), safe AS (
                    SELECT file_uid
                    FROM raw_matches
                    GROUP BY file_uid
                    HAVING count(DISTINCT oph_key)=1
                       AND count(DISTINCT upper(btrim(estate)))=1
                       AND count(DISTINCT period_date)=1
                ), selected AS MATERIALIZED (
                    SELECT DISTINCT ON (r.file_uid)
                        r.*
                    FROM raw_matches r
                    JOIN safe s ON s.file_uid=r.file_uid
                    ORDER BY r.file_uid, r.evidence_priority,
                             r.account_priority,
                             r.source_pk DESC NULLS LAST
                )
                UPDATE processing.image_context c
                SET company_code=s.company_code,
                    oph=s.oph,
                    oph_key=s.oph_key,
                    period_date=s.period_date,
                    estate=s.estate,
                    afdeling=s.afdeling,
                    blok=s.blok,
                    tph=s.tph,
                    pemanen=s.pemanen,
                    kerani_panen=s.kerani_panen,
                    jjg_kerani_panen=s.jjg_kerani_panen,
                    mandor=s.mandor,
                    nik_pemanen=s.nik_pemanen,
                    nik_kerani=s.nik_kerani,
                    nik_mandor=s.nik_mandor,
                    npb=s.npb,
                    latitude=s.latitude,
                    longitude=s.longitude,
                    brd=s.brd,
                    bs=s.bs,
                    bm=s.bm,
                    bk=s.bk,
                    tp=s.tp,
                    source_table=%s,
                    source_pk=s.source_pk,
                    match_method=s.match_method,
                    updated_at=now()
                FROM selected s
                WHERE c.file_uid=s.file_uid
                  AND upper(coalesce(c.match_method, ''))=ANY(%s)
            """
            print(
                "[OPH RECOVERY] exact filename evidence RAW/DB45 "
                "(join temp terindeks) ...",
                flush=True,
            )
            evidence_started = perf_counter()
            with conn.cursor() as cur:
                cur.execute(
                    exact_evidence_sql,
                    [
                        run_id,
                        list(_LEGACY_UNSAFE_OPH_METHODS),
                        f"{settings.raw_business_schema}."
                        f"{settings.raw_business_table}",
                        list(_LEGACY_UNSAFE_OPH_METHODS),
                    ],
                )
                exact_evidence_count = max(0, int(cur.rowcount))
            print(
                "[OPH RECOVERY] exact filename evidence selesai "
                f"upserted={exact_evidence_count:,} elapsed="
                f"{perf_counter() - evidence_started:,.1f}s",
                flush=True,
            )

        raw_timestamp_scope_expr = "NULL::text"
        if generated_at_col:
            raw_timestamp_scope_expr = (
                f"to_char(p.{ident(generated_at_col)}::timestamp, "
                "'YYYYMMDDHH24MISS')"
            )
        raw_scope_sql = f"""
            CREATE TEMP TABLE tmp_legacy_raw_scope
            ON COMMIT DROP
            AS
            SELECT
                actor.actor_key,
                actor.actor_priority,
                {raw_timestamp_scope_expr} AS timestamp_key,
                {values['period_date']} AS period_date,
                upper(btrim({values['estate']})) AS estate_key,
                {values['company_code']} AS company_code,
                {values['oph']} AS oph,
                processing.normalize_oph({values['oph']}) AS oph_key,
                {values['estate']} AS estate,
                {values['afdeling']} AS afdeling,
                {values['blok']} AS blok,
                {values['tph']} AS tph,
                {values['pemanen']} AS pemanen,
                {values['kerani_panen']} AS kerani_panen,
                {values['jjg_kerani_panen']} AS jjg_kerani_panen,
                {values['mandor']} AS mandor,
                {values['nik_pemanen']} AS nik_pemanen,
                {values['nik_kerani']} AS nik_kerani,
                {values['nik_mandor']} AS nik_mandor,
                {values['npb']} AS npb,
                {values['latitude']} AS latitude,
                {values['longitude']} AS longitude,
                {values['brd']} AS brd,
                {values['bs']} AS bs,
                {values['bm']} AS bm,
                {values['bk']} AS bk,
                {values['tp']} AS tp,
                {source_pk_expr} AS source_pk,
                {account_priority} AS account_priority
            FROM {raw_table} p
            CROSS JOIN LATERAL unnest({raw_actor_array_expr})
                WITH ORDINALITY AS actor(actor_key, actor_priority)
            WHERE nullif(
                    processing.normalize_oph({values['oph']}), ''
                  ) IS NOT NULL
              AND nullif(btrim(coalesce({values['estate']}, '')), '')
                  IS NOT NULL
              AND to_char({values['period_date']}, 'YYYY-MM')=ANY(%s)
              {company_filter}
              {account_filter}
        """
        print(
            "[OPH RECOVERY] stage RAW terfilter untuk "
            f"{source_group} months={target_months} ...",
            flush=True,
        )
        stage_started = perf_counter()
        with conn.cursor() as cur:
            cur.execute(
                raw_scope_sql,
                [
                    target_months,
                    *company_params,
                    *account_params,
                ],
            )
            raw_scope_rows = max(0, int(cur.rowcount))
            print(
                "[OPH RECOVERY] RAW terfilter selesai "
                f"rows={raw_scope_rows:,}; membuat index sementara ...",
                flush=True,
            )
            cur.execute(
                """
                CREATE INDEX tmp_legacy_raw_scope_timestamp_idx
                ON tmp_legacy_raw_scope
                    (timestamp_key, actor_key, period_date, estate_key)
                """
            )
            cur.execute(
                """
                CREATE INDEX tmp_legacy_raw_scope_bucket_idx
                ON tmp_legacy_raw_scope
                    (actor_key, period_date, estate_key, oph_key)
                """
            )
            cur.execute("ANALYZE tmp_legacy_raw_scope")
        print(
            "[OPH RECOVERY] RAW stage siap "
            f"rows={raw_scope_rows:,} elapsed="
            f"{perf_counter() - stage_started:,.1f}s",
            flush=True,
        )

        exact_count = 0
        if generated_at_col:
            exact_sql = f"""
                WITH unsafe_usage AS MATERIALIZED (
                    SELECT DISTINCT
                        m.file_uid,
                        m.file_name,
                        c.period_date,
                        c.estate,
                        {filename_actor_expr} AS actor_key,
                        {filename_timestamp_expr} AS timestamp_key
                    FROM processing.validated_group_member m
                    JOIN processing.image_context c ON c.file_uid=m.file_uid
                    JOIN tmp_legacy_oph_repair target
                      ON target.file_uid=m.file_uid
                    WHERE m.run_id=%s
                      AND upper(coalesce(c.match_method, ''))=ANY(%s)
                      AND c.period_date IS NOT NULL
                      AND nullif(btrim(coalesce(c.estate, '')), '') IS NOT NULL
                ), raw_matches AS MATERIALIZED (
                    SELECT
                        u.file_uid,
                        p.company_code,
                        p.oph,
                        p.oph_key,
                        p.period_date,
                        p.estate,
                        p.afdeling,
                        p.blok,
                        p.tph,
                        p.pemanen,
                        p.kerani_panen,
                        p.jjg_kerani_panen,
                        p.mandor,
                        p.nik_pemanen,
                        p.nik_kerani,
                        p.nik_mandor,
                        p.npb,
                        p.latitude,
                        p.longitude,
                        p.brd,
                        p.bs,
                        p.bm,
                        p.bk,
                        p.tp,
                        p.source_pk,
                        p.account_priority
                    FROM unsafe_usage u
                    JOIN tmp_legacy_raw_scope p
                      ON p.timestamp_key=u.timestamp_key
                     AND p.actor_key=u.actor_key
                     AND p.period_date=u.period_date
                     AND p.estate_key=upper(btrim(u.estate))
                    WHERE u.timestamp_key IS NOT NULL
                ), safe AS (
                    SELECT file_uid
                    FROM raw_matches
                    GROUP BY file_uid
                    HAVING count(DISTINCT oph_key)=1
                       AND count(DISTINCT upper(btrim(estate)))=1
                ), selected AS MATERIALIZED (
                    SELECT DISTINCT ON (r.file_uid)
                        r.*
                    FROM raw_matches r
                    JOIN safe s ON s.file_uid=r.file_uid
                    ORDER BY r.file_uid, r.account_priority,
                             r.source_pk DESC NULLS LAST
                )
                UPDATE processing.image_context c
                SET company_code=s.company_code,
                    oph=s.oph,
                    oph_key=s.oph_key,
                    period_date=s.period_date,
                    estate=s.estate,
                    afdeling=s.afdeling,
                    blok=s.blok,
                    tph=s.tph,
                    pemanen=s.pemanen,
                    kerani_panen=s.kerani_panen,
                    jjg_kerani_panen=s.jjg_kerani_panen,
                    mandor=s.mandor,
                    nik_pemanen=s.nik_pemanen,
                    nik_kerani=s.nik_kerani,
                    nik_mandor=s.nik_mandor,
                    npb=s.npb,
                    latitude=s.latitude,
                    longitude=s.longitude,
                    brd=s.brd,
                    bs=s.bs,
                    bm=s.bm,
                    bk=s.bk,
                    tp=s.tp,
                    source_table=%s,
                    source_pk=s.source_pk,
                    match_method='MATCH_BY_RAW_TIMESTAMP_ACTOR_UNIQUE_OPH',
                    updated_at=now()
                FROM selected s
                WHERE c.file_uid=s.file_uid
                  AND upper(coalesce(c.match_method, ''))=ANY(%s)
            """
            exact_params: list[Any] = [
                run_id,
                list(_LEGACY_UNSAFE_OPH_METHODS),
                f"{settings.raw_business_schema}.{settings.raw_business_table}",
                list(_LEGACY_UNSAFE_OPH_METHODS),
            ]
            print(
                "[OPH RECOVERY] exact timestamp+actor ...",
                flush=True,
            )
            exact_started = perf_counter()
            with conn.cursor() as cur:
                cur.execute(exact_sql, exact_params)
                exact_count = max(0, int(cur.rowcount))
            print(
                "[OPH RECOVERY] exact timestamp selesai "
                f"upserted={exact_count:,} elapsed="
                f"{perf_counter() - exact_started:,.1f}s",
                flush=True,
            )

        allocation_sql = f"""
            WITH unsafe_usage AS MATERIALIZED (
                SELECT DISTINCT
                    m.validated_group_id,
                    m.file_uid,
                    m.file_name,
                    m.is_representative,
                    m.member_mean_similarity,
                    c.period_date,
                    upper(btrim(c.estate)) AS estate_key,
                    {filename_actor_expr} AS actor_key
                FROM processing.validated_group_member m
                JOIN processing.image_context c ON c.file_uid=m.file_uid
                JOIN tmp_legacy_oph_repair target
                  ON target.file_uid=m.file_uid
                WHERE m.run_id=%s
                  AND upper(coalesce(c.match_method, ''))=ANY(%s)
                  AND c.period_date IS NOT NULL
                  AND nullif(btrim(coalesce(c.estate, '')), '') IS NOT NULL
            ), requested_keys AS MATERIALIZED (
                SELECT DISTINCT actor_key, period_date, estate_key
                FROM unsafe_usage
                WHERE actor_key IS NOT NULL
            ), claimed_oph AS MATERIALIZED (
                SELECT DISTINCT processing.normalize_oph(c.oph) AS oph_key
                FROM processing.validated_group_member m
                JOIN processing.image_context c ON c.file_uid=m.file_uid
                WHERE m.run_id=%s
                  AND upper(coalesce(c.match_method, ''))<>ALL(%s)
                  AND nullif(processing.normalize_oph(c.oph), '') IS NOT NULL
            ), raw_candidates AS MATERIALIZED (
                SELECT
                    p.*
                FROM tmp_legacy_raw_scope p
                JOIN requested_keys requested
                  ON requested.actor_key=p.actor_key
                 AND requested.period_date=p.period_date
                 AND requested.estate_key=p.estate_key
            ), raw_available AS MATERIALIZED (
                SELECT DISTINCT ON (r.oph_key)
                    r.*
                FROM raw_candidates r
                LEFT JOIN claimed_oph claimed ON claimed.oph_key=r.oph_key
                WHERE claimed.oph_key IS NULL
                ORDER BY r.oph_key, r.actor_priority, r.account_priority,
                         r.source_pk DESC NULLS LAST
            ), ranked_usage AS MATERIALIZED (
                SELECT
                    u.*,
                    row_number() OVER (
                        PARTITION BY u.actor_key, u.period_date, u.estate_key
                        ORDER BY
                            g.min_member_mean_similarity DESC NULLS LAST,
                            g.mean_embedding_similarity DESC NULLS LAST,
                            g.mean_pair_score DESC NULLS LAST,
                            u.validated_group_id,
                            u.is_representative DESC,
                            u.member_mean_similarity DESC NULLS LAST,
                            u.file_uid
                    ) AS allocation_rank
                FROM unsafe_usage u
                JOIN processing.validated_group g
                  ON g.run_id=%s
                 AND g.validated_group_id=u.validated_group_id
                WHERE u.actor_key IS NOT NULL
            ), ranked_raw AS MATERIALIZED (
                SELECT
                    r.*,
                    row_number() OVER (
                        PARTITION BY r.actor_key, r.period_date, r.estate_key
                        ORDER BY r.oph_key
                    ) AS allocation_rank
                FROM raw_available r
            ), matched AS MATERIALIZED (
                SELECT
                    u.file_uid,
                    r.company_code,
                    r.oph,
                    r.oph_key,
                    r.period_date,
                    r.estate,
                    r.afdeling,
                    r.blok,
                    r.tph,
                    r.pemanen,
                    r.kerani_panen,
                    r.jjg_kerani_panen,
                    r.mandor,
                    r.nik_pemanen,
                    r.nik_kerani,
                    r.nik_mandor,
                    r.npb,
                    r.latitude,
                    r.longitude,
                    r.brd,
                    r.bs,
                    r.bm,
                    r.bk,
                    r.tp,
                    r.source_pk
                FROM ranked_usage u
                JOIN ranked_raw r
                  ON r.actor_key=u.actor_key
                 AND r.period_date=u.period_date
                 AND r.estate_key=u.estate_key
                 AND r.allocation_rank=u.allocation_rank
            )
            UPDATE processing.image_context c
            SET company_code=m.company_code,
                oph=m.oph,
                oph_key=m.oph_key,
                period_date=m.period_date,
                estate=m.estate,
                afdeling=m.afdeling,
                blok=m.blok,
                tph=m.tph,
                pemanen=m.pemanen,
                kerani_panen=m.kerani_panen,
                jjg_kerani_panen=m.jjg_kerani_panen,
                mandor=m.mandor,
                nik_pemanen=m.nik_pemanen,
                nik_kerani=m.nik_kerani,
                nik_mandor=m.nik_mandor,
                npb=m.npb,
                latitude=m.latitude,
                longitude=m.longitude,
                brd=m.brd,
                bs=m.bs,
                bm=m.bm,
                bk=m.bk,
                tp=m.tp,
                source_table=%s,
                source_pk=m.source_pk,
                match_method='MATCH_BY_RAW_ACTOR_DATE_ESTATE_ONE_TO_ONE',
                updated_at=now()
            FROM matched m
            WHERE c.file_uid=m.file_uid
              AND upper(coalesce(c.match_method, ''))=ANY(%s)
        """
        allocation_params: list[Any] = [
            run_id,
            list(_LEGACY_UNSAFE_OPH_METHODS),
            run_id,
            list(_LEGACY_UNSAFE_OPH_METHODS),
            run_id,
            f"{settings.raw_business_schema}.{settings.raw_business_table}",
            list(_LEGACY_UNSAFE_OPH_METHODS),
        ]
        allocated_count = 0
        with conn.cursor() as cur:
            if allow_actor_date_allocation:
                print(
                    "[OPH RECOVERY] alokasi actor+date+estate one-to-one ...",
                    flush=True,
                )
                allocation_started = perf_counter()
                cur.execute(allocation_sql, allocation_params)
                allocated_count = max(0, int(cur.rowcount))
                print(
                    "[OPH RECOVERY] alokasi one-to-one selesai "
                    f"upserted={allocated_count:,} elapsed="
                    f"{perf_counter() - allocation_started:,.1f}s",
                    flush=True,
                )
            else:
                print(
                    "[OPH RECOVERY] actor+date+estate capacity tidak dipakai "
                    "karena bukan exact transaction proof",
                    flush=True,
                )
            cur.execute(
                """
                SELECT
                    coalesce(sum(n), 0)::bigint AS unresolved,
                    coalesce(
                        jsonb_object_agg(month, n ORDER BY month)
                            FILTER (WHERE month IS NOT NULL),
                        '{}'::jsonb
                    ) AS by_month
                FROM (
                    SELECT
                        to_char(c.period_date, 'YYYY-MM') AS month,
                        count(*)::bigint AS n
                    FROM processing.validated_group_member m
                    JOIN processing.image_context c ON c.file_uid=m.file_uid
                    JOIN tmp_legacy_oph_repair target
                      ON target.file_uid=m.file_uid
                    WHERE m.run_id=%s
                      AND upper(coalesce(c.match_method, ''))=ANY(%s)
                    GROUP BY to_char(c.period_date, 'YYYY-MM')
                ) q
                """,
                (run_id, list(_LEGACY_UNSAFE_OPH_METHODS)),
            )
            unresolved_row = cur.fetchone()
            unresolved = int(unresolved_row[0] or 0)
            unresolved_by_month = dict(unresolved_row[1] or {})
            cur.execute(
                """
                SELECT coalesce(
                    jsonb_object_agg(month, n ORDER BY month),
                    '{}'::jsonb
                )
                FROM (
                    SELECT
                        to_char(c.period_date, 'YYYY-MM') AS month,
                        count(*)::bigint AS n
                    FROM processing.validated_group_member m
                    JOIN processing.image_context c ON c.file_uid=m.file_uid
                    JOIN tmp_legacy_oph_repair legacy
                      ON legacy.file_uid=m.file_uid
                    WHERE m.run_id=%s
                      AND upper(coalesce(c.match_method, '')) IN (
                          'MATCH_BY_FFB_EVIDENCE_KP',
                          'MATCH_BY_OUTPUT_FILE',
                          'MATCH_BY_ALTERNATE_EVIDENCE',
                          'MATCH_BY_RAW_TIMESTAMP_ACTOR_UNIQUE_OPH',
                          'MATCH_BY_RAW_ACTOR_DATE_ESTATE_ONE_TO_ONE'
                      )
                    GROUP BY to_char(c.period_date, 'YYYY-MM')
                ) q
                """,
                (run_id,),
            )
            recovered_by_month = dict(cur.fetchone()[0] or {})

    metrics = {
        "legacy_oph_before": legacy_before,
        "legacy_oph_exact_evidence": exact_evidence_count,
        "legacy_oph_timestamp_exact": exact_count,
        "legacy_oph_actor_date_allocated": allocated_count,
        "legacy_oph_unresolved": unresolved,
        "legacy_oph_recovery_months": target_months,
        "legacy_oph_recovered_by_month": recovered_by_month,
        "legacy_oph_unresolved_by_month": unresolved_by_month,
    }
    print(
        "[OPH RECOVERY] "
        f"months={target_months} "
        f"legacy_before={legacy_before:,} "
        f"exact_evidence={exact_evidence_count:,} "
        f"timestamp_exact={exact_count:,} "
        f"actor_date_one_to_one={allocated_count:,} "
        f"unresolved={unresolved:,}",
        flush=True,
    )
    print(
        "[OPH RECOVERY MONTH] "
        f"recovered={recovered_by_month} unresolved={unresolved_by_month}",
        flush=True,
    )
    return metrics


def sync_raw_usage_context(
    settings: Settings,
    source_group: str,
    *,
    run_id: uuid.UUID | None = None,
    all_missing: bool = False,
) -> dict[str, int]:
    """Isi Context kosong dari raw dengan exact/unique-estate matching.

    Pencocokan exact filename tidak bergantung pada company_code raw karena
    kode organisasi pada ELHP dan source_group gambar tidak selalu identik.
    Keamanannya dijaga dengan menerima filename hanya bila seluruh row raw yang
    cocok menyepakati tepat satu estate. Tanggal Context yang sudah berasal
    dari filename tidak pernah ditimpa oleh tanggal transaksi raw.
    """
    source_group = source_group.upper()
    raw_table = qname(settings.raw_business_schema, settings.raw_business_table)
    image_table = qname(settings.silver_schema, settings.image_table)

    with connection(settings) as conn:
        # Snapshot Gold historis adalah donor yang paling dekat dengan hasil
        # bisnis yang pernah dipublish. Jalankan sebelum memeriksa RAW agar
        # pemulihan tetap bekerja ketika tabel/kolom RAW sedang tidak tersedia.
        historical_gold_context_upserted = _sync_historical_gold_context(
            conn, settings, source_group
        )
        if historical_gold_context_upserted:
            print(
                "[HISTORICAL CONTEXT] restored="
                f"{historical_gold_context_upserted:,} dari snapshot Gold lama",
                flush=True,
            )

        if not table_exists(conn, settings.raw_business_schema, settings.raw_business_table):
            print(f"[RAW CONTEXT] warning: {raw_table} tidak ada; fallback dilewati")
            return {
                "raw_context_scope_all_missing": int(all_missing),
                "historical_gold_context_upserted": historical_gold_context_upserted,
                "raw_usage_candidates": 0,
                "raw_usage_context_upserted": 0,
                "raw_timestamp_actor_candidates": 0,
                "raw_timestamp_actor_upserted": 0,
                "raw_actor_date_candidates": 0,
                "raw_actor_date_unique_upserted": 0,
                "sap_oph_prfnr_candidates": 0,
                "sap_oph_prfnr_upserted": 0,
            }

        columns = table_columns(conn, settings.raw_business_schema, settings.raw_business_table)
        column_map = {column.lower(): column for column in columns}
        resolved, values = _raw_values(columns)
        actor_columns = list(
            dict.fromkeys(
                column
                for column in (
                    column_map.get("generated_by"),
                    resolved.get("nik_pemanen"),
                    resolved.get("nik_kerani"),
                    resolved.get("nik_mandor"),
                )
                if column
            )
        )
        raw_actor_values = [
            "NULLIF(regexp_replace(regexp_replace(upper(btrim("
            f"p.{ident(column)}::text)), '[^A-Z0-9]', '', 'g'), '^0+', ''), '')"
            for column in actor_columns
        ]
        raw_actor_array_expr = (
            "array_remove(ARRAY[" + ", ".join(raw_actor_values) + "]::text[], NULL)"
            if raw_actor_values
            else "ARRAY[]::text[]"
        )
        filename_actor_expr = """
            NULLIF(
                regexp_replace(
                    regexp_replace(
                        upper(
                            CASE
                                WHEN split_part(u.file_name, '_', 1) ~ '^[0-9]{14}$'
                                THEN split_part(u.file_name, '_', 2)
                                ELSE split_part(u.file_name, '_', 1)
                            END
                        ),
                        '[^A-Z0-9]', '', 'g'
                    ),
                    '^0+', ''
                ),
                ''
            )
        """
        (
            evidence_columns,
            evidence_condition,
            evidence_priority,
            evidence_method,
        ) = _evidence_match_sql(columns, "u.file_name")
        if not evidence_columns:
            print(
                f"[RAW CONTEXT] warning: tidak ada kolom evidence yang didukung di {raw_table}; "
                "fallback dilewati"
            )
            return {
                "raw_context_scope_all_missing": int(all_missing),
                "historical_gold_context_upserted": historical_gold_context_upserted,
                "raw_usage_candidates": 0,
                "raw_usage_context_upserted": 0,
                "raw_timestamp_actor_candidates": 0,
                "raw_timestamp_actor_upserted": 0,
                "raw_actor_date_candidates": 0,
                "raw_actor_date_unique_upserted": 0,
                "sap_oph_prfnr_candidates": 0,
                "sap_oph_prfnr_upserted": 0,
            }

        if all_missing:
            # V172: image_context is the recovery anchor. ELHP rows can already
            # have estate/date while personnel is still blank, and the image
            # inventory / active Gold population is not guaranteed to contain
            # the exact same source_group representation. Resolve file_name by
            # file_uid from inventory first and Gold as fallback.
            usage_sql = f"""
                SELECT DISTINCT
                    c.file_uid::text AS file_uid,
                    coalesce(
                        nullif(trim(i.file_name::text), ''),
                        nullif(trim(g.image_name::text), '')
                    ) AS file_name
                FROM processing.image_context c
                LEFT JOIN {image_table} i
                  ON i.file_uid::text=c.file_uid::text
                 AND coalesce(i.is_transport, false)=false
                 AND coalesce(i.zero_byte, false)=false
                LEFT JOIN LATERAL (
                    SELECT d.image_name
                    FROM gold.reused_photo_detail d
                    WHERE d.file_uid::text=c.file_uid::text
                      AND nullif(trim(coalesce(d.image_name, '')), '') IS NOT NULL
                    ORDER BY d.is_active DESC, d.published_at DESC NULLS LAST
                    LIMIT 1
                ) g ON true
                WHERE upper(c.source_group::text)=upper(%s)
                  AND (
                      nullif(trim(coalesce(c.estate, '')), '') IS NULL
                      OR nullif(trim(coalesce(c.kerani_panen, '')), '') IS NULL
                      OR nullif(trim(coalesce(c.mandor, '')), '') IS NULL
                  )
                  AND coalesce(
                        nullif(trim(i.file_name::text), ''),
                        nullif(trim(g.image_name::text), '')
                      ) IS NOT NULL
            """
            usage_param: Any = source_group
        elif run_id is not None:
            usage_sql = """
                SELECT DISTINCT m.file_uid, m.file_name
                FROM processing.validated_group_member m
                LEFT JOIN processing.image_context c ON c.file_uid=m.file_uid
                WHERE m.run_id=%s
                  AND (
                      nullif(trim(coalesce(c.estate, '')), '') IS NULL
                      OR nullif(trim(coalesce(c.kerani_panen, '')), '') IS NULL
                      OR nullif(trim(coalesce(c.mandor, '')), '') IS NULL
                  )
            """
            usage_param: Any = run_id
        else:
            usage_sql = """
                SELECT DISTINCT d.file_uid, d.image_name AS file_name
                FROM gold.reused_photo_detail d
                LEFT JOIN processing.image_context c ON c.file_uid=d.file_uid
                WHERE upper(d.source_group)=upper(%s)
                  AND d.is_active=true
                  AND (
                      nullif(trim(coalesce(c.estate, '')), '') IS NULL
                      OR nullif(trim(coalesce(c.kerani_panen, '')), '') IS NULL
                      OR nullif(trim(coalesce(c.mandor, '')), '') IS NULL
                  )
            """
            usage_param = source_group

        account_priority = "2"
        if resolved.get("account_status"):
            account_priority = (
                f"CASE upper(trim(p.{ident(resolved['account_status'])}::text)) "
                "WHEN 'ACTUAL APPROVE' THEN 0 "
                "WHEN 'ACTUAL' THEN 1 "
                "ELSE 2 END"
            )

        source_pk_expr = values["source_pk"] if resolved.get("source_pk") else "NULL::text"
        evidence_present = "(" + " OR ".join(
            f"nullif(btrim(p.{ident(column)}::text), '') IS NOT NULL"
            for column in evidence_columns
        ) + ")"
        sql = f"""
            WITH usage AS MATERIALIZED (
                {usage_sql}
            ), raw_matches AS MATERIALIZED (
                SELECT
                    u.file_uid,
                    u.file_name,
                    current_context.period_date AS current_period_date,
                    {values['company_code']} AS company_code,
                    {values['oph']} AS oph,
                    processing.normalize_oph({values['oph']}) AS oph_key,
                    {values['period_date']} AS period_date,
                    {values['estate']} AS estate,
                    {values['afdeling']} AS afdeling,
                    {values['blok']} AS blok,
                    {values['tph']} AS tph,
                    {values['pemanen']} AS pemanen,
                    {values['kerani_panen']} AS kerani_panen,
                    {values['jjg_kerani_panen']} AS jjg_kerani_panen,
                    {values['mandor']} AS mandor,
                    {values['nik_pemanen']} AS nik_pemanen,
                    {values['nik_kerani']} AS nik_kerani,
                    {values['nik_mandor']} AS nik_mandor,
                    {values['npb']} AS npb,
                    {values['latitude']} AS latitude,
                    {values['longitude']} AS longitude,
                    {values['brd']} AS brd,
                    {values['bs']} AS bs,
                    {values['bm']} AS bm,
                    {values['bk']} AS bk,
                    {values['tp']} AS tp,
                    {source_pk_expr} AS source_pk,
                    {evidence_method}::text AS match_method,
                    {evidence_priority}::integer AS evidence_priority,
                    {account_priority}::integer AS account_priority
                FROM usage u
                JOIN {raw_table} p
                  ON {evidence_condition}
                LEFT JOIN processing.image_context current_context
                  ON current_context.file_uid=u.file_uid
                WHERE nullif(trim(coalesce({values['estate']}, '')), '') IS NOT NULL
                  AND {evidence_present}
            ), safe_match AS (
                SELECT r.file_uid
                FROM raw_matches r
                GROUP BY r.file_uid
                HAVING count(DISTINCT upper(trim(r.estate)))=1
                   AND count(DISTINCT nullif(r.oph_key, ''))=1
            ), matched AS (
                SELECT DISTINCT ON (r.file_uid)
                    r.file_uid,
                    %s::text AS source_group,
                    r.company_code,
                    r.oph,
                    r.oph_key,
                    r.period_date,
                    r.estate,
                    r.afdeling,
                    r.blok,
                    r.tph,
                    r.pemanen,
                    r.kerani_panen,
                    r.jjg_kerani_panen,
                    r.mandor,
                    r.nik_pemanen,
                    r.nik_kerani,
                    r.nik_mandor,
                    r.npb,
                    r.latitude,
                    r.longitude,
                    r.brd,
                    r.bs,
                    r.bm,
                    r.bk,
                    r.tp,
                    %s::text AS source_table,
                    r.source_pk,
                    r.match_method
                FROM raw_matches r
                JOIN safe_match s ON s.file_uid=r.file_uid
                ORDER BY
                    r.file_uid,
                    r.evidence_priority,
                    CASE
                        WHEN r.period_date=r.current_period_date THEN 0
                        ELSE 1
                    END,
                    r.account_priority,
                    r.period_date DESC NULLS LAST,
                    r.source_pk DESC NULLS LAST
            )
            INSERT INTO processing.image_context (
                file_uid, source_group, company_code, oph, oph_key, period_date,
                estate, afdeling, blok, tph, pemanen, kerani_panen,
                jjg_kerani_panen, mandor, nik_pemanen, nik_kerani, nik_mandor,
                npb, latitude, longitude, brd, bs, bm, bk, tp,
                source_table, source_pk, match_method
            )
            SELECT
                file_uid, source_group, company_code, oph, oph_key, period_date,
                estate, afdeling, blok, tph, pemanen, kerani_panen,
                jjg_kerani_panen, mandor, nik_pemanen, nik_kerani, nik_mandor,
                npb, latitude, longitude, brd, bs, bm, bk, tp,
                source_table, source_pk, match_method
            FROM matched
            ON CONFLICT (file_uid) DO UPDATE SET
                source_group=excluded.source_group,
                company_code=coalesce(nullif(trim(processing.image_context.company_code), ''), excluded.company_code),
                oph=coalesce(nullif(trim(processing.image_context.oph), ''), excluded.oph),
                oph_key=coalesce(nullif(trim(processing.image_context.oph_key), ''), excluded.oph_key),
                period_date=coalesce(
                    processing.image_context.period_date,
                    excluded.period_date
                ),
                estate=coalesce(nullif(trim(processing.image_context.estate), ''), excluded.estate),
                afdeling=coalesce(nullif(trim(processing.image_context.afdeling), ''), excluded.afdeling),
                blok=coalesce(nullif(trim(processing.image_context.blok), ''), excluded.blok),
                tph=coalesce(nullif(trim(processing.image_context.tph), ''), excluded.tph),
                pemanen=coalesce(nullif(trim(processing.image_context.pemanen), ''), excluded.pemanen),
                kerani_panen=coalesce(nullif(trim(processing.image_context.kerani_panen), ''), excluded.kerani_panen),
                jjg_kerani_panen=coalesce(processing.image_context.jjg_kerani_panen, excluded.jjg_kerani_panen),
                mandor=coalesce(nullif(trim(processing.image_context.mandor), ''), excluded.mandor),
                nik_pemanen=coalesce(nullif(trim(processing.image_context.nik_pemanen), ''), excluded.nik_pemanen),
                nik_kerani=coalesce(nullif(trim(processing.image_context.nik_kerani), ''), excluded.nik_kerani),
                nik_mandor=coalesce(nullif(trim(processing.image_context.nik_mandor), ''), excluded.nik_mandor),
                npb=coalesce(nullif(trim(processing.image_context.npb), ''), excluded.npb),
                latitude=coalesce(processing.image_context.latitude, excluded.latitude),
                longitude=coalesce(processing.image_context.longitude, excluded.longitude),
                brd=coalesce(processing.image_context.brd, excluded.brd),
                bs=coalesce(processing.image_context.bs, excluded.bs),
                bm=coalesce(processing.image_context.bm, excluded.bm),
                bk=coalesce(processing.image_context.bk, excluded.bk),
                tp=coalesce(processing.image_context.tp, excluded.tp),
                source_table=excluded.source_table,
                source_pk=excluded.source_pk,
                match_method=excluded.match_method,
                updated_at=now()
        """
        params = [
            usage_param,
            source_group,
            f"{settings.raw_business_schema}.{settings.raw_business_table}",
        ]
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM (" + usage_sql + ") q", (usage_param,))
            raw_usage_candidates = int(cur.fetchone()[0])
            if raw_usage_candidates:
                print(
                    f"[RAW CONTEXT] candidates={raw_usage_candidates:,}; "
                    f"mulai lookup {raw_table} evidence={evidence_columns}; "
                    "strategy=INDEX_BASENAME_SINGLE_JOIN ...",
                    flush=True,
                )
                started = perf_counter()
                cur.execute(sql, params)
                raw_usage_context_upserted = max(0, int(cur.rowcount))
                print(
                    f"[RAW CONTEXT] lookup selesai; upserted={raw_usage_context_upserted:,}; "
                    f"elapsed={perf_counter() - started:,.1f}s",
                    flush=True,
                )
            else:
                raw_usage_context_upserted = 0

            # Format ELHP umumnya:
            #   YYYYMMDDHHMMSS_GENERATED_BY_....jpg
            # Token timestamp bukan generated_by. Cocokkan keduanya ke raw
            # agar OPH, estate, afdeling dan metadata transaksi dapat diangkat
            # tanpa menebak berdasarkan source_group.
            generated_at_col = column_map.get("generated_at")
            if generated_at_col and actor_columns:
                filename_timestamp_expr = """
                    CASE
                        WHEN split_part(u.file_name, '_', 1) ~ '^[0-9]{14}$'
                        THEN split_part(u.file_name, '_', 1)
                        WHEN split_part(u.file_name, '_', 2) ~ '^20[0-9]{6}$'
                         AND split_part(u.file_name, '_', 3) ~ '^[0-9]{6}'
                        THEN split_part(u.file_name, '_', 2)
                             || left(split_part(u.file_name, '_', 3), 6)
                        ELSE NULL::text
                    END
                """
                raw_timestamp_expr = (
                    f"to_char(p.{ident(generated_at_col)}::timestamp, "
                    "'YYYYMMDDHH24MISS')"
                )
                timestamp_sql = f"""
                    WITH usage AS (
                        {usage_sql}
                    ), safe_match AS (
                        SELECT u.file_uid
                        FROM usage u
                        JOIN {raw_table} p
                          ON {raw_timestamp_expr}={filename_timestamp_expr}
                         AND {filename_actor_expr}=ANY({raw_actor_array_expr})
                        WHERE {filename_timestamp_expr} IS NOT NULL
                          AND nullif(trim(coalesce({values['estate']}, '')), '') IS NOT NULL
                        GROUP BY u.file_uid
                        HAVING count(DISTINCT upper(trim({values['estate']})))=1
                           AND count(DISTINCT nullif(
                                   processing.normalize_oph({values['oph']}), ''
                               ))=1
                    ), matched AS (
                        SELECT DISTINCT ON (u.file_uid)
                            u.file_uid,
                            {values['company_code']} AS company_code,
                            {values['oph']} AS oph,
                            processing.normalize_oph({values['oph']}) AS oph_key,
                            {values['period_date']} AS period_date,
                            {values['estate']} AS estate,
                            {values['afdeling']} AS afdeling,
                            {values['blok']} AS blok,
                            {values['tph']} AS tph,
                            {values['pemanen']} AS pemanen,
                            {values['kerani_panen']} AS kerani_panen,
                            {values['jjg_kerani_panen']} AS jjg_kerani_panen,
                            {values['mandor']} AS mandor,
                            {values['nik_pemanen']} AS nik_pemanen,
                            {values['nik_kerani']} AS nik_kerani,
                            {values['nik_mandor']} AS nik_mandor,
                            {values['npb']} AS npb,
                            {values['latitude']} AS latitude,
                            {values['longitude']} AS longitude,
                            {values['brd']} AS brd,
                            {values['bs']} AS bs,
                            {values['bm']} AS bm,
                            {values['bk']} AS bk,
                            {values['tp']} AS tp,
                            %s::text AS source_table,
                            {source_pk_expr} AS source_pk
                        FROM usage u
                        JOIN safe_match sm ON sm.file_uid=u.file_uid
                        JOIN {raw_table} p
                          ON {raw_timestamp_expr}={filename_timestamp_expr}
                         AND {filename_actor_expr}=ANY({raw_actor_array_expr})
                        WHERE {filename_timestamp_expr} IS NOT NULL
                          AND nullif(trim(coalesce({values['estate']}, '')), '') IS NOT NULL
                        ORDER BY u.file_uid, {account_priority},
                                 {source_pk_expr} DESC NULLS LAST
                    )
                    UPDATE processing.image_context c
                    SET
                        company_code=coalesce(nullif(trim(c.company_code), ''), m.company_code),
                        oph=coalesce(nullif(trim(c.oph), ''), m.oph),
                        oph_key=coalesce(nullif(trim(c.oph_key), ''), m.oph_key),
                        period_date=coalesce(c.period_date, m.period_date),
                        estate=m.estate,
                        afdeling=coalesce(nullif(trim(c.afdeling), ''), m.afdeling),
                        blok=coalesce(nullif(trim(c.blok), ''), m.blok),
                        tph=coalesce(nullif(trim(c.tph), ''), m.tph),
                        pemanen=coalesce(nullif(trim(c.pemanen), ''), m.pemanen),
                        kerani_panen=coalesce(nullif(trim(c.kerani_panen), ''), m.kerani_panen),
                        jjg_kerani_panen=coalesce(c.jjg_kerani_panen, m.jjg_kerani_panen),
                        mandor=coalesce(nullif(trim(c.mandor), ''), m.mandor),
                        nik_pemanen=coalesce(nullif(trim(c.nik_pemanen), ''), m.nik_pemanen),
                        nik_kerani=coalesce(nullif(trim(c.nik_kerani), ''), m.nik_kerani),
                        nik_mandor=coalesce(nullif(trim(c.nik_mandor), ''), m.nik_mandor),
                        npb=coalesce(nullif(trim(c.npb), ''), m.npb),
                        latitude=coalesce(c.latitude, m.latitude),
                        longitude=coalesce(c.longitude, m.longitude),
                        brd=coalesce(c.brd, m.brd),
                        bs=coalesce(c.bs, m.bs),
                        bm=coalesce(c.bm, m.bm),
                        bk=coalesce(c.bk, m.bk),
                        tp=coalesce(c.tp, m.tp),
                        source_table=m.source_table,
                        source_pk=m.source_pk,
                        match_method='MATCH_BY_RAW_TIMESTAMP_ACTOR_UNIQUE_OPH',
                        updated_at=now()
                    FROM matched m
                    WHERE c.file_uid=m.file_uid
                      AND (
                          nullif(trim(coalesce(c.estate, '')), '') IS NULL
                          OR nullif(trim(coalesce(c.kerani_panen, '')), '') IS NULL
                          OR nullif(trim(coalesce(c.mandor, '')), '') IS NULL
                      )
                """
                cur.execute("SELECT count(*) FROM (" + usage_sql + ") q", (usage_param,))
                timestamp_actor_candidates = int(cur.fetchone()[0])
                if timestamp_actor_candidates:
                    print(
                        f"[RAW CONTEXT] candidates={timestamp_actor_candidates:,}; "
                        "mulai fallback timestamp+actor bisnis ...",
                        flush=True,
                    )
                    started = perf_counter()
                    cur.execute(
                        timestamp_sql,
                        (usage_param, f"{settings.raw_business_schema}.{settings.raw_business_table}"),
                    )
                    raw_timestamp_actor_upserted = max(0, int(cur.rowcount))
                    print(
                        "[RAW CONTEXT] timestamp+actor bisnis selesai; "
                        f"upserted={raw_timestamp_actor_upserted:,}; "
                        f"elapsed={perf_counter() - started:,.1f}s",
                        flush=True,
                    )
                else:
                    raw_timestamp_actor_upserted = 0
            else:
                timestamp_actor_candidates = 0
                raw_timestamp_actor_upserted = 0

            # Fallback kedua mengikuti pola file lama secara universal:
            # ACTOR_YYYYMMDD_* atau YYYYMMDDHHMMSS_ACTOR_*. ACTOR dicocokkan
            # ke generated_by/NIK pemanen/NIK kerani/NIK mandor. Ini adalah
            # perbaikan penting dari flow Excel lama yang tidak hanya bergantung
            # pada generated_by. Pasangan hanya diterima bila tanggal sama dan
            # seluruh kandidat menyepakati satu estate.
            raw_has_period = bool(
                resolved.get("period_date")
                or all(
                    name in column_map
                    for name in ("period_year", "period_month", "period_day")
                )
            )
            if actor_columns and raw_has_period:
                raw_period_date_expr = values["period_date"]
                unique_sql = f"""
                    WITH usage AS (
                        {usage_sql}
                    ), usage_keys AS MATERIALIZED (
                        SELECT DISTINCT
                            u.file_uid,
                            {filename_actor_expr} AS actor_key,
                            c.period_date
                        FROM usage u
                        JOIN processing.image_context c ON c.file_uid=u.file_uid
                        WHERE c.period_date IS NOT NULL
                          AND {filename_actor_expr} IS NOT NULL
                    ), requested_keys AS MATERIALIZED (
                        SELECT DISTINCT actor_key, period_date
                        FROM usage_keys
                    ), raw_rows AS MATERIALIZED (
                        SELECT
                            actor.actor_key,
                            {raw_period_date_expr} AS period_date,
                            {values['company_code']} AS company_code,
                            {values['oph']} AS oph,
                            processing.normalize_oph({values['oph']}) AS oph_key,
                            {values['estate']} AS estate,
                            {values['afdeling']} AS afdeling,
                            {values['blok']} AS blok,
                            {values['tph']} AS tph,
                            {values['pemanen']} AS pemanen,
                            {values['kerani_panen']} AS kerani_panen,
                            {values['jjg_kerani_panen']} AS jjg_kerani_panen,
                            {values['mandor']} AS mandor,
                            {values['nik_pemanen']} AS nik_pemanen,
                            {values['nik_kerani']} AS nik_kerani,
                            {values['nik_mandor']} AS nik_mandor,
                            {values['npb']} AS npb,
                            {values['latitude']} AS latitude,
                            {values['longitude']} AS longitude,
                            {values['brd']} AS brd,
                            {values['bs']} AS bs,
                            {values['bm']} AS bm,
                            {values['bk']} AS bk,
                            {values['tp']} AS tp,
                            {source_pk_expr} AS source_pk,
                            {account_priority} AS account_priority
                        FROM {raw_table} p
                        CROSS JOIN LATERAL unnest({raw_actor_array_expr}) AS actor(actor_key)
                        JOIN requested_keys requested
                          ON requested.actor_key=actor.actor_key
                         AND requested.period_date={raw_period_date_expr}
                        WHERE nullif(trim(coalesce({values['estate']}, '')), '') IS NOT NULL
                    ), unique_estate_oph AS (
                        SELECT actor_key, period_date
                        FROM raw_rows
                        GROUP BY actor_key, period_date
                        HAVING count(DISTINCT upper(btrim(estate)))=1
                           AND count(DISTINCT nullif(btrim(oph_key), ''))=1
                    ), raw_selected AS MATERIALIZED (
                        SELECT DISTINCT ON (r.actor_key, r.period_date)
                            r.*
                        FROM raw_rows r
                        JOIN unique_estate_oph u
                          ON u.actor_key=r.actor_key
                         AND u.period_date=r.period_date
                        ORDER BY r.actor_key, r.period_date,
                                 r.account_priority, r.source_pk DESC NULLS LAST
                    ), matched AS (
                        SELECT
                            u.file_uid,
                            r.company_code,
                            r.oph,
                            r.oph_key,
                            r.period_date,
                            r.estate,
                            r.afdeling,
                            r.blok,
                            r.tph,
                            r.pemanen,
                            r.kerani_panen,
                            r.jjg_kerani_panen,
                            r.mandor,
                            r.nik_pemanen,
                            r.nik_kerani,
                            r.nik_mandor,
                            r.npb,
                            r.latitude,
                            r.longitude,
                            r.brd,
                            r.bs,
                            r.bm,
                            r.bk,
                            r.tp,
                            r.source_pk
                        FROM usage_keys u
                        JOIN raw_selected r
                          ON r.actor_key=u.actor_key
                         AND r.period_date=u.period_date
                    )
                    UPDATE processing.image_context c
                    SET
                        company_code=coalesce(nullif(btrim(c.company_code), ''), m.company_code),
                        oph=coalesce(nullif(btrim(c.oph), ''), m.oph),
                        oph_key=coalesce(nullif(btrim(c.oph_key), ''), m.oph_key),
                        period_date=coalesce(c.period_date, m.period_date),
                        estate=m.estate,
                        afdeling=coalesce(nullif(btrim(c.afdeling), ''), m.afdeling),
                        blok=coalesce(nullif(btrim(c.blok), ''), m.blok),
                        tph=coalesce(nullif(btrim(c.tph), ''), m.tph),
                        pemanen=coalesce(nullif(btrim(c.pemanen), ''), m.pemanen),
                        kerani_panen=coalesce(nullif(btrim(c.kerani_panen), ''), m.kerani_panen),
                        jjg_kerani_panen=coalesce(c.jjg_kerani_panen, m.jjg_kerani_panen),
                        mandor=coalesce(nullif(btrim(c.mandor), ''), m.mandor),
                        nik_pemanen=coalesce(nullif(btrim(c.nik_pemanen), ''), m.nik_pemanen),
                        nik_kerani=coalesce(nullif(btrim(c.nik_kerani), ''), m.nik_kerani),
                        nik_mandor=coalesce(nullif(btrim(c.nik_mandor), ''), m.nik_mandor),
                        npb=coalesce(nullif(btrim(c.npb), ''), m.npb),
                        latitude=coalesce(c.latitude, m.latitude),
                        longitude=coalesce(c.longitude, m.longitude),
                        brd=coalesce(c.brd, m.brd),
                        bs=coalesce(c.bs, m.bs),
                        bm=coalesce(c.bm, m.bm),
                        bk=coalesce(c.bk, m.bk),
                        tp=coalesce(c.tp, m.tp),
                        source_table=%s,
                        source_pk=m.source_pk,
                        match_method='MATCH_BY_RAW_ACTOR_DATE_UNIQUE_OPH',
                        updated_at=now()
                    FROM matched m
                    WHERE c.file_uid=m.file_uid
                      AND (
                          nullif(trim(coalesce(c.estate, '')), '') IS NULL
                          OR nullif(trim(coalesce(c.kerani_panen, '')), '') IS NULL
                          OR nullif(trim(coalesce(c.mandor, '')), '') IS NULL
                      )
                """
                unique_params = [
                    usage_param,
                    f"{settings.raw_business_schema}.{settings.raw_business_table}",
                ]
                cur.execute("SELECT count(*) FROM (" + usage_sql + ") q", (usage_param,))
                actor_date_candidates = int(cur.fetchone()[0])
                if actor_date_candidates:
                    print(
                        f"[RAW CONTEXT] candidates={actor_date_candidates:,}; "
                        "mulai fallback actor+period_date (unique estate) ...",
                        flush=True,
                    )
                    started = perf_counter()
                    cur.execute(unique_sql, unique_params)
                    raw_actor_date_unique_upserted = max(0, int(cur.rowcount))
                    print(
                        "[RAW CONTEXT] actor+period_date selesai; "
                        f"upserted={raw_actor_date_unique_upserted:,}; "
                        f"elapsed={perf_counter() - started:,.1f}s",
                        flush=True,
                    )
                else:
                    raw_actor_date_unique_upserted = 0
            else:
                actor_date_candidates = 0
                raw_actor_date_unique_upserted = 0

            # Last-resort yang diminta bisnis: bila Context sudah mempunyai
            # OPH tetapi estate masih kosong, tarik PRFNR SAP hanya jika satu
            # OPH mempunyai tepat satu PRFNR. Tidak ada mapping khusus company.
            sap_oph_prfnr_candidates = 0
            sap_oph_prfnr_upserted = 0
            if table_exists(conn, settings.sap_schema, settings.sap_table):
                sap_table = qname(settings.sap_schema, settings.sap_table)
                sap_columns = table_columns(conn, settings.sap_schema, settings.sap_table)
                sap_map = {column.lower(): column for column in sap_columns}
                sap_oph_col = next(
                    (sap_map[name] for name in ("oph", "harvest_ticket", "tiket_panen", "no_bcc") if name in sap_map),
                    None,
                )
                sap_prfnr_col = next(
                    (sap_map[name] for name in ("prfnr", "estate", "estate_name") if name in sap_map),
                    None,
                )
                if sap_oph_col and sap_prfnr_col:
                    sap_sql = f"""
                        WITH usage AS (
                            {usage_sql}
                        ), sap_match AS (
                            SELECT
                                u.file_uid,
                                min(nullif(btrim(s.{ident(sap_prfnr_col)}::text), '')) AS estate,
                                count(DISTINCT upper(btrim(s.{ident(sap_prfnr_col)}::text))) AS estate_count
                            FROM usage u
                            JOIN processing.image_context c ON c.file_uid=u.file_uid
                            JOIN {sap_table} s
                              ON processing.normalize_oph(s.{ident(sap_oph_col)}::text)=c.oph_key
                            WHERE nullif(btrim(c.oph_key), '') IS NOT NULL
                              AND nullif(btrim(s.{ident(sap_prfnr_col)}::text), '') IS NOT NULL
                            GROUP BY u.file_uid
                        ), unique_prfnr AS (
                            SELECT file_uid, estate
                            FROM sap_match
                            WHERE estate_count=1
                        )
                        UPDATE processing.image_context c
                        SET estate=u.estate,
                            source_table=%s,
                            match_method='MATCH_BY_SAP_OPH_UNIQUE_PRFNR',
                            updated_at=now()
                        FROM unique_prfnr u
                        WHERE c.file_uid=u.file_uid
                          AND nullif(trim(coalesce(c.estate, '')), '') IS NULL
                    """
                    cur.execute("SELECT count(*) FROM (" + usage_sql + ") q", (usage_param,))
                    sap_oph_prfnr_candidates = int(cur.fetchone()[0])
                    if sap_oph_prfnr_candidates:
                        print(
                            f"[SAP CONTEXT] candidates={sap_oph_prfnr_candidates:,}; "
                            f"mulai fallback OPH -> {sap_table}.PRFNR ...",
                            flush=True,
                        )
                        started = perf_counter()
                        cur.execute(
                            sap_sql,
                            (usage_param, f"{settings.sap_schema}.{settings.sap_table}"),
                        )
                        sap_oph_prfnr_upserted = max(0, int(cur.rowcount))
                        print(
                            "[SAP CONTEXT] OPH->PRFNR selesai; "
                            f"upserted={sap_oph_prfnr_upserted:,}; "
                            f"elapsed={perf_counter() - started:,.1f}s",
                            flush=True,
                        )

    metrics = {
        "raw_context_scope_all_missing": int(all_missing),
        "historical_gold_context_upserted": historical_gold_context_upserted,
        "raw_usage_candidates": raw_usage_candidates,
        "raw_usage_context_upserted": raw_usage_context_upserted,
        "raw_timestamp_actor_candidates": timestamp_actor_candidates,
        "raw_timestamp_actor_upserted": raw_timestamp_actor_upserted,
        "raw_actor_date_candidates": actor_date_candidates,
        "raw_actor_date_unique_upserted": raw_actor_date_unique_upserted,
        "sap_oph_prfnr_candidates": sap_oph_prfnr_candidates,
        "sap_oph_prfnr_upserted": sap_oph_prfnr_upserted,
    }
    print(f"[RAW CONTEXT] {metrics}")
    return metrics


def backfill_gold_from_context(
    settings: Settings,
    source_group: str,
    *,
    run_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Isi detail Gold kosong dan bangun ulang summary hanya dari active usage."""
    source_group = source_group.upper()
    detail_run_filter = "AND d.run_id=%s" if run_id is not None else ""
    summary_run_filter = "AND d.run_id=%s" if run_id is not None else ""
    remaining_run_filter = "AND run_id=%s" if run_id is not None else ""
    params: list[Any] = [source_group]
    if run_id is not None:
        params.append(run_id)

    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE gold.reused_photo_detail d
            SET
                source_company=coalesce(nullif(trim(d.source_company), ''), c.company_code),
                estate=coalesce(nullif(trim(d.estate), ''), c.estate),
                afdeling=coalesce(nullif(trim(d.afdeling), ''), c.afdeling),
                blok=coalesce(nullif(trim(d.blok), ''), c.blok),
                tph=coalesce(nullif(trim(d.tph), ''), c.tph),
                oph=coalesce(nullif(trim(d.oph), ''), c.oph),
                tanggal=CASE
                    WHEN d.match_source='MATCH_DATE_FROM_FILENAME' AND c.period_date IS NOT NULL
                    THEN c.period_date
                    ELSE coalesce(d.tanggal, c.period_date)
                END,
                pemanen=coalesce(nullif(trim(d.pemanen), ''), c.pemanen),
                kerani_panen=coalesce(nullif(trim(d.kerani_panen), ''), c.kerani_panen),
                jjg_kerani_panen=coalesce(d.jjg_kerani_panen, c.jjg_kerani_panen),
                mandor=coalesce(nullif(trim(d.mandor), ''), c.mandor),
                bunch_pay=CASE
                    WHEN (d.bunch_pay IS NULL OR d.bunch_pay=0)
                         AND c.jjg_kerani_panen IS NOT NULL
                    THEN c.jjg_kerani_panen
                    ELSE d.bunch_pay
                END,
                latitude=coalesce(d.latitude, c.latitude),
                longitude=coalesce(d.longitude, c.longitude),
                match_source=coalesce(nullif(trim(c.match_method), ''), d.match_source),
                published_at=now()
            FROM processing.image_context c
            WHERE c.file_uid=d.file_uid
              AND upper(d.source_group)=upper(%s)
              AND d.is_active=true
              {detail_run_filter}
              AND (
                  nullif(trim(coalesce(d.estate, '')), '') IS NULL
                  OR nullif(trim(coalesce(d.kerani_panen, '')), '') IS NULL
                  OR nullif(trim(coalesce(d.mandor, '')), '') IS NULL
              )
              AND (
                  nullif(trim(coalesce(c.estate, '')), '') IS NOT NULL
                  OR nullif(trim(coalesce(c.kerani_panen, '')), '') IS NOT NULL
                  OR nullif(trim(coalesce(c.mandor, '')), '') IS NOT NULL
              )
            """,
            params,
        )
        gold_detail_enriched = max(0, int(cur.rowcount))

        cur.execute(
            f"""
            WITH agg AS (
                SELECT
                    d.run_id,
                    d.group_id,
                    count(*)::integer AS usage_count,
                    count(*) FILTER (
                        WHERE nullif(trim(coalesce(d.match_source, '')), '') IS NOT NULL
                          AND d.match_source <> 'NOT_MATCH_DB_CONTEXT'
                    )::integer AS matched_image_count,
                    string_agg(DISTINCT nullif(trim(d.estate), ''), ', '
                        ORDER BY nullif(trim(d.estate), '')) AS estate_list,
                    string_agg(DISTINCT nullif(trim(d.afdeling), ''), ', '
                        ORDER BY nullif(trim(d.afdeling), '')) AS afdeling_list,
                    string_agg(DISTINCT nullif(trim(d.blok), ''), ', '
                        ORDER BY nullif(trim(d.blok), '')) AS blok_list,
                    string_agg(DISTINCT nullif(trim(d.tph), ''), ', '
                        ORDER BY nullif(trim(d.tph), '')) AS tph_list,
                    string_agg(DISTINCT nullif(trim(d.oph), ''), ', '
                        ORDER BY nullif(trim(d.oph), '')) AS oph_list,
                    string_agg(DISTINCT d.tanggal::text, ', '
                        ORDER BY d.tanggal::text) FILTER (WHERE d.tanggal IS NOT NULL) AS sql_date_list,
                    string_agg(DISTINCT nullif(trim(d.pemanen), ''), ', '
                        ORDER BY nullif(trim(d.pemanen), '')) AS pemanen_list,
                    string_agg(DISTINCT nullif(trim(d.kerani_panen), ''), ', '
                        ORDER BY nullif(trim(d.kerani_panen), '')) AS kerani_panen_list,
                    string_agg(DISTINCT nullif(trim(d.mandor), ''), ', '
                        ORDER BY nullif(trim(d.mandor), '')) AS mandor_list,
                    string_agg(DISTINCT nullif(trim(d.kerani_ft), ''), ', '
                        ORDER BY nullif(trim(d.kerani_ft), '')) AS kerani_ft_list,
                    string_agg(DISTINCT nullif(trim(d.kerani_dt), ''), ', '
                        ORDER BY nullif(trim(d.kerani_dt), '')) AS kerani_dt_list,
                    string_agg(DISTINCT nullif(trim(d.transport_status), ''), ', '
                        ORDER BY nullif(trim(d.transport_status), '')) AS status_transport_list,
                    string_agg(DISTINCT nullif(trim(d.similarity), ''), ', '
                        ORDER BY nullif(trim(d.similarity), '')) AS similarity_list,
                    coalesce(sum(d.bunch_pay), 0) AS total_bunch_pay_group,
                    count(DISTINCT nullif(trim(d.estate), '')) AS estate_count,
                    count(DISTINCT nullif(trim(d.afdeling), '')) AS afdeling_count,
                    count(DISTINCT nullif(trim(d.blok), '')) AS blok_count,
                    count(DISTINCT nullif(trim(d.tph), '')) AS tph_count,
                    count(DISTINCT d.tanggal) AS date_count
                FROM gold.reused_photo_detail d
                WHERE upper(d.source_group)=upper(%s)
                  AND d.is_active=true
                  {summary_run_filter}
                GROUP BY d.run_id, d.group_id
            ), classified AS (
                SELECT
                    agg.*,
                    CASE
                        WHEN estate_count > 1 THEN 'Estate Berbeda'
                        WHEN afdeling_count > 1 THEN 'Afdeling Berbeda'
                        WHEN blok_count > 1 THEN 'Blok Berbeda'
                        WHEN tph_count > 1 THEN 'Blok Sama / TPH Berbeda'
                        ELSE 'TPH Sama'
                    END AS reuse_category_new,
                    CASE
                        WHEN date_count > 1 THEN 'Tanggal Berbeda'
                        ELSE 'Tanggal Sama'
                    END AS date_category_new
                FROM agg
            )
            UPDATE gold.reused_photo_group_summary s
            SET
                usage_count=a.usage_count,
                matched_image_count=a.matched_image_count,
                estate_list=a.estate_list,
                afdeling_list=a.afdeling_list,
                blok_list=a.blok_list,
                tph_list=a.tph_list,
                oph_list=a.oph_list,
                sql_date_list=a.sql_date_list,
                pemanen_list=a.pemanen_list,
                kerani_panen_list=a.kerani_panen_list,
                mandor_list=a.mandor_list,
                kerani_ft_list=a.kerani_ft_list,
                kerani_dt_list=a.kerani_dt_list,
                status_transport_list=a.status_transport_list,
                similarity_list=a.similarity_list,
                total_bunch_pay_group=a.total_bunch_pay_group,
                reuse_category=a.reuse_category_new,
                date_category=a.date_category_new,
                reuse_date_category=a.reuse_category_new || ' | ' || a.date_category_new,
                published_at=now()
            FROM classified a
            WHERE s.run_id=a.run_id
              AND s.group_id=a.group_id
              AND s.is_active=true
            """,
            params,
        )
        gold_groups_refreshed = max(0, int(cur.rowcount))

        cur.execute(
            f"""
            SELECT count(*)
            FROM gold.reused_photo_detail
            WHERE upper(source_group)=upper(%s)
              AND is_active=true
              {remaining_run_filter}
              AND nullif(trim(coalesce(estate, '')), '') IS NULL
            """,
            params,
        )
        active_usage_missing_estate = int(cur.fetchone()[0])

        if active_usage_missing_estate:
            cur.execute(
                f"""
                SELECT image_name
                FROM gold.reused_photo_detail
                WHERE upper(source_group)=upper(%s)
                  AND is_active=true
                  {remaining_run_filter}
                  AND nullif(trim(coalesce(estate, '')), '') IS NULL
                ORDER BY image_name
                LIMIT 12
                """,
                params,
            )
            missing_samples = [row[0] for row in cur.fetchall()]
        else:
            missing_samples = []

    metrics = {
        "gold_detail_enriched": gold_detail_enriched,
        "gold_groups_refreshed": gold_groups_refreshed,
        "active_usage_missing_estate": active_usage_missing_estate,
    }
    print(f"[GOLD CONTEXT] {metrics}")
    if missing_samples:
        print(f"[GOLD CONTEXT] remaining_missing_samples={missing_samples}")
    return metrics


def enrich_active_gold_context(settings: Settings, source_group: str) -> dict[str, int]:
    # V172: recover every missing Context row first, not only rows reachable
    # from the current active Gold population. This is required for ELHP month
    # catch-up where estate/date exists but Kerani/Mandor is still blank.
    raw_metrics = sync_raw_usage_context(settings, source_group, all_missing=True)
    gold_metrics = backfill_gold_from_context(settings, source_group)
    return {**raw_metrics, **gold_metrics}


def recover_elhp_personnel_month(
    settings: Settings,
    source_group: str,
    month: str,
) -> dict[str, int]:
    """V175 fast recovery: Gold target -> RAW ACTUAL by OPH -> Context.

    Dashboard/report only need active Gold rows.  Starting from the whole
    ``processing.image_context`` was unnecessarily expensive because that table
    has no month-oriented index.  This path starts from the much smaller active
    Gold target for one PT/month, resolves personnel from RAW ``ACTUAL`` rows,
    updates Gold first, then mirrors the result into Context.
    """
    import re
    from datetime import date

    source_group = source_group.upper().strip()
    if source_group not in {"DAN", "DWT"}:
        raise ValueError("recover-elhp-personnel hanya untuk source-group DAN/DWT")
    if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", str(month or "")):
        raise ValueError("--month harus YYYY-MM, contoh 2026-08")

    year, mon = [int(x) for x in month.split("-")]
    start_date = date(year, mon, 1)
    end_date = date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)
    raw_table = qname(settings.raw_business_schema, settings.raw_business_table)
    aliases = _context_source_aliases(settings, source_group)

    metrics = {
        "gold_target": 0,
        "gold_target_with_oph": 0,
        "raw_actual_rows": 0,
        "raw_unique_oph": 0,
        "gold_detail_updated": 0,
        "context_personnel_updated": 0,
        "remaining_gold_missing": 0,
    }

    print(
        f"[ELHP PERSONNEL FAST {month}] start source={source_group} "
        f"aliases={aliases}",
        flush=True,
    )

    with connection(settings) as conn:
        if not table_exists(conn, settings.raw_business_schema, settings.raw_business_table):
            raise RuntimeError(f"Tabel RAW tidak ada: {raw_table}")

        columns = table_columns(conn, settings.raw_business_schema, settings.raw_business_table)
        column_map = {c.lower(): c for c in columns}
        resolved, values = _raw_values(columns)
        oph_expr = values.get("oph", "NULL::text")
        kerani_expr = values.get("kerani_panen", "NULL::text")
        mandor_expr = values.get("mandor", "NULL::text")
        nik_kerani_expr = values.get("nik_kerani", "NULL::text")
        nik_mandor_expr = values.get("nik_mandor", "NULL::text")
        status_col = resolved.get("account_status")
        if not status_col:
            raise RuntimeError(f"Kolom ACCOUNT_STATUS tidak ditemukan di {raw_table}")
        if oph_expr == "NULL::text":
            raise RuntimeError(f"Kolom OPH/harvest_ticket tidak ditemukan di {raw_table}")

        company_expr = values.get("company_code", "NULL::text")
        company_code = settings.source_company(source_group)

        if "period_year" in column_map and "period_month" in column_map:
            month_filter = (
                f"AND p.{ident(column_map['period_year'])}::text=%s "
                f"AND p.{ident(column_map['period_month'])}::text::int=%s"
            )
            month_params: list[Any] = [str(year), mon]
        else:
            month_filter = f"AND {values['period_date']} >= %s AND {values['period_date']} < %s"
            month_params = [start_date, end_date]

        with conn.cursor() as cur:
            # Step 1: active Gold rows that are actually visible to dashboard/report.
            print(f"[ELHP PERSONNEL FAST {month}] step1 gold target ...", flush=True)
            cur.execute(
                """
                CREATE TEMP TABLE tmp_elhp_gold_personnel_target ON COMMIT DROP AS
                SELECT DISTINCT
                    d.run_id,
                    d.group_id,
                    d.file_uid,
                    processing.normalize_oph(d.oph) AS oph_key
                FROM gold.reused_photo_detail d
                WHERE d.source_group = ANY(%s)
                  AND d.is_active=true
                  AND d.tanggal >= %s AND d.tanggal < %s
                  AND (
                    d.kerani_panen IS NULL OR btrim(d.kerani_panen)='' OR
                    d.mandor IS NULL OR btrim(d.mandor)=''
                  )
                """,
                (aliases, start_date, end_date),
            )
            cur.execute("CREATE INDEX ON tmp_elhp_gold_personnel_target (oph_key)")
            cur.execute(
                "SELECT count(*), count(*) FILTER (WHERE nullif(oph_key,'') IS NOT NULL) "
                "FROM tmp_elhp_gold_personnel_target"
            )
            metrics["gold_target"], metrics["gold_target_with_oph"] = [int(x) for x in cur.fetchone()]
            print(
                f"[ELHP PERSONNEL FAST {month}] gold_target={metrics['gold_target']:,} "
                f"with_oph={metrics['gold_target_with_oph']:,}",
                flush=True,
            )
            if metrics["gold_target_with_oph"] == 0:
                print(f"[ELHP PERSONNEL FAST {month}] tidak ada target OPH; selesai.", flush=True)
                return metrics

            # Step 2: only RAW ACTUAL rows for the exact PT/month and target OPHs.
            print(f"[ELHP PERSONNEL FAST {month}] step2 raw ACTUAL target OPH ...", flush=True)
            cur.execute(
                f"""
                CREATE TEMP TABLE tmp_elhp_raw_actual_target ON COMMIT DROP AS
                SELECT
                    processing.normalize_oph({oph_expr}) AS oph_key,
                    {kerani_expr} AS kerani_panen,
                    {mandor_expr} AS mandor,
                    {nik_kerani_expr} AS nik_kerani,
                    {nik_mandor_expr} AS nik_mandor
                FROM {raw_table} p
                WHERE p.{ident(status_col)}::text='ACTUAL'
                  AND {company_expr}=%s
                  {month_filter}
                  AND (
                    nullif(btrim(coalesce({kerani_expr},'')), '') IS NOT NULL OR
                    nullif(btrim(coalesce({mandor_expr},'')), '') IS NOT NULL
                  )
                  AND EXISTS (
                    SELECT 1
                    FROM tmp_elhp_gold_personnel_target t
                    WHERE t.oph_key=processing.normalize_oph({oph_expr})
                  )
                """,
                tuple([company_code, *month_params]),
            )
            cur.execute("SELECT count(*) FROM tmp_elhp_raw_actual_target")
            metrics["raw_actual_rows"] = int(cur.fetchone()[0])
            print(
                f"[ELHP PERSONNEL FAST {month}] raw_actual_target_rows={metrics['raw_actual_rows']:,}",
                flush=True,
            )

            cur.execute(
                """
                CREATE TEMP TABLE tmp_elhp_raw_personnel_oph ON COMMIT DROP AS
                SELECT
                    oph_key,
                    CASE WHEN count(DISTINCT nullif(upper(btrim(kerani_panen)),''))=1
                         THEN max(kerani_panen) FILTER (WHERE nullif(btrim(kerani_panen),'') IS NOT NULL)
                         ELSE NULL::text END AS kerani_panen,
                    CASE WHEN count(DISTINCT nullif(upper(btrim(mandor)),''))=1
                         THEN max(mandor) FILTER (WHERE nullif(btrim(mandor),'') IS NOT NULL)
                         ELSE NULL::text END AS mandor,
                    CASE WHEN count(DISTINCT nullif(upper(btrim(nik_kerani)),''))<=1
                         THEN max(nik_kerani) FILTER (WHERE nullif(btrim(nik_kerani),'') IS NOT NULL)
                         ELSE NULL::text END AS nik_kerani,
                    CASE WHEN count(DISTINCT nullif(upper(btrim(nik_mandor)),''))<=1
                         THEN max(nik_mandor) FILTER (WHERE nullif(btrim(nik_mandor),'') IS NOT NULL)
                         ELSE NULL::text END AS nik_mandor
                FROM tmp_elhp_raw_actual_target
                WHERE nullif(oph_key,'') IS NOT NULL
                GROUP BY oph_key
                HAVING count(DISTINCT nullif(upper(btrim(kerani_panen)),'')) <= 1
                   AND count(DISTINCT nullif(upper(btrim(mandor)),'')) <= 1
                   AND (
                     count(DISTINCT nullif(upper(btrim(kerani_panen)),''))=1 OR
                     count(DISTINCT nullif(upper(btrim(mandor)),''))=1
                   )
                """
            )
            cur.execute("CREATE UNIQUE INDEX ON tmp_elhp_raw_personnel_oph (oph_key)")
            cur.execute("ANALYZE tmp_elhp_raw_personnel_oph")
            cur.execute("SELECT count(*) FROM tmp_elhp_raw_personnel_oph")
            metrics["raw_unique_oph"] = int(cur.fetchone()[0])
            print(
                f"[ELHP PERSONNEL FAST {month}] raw_unique_oph={metrics['raw_unique_oph']:,}",
                flush=True,
            )

            # Step 3: update Gold FIRST; this is the dashboard/report source.
            print(f"[ELHP PERSONNEL FAST {month}] step3 update Gold ...", flush=True)
            cur.execute(
                """
                UPDATE gold.reused_photo_detail d
                SET kerani_panen=coalesce(nullif(btrim(d.kerani_panen),''), r.kerani_panen),
                    mandor=coalesce(nullif(btrim(d.mandor),''), r.mandor),
                    published_at=now()
                FROM tmp_elhp_gold_personnel_target t
                JOIN tmp_elhp_raw_personnel_oph r ON r.oph_key=t.oph_key
                WHERE d.run_id=t.run_id
                  AND d.group_id=t.group_id
                  AND d.file_uid=t.file_uid
                  AND (
                    d.kerani_panen IS NULL OR btrim(d.kerani_panen)='' OR
                    d.mandor IS NULL OR btrim(d.mandor)=''
                  )
                """
            )
            metrics["gold_detail_updated"] = max(0, int(cur.rowcount))
            print(
                f"[ELHP PERSONNEL FAST {month}] gold_detail_updated={metrics['gold_detail_updated']:,}",
                flush=True,
            )

            # Step 4: mirror the exact fixed Gold rows to Context by file_uid.
            print(f"[ELHP PERSONNEL FAST {month}] step4 mirror Context ...", flush=True)
            cur.execute(
                """
                UPDATE processing.image_context c
                SET kerani_panen=coalesce(nullif(btrim(c.kerani_panen),''), d.kerani_panen),
                    mandor=coalesce(nullif(btrim(c.mandor),''), d.mandor),
                    source_table='gold.reused_photo_detail',
                    match_method='RECOVER_FROM_RAW_ACTUAL_OPH_VIA_GOLD',
                    updated_at=now()
                FROM gold.reused_photo_detail d
                JOIN tmp_elhp_gold_personnel_target t
                  ON t.run_id=d.run_id AND t.group_id=d.group_id AND t.file_uid=d.file_uid
                WHERE c.file_uid=d.file_uid
                  AND (
                    c.kerani_panen IS NULL OR btrim(c.kerani_panen)='' OR
                    c.mandor IS NULL OR btrim(c.mandor)=''
                  )
                  AND (
                    nullif(btrim(coalesce(d.kerani_panen,'')), '') IS NOT NULL OR
                    nullif(btrim(coalesce(d.mandor,'')), '') IS NOT NULL
                  )
                """
            )
            metrics["context_personnel_updated"] = max(0, int(cur.rowcount))
            print(
                f"[ELHP PERSONNEL FAST {month}] context_updated={metrics['context_personnel_updated']:,}",
                flush=True,
            )

            # Refresh summaries only for touched target groups.
            cur.execute(
                """
                WITH touched AS (
                    SELECT DISTINCT run_id, group_id FROM tmp_elhp_gold_personnel_target
                ), agg AS (
                    SELECT d.run_id, d.group_id,
                           string_agg(DISTINCT nullif(btrim(d.kerani_panen),''), ', ' ORDER BY nullif(btrim(d.kerani_panen),'')) AS kerani_panen_list,
                           string_agg(DISTINCT nullif(btrim(d.mandor),''), ', ' ORDER BY nullif(btrim(d.mandor),'')) AS mandor_list
                    FROM gold.reused_photo_detail d
                    JOIN touched t ON t.run_id=d.run_id AND t.group_id=d.group_id
                    WHERE d.is_active=true
                    GROUP BY d.run_id, d.group_id
                )
                UPDATE gold.reused_photo_group_summary s
                SET kerani_panen_list=a.kerani_panen_list,
                    mandor_list=a.mandor_list,
                    published_at=now()
                FROM agg a
                WHERE s.run_id=a.run_id AND s.group_id=a.group_id
                """
            )

            cur.execute(
                """
                SELECT count(*)
                FROM gold.reused_photo_detail d
                WHERE d.source_group = ANY(%s)
                  AND d.is_active=true
                  AND d.tanggal >= %s AND d.tanggal < %s
                  AND (
                    d.kerani_panen IS NULL OR btrim(d.kerani_panen)='' OR
                    d.mandor IS NULL OR btrim(d.mandor)=''
                  )
                """,
                (aliases, start_date, end_date),
            )
            metrics["remaining_gold_missing"] = int(cur.fetchone()[0])

    print(f"[ELHP PERSONNEL FAST {month}] DONE {metrics}", flush=True)
    return metrics
