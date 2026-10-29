# Gambar Reused BYTEA Hot Store

## Tujuan

`processing.gambar_reused` adalah satu-satunya sumber BYTEA untuk dashboard dan
PDF. Tabel ini hanya berisi gambar yang direferensikan Gold aktif (panen,
transport DT, dan transport FT). Silver tetap menyimpan corpus seluruh gambar
untuk pipeline, tetapi dashboard/PDF tidak membacanya.

Recovery v1/v2 tidak disalin ke tabel baru karena relation tersebut pernah
rusak. Isi `gambar_reused` selalu dibuat ulang dari file asli yang berhasil
dibaca dan di-decode.

## Rebuild jika heap/TOAST rusak secara fisik

Pesan `invalid page`, `could not read blocks`, atau kerusakan pada
`pg_toast.pg_toast_4222903` tidak dapat diperbaiki dengan `REINDEX`. `REINDEX`
hanya membangun ulang indeks; byte gambar pada heap/TOAST yang hilang harus
diambil kembali dari NAS.

Hentikan dashboard dan semua job. Pastikan seluruh root NAS dapat dibaca
sebelum menghapus tabel:

```powershell
Test-Path "H:\Data Harmony dan ELHP (Gambar Jan - Mei)\DWT"
Test-Path "H:\Data Harmony dan ELHP (Gambar Jan - Mei)\DAN"
Test-Path "H:\Data Harmony dan ELHP (Gambar Jan - Mei)\ffb_evidence_download_DIN"
Test-Path "H:\Data Harmony dan ELHP (Gambar Jan - Mei)\ffb_evidence_download"
```

Semua root yang akan dipakai wajib menghasilkan `True`. Kemudian jalankan di
DBeaver sebagai satu-satunya tindakan penghapusan:

```sql
DROP TABLE processing.gambar_reused RESTRICT;
CHECKPOINT;
```

Jangan menggunakan `CASCADE`. Jika `RESTRICT` gagal, hentikan dan periksa
dependency yang dilaporkan. Setelah drop berhasil, buat ulang tabel kosong yang
sehat:

```powershell
py run_pipeline.py init-db
```

Lalu jalankan recovery `--active-gold --apply` per source seperti bagian
berikut. Recovery DAN/DWT menerapkan ownership estate: DAN hanya `PU*`, DWT
hanya `ME*`. Proses ini tidak menjalankan MATCH, VALIDATE, atau PUBLISH dan
tidak mengganti run Gold aktif.

## Setelah dry-run lulus

Jalankan migrasi satu kali:

```powershell
cd C:\golden_eye_db_first
py run_pipeline.py init-db
```

Pastikan `.env`:

```dotenv
TABLE_BYTEA_RECOVERY=gambar_reused
ALLOW_SILVER_IMAGE_FALLBACK=false
```

Ulangi perintah dry-run masing-masing source dengan menambahkan `--apply`.
Contoh DWT:

```powershell
py recover_corrupt_bytea.py `
  --source-group DWT `
  --active-gold `
  --nas-root "H:\Data Harmony dan ELHP (Gambar Jan - Mei)\DWT" `
  --batch-size 256 `
  --report "gambar_reused_dwt.csv" `
  --apply
```

Jika Juni berada pada root lain, `--nas-root` dapat diulang. Jalankan PWP, DIN,
DAN, dan DWT satu per satu.

Mode `--active-gold --apply` bersifat idempotent:

- identity yang sudah aktif di `gambar_reused` dilewati;
- payload sama tidak di-update ulang;
- file baru divalidasi decode dan SHA-256 sebelum disimpan;
- ia tidak membaca BYTEA Silver atau recovery lama.

## Audit sebelum menghapus recovery lama

Audit coverage metadata:

```powershell
py dashboard_diagnose.py --audit-all-images --source-group ALL
```

Audit full-read dan decode semua gambar:

```powershell
py dashboard_diagnose.py `
  --audit-all-images `
  --source-group ALL `
  --deep-image-read
```

Hasil wajib:

```text
pending_gambar_reused=0
failed=0
RESULT: PASS
```

Preflight cleanup menjalankan full detoast read dan membandingkan seluruh
evidence Gold:

```powershell
py run_pipeline.py cleanup-legacy-recovery
```

Jika seluruh source `missing=0` dan `length_mismatch=0`, hapus v1/v2:

```powershell
py run_pipeline.py cleanup-legacy-recovery --apply
```

Perintah menggunakan `DROP ... RESTRICT`; dependency tak terduga menggagalkan
cleanup dan tidak menghapus objek lain. Silver tidak diubah atau dihapus.

## Menjalankan dashboard dan report

```powershell
py run_dashboard.py
```

Dashboard dan PDF mencari BYTEA hanya di `processing.gambar_reused`. Jika row
belum ada, UI menampilkan `Gambar perlu recovery` dan tidak fallback ke Silver.

## Sinkronisasi harian

```powershell
powershell -ExecutionPolicy Bypass `
  -File .\run_daily_gambar_reused.ps1
```

Deep audit mingguan:

```powershell
powershell -ExecutionPolicy Bypass `
  -File .\run_daily_gambar_reused.ps1 `
  -DeepAudit
```

Job harian tidak memulihkan seluruh corpus. Ia hanya menambahkan evidence Gold
yang belum ada sehingga tidak membuat versi TOAST baru untuk gambar yang sama.
