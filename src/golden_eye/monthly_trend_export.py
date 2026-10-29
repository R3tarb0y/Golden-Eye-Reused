from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd


PAIR_SCOPE = [
    ("HARMONY", "PWP"),
    ("HARMONY", "DIN"),
    ("ELHP", "DAN"),
    ("ELHP", "DWT"),
]


def _project_root() -> Path:
    # .../src/golden_eye/monthly_trend_export.py -> project root
    return Path(__file__).resolve().parents[2]


def _load_report_modules():
    root = _project_root()
    dashboard_dir = root / "dashboard"
    if str(dashboard_dir) not in sys.path:
        sys.path.insert(0, str(dashboard_dir))
    import generate_pdf_reports as reused_report  # type: ignore
    import generate_transport_validation_report as transport_report  # type: ignore
    return reused_report, transport_report


def _validate_month(month: str) -> str:
    value = str(month or "").strip()
    if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", value):
        raise ValueError("--month harus format YYYY-MM, contoh 2026-08")
    return value


def _report_args() -> SimpleNamespace:
    # prepare_data() expects the same fields as the PDF report CLI.  Month is
    # intentionally empty here: export applies its own YTD cutoff after loading
    # the full source so a Monthly Trend remains a trend, not one isolated bar.
    return SimpleNamespace(
        demo=False,
        estate="",
        afdeling="",
        block="",
        month="",
        reuse_category="",
        date_category="",
        search="",
    )


def _months_from_reused_details(details: pd.DataFrame) -> set[str]:
    if details is None or details.empty:
        return set()
    if "tanggal" in details.columns:
        dt = pd.to_datetime(details["tanggal"], errors="coerce")
    elif "period_date" in details.columns:
        dt = pd.to_datetime(details["period_date"], errors="coerce")
    elif "month" in details.columns:
        return set(
            details["month"].dropna().astype(str).str[:7]
            .loc[lambda s: s.str.match(r"^20\d{2}-(0[1-9]|1[0-2])$")]
            .tolist()
        )
    else:
        return set()
    return set(dt.dropna().dt.strftime("%Y-%m").tolist())


def _filter_detail_ytd(details: pd.DataFrame, cutoff_month: str) -> pd.DataFrame:
    if details is None or details.empty:
        return pd.DataFrame() if details is None else details.copy()
    out = details.copy()
    if "tanggal" in out.columns:
        dt = pd.to_datetime(out["tanggal"], errors="coerce")
        month = dt.dt.strftime("%Y-%m")
    elif "period_date" in out.columns:
        dt = pd.to_datetime(out["period_date"], errors="coerce")
        month = dt.dt.strftime("%Y-%m")
    elif "month" in out.columns:
        month = out["month"].astype(str).str[:7]
    else:
        return out
    return out.loc[month.notna() & month.le(cutoff_month)].copy()


def _filter_month_df_ytd(month_df: pd.DataFrame, cutoff_month: str) -> pd.DataFrame:
    if month_df is None or month_df.empty:
        return pd.DataFrame(columns=["month", "company", "usage"])
    out = month_df.copy()
    m = out["month"].astype(str)
    valid = m.str.match(r"^20\d{2}-(0[1-9]|1[0-2])$")
    return out.loc[valid & m.le(cutoff_month)].copy()


def _filter_transport_ytd(rows: pd.DataFrame, cutoff_month: str, company: str) -> pd.DataFrame:
    if rows is None or rows.empty:
        return pd.DataFrame() if rows is None else rows.copy()
    out = rows.copy()
    company_series = out.get("company", pd.Series("", index=out.index)).fillna("").astype(str).str.upper()
    month_series = out.get("month", pd.Series("", index=out.index)).fillna("").astype(str)
    valid_month = month_series.str.match(r"^20\d{2}-(0[1-9]|1[0-2])$")
    return out.loc[company_series.eq(company.upper()) & valid_month & month_series.le(cutoff_month)].copy()


def export_monthly_trend_png(
    *,
    month: str | None = None,
    year: int = 2026,
    output_dir: str | Path = "exports/monthly_trend_png",
    kind: str = "all",
) -> dict[str, Any]:
    """Export final-report monthly charts as PNG snapshots.

    With --month YYYY-MM, writes one folder named YYYY-MM and plots YTD through
    that month.  Without --month, writes one folder for every month available in
    the selected year, each as a YTD snapshot through that month.
    """
    kind = str(kind or "all").strip().lower()
    if kind not in {"all", "reused", "transport"}:
        raise ValueError("kind harus all, reused, atau transport")

    if month:
        month = _validate_month(month)
        year = int(month[:4])
    if not (2000 <= int(year) <= 2100):
        raise ValueError("--year tidak valid")

    reused_report, transport_report = _load_report_modules()
    out_root = Path(output_dir).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    print("=" * 88, flush=True)
    print("GOLDEN EYE - EXPORT MONTHLY TREND PNG", flush=True)
    print("=" * 88, flush=True)
    print(
        f"scope={'single month ' + month if month else 'all available months'} "
        f"year={year} kind={kind} output={out_root}",
        flush=True,
    )
    print("mode=YTD snapshot through each output month", flush=True)

    reused_data: dict[tuple[str, str], tuple[pd.DataFrame, pd.DataFrame]] = {}
    transport_rows = pd.DataFrame()
    available_months: set[str] = set()

    if kind in {"all", "reused"}:
        args = _report_args()
        for system, company in PAIR_SCOPE:
            print(f"[LOAD REUSED] {system}/{company} ...", flush=True)
            groups, details = reused_report.prepare_data(system, company, args)
            reused_data[(system, company)] = (groups, details)
            available_months.update(_months_from_reused_details(details))
            print(
                f"[LOAD REUSED] {system}/{company} groups={len(groups):,} details={len(details):,}",
                flush=True,
            )

    if kind in {"all", "transport"}:
        print(f"[LOAD TRANSPORT] PWP,DIN year={year} ...", flush=True)
        transport_rows = transport_report.load_rows(["PWP", "DIN"], int(year), demo=False)
        transport_rows = transport_report.apply_transport_scope_rules(transport_rows)
        if not transport_rows.empty and "month" in transport_rows.columns:
            tmonths = transport_rows["month"].dropna().astype(str)
            available_months.update(
                tmonths[tmonths.str.match(r"^20\d{2}-(0[1-9]|1[0-2])$")].tolist()
            )
        print(f"[LOAD TRANSPORT] rows={len(transport_rows):,}", flush=True)

    if month:
        months = [month]
    else:
        months = sorted(m for m in available_months if m.startswith(f"{int(year):04d}-"))
        if not months:
            raise RuntimeError(f"Tidak ada bulan {year} pada data report/dashboard")

    saved: list[Path] = []
    skipped: list[str] = []

    for cutoff in months:
        month_dir = out_root / cutoff
        month_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n[EXPORT {cutoff}] folder={month_dir}", flush=True)

        if kind in {"all", "reused"}:
            for system, company in PAIR_SCOPE:
                groups, details = reused_data[(system, company)]
                details_ytd = _filter_detail_ytd(details, cutoff)
                month_df = reused_report.month_usage_summary(groups, details_ytd)
                month_df = _filter_month_df_ytd(month_df, cutoff)
                if month_df.empty:
                    skipped.append(f"{cutoff}:REUSED:{system}:{company}")
                    print(f"  SKIP REUSED {system}/{company}: no data <= {cutoff}", flush=True)
                    continue
                path = month_dir / f"REUSED_{system}_{company}_monthly_trend.png"
                reused_report.chart_monthly_pattern(month_df, path)
                saved.append(path)
                print(f"  SAVED {path.name} rows={len(month_df):,}", flush=True)

        if kind in {"all", "transport"}:
            for company in ["PWP", "DIN"]:
                rows_ytd = _filter_transport_ytd(transport_rows, cutoff, company)
                if rows_ytd.empty:
                    skipped.append(f"{cutoff}:TRANSPORT:{company}")
                    print(f"  SKIP TRANSPORT {company}: no data <= {cutoff}", flush=True)
                    continue
                path = month_dir / f"TRANSPORT_{company}_monthly_trend.png"
                transport_report._chart_month(rows_ytd, path, company)
                saved.append(path)
                print(f"  SAVED {path.name} rows={len(rows_ytd):,}", flush=True)

    print("\n" + "=" * 88, flush=True)
    print(
        f"DONE months={len(months):,} saved_png={len(saved):,} skipped={len(skipped):,}",
        flush=True,
    )
    print(f"OUTPUT_ROOT={out_root}", flush=True)
    print("=" * 88, flush=True)
    return {
        "months": months,
        "saved": [str(p) for p in saved],
        "skipped": skipped,
        "output_root": str(out_root),
    }
