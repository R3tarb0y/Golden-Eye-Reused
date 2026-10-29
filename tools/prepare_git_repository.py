#!/usr/bin/env python3
"""Export Golden Eye code-only snapshot without modifying the working project.

Dry-run is the default. No network access and no git operations are performed.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
from pathlib import Path
import shutil
import sys
from collections import Counter

# Only code/config folders and known root entrypoints. Never package a data directory.
CODE_DIRS = {"src", "dashboard", "control_center", "sql", "tests", "deploy", "tools", "scripts"}
DOCS_ALLOW = {
    "docs/ARCHITECTURE.md", "docs/RUNBOOK.md", "docs/SOURCE_MAPPING.md",
    "docs/MIGRATION_MAP.md", "docs/GAMBAR_REUSED_RUNBOOK.md",
}
ALLOWED_SUFFIXES = {".py", ".pyi", ".sql", ".js", ".css", ".html", ".yaml", ".yml",
                    ".toml", ".json", ".md", ".txt", ".ps1", ".bat", ".sh", ".cfg", ".ini"}
APPROVED_ASSETS = {"dashboard/assets/dsn_group_logo.png"}
IGNORED_PARTS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
    "logs", "log", "outputs", "output", "exports", "results", "reports", "tmp", "temp",
    "cache", "thumb_cache", "models", "weights", "datasets", "artifacts", "backups", ".idea", ".vscode",
    "htmlcov", ".ruff_cache", ".tox", "data", "archive", "archives", "downloads", "secrets"
}
ROOT_FILE_ALLOW = {"requirements.txt", "README.md", ".gitignore", ".gitattributes", ".dockerignore"}
SENSITIVE_NAME_MARKERS = ("credential", "password", "private_key", "client_secret", "secret_key", "service-account", "service_account")
MAX_FILE_BYTES = 5 * 1024 * 1024


def allowed(rel: Path, size: int) -> tuple[bool, str]:
    s = rel.as_posix()
    parts = tuple(p.lower() for p in rel.parts)
    lower = s.lower()
    if any(p in IGNORED_PARTS for p in parts):
        return False, "directory excluded"
    if any(p.startswith(("demo_", "render_", "transport_demo", "transport_render")) for p in parts):
        return False, "generated demo/render"
    if any(p.startswith(".") and p not in {".gitignore", ".gitattributes", ".dockerignore"} for p in parts):
        return False, "hidden/runtime file"
    if rel.name.lower().startswith(".env") or lower.endswith((".bak", ".backup", ".db", ".sqlite3", ".sql.gz")):
        return False, "local configuration or database backup"
    if any(m in lower for m in SENSITIVE_NAME_MARKERS):
        return False, "potential secret filename"
    if size > MAX_FILE_BYTES:
        return False, "larger than 5 MiB"
    if len(rel.parts) == 1:
        if lower in {".env.example", ".env.sample", ".env.template"}:
            return False, "use sanitized template bundled with Git tools"
        if lower in {x.lower() for x in ROOT_FILE_ALLOW}:
            return True, "root manifest"
        if rel.suffix.lower() in {".py", ".ps1", ".sql", ".bat"}:
            return True, "root executable"
        return False, "old root docs/output excluded"
    if lower in {x.lower() for x in DOCS_ALLOW}:
        return True, "selected documentation"
    if parts[0] not in CODE_DIRS:
        return False, "not an approved code folder"
    if lower in APPROVED_ASSETS:
        return True, "required dashboard asset (private repository recommended)"
    if rel.suffix.lower() not in ALLOWED_SUFFIXES:
        return False, "non-source extension"
    return True, "source/config"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=".", help="Original Golden Eye folder")
    parser.add_argument("--output", default="../golden_eye_git", help="NEW separate output folder")
    parser.add_argument("--execute", action="store_true", help="Copy selected files; default is preview only")
    parser.add_argument("--max-lines", type=int, default=35)
    args = parser.parse_args()
    src = Path(args.source).resolve()
    dest = Path(args.output).resolve()
    if not src.is_dir():
        parser.error(f"Source not found: {src}")
    if dest == src or src in dest.parents or dest in src.parents and dest != src.parent:
        parser.error("Output cannot be inside source or an ancestor of source. Use a separate sibling folder.")
    if args.execute and dest.exists() and any(dest.iterdir()):
        parser.error("Output already exists and is not empty. Choose a NEW folder. Never overwrite an existing Git repo.")

    keep: list[tuple[Path,Path]] = []
    omitted = Counter()
    for base, folders, files in os.walk(src):
        b = Path(base)
        folders[:] = [f for f in folders if f.lower() not in IGNORED_PARTS
                       and not f.lower().startswith(("demo_", "render_", "transport_demo", "transport_render"))]
        for name in files:
            source_file = b / name
            rel = source_file.relative_to(src)
            if source_file.is_symlink():
                omitted["symlink"] += 1
                continue
            try:
                ok, why = allowed(rel, source_file.stat().st_size)
            except OSError:
                omitted["unreadable"] += 1
                continue
            if ok:
                keep.append((source_file, rel))
            else:
                omitted[why] += 1
    print(f"SOURCE: {src}\nDESTINATION: {dest}\nMODE: {'EXECUTE' if args.execute else 'DRY RUN'}")
    print(f"INCLUDED: {len(keep)} files, {sum(a.stat().st_size for a,_ in keep)/1024/1024:.2f} MiB")
    print("EXCLUDED:", dict(omitted))
    print("SAMPLE INCLUDED:")
    for _,rel in keep[:max(0,args.max_lines)]: print("  +",rel.as_posix())
    if not args.execute:
        print("\nNo files were changed. Add --execute to create a new clean folder.")
        return 0
    dest.mkdir(parents=True,exist_ok=True)
    for source, rel in keep:
        target=dest/rel
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source,target)
    tool_root=Path(__file__).resolve().parents[1]
    template_root=Path(__file__).resolve().parent / "git_templates"
    for rel in [".gitignore", ".gitattributes", ".env.example", "README.md", "SECURITY.md", "README_GIT.md",
                ".githooks/pre-commit", "tools/git_preflight.py", "tools/prepare_git_repository.py"]:
        f=template_root/rel
        if not f.exists():
            f=tool_root/rel
        if f.exists():
            out=dest/rel
            out.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(f,out)
    # Specifically avoid copying old README from the source if it contains internal connection details.
    print("\nExport finished. No git init, commit, delete, or push was performed.")
    print("IMPORTANT: run `python tools/git_preflight.py --all-files` inside output before git init.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
