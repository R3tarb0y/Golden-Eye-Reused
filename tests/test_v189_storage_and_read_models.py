"""Offline V189 regression checks: no PostgreSQL connection or deletions."""
from __future__ import annotations

import ast
import importlib
import os
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

ROOT=Path(__file__).resolve().parents[1]


def test_v189_python_files_parse():
    for rel in ('control_center/pg_storage.py','control_center/app.py','control_center/pipeline.py',
                'dashboard_read_models.py','dashboard/db.py','dashboard/queries.py'):
        ast.parse((ROOT/rel).read_text(encoding='utf-8'))


def test_no_arbitrary_storage_drop():
    s=(ROOT/'control_center/pg_storage.py').read_text()
    assert 'DROP TABLE {}.{} RESTRICT' in s
    assert 'CASCADE' not in s.split('cur.execute(sql.SQL("DROP TABLE')[1].split('\n')[0]
    assert '_require_admin(admin_token)' in s
    assert 'image_store_all_files_corrupt_' in s
    assert 'pg_total_relation_size' in s
    assert 'pg_stat_user_tables' in s
    assert 'count(*) FROM' not in s.lower()


def test_read_models_preserve_gold_source_and_no_bytea_copy():
    source=(ROOT/'dashboard_read_models.py').read_text()
    assert "sql.Identifier('gold'), sql.Identifier(model)" in source
    assert 'CREATE TABLE {} AS SELECT * FROM {}' in source
    assert "CORE + (DETAIL if a.include_detail or detail_active else ())" in source
    assert 'active_snapshot' in source
    assert 'ON CONFLICT(source_name) DO UPDATE' in source


def test_snapshots_must_be_opted_in():
    code=(ROOT/'dashboard/db.py').read_text()
    assert 'os.getenv("DASHBOARD_READ_MODEL", "false")' in code
    assert 'resolve_dashboard_relation(table)' in code
    assert 'table:{table}:{generation}' in code


def test_normalized_cache_uses_read_model_version():
    code=(ROOT/'dashboard/queries.py').read_text()
    assert code.count('resolve_dashboard_relation(table)[1]')>=3
    assert 'resolve_dashboard_relation(table_name)[0]' in code


def test_storage_never_autoscans_with_worker_poll():
    js=(ROOT/'control_center/static/control.js').read_text()
    assert "if(b.dataset.scroll==='storage')loadPgStorage()" in js
    assert "'/api/storage/relations?limit=100'" in js
    assert 'setInterval(refresh,3500)' in js
    assert 'setInterval(loadPgStorage' not in js


def test_control_center_auto_refresh_opt_in():
    code=(ROOT/'control_center/pipeline.py').read_text()
    assert 'DASHBOARD_AUTO_REFRESH_READ_MODELS", "false"' in code
    assert 'dashboard_read_models.py' in code


def test_storage_ui_has_double_confirmation():
    code=(ROOT/'control_center/static/control.js').read_text()
    assert 'previewStorageDelete' in code
    assert 'preview_token:result.preview_token' in code
    assert 'if(typed!==relation)' in code
    assert 'X-Storage-Admin-Token' in code
