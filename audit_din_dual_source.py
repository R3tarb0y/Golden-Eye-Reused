from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src'))
from golden_eye.config import Settings
from golden_eye.db import connection, fetch_all_dict

def show(title, rows):
    print('\n' + title)
    print('-' * len(title))
    if not rows:
        print('(tidak ada row)')
        return
    cols=list(rows[0].keys())
    print(' | '.join(cols))
    for r in rows:
        print(' | '.join(str(r.get(c,'')) for c in cols))

def main():
    s=Settings()
    with connection(s) as conn:
        print('='*100)
        print('DIN DUAL-SOURCE / CONTEXT AUDIT (READ ONLY)')
        print('='*100)
        show('1. SILVER IMAGE source_group yang terkait DIN', fetch_all_dict(conn, f"""
            SELECT upper(source_group::text) source_group, count(*)::bigint rows
            FROM {s.silver_schema}.{s.image_table}
            WHERE upper(source_group::text) LIKE '%DIN%'
            GROUP BY 1 ORDER BY 2 DESC
        """))
        show('2. ACTIVE GOLD DIN / ELHP_DIN', fetch_all_dict(conn, """
            SELECT upper(source_group) source_group,
                   upper(coalesce(source_system,'')) source_system,
                   upper(coalesce(source_company,'')) source_company,
                   count(*)::bigint usages,
                   count(DISTINCT group_id)::bigint groups
            FROM gold.reused_photo_detail
            WHERE is_active=true
              AND (processing.canonical_source_group(source_group)='DIN'
                   OR upper(source_group) LIKE '%DIN%'
                   OR upper(coalesce(source_company,''))='DIN')
            GROUP BY 1,2,3 ORDER BY 4 DESC
        """))
        show('3. ACTIVE DIN match_source', fetch_all_dict(conn, """
            SELECT coalesce(nullif(btrim(match_source),''),'(EMPTY)') match_source,
                   count(*)::bigint usages,
                   count(*) FILTER (WHERE nullif(processing.normalize_oph(oph),'') IS NOT NULL)::bigint with_oph
            FROM gold.reused_photo_detail
            WHERE is_active=true AND processing.canonical_source_group(source_group)='DIN'
            GROUP BY 1 ORDER BY 2 DESC
        """))
        show('4. PROCESSING CONTEXT source_group/method untuk validated DIN aktif', fetch_all_dict(conn, """
            WITH ar AS (
              SELECT run_id FROM gold.reused_photo_group
              WHERE is_active=true AND processing.canonical_source_group(source_group)='DIN'
              ORDER BY published_at DESC LIMIT 1
            )
            SELECT upper(coalesce(c.source_group,'')) source_group,
                   upper(coalesce(c.match_method,'')) match_method,
                   count(*)::bigint rows,
                   count(*) FILTER (WHERE nullif(processing.normalize_oph(c.oph),'') IS NOT NULL)::bigint with_oph
            FROM ar
            JOIN processing.validated_group_member m ON m.run_id=ar.run_id
            LEFT JOIN processing.image_context c ON c.file_uid=m.file_uid
            GROUP BY 1,2 ORDER BY 3 DESC
        """))
        show('5. Historical Gold donor labels untuk DIN', fetch_all_dict(conn, """
            SELECT upper(source_group) source_group,
                   upper(coalesce(source_system,'')) source_system,
                   upper(coalesce(source_company,'')) source_company,
                   count(*)::bigint rows,
                   min(published_at) first_publish,
                   max(published_at) last_publish
            FROM gold.reused_photo_detail
            WHERE upper(source_group) LIKE '%DIN%'
               OR upper(coalesce(source_company,''))='DIN'
            GROUP BY 1,2,3 ORDER BY 4 DESC
        """))
    return 0
if __name__=='__main__':
    raise SystemExit(main())
