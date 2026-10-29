from __future__ import annotations

import argparse
from datetime import date, timedelta

from brondolan_audit.reporting import ReportFilters, generate_report


def _split(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _previous_month() -> tuple[int, int]:
    first = date.today().replace(day=1)
    last_prev = first - timedelta(days=1)
    return last_prev.year, last_prev.month


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate report audit brondolan PDF + Excel")
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--previous-month", action="store_true")
    parser.add_argument("--pt", default="")
    parser.add_argument("--estate", default="")
    parser.add_argument("--progeny", default="")
    parser.add_argument("--status", default="")
    parser.add_argument("--evidence", type=int, default=10, help="Jumlah foto extreme per PT (default 10)")
    parser.add_argument("--no-images", action="store_true")
    parser.add_argument("--output-root", default=None)
    args = parser.parse_args()

    if args.previous_month or (args.year is None and args.month is None):
        year, month = _previous_month()
    else:
        if args.year is None or args.month is None:
            parser.error("Gunakan --year dan --month bersama-sama, atau --previous-month")
        year, month = args.year, args.month
    if not 1 <= month <= 12:
        parser.error("--month harus 1..12")

    filters = ReportFilters(
        bukrs=_split(args.pt), estates=_split(args.estate), progenies=_split(args.progeny),
        years=(year,), months=(month,), statuses=_split(args.status),
    )
    result = generate_report(
        filters, output_root=args.output_root, evidence_count=max(0, args.evidence),
        include_images=not args.no_images,
    )
    print("=" * 88)
    print("BRONDOLAN AUTOMATIC REPORT - SELESAI")
    print("=" * 88)
    print(f"Scope      : {filters.scope_label()}")
    print(f"Source rows: {result.source_rows:,}")
    print(f"Processed  : {result.processed_rows:,}")
    print(f"Evidence   : {result.evidence_rows:,} total (maks. {max(0, args.evidence)} per PT)\nBulan AI   : {result.evidence_period}")
    print(f"PDF        : {result.pdf_path}")
    print(f"Excel      : {result.xlsx_path}")
    print(f"Manifest   : {result.manifest_path}")


if __name__ == "__main__":
    main()
