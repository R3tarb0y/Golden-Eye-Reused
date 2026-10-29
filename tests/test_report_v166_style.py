from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REUSED = ROOT / "dashboard" / "generate_pdf_reports.py"
TRANSPORT = ROOT / "dashboard" / "generate_transport_validation_report.py"


def test_reportlab_typography_is_bold_only():
    text = REUSED.read_text(encoding="utf-8") + TRANSPORT.read_text(encoding="utf-8")
    assert '"Helvetica"' not in text
    assert '"Helvetica-Oblique"' not in text
    assert '"Helvetica-Bold"' in text


def test_label_background_is_consistent_not_red_blue():
    text = REUSED.read_text(encoding="utf-8")
    assert 'NAVY = colors.HexColor("#003B7A")' in text
    assert 'LABEL_BG = NAVY' in text
    assert 'RED = LABEL_BG' in text
    assert 'SOFT_RED = SOFT_BLUE_2' in text
    assert 'colors.HexColor("#C91F2B")' not in text


def test_v170_category_and_status_charts_use_distinct_colors():
    reused = REUSED.read_text(encoding="utf-8")
    transport = TRANSPORT.read_text(encoding="utf-8")
    assert 'MONTH_DISTINCT_PALETTE = [' in reused
    assert 'TRANSPORT_STATUS_PALETTE = {' in reused
    # Top Reused by Month and category donuts use colors rather than hatch as primary distinction.
    assert 'palette = MONTH_DISTINCT_PALETTE' in reused
    assert 'colors=[MONTH_DISTINCT_PALETTE[i % len(MONTH_DISTINCT_PALETTE)]' in reused
    assert 'wedge.set_hatch' not in reused
    # Transport trend remains status-colored.
    assert 'common.TRANSPORT_STATUS_PALETTE["SAMA"]' in transport
    assert 'common.TRANSPORT_STATUS_PALETTE["BERBEDA"]' in transport
    assert 'common.TRANSPORT_STATUS_PALETTE["TIDAK ADA TRANSPORT"]' in transport


def test_reused_transport_similarity_has_only_three_business_statuses_and_83_note():
    text = REUSED.read_text(encoding="utf-8")
    assert '"TIDAK ADA TRANSPORT", "SAMA", "BERBEDA"' in text
    assert '"SKOR TIDAK TERSEDIA"' not in text
    assert 'SAMA = similarity >= 83%' in text
    assert 'BERBEDA = similarity < 83%' in text
    assert 'order_map = {"TIDAK ADA TRANSPORT": 1, "SAMA": 2, "BERBEDA": 3}' in text


def test_transport_monthly_trend_remains_per_pt():
    text = TRANSPORT.read_text(encoding="utf-8")
    assert 'Monthly Trend is intentionally separated per PT' in text
    assert 'filter_scope(rows, company="PWP")' in text
    assert 'filter_scope(rows, company="DIN")' in text
