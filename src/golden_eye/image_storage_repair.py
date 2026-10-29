from __future__ import annotations

import re
from typing import Any

from psycopg2 import sql

from .config import Settings
from .db import connection


_BAD_RELATION_RE = re.compile(r'relation\s+"(?:[^"/]+/)*([0-9]+)"', re.IGNORECASE)


def _relation_from_filenode(settings: Settings, filenode: int) -> dict[str, Any] | None:
    """Resolve a physical filenode from a corruption error back to pg_class."""
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.oid::bigint,
                   n.nspname,
                   c.relname,
                   c.relkind,
                   pg_relation_filenode(c.oid)::bigint AS current_filenode,
                   pg_relation_filepath(c.oid) AS filepath,
                   CASE WHEN i.indrelid IS NULL THEN NULL
                        ELSE i.indrelid::regclass::text END AS index_parent
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_index i ON i.indexrelid = c.oid
            WHERE pg_relation_filenode(c.oid) = %s
               OR c.relfilenode = %s
            ORDER BY CASE c.relkind WHEN 'i' THEN 0 WHEN 't' THEN 1 ELSE 2 END,
                     c.oid
            LIMIT 1
            """,
            (int(filenode), int(filenode)),
        )
        row = cur.fetchone()
        if not row:
            return None
        oid, schema_name, relname, relkind, current_filenode, filepath, parent = row
        return {
            "oid": int(oid),
            "schema": str(schema_name),
            "name": str(relname),
            "qualified": f"{schema_name}.{relname}",
            "relkind": str(relkind),
            "filenode": int(current_filenode) if current_filenode is not None else int(filenode),
            "filepath": str(filepath or ""),
            "index_parent": str(parent or ""),
        }


def _extract_filenode(exc: Exception) -> int | None:
    text = str(exc)
    match = _BAD_RELATION_RE.search(text)
    if match:
        return int(match.group(1))
    # psycopg often formats it as base/<db_oid>/<filenode>
    matches = re.findall(r'(?:base|global|pg_tblspc)[^\s"]*/([0-9]+)', text)
    return int(matches[-1]) if matches else None


def inspect_gambar_reused_storage(settings: Settings) -> dict[str, Any]:
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.oid::regclass::text AS table_name,
                   c.oid::bigint AS table_oid,
                   pg_relation_filenode(c.oid)::bigint AS table_filenode,
                   c.reltoastrelid,
                   CASE WHEN c.reltoastrelid = 0 THEN NULL
                        ELSE c.reltoastrelid::regclass::text END AS toast_table,
                   CASE WHEN c.reltoastrelid = 0 THEN NULL
                        ELSE pg_relation_filenode(c.reltoastrelid)::bigint END AS toast_filenode
            FROM pg_class c
            WHERE c.oid = 'processing.gambar_reused'::regclass
            """
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError('processing.gambar_reused tidak ditemukan')
        table_name, table_oid, table_filenode, toast_oid, toast_table, toast_filenode = row
        indexes: list[dict[str, Any]] = []
        if int(toast_oid or 0) > 0:
            cur.execute(
                """
                SELECT indexrelid::regclass::text,
                       indexrelid::bigint,
                       pg_relation_filenode(indexrelid)::bigint,
                       pg_relation_filepath(indexrelid)
                FROM pg_index
                WHERE indrelid = %s
                ORDER BY indexrelid::regclass::text
                """,
                (int(toast_oid),),
            )
            indexes = [
                {
                    "name": str(r[0]),
                    "oid": int(r[1]),
                    "filenode": int(r[2]) if r[2] is not None else None,
                    "filepath": str(r[3] or ""),
                }
                for r in cur.fetchall()
            ]
        cur.execute("SELECT count(*)::bigint FROM processing.gambar_reused")
        row_count = int(cur.fetchone()[0])
        return {
            'table': str(table_name),
            'table_oid': int(table_oid),
            'table_filenode': int(table_filenode) if table_filenode is not None else None,
            'toast_table': str(toast_table or ''),
            'toast_oid': int(toast_oid or 0),
            'toast_filenode': int(toast_filenode) if toast_filenode is not None else None,
            'toast_indexes': indexes,
            'row_count': row_count,
        }


def _print_relation_diagnosis(rel: dict[str, Any] | None, filenode: int) -> None:
    print('-' * 78)
    print(f'[IMAGE STORAGE DIAG] corrupt_filenode={filenode}')
    if not rel:
        print('[IMAGE STORAGE DIAG] relation tidak dapat dipetakan dari pg_class.')
        return
    kind_label = {
        'i': 'INDEX',
        't': 'TOAST_TABLE',
        'r': 'TABLE',
        'm': 'MATERIALIZED_VIEW',
    }.get(rel['relkind'], rel['relkind'])
    print(
        f"[IMAGE STORAGE DIAG] relation={rel['qualified']} kind={kind_label} "
        f"oid={rel['oid']} filenode={rel['filenode']} filepath={rel['filepath']}"
    )
    if rel.get('index_parent'):
        print(f"[IMAGE STORAGE DIAG] index_parent={rel['index_parent']}")


def _reindex_specific_index(settings: Settings, rel: dict[str, Any]) -> None:
    with connection(settings, autocommit=True) as conn, conn.cursor() as cur:
        # Make this repair deterministic and avoid the parallel-worker error path.
        cur.execute('SET max_parallel_maintenance_workers = 0')
        stmt = sql.SQL('REINDEX (VERBOSE) INDEX {}.{}').format(
            sql.Identifier(rel['schema']), sql.Identifier(rel['name'])
        )
        print(f"[IMAGE STORAGE REPAIR] direct REINDEX INDEX {rel['qualified']} (parallel=0) ...", flush=True)
        cur.execute(stmt)
        print(f"[IMAGE STORAGE REPAIR] direct REINDEX INDEX {rel['qualified']} selesai.", flush=True)


def _reindex_whole_table_no_parallel(settings: Settings) -> None:
    with connection(settings, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute('SET max_parallel_maintenance_workers = 0')
        print('[IMAGE STORAGE REPAIR] retry REINDEX TABLE processing.gambar_reused (parallel=0) ...', flush=True)
        cur.execute('REINDEX (VERBOSE) TABLE processing.gambar_reused')
        print('[IMAGE STORAGE REPAIR] retry REINDEX TABLE selesai.', flush=True)


def repair_gambar_reused_indexes(settings: Settings, *, apply: bool = False) -> dict[str, Any]:
    info = inspect_gambar_reused_storage(settings)
    print('=' * 78)
    print('GAMBAR_REUSED INDEX / TOAST REPAIR V108')
    print('=' * 78)
    print(
        f"table={info['table']} rows={info['row_count']:,} "
        f"oid={info['table_oid']} filenode={info['table_filenode']}"
    )
    print(
        f"toast_table={info['toast_table'] or 'NONE'} "
        f"toast_oid={info['toast_oid'] or 'NONE'} "
        f"toast_filenode={info['toast_filenode'] or 'NONE'}"
    )
    if info['toast_indexes']:
        for idx in info['toast_indexes']:
            print(
                f"toast_index={idx['name']} oid={idx['oid']} "
                f"filenode={idx['filenode']} filepath={idx['filepath']}"
            )
    else:
        print('toast_indexes=NONE')
    print('repair_command=REINDEX TABLE processing.gambar_reused')
    print('V108 fallback: bila error menyebut filenode INDEX, reindex index itu langsung dengan parallel=0 lalu retry table.')
    if not apply:
        print('DRY-RUN: belum ada perubahan. Tambahkan --apply untuk menjalankan repair.')
        return {**info, 'applied': False}

    try:
        with connection(settings, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute('SET max_parallel_maintenance_workers = 0')
            print('[IMAGE STORAGE REPAIR] REINDEX TABLE processing.gambar_reused (parallel=0) ...', flush=True)
            cur.execute('REINDEX (VERBOSE) TABLE processing.gambar_reused')
            print('[IMAGE STORAGE REPAIR] REINDEX selesai.', flush=True)
    except Exception as exc:
        filenode = _extract_filenode(exc)
        rel = _relation_from_filenode(settings, filenode) if filenode is not None else None
        if filenode is not None:
            _print_relation_diagnosis(rel, filenode)
        print(f"[IMAGE STORAGE REPAIR] initial REINDEX gagal: {' '.join(str(exc).split())[:1200]}")

        if rel and rel.get('relkind') == 'i':
            try:
                _reindex_specific_index(settings, rel)
                _reindex_whole_table_no_parallel(settings)
            except Exception as direct_exc:
                filenode2 = _extract_filenode(direct_exc)
                rel2 = _relation_from_filenode(settings, filenode2) if filenode2 is not None else None
                if filenode2 is not None:
                    _print_relation_diagnosis(rel2, filenode2)
                raise RuntimeError(
                    'Direct index repair juga gagal. Jangan aktifkan zero_damaged_pages. '
                    'Lanjutkan salvage ke tabel baru dengan: '
                    'py run_pipeline.py rebuild-gambar-reused-storage --batch-size 32 --apply. '
                    'Tabel lama akan dipertahankan sebagai quarantine. Detail=' +
                    ' '.join(str(direct_exc).split())[:1000]
                ) from direct_exc
        else:
            kind = rel.get('relkind') if rel else 'UNKNOWN'
            qname = rel.get('qualified') if rel else f'filenode {filenode}'
            raise RuntimeError(
                f'Kerusakan bukan index yang aman direindex (relation={qname}, relkind={kind}). '
                'JANGAN gunakan zero_damaged_pages. Salvage healthy rows ke relation baru dengan: '
                'py run_pipeline.py rebuild-gambar-reused-storage --batch-size 32 --apply. '
                'Sesudah swap, recovery hanya row yang tercatat di CSV skipped dari NAS.'
            ) from exc

    after = inspect_gambar_reused_storage(settings)
    print(
        f"[IMAGE STORAGE REPAIR] VERIFY table={after['table']} "
        f"rows={after['row_count']:,} toast={after['toast_table']} "
        f"toast_filenode={after['toast_filenode']}"
    )
    return {**after, 'applied': True}
