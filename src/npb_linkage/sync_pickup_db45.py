from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from decimal import Decimal

from npb_linkage.common import load_config, pg_connect
from npb_linkage.mssql_source import connect_read_only

SOURCE_NAME = "DB45_PICKUP_PWP_2026"
REQUIRED_COLUMNS = {
    "ID","PERIOD_YEAR","PERIOD_MONTH","PERIOD_DAY","PERIOD_DATE",
    "COMPANY_CODE","DEPARTMENT_CODE","AFDELING_CODE","BLOCK",
    "HARVEST_TICKET","NPB_NO","NPB_DATE","GENERATED_AT",
    "NIK_KR","NAME_KR","PICKUP_START_TIME","PICKUP_END_TIME",
    "BUNCH_KT","BRD_KT",
}

COLS = [
    "db45_id","source_id_bigint","company_code","period_year","period_month",
    "period_day","period_date","department_code","afdeling_code","block",
    "harvest_ticket","npb_no","npb_date","nik_kr","name_kr",
    "pickup_start_time","pickup_end_time","bunch_kt","brd_kt","generated_at",
]


def _source_table(cfg):
    return cfg.get("db45",{}).get(
        "source_table","[DASHBOARD].[dbo].[PRODUCTION_PLANTATION]"
    )


def verify_source_columns():
    sql = """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA='dbo'
          AND TABLE_NAME='PRODUCTION_PLANTATION'
    """
    with connect_read_only(as_dict=True) as conn:
        cur=conn.cursor(); cur.execute(sql)
        got={r["COLUMN_NAME"].upper() for r in cur.fetchall()}
    missing=sorted(REQUIRED_COLUMNS-got)
    if missing:
        raise RuntimeError("DB45 pickup columns missing: "+", ".join(missing))
    return got


def _get_state(pg):
    with pg.cursor() as cur:
        cur.execute("""
            SELECT last_source_id,backfill_completed
            FROM control.db45_pickup_sync_state
            WHERE source_name=%s
        """,(SOURCE_NAME,))
        row=cur.fetchone()
    if not row:
        raise RuntimeError("Sync state belum ada. Jalankan SQL 060 dahulu.")
    return int(row[0] or 0), bool(row[1])


def _mark_running(pg):
    with pg.cursor() as cur:
        cur.execute("""
            UPDATE control.db45_pickup_sync_state
            SET last_sync_started_at=now(),last_sync_status='RUNNING',last_error=NULL,updated_at=now()
            WHERE source_name=%s
        """,(SOURCE_NAME,))
    pg.commit()


def _save_checkpoint(pg,last_id,fetched,upserted,completed=None,status='RUNNING',error=None):
    with pg.cursor() as cur:
        cur.execute("""
            UPDATE control.db45_pickup_sync_state
            SET last_source_id=GREATEST(last_source_id,%s),
                backfill_completed=COALESCE(%s,backfill_completed),
                last_sync_status=%s,
                rows_fetched=%s,
                rows_upserted=%s,
                last_error=%s,
                last_sync_finished_at=CASE WHEN %s IN ('COMPLETED','FAILED') THEN now() ELSE last_sync_finished_at END,
                updated_at=now()
            WHERE source_name=%s
        """,(last_id,completed,status,fetched,upserted,error,status,SOURCE_NAME))
    pg.commit()


def _map_row(r):
    sid=r.get('ID')
    if sid is None:
        raise RuntimeError('DB45 row ID NULL; cannot checkpoint safely.')
    try:
        sid_int=int(sid)
    except Exception as exc:
        raise RuntimeError(f'DB45 ID is not numeric: {sid!r}') from exc

    company=(str(r.get('COMPANY_CODE')).strip().upper() if r.get('COMPANY_CODE') is not None else None)
    return {
        'db45_id':str(sid), 'source_id_bigint':sid_int, 'company_code':company,
        'period_year':r.get('PERIOD_YEAR'), 'period_month':r.get('PERIOD_MONTH'),
        'period_day':r.get('PERIOD_DAY'), 'period_date':r.get('PERIOD_DATE'),
        'department_code':r.get('DEPARTMENT_CODE'), 'afdeling_code':r.get('AFDELING_CODE'),
        'block':r.get('BLOCK'), 'harvest_ticket':r.get('HARVEST_TICKET'),
        'npb_no':r.get('NPB_NO'), 'npb_date':r.get('NPB_DATE'),
        'nik_kr':r.get('NIK_KR'), 'name_kr':r.get('NAME_KR'),
        'pickup_start_time':r.get('PICKUP_START_TIME'), 'pickup_end_time':r.get('PICKUP_END_TIME'),
        'bunch_kt':r.get('BUNCH_KT'), 'brd_kt':r.get('BRD_KT'),
        'generated_at':r.get('GENERATED_AT'),
    }


def _upsert(pg,rows):
    if not rows: return 0
    placeholders=','.join(['%s']*len(COLS))
    updates=','.join(f'{c}=EXCLUDED.{c}' for c in COLS if c!='db45_id')
    sql=f"""
        INSERT INTO staging.db45_pickup_event({','.join(COLS)},source_loaded_at,source_updated_at)
        VALUES({placeholders},now(),now())
        ON CONFLICT(db45_id) DO UPDATE SET
            {updates},
            source_updated_at=now()
    """
    values=[tuple(r[c] for c in COLS) for r in rows]
    with pg.cursor() as cur:
        cur.executemany(sql,values)
    pg.commit()
    return len(values)


def _fetch_keyset(cfg,last_id,batch,company='PWP',year=2026):
    table=_source_table(cfg)
    sql=f"""
        SELECT TOP ({int(batch)})
            [ID],[PERIOD_YEAR],[PERIOD_MONTH],[PERIOD_DAY],[PERIOD_DATE],
            [COMPANY_CODE],[DEPARTMENT_CODE],[AFDELING_CODE],[BLOCK],
            [HARVEST_TICKET],[NPB_NO],[NPB_DATE],[GENERATED_AT],
            [NIK_KR],[NAME_KR],[PICKUP_START_TIME],[PICKUP_END_TIME],
            [BUNCH_KT],[BRD_KT]
        FROM {table}
        WHERE [COMPANY_CODE]=%s
          AND [PERIOD_YEAR]=%s
          AND [NPB_NO] IS NOT NULL
          AND [ID] > %s
        ORDER BY [ID]
    """
    with connect_read_only(as_dict=True) as conn:
        cur=conn.cursor(); cur.execute(sql,(company,int(year),int(last_id)))
        return cur.fetchall()


def _refresh_window(cfg,days,batch,company='PWP',year=2026):
    table=_source_table(cfg)
    cutoff=date.today()-timedelta(days=int(days))
    last_id=0
    while True:
        sql=f"""
            SELECT TOP ({int(batch)})
                [ID],[PERIOD_YEAR],[PERIOD_MONTH],[PERIOD_DAY],[PERIOD_DATE],
                [COMPANY_CODE],[DEPARTMENT_CODE],[AFDELING_CODE],[BLOCK],
                [HARVEST_TICKET],[NPB_NO],[NPB_DATE],[GENERATED_AT],
                [NIK_KR],[NAME_KR],[PICKUP_START_TIME],[PICKUP_END_TIME],
                [BUNCH_KT],[BRD_KT]
            FROM {table}
            WHERE [COMPANY_CODE]=%s
              AND [PERIOD_YEAR]=%s
              AND [NPB_NO] IS NOT NULL
              AND [ID] > %s
              AND (
                    [PERIOD_DATE] >= %s
                 OR [PICKUP_START_TIME] >= %s
                 OR [PICKUP_END_TIME] >= %s
              )
            ORDER BY [ID]
        """
        with connect_read_only(as_dict=True) as conn:
            cur=conn.cursor(); cur.execute(sql,(company,int(year),last_id,cutoff,cutoff,cutoff))
            rows=cur.fetchall()
        if not rows: break
        mapped=[_map_row(r) for r in rows]
        yield mapped
        last_id=max(r['source_id_bigint'] for r in mapped)


def _refresh_date(cfg,target,batch,company='PWP'):
    table=_source_table(cfg)
    target=date.fromisoformat(target) if isinstance(target,str) else target
    last_id=0
    while True:
        sql=f"""
            SELECT TOP ({int(batch)})
                [ID],[PERIOD_YEAR],[PERIOD_MONTH],[PERIOD_DAY],[PERIOD_DATE],
                [COMPANY_CODE],[DEPARTMENT_CODE],[AFDELING_CODE],[BLOCK],
                [HARVEST_TICKET],[NPB_NO],[NPB_DATE],[GENERATED_AT],
                [NIK_KR],[NAME_KR],[PICKUP_START_TIME],[PICKUP_END_TIME],
                [BUNCH_KT],[BRD_KT]
            FROM {table}
            WHERE [COMPANY_CODE]=%s
              AND [NPB_NO] IS NOT NULL
              AND [ID] > %s
              AND [PERIOD_DATE]=%s
            ORDER BY [ID]
        """
        with connect_read_only(as_dict=True) as conn:
            cur=conn.cursor(); cur.execute(sql,(company,last_id,target))
            rows=cur.fetchall()
        if not rows: break
        mapped=[_map_row(r) for r in rows]
        yield mapped
        last_id=max(r['source_id_bigint'] for r in mapped)


def run(cfg,mode='resume',batch=5000,refresh_days=14,refresh_date=None):
    verify_source_columns()
    total_fetched=0; total_upserted=0
    with pg_connect() as pg:
        _mark_running(pg)
        checkpoint,completed=_get_state(pg)
        try:
            if refresh_date:
                print(f'[PICKUP SYNC] REFRESH DATE {refresh_date}')
                for mapped in _refresh_date(cfg,refresh_date,batch):
                    total_fetched+=len(mapped); total_upserted+=_upsert(pg,mapped)
                    print(f'[REFRESH DATE] fetched={total_fetched:,} upserted={total_upserted:,}',flush=True)
                _save_checkpoint(pg,checkpoint,total_fetched,total_upserted,completed=completed,status='COMPLETED')
                return

            if mode=='reset':
                checkpoint=0
                with pg.cursor() as cur:
                    cur.execute("UPDATE control.db45_pickup_sync_state SET last_source_id=0,backfill_completed=false WHERE source_name=%s",(SOURCE_NAME,))
                pg.commit()

            print('='*96)
            print('DB45 PICKUP -> LOCAL PG | PWP 2026 | RESUME-SAFE KEYSET SYNC')
            print('='*96)
            print('START ID     :',checkpoint)
            print('BATCH        :',batch)
            print('REFRESH DAYS :',refresh_days)

            last_id=checkpoint
            while True:
                rows=_fetch_keyset(cfg,last_id,batch)
                if not rows: break
                mapped=[_map_row(r) for r in rows]
                fetched=len(mapped); upserted=_upsert(pg,mapped)
                total_fetched+=fetched; total_upserted+=upserted
                last_id=max(r['source_id_bigint'] for r in mapped)
                _save_checkpoint(pg,last_id,total_fetched,total_upserted,status='RUNNING')
                print(f'[KEYSET] last_id={last_id:,} fetched={total_fetched:,} upserted={total_upserted:,}',flush=True)

            # New IDs are done. Mark backfill complete, then re-read recent operations
            # so existing old IDs that received late pickup updates are refreshed too.
            _save_checkpoint(pg,last_id,total_fetched,total_upserted,completed=True,status='RUNNING')

            if int(refresh_days)>0:
                for mapped in _refresh_window(cfg,refresh_days,batch):
                    total_fetched+=len(mapped); total_upserted+=_upsert(pg,mapped)
                    print(f'[LOOKBACK] fetched={total_fetched:,} upserted={total_upserted:,}',flush=True)

            _save_checkpoint(pg,last_id,total_fetched,total_upserted,completed=True,status='COMPLETED')
            print(f'[DONE] fetched={total_fetched:,} upserted={total_upserted:,} last_id={last_id:,}')

        except Exception as exc:
            _save_checkpoint(pg,checkpoint,total_fetched,total_upserted,status='FAILED',error=str(exc))
            raise


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--config',default='config.yaml')
    p.add_argument('--mode',choices=['resume','reset'],default='resume')
    p.add_argument('--batch',type=int,default=5000)
    p.add_argument('--refresh-days',type=int,default=14)
    p.add_argument('--refresh-date')
    args=p.parse_args()
    cfg=load_config(args.config)
    run(cfg,args.mode,args.batch,args.refresh_days,args.refresh_date)


if __name__=='__main__':
    main()
