from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


REQUIRED_VALUES = {
    "REVIEW_THRESHOLD": "90.0",
    "REUSED_THRESHOLD": "93.0",
    "STRICT_GROUP_VALIDATION": "true",
    "VALIDATION_GROUP_MEAN_SIMILARITY": "90.0",
    "VALIDATION_MEMBER_MEAN_SIMILARITY": "93.0",
    "VALIDATION_REPRESENTATIVE_SIMILARITY": "93.0",
    "DUPLICATE_REQUIRE_STRUCTURAL_EVIDENCE": "true",
    "DUPLICATE_NEAR_EXACT_SIMILARITY": "96.0",
    "DUPLICATE_MAX_PHASH_DISTANCE": "10",
    "DUPLICATE_MAX_EDGEHASH_DISTANCE": "12",
    "DUPLICATE_GEOMETRIC_SIMILARITY": "93.0",
    "DUPLICATE_MIN_EDGEVEC_SIMILARITY": "0.80",
    "DUPLICATE_MIN_ORB_INLIERS": "12",
    "DUPLICATE_MIN_ORB_RATIO": "0.18",
    "DUPLICATE_REQUIRE_HASH_SUPPORT": "true",
    "DUPLICATE_GEOMETRIC_MAX_HASH_DISTANCE": "18",
    "SAP_SCOPE_MODE": "company",
}


def _key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key = stripped.split("=", 1)[0].strip()
    if not key or not key.replace("_", "").isalnum():
        return None
    return key


def build_clean_env(lines: list[str]) -> tuple[list[str], int, list[str]]:
    """Keep the last value for normal keys and enforce the v1.3.16 contract."""
    last_index: dict[str, int] = {}
    for index, line in enumerate(lines):
        key = _key(line)
        if key:
            last_index[key] = index

    output: list[str] = []
    removed_duplicates = 0
    changed: list[str] = []
    emitted: set[str] = set()
    for index, line in enumerate(lines):
        key = _key(line)
        if key and last_index[key] != index:
            removed_duplicates += 1
            continue
        if key in REQUIRED_VALUES:
            replacement = f"{key}={REQUIRED_VALUES[key]}\n"
            if line.rstrip("\r\n") != replacement.rstrip("\n"):
                changed.append(key)
            output.append(replacement)
            emitted.add(key)
        else:
            output.append(line if line.endswith(("\n", "\r")) else line + "\n")

    missing = [key for key in REQUIRED_VALUES if key not in emitted]
    if missing:
        if output and output[-1].strip():
            output.append("\n")
        output.append("# Golden Eye v1.3.16 universal strict validation\n")
        for key in missing:
            output.append(f"{key}={REQUIRED_VALUES[key]}\n")
            changed.append(key)
    return output, removed_duplicates, changed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deduplicate .env and apply Golden Eye v1.3.16 universal settings"
    )
    parser.add_argument("--env", default=".env", help="Path .env (default: .env)")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Simpan perubahan; tanpa flag ini hanya dry-run",
    )
    args = parser.parse_args()

    env_path = Path(args.env).resolve()
    if not env_path.is_file():
        raise SystemExit(f"File tidak ditemukan: {env_path}")
    original = env_path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    cleaned, removed_duplicates, changed = build_clean_env(original)

    print("=" * 78)
    print("GOLDEN EYE v1.3.16 ENV UPGRADE")
    print("=" * 78)
    print(f"env                : {env_path}")
    print(f"duplicate removed  : {removed_duplicates}")
    print(f"keys changed/added : {len(set(changed))}")
    if changed:
        print("keys               : " + ", ".join(sorted(set(changed))))
    print("SAP scope          : company (SAP_PRFNR_MAP lama dipertahankan)")

    if not args.apply:
        print("DRY-RUN: ulangi dengan --apply untuk menyimpan.")
        return 0

    backup = env_path.with_name(env_path.name + ".before_v1_3_16")
    if not backup.exists():
        shutil.copy2(env_path, backup)
    temporary = env_path.with_name(env_path.name + ".v1_3_16.tmp")
    temporary.write_text("".join(cleaned), encoding="utf-8", newline="")
    os.replace(temporary, env_path)
    print(f"APPLIED            : {env_path}")
    print(f"backup             : {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
