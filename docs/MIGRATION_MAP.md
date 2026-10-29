# Pemetaan file lama ke repository baru

| Lampiran lama | Peran | Pengganti DB-first |
|---|---|---|
| `Pasted code(129).py` | ResNet18, threshold review/reused, Union-Find, Excel | `features.py`, `matcher.py`, tabel candidate |
| `Pasted code (2)(15).py` | pHash, edge, HSV, ORB, recluster/split | `validator.py`, validation audit, quarantine |
| `Pasted code (3)(7).py` | GT dari group + download report + transport | `context.py`, `publisher.py`, Gold detail/summary |
| `Pasted text (4)(1).txt` | dashboard HTML + Excel/folder + SQL Server | dashboard Dash membaca compatibility view Gold |
| ZIP dashboard saat ini | dashboard PostgreSQL dari tabel hasil copy | folder `dashboard/`, metadata dipindahkan ke `gold` |

Tidak ada file proses yang memakai `pd.read_excel`, `pd.read_csv`, `os.listdir`, atau path `C:\...`. Compatibility view mempertahankan nama tabel lama agar kode visual dashboard tidak perlu ditulis ulang seluruhnya.

