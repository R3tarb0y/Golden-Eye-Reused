"""CLI, read-only: PYTHON hot_evidence_audit.py [--output report.json]."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src'))

from control_center.hot_evidence_audit import hot_evidence_report


def main() -> int:
    p=argparse.ArgumentParser(description='Audit canonical Golden Eye BYTEA vs Gold and KT Before; never deletes')
    p.add_argument('--output',default='hot_evidence_audit.json')
    a=p.parse_args()
    r=hot_evidence_report()
    output=Path(a.output).resolve()
    output.write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')
    print('='*72)
    print('HOT EVIDENCE AUDIT - READ ONLY')
    print('physical_bytes=', r['physical_bytes'])
    for category in r['summary']:
        print(f"{category['category']:30} {category['rows']:>12,} rows {category['logical_bytes'] / (1024**3):>10,.2f} GiB logical")
    print('Report:',output)
    print(r['note'])
    return 0

if __name__=='__main__':
    raise SystemExit(main())
