from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from golden_eye.config import Settings
from golden_eye.db import connection, fetch_all_dict, ident, qname, table_columns, table_exists
from golden_eye.publisher import (
    _resolve_sap_prfnr_scopes,
    _sap_company_column,
    _load_sap_count_snapshots,
)


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit read-only SAP company/estate scope dan DISTINCT OPH Golden Eye v103."
    )
    parser.add_argument("--source-group", default="ALL")
    parser.add_argument("--outside-limit", type=int, default=30)
    args = parser.parse_args()

    settings = Settings()
    requested = clean(args.source_group).upper() or "ALL"
    known = ["PWP", "DIN", "DAN", "DWT"]
    sources = known if requested == "ALL" else [requested]

    with connection(settings) as conn:
        if not table_exists(conn, settings.sap_schema, settings.sap_table):
            raise RuntimeError(f"SAP table tidak ditemukan: {settings.sap_schema}.{settings.sap_table}")
        columns = table_columns(conn, settings.sap_schema, settings.sap_table)
        cmap = {column.lower(): column for column in columns}
        oph_col = next((cmap[name] for name in ("oph", "harvest_ticket", "tiket_panen", "no_bcc") if name in cmap), None)
        prfnr_col = next((cmap[name] for name in ("prfnr", "estate", "estate_name") if name in cmap), None)
        date_col = next((cmap[name] for name in ("budat", "posting_date", "period_date") if name in cmap), None)
        company_col = _sap_company_column(cmap)
        print("=" * 90)
        print("SAP SCOPE AUDIT v103")
        print("=" * 90)
        print(f"table          : {settings.sap_schema}.{settings.sap_table}")
        print(f"year           : {settings.sap_period_year}")
        print(f"company column : {company_col or 'NOT_FOUND'}")
        print(f"estate column  : {prfnr_col or 'NOT_FOUND'}")
        print(f"OPH column     : {oph_col or 'NOT_FOUND'}")
        print(f"date column    : {date_col or 'NOT_FOUND'}")
        if not oph_col or not prfnr_col:
            raise RuntimeError("Kolom OPH/PRFNR SAP tidak ditemukan.")

        rules = {source: settings.sap_prfnr_values(source) for source in sources}
        scopes = _resolve_sap_prfnr_scopes(conn, settings, rules)
        snapshots = _load_sap_count_snapshots(conn, settings, scopes)
        for source in sources:
            total, monthly, estate, estate_monthly, evaluated = snapshots.get(
                source, (None, {}, {}, {}, False)
            )
            print("\n" + "-" * 90)
            print(f"{source} | company={settings.sap_company_code(source)} | evaluated={evaluated}")
            print(f"scope exact : {list(scopes.get(source) or ())}")
            print(f"TOTAL DISTINCT OPH normalized : {total}")
            print("per estate:")
            for key, value in sorted(estate.items()):
                print(f"  {key:<40} {int(value):>12,}")
            print("per month:")
            for key, value in sorted(monthly.items()):
                print(f"  {key:<12} {int(value):>12,}")

            if not date_col:
                continue
            allowed = tuple(scopes.get(source) or ())
            if not allowed:
                continue
            params: list[Any] = [str(settings.sap_period_year)]
            where = [
                f"left(btrim(s.{ident(date_col)}::text), 4)=%s",
                f"nullif(processing.normalize_oph(s.{ident(oph_col)}::text), '') IS NOT NULL",
                f"nullif(btrim(s.{ident(prfnr_col)}::text), '') IS NOT NULL",
            ]
            if company_col:
                where.append(f"upper(btrim(s.{ident(company_col)}::text))=upper(%s)")
                params.append(settings.sap_company_code(source))
            rows = fetch_all_dict(
                conn,
                f"""
                SELECT upper(btrim(s.{ident(prfnr_col)}::text)) AS prfnr,
                       count(DISTINCT processing.normalize_oph(s.{ident(oph_col)}::text))::bigint AS distinct_oph
                FROM {qname(settings.sap_schema, settings.sap_table)} s
                WHERE {' AND '.join(where)}
                GROUP BY upper(btrim(s.{ident(prfnr_col)}::text))
                ORDER BY distinct_oph DESC, prfnr
                """,
                tuple(params),
            )
            outside = [
                row for row in rows
                if clean(row.get("prfnr")).upper() not in set(allowed)
            ]
            print(f"outside exact scope (read-only, top {args.outside_limit}):")
            if not outside:
                print("  NONE")
            for row in outside[: max(0, int(args.outside_limit))]:
                print(
                    f"  {clean(row.get('prfnr')):<40} "
                    f"{int(row.get('distinct_oph') or 0):>12,}  EXCLUDED"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
