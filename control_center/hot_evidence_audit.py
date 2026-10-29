"""Read-only lineage audit of processing.gambar_reused.

Maps cached BYTEA *metadata* to active Gold evidence and active KT Before.
Never selects image_bytes, never deletes rows. Candidate estimates are NOT
permission to delete; live NAS verification and pipeline dependency audit are
required before any cleanup.
"""
from __future__ import annotations

from typing import Any
from golden_eye.config import Settings
from golden_eye.db import connection


def _canonical_sql(expr: str) -> str:
    value = f"upper(btrim(coalesce({expr}::text, '')))"
    return f"(CASE WHEN {value}='ELHP_DAN' THEN 'DAN' WHEN {value}='ELHP_DWT' THEN 'DWT' ELSE {value} END)"


def _filename_sql(expr: str) -> str:
    return f"nullif(upper(btrim(regexp_replace(replace(coalesce({expr}::text, ''), chr(92), '/'), '^.*/', ''))), '')"


def hot_evidence_report(*, settings: Settings | None = None) -> dict[str, Any]:
    """Manual-only audit. Only references and declared bytea_len are read.

    Uses temporary keys to prevent repeatedly evaluating Gold-view aggregates.
    Kept sets are conservative: active visual Gold (including unverified audit
    findings) and active Harmony KT Before. Other images are REVIEW, never SAFE.
    """
    settings = settings or Settings()
    gold_sg = _canonical_sql('source_group')
    gold_uid = 'nullif(btrim(file_uid::text), \'\')'
    kt_sg = _canonical_sql('source_group')
    hot_sg = _canonical_sql('h.source_group')
    gold_names = [
        ('file_uid', 'image_name'),
        ('foto_dt_uid', 'foto_dt'),
        ('foto_ft_uid', 'foto_ft'),
    ]
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout='120s'")
        cur.execute("SET LOCAL lock_timeout='3s'")
        cur.execute("SELECT to_regclass('processing.gambar_reused'), to_regclass('gold.reused_photo_detail'), to_regclass('processing.transport_kt_before_context')")
        hot, gold, kt = cur.fetchone()
        if hot is None or gold is None:
            raise RuntimeError('processing.gambar_reused atau gold.reused_photo_detail tidak ditemukan')
        cur.execute("""SELECT attname FROM pg_attribute WHERE attrelid='gold.reused_photo_detail'::regclass AND attnum>0 AND NOT attisdropped""")
        columns = {r[0] for r in cur.fetchall()}
        gold_parts=[]
        for uid, name in gold_names:
            if uid not in columns or name not in columns:
                continue
            gold_parts.append(f"SELECT {gold_sg} AS source_group, nullif(btrim({uid}::text),'') AS file_uid, {_filename_sql(name)} AS file_name FROM gold.reused_photo_detail WHERE is_active=true AND (nullif(btrim({uid}::text),'') IS NOT NULL OR nullif(btrim({name}::text),'') IS NOT NULL)")
        if not gold_parts:
            raise RuntimeError('Gold detail tidak memiliki kolom referensi evidence yang dikenal')
        cur.execute("CREATE TEMP TABLE ge_hot_gold_ref ON COMMIT DROP AS " + ' UNION ALL '.join(gold_parts))
        cur.execute("CREATE TEMP TABLE ge_hot_gold_uid ON COMMIT DROP AS SELECT DISTINCT source_group, file_uid FROM ge_hot_gold_ref WHERE file_uid IS NOT NULL")
        cur.execute("CREATE UNIQUE INDEX ge_hot_gold_uid_idx ON ge_hot_gold_uid (source_group,file_uid)")
        cur.execute("CREATE TEMP TABLE ge_hot_gold_name ON COMMIT DROP AS SELECT DISTINCT source_group, file_name FROM ge_hot_gold_ref WHERE file_name IS NOT NULL")
        cur.execute("CREATE UNIQUE INDEX ge_hot_gold_name_idx ON ge_hot_gold_name (source_group,file_name)")
        if kt is not None:
            cur.execute(f"CREATE TEMP TABLE ge_hot_kt_ref ON COMMIT DROP AS SELECT {kt_sg} AS source_group, nullif(btrim(file_uid::text),'') AS file_uid, {_filename_sql('evidence_file_name')} AS file_name FROM processing.transport_kt_before_context WHERE is_active=true")
            cur.execute("CREATE TEMP TABLE ge_hot_kt_uid ON COMMIT DROP AS SELECT DISTINCT source_group, file_uid FROM ge_hot_kt_ref WHERE file_uid IS NOT NULL")
            cur.execute("CREATE UNIQUE INDEX ge_hot_kt_uid_idx ON ge_hot_kt_uid (source_group,file_uid)")
            cur.execute("CREATE TEMP TABLE ge_hot_kt_name ON COMMIT DROP AS SELECT DISTINCT source_group, file_name FROM ge_hot_kt_ref WHERE file_name IS NOT NULL")
            cur.execute("CREATE UNIQUE INDEX ge_hot_kt_name_idx ON ge_hot_kt_name (source_group,file_name)")
        else:
            cur.execute("CREATE TEMP TABLE ge_hot_kt_uid ON COMMIT DROP AS SELECT source_group,file_uid FROM ge_hot_gold_uid WHERE false")
            cur.execute("CREATE TEMP TABLE ge_hot_kt_name ON COMMIT DROP AS SELECT source_group,file_name FROM ge_hot_gold_name WHERE false")
        cur.execute("ANALYZE ge_hot_gold_uid")
        cur.execute("ANALYZE ge_hot_gold_name")
        cur.execute("ANALYZE ge_hot_kt_uid")
        cur.execute("ANALYZE ge_hot_kt_name")
        # The hot-store scan below never touches its image_bytes TOAST column.
        cur.execute(f"""
            CREATE TEMP TABLE ge_hot_classified ON COMMIT DROP AS
            SELECT {hot_sg} AS source_group,
                   h.file_uid AS file_uid, h.is_active,
                   greatest(coalesce(h.bytea_len,0),0)::bigint AS bytea_len,
                   CASE
                     WHEN gu.file_uid IS NOT NULL OR gn.file_name IS NOT NULL THEN 'GOLD_ACTIVE_KEEP'
                     WHEN ku.file_uid IS NOT NULL OR kn.file_name IS NOT NULL THEN 'KT_BEFORE_KEEP_REVIEW'
                     WHEN NOT h.is_active THEN 'INACTIVE_REVIEW_NAS'
                     ELSE 'UNREFERENCED_REVIEW_NAS'
                   END AS category
            FROM processing.gambar_reused h
            LEFT JOIN ge_hot_gold_uid gu ON gu.source_group={hot_sg} AND gu.file_uid=h.file_uid
            LEFT JOIN ge_hot_gold_name gn ON gn.source_group={hot_sg} AND gn.file_name={_filename_sql('h.file_name')}
            LEFT JOIN ge_hot_kt_uid ku ON ku.source_group={hot_sg} AND ku.file_uid=h.file_uid
            LEFT JOIN ge_hot_kt_name kn ON kn.source_group={hot_sg} AND kn.file_name={_filename_sql('h.file_name')}
        """)
        cur.execute("SELECT category, count(*)::bigint,coalesce(sum(bytea_len),0)::bigint FROM ge_hot_classified GROUP BY category ORDER BY category")
        summary = [{"category": a, "rows": int(b), "logical_bytes": int(c)} for a,b,c in cur.fetchall()]
        cur.execute("SELECT source_group,category,count(*)::bigint,coalesce(sum(bytea_len),0)::bigint FROM ge_hot_classified GROUP BY source_group,category ORDER BY source_group,category")
        by_company = [{"source_group":a,"category":b,"rows":int(c),"logical_bytes":int(d)} for a,b,c,d in cur.fetchall()]
        cur.execute("SELECT pg_total_relation_size('processing.gambar_reused'::regclass)::bigint")
        physical_bytes = int(cur.fetchone()[0])
    return {
        "relation": "processing.gambar_reused", "physical_bytes": physical_bytes,
        "summary":summary, "by_company":by_company,
        "deletion_allowed":False,
        "note":"READ-ONLY. GOLD_ACTIVE_KEEP mencakup Gold visual aktif, termasuk finding belum SAP-verified. KT_BEFORE_KEEP_REVIEW dipakai oleh pipeline transport. UNREFERENCED bukan otomatis aman: validasi NAS, proses pipeline, rollback, dan backup wajib sebelum menghapus. logical_bytes adalah SUM(bytea_len), bukan ukuran disk yang pasti dibebaskan.",
    }
