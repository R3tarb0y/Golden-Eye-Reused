"""Read-only CLI dashboard load profiler. No BYTEA or full image reads."""
from __future__ import annotations
import argparse
import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parent
load_dotenv(ROOT/".env")
sys.path.insert(0,str(ROOT/"dashboard"))
from queries import load_groups, load_dashboard_monthly, load_details, load_harmony_transaction_validation


def profile(label, fn):
    begin=time.perf_counter()
    data=fn()
    duration=(time.perf_counter()-begin)*1000
    mb=data.memory_usage(index=True,deep=True).sum()/(1024*1024) if hasattr(data,"memory_usage") else 0
    print(f"{label:<28} {duration:>10,.0f} ms | rows={len(data):>10,} | DataFrame={mb:>7.1f} MiB",flush=True)


def main():
    p=argparse.ArgumentParser(description="Golden Eye dashboard DB and Pandas benchmark (read-only)")
    p.add_argument("--system",choices=["HARMONY","ELHP"],default="ELHP")
    p.add_argument("--company",choices=["PWP","DIN","DAN","DWT"],default="DAN")
    p.add_argument("--detail",action="store_true",help="Read full detail metadata; may be expensive")
    p.add_argument("--transactions",action="store_true",help="Run Harmony transaction join; may be expensive")
    args=p.parse_args()
    print("[GOLDEN EYE PERF] START read-only - cold pass")
    tasks=[("load_groups",lambda:load_groups(args.system,args.company)),("monthly_aggregate",lambda:load_dashboard_monthly(args.system,args.company))]
    if args.detail: tasks.append(("full_detail_metadata",lambda:load_details(args.system,args.company)))
    if args.transactions: tasks.append(("panen_vs_transport",lambda:load_harmony_transaction_validation([args.company],2026)))
    for name,fn in tasks:profile(name,fn)
    print("[GOLDEN EYE PERF] WARM pass (cache)")
    for name,fn in tasks:profile(name,fn)

if __name__=="__main__":main()
