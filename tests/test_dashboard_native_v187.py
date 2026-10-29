from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
DB_DIR = ROOT / "dashboard"


def _load_modules(monkeypatch):
    # This test deliberately performs no PostgreSQL I/O.
    psycopg2 = ModuleType("psycopg2")
    psycopg2.connect = lambda **kw: None
    extras = ModuleType("psycopg2.extras")
    extras.RealDictCursor = object
    pool = ModuleType("psycopg2.pool")
    pool.ThreadedConnectionPool = object
    monkeypatch.setitem(sys.modules, "psycopg2", psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", extras)
    monkeypatch.setitem(sys.modules, "psycopg2.pool", pool)
    monkeypatch.syspath_prepend(str(DB_DIR))
    for name in ("db", "queries"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    db = importlib.import_module("db")
    queries = importlib.import_module("queries")
    return db, queries


def test_sql_pushdown_preserves_active_and_canonical_aliases(monkeypatch):
    db, _ = _load_modules(monkeypatch)
    where, params = db._table_filter_sql(
        {"is_active", "source_group", "source_company"}, ("DAN",), True
    )
    assert "is_active=true" in where
    assert "source_company" in where
    assert "source_group" in where
    assert params[0] == ["DAN"]
    assert params[1] == ["DAN", "ELHP_DAN"]
    # No raw SQL string interpolation of user-supplied values.
    assert "DAN" not in where


def test_read_table_binds_company_before_pandas(monkeypatch):
    db, _ = _load_modules(monkeypatch)
    monkeypatch.setattr(db, "table_exists", lambda name: True)
    monkeypatch.setattr(db, "get_columns", lambda name: ["is_active", "source_group", "source_company"])
    collected = []
    def fake_read(sql, params, cache_key):
        collected.append((sql, params, cache_key))
        import pandas as pd
        return pd.DataFrame([{"source_company":"DAN"}])
    monkeypatch.setattr(db, "read_df", fake_read)
    df = db.read_table("elhp_gt_group_summary_cleaned", source_companies=["DAN"])
    assert len(df) == 1
    assert 'FROM "gold"."elhp_gt_group_summary_cleaned"' in collected[0][0]
    assert collected[0][1][0] == ["DAN"]
    assert 'company=DAN' in collected[0][2]


def test_company_filter_does_not_affect_all_month_membership(monkeypatch):
    db, queries = _load_modules(monkeypatch)
    monkeypatch.setattr(queries, "table_exists", lambda name: True)
    captured = []
    import pandas as pd
    def fake_read(table, **kwargs):
        captured.append((table, kwargs.get("source_companies")))
        if "monthly" in table:
            return pd.DataFrame([
                {"source_company":"DAN", "source_group":"DAN", "group_id":5, "month":"2026-05-01", "usage_count":2, "estate":"PU1"},
                {"source_company":"DAN", "source_group":"DAN", "group_id":5, "month":"2026-09-01", "usage_count":3, "estate":"PU1"}
            ])
        return pd.DataFrame()
    monkeypatch.setattr(queries, "read_table", fake_read)
    result = queries.load_dashboard_monthly("ELHP", "DAN")
    assert len(result) == 2
    assert set(result["month"]) == {"2026-05", "2026-09"}
    assert captured[0][1] == ("DAN",)


def test_company_options_do_not_query_database(monkeypatch):
    db, queries = _load_modules(monkeypatch)
    monkeypatch.setattr(queries, "read_df", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("SQL must not run")))
    assert [x["value"] for x in queries.get_company_options("ELHP")] == ["DAN", "DWT"]
    assert [x["value"] for x in queries.get_company_options("HARMONY")] == ["PWP", "DIN"]


def test_hotstore_first_falls_back_to_nas_on_miss(monkeypatch):
    db, _ = _load_modules(monkeypatch)
    db.NAS_IMAGE_FIRST = False
    db.ALLOW_SILVER_IMAGE_FALLBACK = False
    calls = []
    monkeypatch.setattr(db, "table_exists", lambda name, schema=None: False)
    monkeypatch.setattr(db, "table_exists_uncached", lambda name, schema=None: False)
    monkeypatch.setattr(db, "_fetch_nas_image_bytes", lambda candidates, requested, base: (calls.append("nas") or (b"img", "image/jpeg", "a.jpg")))
    result = db.fetch_image_bytes("a.jpg")
    assert result[0] == b"img"
    assert calls == ["nas"]


def test_native_bootstrap_keeps_heavy_detail_lazy():
    app = (DB_DIR / "app.py").read_text(encoding="utf-8")
    ast.parse(app)
    assert 'LAZY_PAGE_FIGURES = os.getenv("LAZY_PAGE_FIGURES", "true")' in app
    assert 'needs_all_detail = is_harmony and active_page_name in {"overview", "similarity"}' in app
    assert 'if active_page_name == "groups":' in app


def test_hotstore_hit_does_not_access_nas(monkeypatch):
    db, _ = _load_modules(monkeypatch)
    db.NAS_IMAGE_FIRST = False
    db.ALLOW_SILVER_IMAGE_FALLBACK = False
    monkeypatch.setattr(db, "table_exists_uncached", lambda table, schema=None: False)
    monkeypatch.setattr(db, "table_exists", lambda table, schema=None: table == db.TABLE_BYTEA_RECOVERY)
    def fake_hot(schema, table, sql, params=None):
        if table == db.TABLE_BYTEA_RECOVERY:
            return {"image_bytes":b"local-hot", "mime_type":"image/jpeg", "file_name":"foo.jpg"}
        return None
    monkeypatch.setattr(db, "_safe_image_read_one", fake_hot)
    monkeypatch.setattr(db, "_fetch_nas_image_bytes", lambda *a: (_ for _ in ()).throw(AssertionError("NAS should not be read")))
    assert db.fetch_image_bytes("foo.jpg")[0] == b"local-hot"


def test_bunch_pay_fallback_queries_cleaned_harmony_view(monkeypatch):
    db, queries = _load_modules(monkeypatch)
    monkeypatch.setattr(queries, "table_exists", lambda table: True)
    monkeypatch.setattr(queries, "get_columns", lambda table: ["group_id","bunch_pay","source_company","source_group","is_active"])
    import pandas as pd
    queries_read=[]
    def fake_query(sql, params, cache_key):
        queries_read.append((sql,params))
        return pd.DataFrame({"group_id":[10], "bunch_pay":[25]})
    monkeypatch.setattr(queries,"read_df",fake_query)
    out=queries.load_group_bunch_pay_aggregate("HARMONY","PWP",[10])
    assert '"gold"."reused_photo_detail_transaction"' in queries_read[0][0]
    assert "PWP" in str(queries_read[0][1])
    assert out.iloc[0]["group_key"] == "HARMONY|10"
    assert out.iloc[0]["bunch_pay"] == 25
