from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd

# Reuse the exact Visual Audit query layer used by the analytical dashboard.
# Keep this adapter inside Control Center so Visual Audit can move out of Dash UI
# without duplicating business/SAP verification logic.
_DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
if str(_DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD_DIR))

from queries import load_visual_reused_audit_groups, load_visual_reused_audit_monthly  # noqa: E402


def _clean_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    return [
        {str(k): _clean_scalar(v) for k, v in row.items()}
        for row in df.to_dict("records")
    ]


def build_visual_audit_payload(
    system: str | None = None,
    companies: list[str] | None = None,
    year: int = 2026,
    months: list[str] | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    company_arg: Any = companies or None
    monthly = load_visual_reused_audit_monthly(system, company_arg, int(year))
    selected = [str(x).strip() for x in (months or []) if str(x).strip()]
    if selected and not monthly.empty:
        monthly = monthly[monthly["month"].astype(str).isin(selected)].copy()

    groups = load_visual_reused_audit_groups(
        system,
        company_arg,
        int(year),
        selected or None,
        limit=max(1, int(limit)),
    )

    def _sum(col: str) -> int:
        if monthly.empty or col not in monthly.columns:
            return 0
        return int(pd.to_numeric(monthly[col], errors="coerce").fillna(0).sum())

    summary = {
        "raw_visual_usage": _sum("raw_visual_usage"),
        "estate_visual_usage": _sum("estate_visual_usage"),
        "visual_usage": _sum("visual_usage"),
        "official_dashboard_usage": _sum("official_dashboard_usage"),
        "unverified_oph": _sum("unverified_oph"),
        "not_in_sap": _sum("not_in_sap"),
        "wrong_estate": _sum("wrong_estate"),
        "policy_bypass_usage": _sum("policy_bypass_usage"),
        "dashboard_delta": _sum("dashboard_delta"),
        "official_delta": _sum("official_delta"),
        "official_over_sap": _sum("official_over_sap"),
    }

    checks: list[dict[str, str]] = []
    if summary["official_over_sap"] > 0:
        checks.append({"tone": "danger", "text": f"FAIL official usage melebihi SAP sebanyak {summary['official_over_sap']:,} usage"})
    if summary["dashboard_delta"] != 0:
        checks.append({"tone": "warning", "text": f"CHECK main visual dashboard delta = {summary['dashboard_delta']:+,}"})
    if summary["official_delta"] != 0:
        checks.append({"tone": "warning", "text": f"CHECK SAP verified delta = {summary['official_delta']:+,}"})
    if summary["policy_bypass_usage"] > 0:
        checks.append({"tone": "info", "text": f"INFO {summary['policy_bypass_usage']:,} usage tampil melalui policy bypass"})
    if summary["wrong_estate"] > 0:
        checks.append({"tone": "info", "text": f"INFO {summary['wrong_estate']:,} out-of-scope usage dikeluarkan dari main dashboard"})
    if not checks:
        checks.append({"tone": "success", "text": "PASS visual dashboard lengkap; policy display aktif; SAP verified tetap sebagai pembanding"})

    return {
        "system": str(system or "ALL").upper(),
        "companies": companies or [],
        "year": int(year),
        "months": selected,
        "summary": summary,
        "checks": checks,
        "monthly": _records(monthly),
        "groups": _records(groups),
        "group_count": int(len(groups)),
        "group_limit": max(1, int(limit)),
    }
