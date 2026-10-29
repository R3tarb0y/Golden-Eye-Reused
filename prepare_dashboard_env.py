"""Prepare a least-privilege dashboard-only environment file without DB60/MSSQL credentials."""
from __future__ import annotations
import sys
from pathlib import Path
from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parent
INPUT=ROOT/".env"
OUTPUT=ROOT/".env.dashboard"
ALLOWED_PREFIXES=(
    "PG_", "APP_", "TABLE_", "SAP_KPI_", "DASHBOARD_", "DATA_CACHE_", "QUERY_CACHE_",
    "SOURCE_COMPANY_MAP", "DEFAULT_", "TOP_N", "GROUP_TABLE_", "SIMILARITY_",
    "HARMONY_", "DIN_", "INCLUDE_ELHP_", "MIN_VISIBLE_", "FRAME_GALLERY_",
    "THUMB_", "MEDIA_GATEWAY_", "MODEL_VERSION", "COMPANY_ORDER", "FILTER_OPTION_",
    "PRELOAD_DATA_", "REFRESH_SECONDS", "ALLOW_SILVER_IMAGE_", "NAS_IMAGE_", "GEO_",
)
EXPLICIT={"SAP_COMPANY_CODE_MAP", "SAP_ESTATE_SCOPE_MAP", "SAP_PRFNR_MAP", "LOG_SQL_TIMING"}


def main():
    if not INPUT.is_file():
        raise SystemExit(".env tidak ditemukan pada project root")
    if OUTPUT.exists():
        raise SystemExit(".env.dashboard sudah ada. Hapus/backup manual sebelum generate ulang.")
    pairs=dotenv_values(INPUT)
    picked=[]
    for key,value in pairs.items():
        if value is None:
            continue
        if key in EXPLICIT or key.startswith(ALLOWED_PREFIXES):
            if key in {"PG_HOST"}:
                continue  # Compose forces Docker's PG_HOST to host.docker.internal
            val=str(value)
            if "\n" in val or "\r" in val:
                raise SystemExit("Multiline value not supported in .env.dashboard: "+key)
            picked.append((key,val))
    if not any(k=="PG_PASSWORD" and v for k,v in picked):
        raise SystemExit("PG_PASSWORD wajib di .env")
    if not any(k=="MEDIA_GATEWAY_TOKEN" and len(v)>=24 for k,v in picked):
        raise SystemExit("MEDIA_GATEWAY_TOKEN minimal 24 karakter wajib di .env")
    OUTPUT.write_text("# Dashboard-only (generated). Do not commit.\n"+"".join(f"{k}={v}\n" for k,v in picked),encoding="utf-8")
    print(f"Created {OUTPUT} with {len(picked)} variables (not printing secrets).")
    print("Verify no ETL/DB60/MSSQL creds before deployment.")

if __name__=="__main__":main()
