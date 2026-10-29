from __future__ import annotations

import argparse
import json

from control_center.server_diagnostics import run_silver_diagnostics


def main() -> int:
    p = argparse.ArgumentParser("Golden Eye Silver connection diagnostics")
    p.add_argument("--company", default="PWP", choices=["PWP", "DIN", "DAN", "DWT"])
    p.add_argument("--kind", default="PANEN", choices=["PANEN", "TRANSPORT"])
    args = p.parse_args()
    result = run_silver_diagnostics(args.company, args.kind)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result.get("overall") != "FAILED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
