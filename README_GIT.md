# Golden Eye — code-only Git preparation (GitHub + GitLab)

The repository is a source snapshot. It intentionally excludes data, Excel workbooks, PDF reports, geospatial shapefiles, database backups, large models, photos, caches and local secrets. Nothing is removed from the original project.

## Recommended: create a clean sibling directory

From `C:\golden_eye_db_first` after copying the `tools/` folder from the patch (nothing in the live source is overwritten):

```powershell
cd C:\golden_eye_db_first
python tools\prepare_git_repository.py --source . --output C:\golden_eye_git
python tools\prepare_git_repository.py --source . --output C:\golden_eye_git --execute
cd C:\golden_eye_git
python tools\git_preflight.py --all-files
```

The `--execute` operation refuses to overwrite an existing nonempty destination and never removes local data. It writes an entirely separate folder.

## Initialize Git only in the clean folder

```powershell
git init -b main
git add .
python tools\git_preflight.py
git status --short
git diff --cached --stat
git config core.hooksPath .githooks
git commit -m "Initial Golden Eye source snapshot"
```

Use **two independent private empty remote repositories**:

```powershell
git remote add github https://github.com/<OWNER>/golden-eye.git
git remote add gitlab https://gitlab.com/<GROUP>/golden-eye.git
git push -u github main
git push -u gitlab main
```

Replace both URL placeholders. For company GitLab, use its actual self-hosted URL. Authenticate via credential manager, SSH or a PAT stored in the credential manager; do not put tokens in remote URLs, scripts, or source files. If either remote already contains a README or default commit, reconcile the histories; **do not force-push**.

Later updates: repeat a clean export into a **new** directory and copy changed source into the Git working tree after review. Do not initialize Git inside the live production folder if it already contains data or has sensitive Git history.

## Verify what is tracked

```powershell
git ls-files
git status --short
git diff --cached --name-only
python tools\git_preflight.py
```

No Excel or data should be tracked. If a file was already tracked in an existing repo, `.gitignore` alone will not untrack it; use `git rm --cached <path>` and check history.

## Notes about runtime

This package keeps source layout (`src/golden_eye`, `dashboard`, `control_center`, `sql`, entrypoint scripts) so imports continue working. Database schemas, secrets, NAS mounts, geospatial reference files, trained weights, and operational data remain outside Git. The clean export is not a complete runnable production image without those dependencies.
