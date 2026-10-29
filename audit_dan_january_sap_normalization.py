from __future__ import annotations
import argparse, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
DASH=ROOT/'dashboard'
if str(DASH) not in sys.path: sys.path.insert(0,str(DASH))
from db import read_df_resilient

def n(v):
    try:return f"{int(v or 0):,}"
    except:return str(v)

def main():
    ap=argparse.ArgumentParser(description='Preview DAN January legacy OPH -> exact SAP same-month normalization')
    ap.add_argument('--year',type=int,default=2026); ap.add_argument('--month',type=int,default=1)
    a=ap.parse_args(); ym=f'{a.year:04d}-{a.month:02d}'
    print('='*140); print(f'DAN {ym} - SAP NORMALIZATION PREVIEW (READ ONLY)'); print('='*140)
    q='''
    WITH d AS (
      SELECT d.run_id,d.group_id,d.file_uid,d.estate,d.match_source,
             nullif(processing.normalize_oph(c.oph),'') AS context_oph_key,
             c.oph AS context_oph,
             c.match_method AS context_method
      FROM gold.reused_photo_detail d
      LEFT JOIN processing.image_context c ON c.file_uid=d.file_uid
      WHERE d.is_active=true
        AND processing.canonical_source_group(d.source_group)='DAN'
        AND to_char(d.tanggal,'YYYY-MM')=%s
        AND processing.elhp_estate_matches_company('DAN',d.estate)
    ), x AS (
      SELECT d.*, EXISTS(
        SELECT 1 FROM gold.sap_oph_scope_key k
        WHERE k.source_group='DAN' AND k.period_year=%s AND k.sap_month=%s
          AND k.oph_key=d.context_oph_key
      ) AS sap_same
      FROM d
    ), g AS (
      SELECT run_id,group_id,
             count(*) FILTER (WHERE sap_same) AS recoverable_members
      FROM x GROUP BY run_id,group_id
    )
    SELECT count(*) AS estate_ok_visual,
           count(*) FILTER (WHERE context_oph_key IS NOT NULL) AS context_has_oph,
           count(*) FILTER (WHERE sap_same) AS sap_same_recoverable,
           count(*) FILTER (WHERE sap_same AND g.recoverable_members>=2) AS potential_main_usage,
           count(DISTINCT (x.run_id,x.group_id)) FILTER (WHERE g.recoverable_members>=2) AS potential_main_groups,
           min(x.run_id::text) AS sample_active_run
    FROM x JOIN g USING(run_id,group_id)
    '''
    df=read_df_resilient(q,[ym,a.year,ym],operation='DAN Jan normalization preview')
    if df.empty: print('NO DATA'); return 1
    r=df.iloc[0].to_dict()
    for k in ['estate_ok_visual','context_has_oph','sap_same_recoverable','potential_main_usage','potential_main_groups']:
        print(f'{k:<28}: {n(r.get(k))}')
    run=r.get('sample_active_run') or ''
    print('\nPolicy: hanya PU* + original Context OPH + exact gold.sap_oph_scope_key DAN + bulan sama.')
    print('ME1-4 tidak pernah dipromosikan oleh exception ini.')
    if run:
        print('\nCurrent active Gold/validated run candidate:')
        print(f'  {run}')
        print('\nJika sap_same_recoverable > 0, publish tanpa scan RAW berat:')
        print(f'  py run_pipeline.py republish-clean-oph --source-group DAN --source-run-id {run} --visual-only --apply')
    return 0
if __name__=='__main__': raise SystemExit(main())
