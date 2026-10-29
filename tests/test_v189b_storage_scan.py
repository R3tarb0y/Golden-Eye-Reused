"""Offline regression for storage scan malformed %-placeholder and lazy audit."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _parameterized_inventory_sql():
    tree = ast.parse((ROOT / 'control_center/pg_storage.py').read_text(encoding='utf-8'))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'storage_report')
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'execute' and len(node.args) == 2):
            sql = node.args[0]
            if isinstance(sql, ast.Constant) and isinstance(sql.value, str) and 'FROM pg_class c' in sql.value:
                return sql.value, ast.unparse(node.args[1])
    raise AssertionError('storage inventory query not found')


def test_storage_scan_has_no_stray_percent_in_psycopg2_parameterized_sql():
    query, params = _parameterized_inventory_sql()
    assert "n.nspname NOT LIKE %s" in query
    assert params == "('pg_toast%', limit)"
    # A stray % in the query would throw on Python-style %-parameter expansion;
    # this is a conservative check, not a substitution used in production.
    assert query.count('%') == 2


def test_storage_scan_parameterized_query_has_no_raw_percent_literal():
    query, _ = _parameterized_inventory_sql()
    formatted = query % ('pg_toast%', 80)
    assert "NOT LIKE pg_toast%" in formatted
    assert 'LIMIT 80' in formatted
    assert 'pg_total_relation_size' in query
    assert 'pg_stat_user_tables' in query


def test_control_center_visual_audit_not_loaded_on_initial_page_visit():
    js = (ROOT / 'control_center/static/control.js').read_text(encoding='utf-8')
    assert 'syncVisualAuditCompanyOptions();refresh();loadVisualAudit();' not in js
    assert "if(vab)vab.onclick=loadVisualAudit" in js
    assert "'/api/storage/relations?limit=100'" in js
