from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import generate_pdf_reports as report


def _frames():
    groups = pd.DataFrame([
        {"group_key": "g1", "group_id": "1", "usage_count": 8, "reuse_category": "Blok Berbeda"},
        {"group_key": "g2", "group_id": "2", "usage_count": 5, "reuse_category": "TPH Sama"},
        {"group_key": "g3", "group_id": "3", "usage_count": 9, "reuse_category": "Afdeling Berbeda"},
    ])
    details = pd.DataFrame([
        {"group_key": "g3", "group_id": "3", "tanggal": "2026-06-30", "image_name": "old.jpg", "foto_dt": "old_t.jpg", "similarity": "99%", "pemanen": "A", "kerani_panen": "K1"},
        {"group_key": "g1", "group_id": "1", "tanggal": "2026-07-02", "image_name": "a.jpg", "foto_dt": "ta.jpg", "similarity": "94%", "pemanen": "A", "kerani_panen": "K1"},
        {"group_key": "g1", "group_id": "1", "tanggal": "2026-07-05", "image_name": "b.jpg", "foto_dt": "tb.jpg", "similarity": "95%", "pemanen": "A", "kerani_panen": "K1"},
        {"group_key": "g1", "group_id": "1", "tanggal": "2026-07-07", "image_name": "c.jpg", "foto_dt": "tc.jpg", "similarity": "92%", "pemanen": "A", "kerani_panen": "K1"},
        {"group_key": "g2", "group_id": "2", "tanggal": "2026-07-31", "image_name": "z.jpg", "foto_dt": "tz.jpg", "similarity": "96%", "pemanen": "B", "kerani_panen": "K1"},
    ])
    return groups, details


def test_highlights_use_latest_month_and_two_roles(monkeypatch):
    groups, details = _frames()
    monkeypatch.setattr(report, "fetch_image_bytes", None)
    highlights, month = report.build_latest_month_highlights(groups, details)
    assert month == "2026-07"
    assert highlights[0]["_highlight_label"] == "SERING MUNCUL"
    assert highlights[0]["group_key"] == "g1"
    assert highlights[0]["_month_usage"] == 3
    assert highlights[1]["_highlight_label"] == "TRANSAKSI TERBARU"
    assert highlights[1]["group_key"] == "g2"
    assert highlights[1]["tanggal"] == "2026-07-31"


def test_transport_reuse_latest_month_only(monkeypatch):
    groups, details = _frames()
    monkeypatch.setattr(report, "fetch_image_bytes", None)
    pairs = report.build_similarity_candidates(groups, details, reused_only=True)
    assert pairs
    assert pairs[0][0]["group_key"] == "g2"
    assert all(str(row[0]["tanggal"]).startswith("2026-07") for row in pairs)
    assert all(float(row[0]["_sim_sort"]) >= 93 for row in pairs)


def test_top_kerani_uses_case_count_not_group_or_category():
    _, details = _frames()
    details = details.copy()
    details["oph"] = ["OPH-OLD", "OPH-001", "OPH-001", "OPH-002", "OPH-003"]
    # OPH-001 appears twice in the same reused group but is one Case.
    summary = report.personnel_case_summary(details, ["kerani_panen"], top=10)
    assert summary.iloc[0]["label"] == "K1"
    assert int(summary.iloc[0]["case_count"]) == 4
    assert int(summary.iloc[0]["total_usage"]) == 5


def test_top_kerani_case_count_does_not_depend_on_group_key():
    details = pd.DataFrame([
        {"group_key": "g1", "oph": "OPH-A", "kerani_panen": "K1"},
        {"group_key": "g2", "oph": "OPH-A", "kerani_panen": "K1"},
        {"group_key": "g2", "oph": "OPH-B", "kerani_panen": "K1"},
        {"group_key": "g9", "oph": "OPH-C", "kerani_panen": "K2"},
    ])
    summary = report.personnel_case_summary(details, ["kerani_panen"], top=10).set_index("label")
    assert int(summary.loc["K1", "case_count"]) == 2
    assert int(summary.loc["K1", "total_usage"]) == 3
    assert int(summary.loc["K2", "case_count"]) == 1
