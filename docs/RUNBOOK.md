# Runbook operasional

## Menjalankan company lain

Gunakan command bawaan agar tidak perlu mengingat urutan secara manual:

```powershell
py run_pipeline.py company-guide
py run_pipeline.py company-guide --source-group DIN
```

Alur normal untuk setiap source group:

```powershell
py run_pipeline.py audit-source --source-group DIN
py run_pipeline.py run --source-group DIN
py run_pipeline.py verify
py run_dashboard.py
```

Ganti `DIN` dengan `PWP`, `DAN`, atau `DWT`. Jangan menjalankan dua pipeline
untuk source group yang sama secara bersamaan.

## Pemeriksaan sebelum run

```sql
SELECT source_group,
       count(*) AS total_file,
       count(*) FILTER (WHERE image_bytes IS NOT NULL) AS ada_bytea,
       count(*) FILTER (WHERE coalesce(is_transport, false)) AS transport,
       count(*) FILTER (WHERE coalesce(zero_byte, false)) AS zero_byte
FROM silver.image_store_all_files
GROUP BY source_group
ORDER BY source_group;
```

Pastikan nilai `source_group` pada perintah pipeline sama persis dengan hasil query.

Jalankan rekonsiliasi DB sebelum similarity:

```powershell
py run_pipeline.py audit-source --source-group PWP
```

`Eligible panen`, `Embedding panen valid`, `Context matched`, dan `Context punya
bulan` harus ditelusuri berurutan. Selisih pada satu tahap tidak boleh dianggap
sebagai masalah dashboard.

Perhatikan juga `Eligible panen sebelum dedup` dan `Duplicate logical filename`.
Folder lama hanya mempunyai satu file untuk satu nama, sedangkan tabel gabungan
dapat mempunyai beberapa salinan dari root berbeda. Matching memakai angka
`Eligible panen untuk similarity` setelah canonical dedup.

## Jalankan bertahap untuk data besar

```powershell
py run_pipeline.py run --source-group DIN --to-stage embed
py run_pipeline.py status --limit 1
```

Salin `run_id`, lalu lanjutkan:

```powershell
py run_pipeline.py run --source-group DIN --run-id UUID --from-stage match --to-stage validate
py run_pipeline.py run --source-group DIN --run-id UUID --from-stage publish
py run_pipeline.py verify
```

Untuk source di atas 100.000 canonical image, pastikan FAISS tersedia sebelum
stage MATCH:

```powershell
py -m pip install faiss-cpu==1.14.3
py -c "import faiss; print(faiss.__version__)"
```

Default versi 1.3.13 memakai Torch CUDA exact top-k bila smoke test GPU berhasil.
Jika CUDA tidak siap, pipeline otomatis memakai FAISS HNSW untuk source besar.
Kedua jalur membatasi `top_k` efektif berdasarkan `MAX_MATCH_EDGES`. Jangan menaikkan
`TORCH_FALLBACK_MAX_IMAGES` untuk memaksa 329 ribu image masuk fallback O(n²).

## Jika MATCH lama menghasilkan mega-group

Group puluhan/ratusan ribu adalah chaining palsu dan run tersebut tidak boleh
diteruskan ke publish. Hentikan dengan `Ctrl+C`, pasang versi 1.3.13, lalu buat
run ID baru mulai dari MATCH:

```powershell
py run_pipeline.py --version
py run_pipeline.py run --source-group DIN --from-stage match
```

Jangan gunakan `--run-id` lama karena `candidate_group` yang salah sudah
tersimpan dan mekanisme resume sengaja tidak menghitung ulang MATCH. Context dan
embedding berada pada cache per-file, sehingga perintah di atas tidak mengulang
dua tahap tersebut.

Versi 1.3.8 hanya membentuk group dari mutual KNN, membatasi union maksimal 500
anggota, dan memakai sparse edge validation untuk candidate di atas 40 anggota.

## VALIDATE menggunakan GPU

Versi 1.3.13 mengaktifkan hybrid GPU validator dan batch DB secara default:

```dotenv
VALIDATION_DEVICE=auto
VALIDATION_GPU_PAIR_BATCH=65536
VALIDATION_CPU_WORKERS=8
VALIDATION_CPU_PAIR_BATCH=512
VALIDATION_DB_MEMBER_BATCH=2000
VALIDATION_DB_CANDIDATE_BATCH=500
VALIDATION_SOURCE_READ_ORDER=physical
VALIDATION_MAX_AUDIT_PAIRS_PER_GROUP=500
```

CUDA menangani full/center embedding, edge-vector, dan HSV similarity secara
batch. CPU worker menangani JPEG decode, pHash, edge hash, dan ORB. Pastikan log
menampilkan `numeric_device=cuda`. Pembacaan BYTEA menggunakan batch dan koneksi
persisten; threshold similarity tetap 90/93 dan aturan group mengikuti
`image_utils` lama. Jika run dihentikan ketika VALIDATE, lanjutkan
run ID yang sama agar candidate yang sudah selesai tidak dihitung ulang:

```powershell
py run_pipeline.py run --source-group DIN --run-id UUID_RUN --from-stage validate
```

## Jika BYTEA/TOAST mengembalikan DataCorrupted

`audit-source` hanya memeriksa pointer `image_bytes IS NOT NULL` dan sengaja
tidak membaca ratusan GB BYTEA. Setelah error `XX001`, hentikan loader dan
`VACUUM FULL`, lalu jalankan deep audit:

```powershell
py run_pipeline.py init-db
py run_pipeline.py audit-bytea --source-group DIN
```

Audit menghitung MD5 atas seluruh BYTEA. Jika satu batch menyentuh halaman
TOAST rusak, batch dibagi sampai `file_uid` penyebab ditemukan. CSV masalah
ditulis di folder repository dan exception aktif dapat diperiksa dengan:

```sql
SELECT source_group, file_uid, file_name, error_code, error_message
FROM processing.source_bytea_exception
WHERE source_group='DIN' AND is_active=true
ORDER BY file_name;
```

EMBED/MATCH melewati exception aktif tanpa menghapus row sumber. Setelah audit
selesai, resume run yang gagal dari EMBED.

## Hot store gambar reused

Dashboard dan PDF tidak lagi memakai recovery v1/v2 atau memindai BYTEA Silver.
Seluruh evidence Gold aktif dipulihkan dari file asli ke satu tabel final:

```text
processing.gambar_reused
```

Dry-run, apply, audit full-read, cleanup relation legacy, dan sinkronisasi
harian dijelaskan lengkap pada:

```text
docs/GAMBAR_REUSED_RUNBOOK.md
```

Generator PDF tidak berhenti jika satu relation/payload gambar gagal dibaca.
Ia memakai fallback sehat atau placeholder `Gambar perlu recovery` dan
mencetak `PERINGATAN STORAGE GAMBAR` pada terminal.

```powershell
cd dashboard
py generate_pdf_reports.py --system ALL --company ALL --top 10
```

Perintah `ALL/ALL` menghasilkan satu bulletin gabungan berwarna untuk
HARMONY/PWP, HARMONY/DIN, ELHP/DAN, dan ELHP/DWT. Tambahkan `--separate` jika
diperlukan satu PDF per company.

`% Tiket` pada PDF memakai kontrak yang sama dengan dashboard:

```text
% Tiket = Total Usage Golden Eye / Total Transaksi SAP × 100
```

Denominator dibaca dari `gold.sap_oph_kpi_summary` melalui loader KPI dashboard.
Nilai tidak dibatasi 100%; jika Usage lebih besar daripada tiket SAP, hasilnya
memang dapat melebihi 100%. Jika report menampilkan `N/A`, refresh snapshot SAP:

```powershell
cd ..
py run_pipeline.py refresh-sap-kpi
```

## Query monitoring

```sql
SELECT * FROM gold.pipeline_health ORDER BY started_at DESC;

SELECT reason, count(*)
FROM processing.pipeline_exception
WHERE run_id = 'UUID_RUN'
GROUP BY reason
ORDER BY count(*) DESC;

SELECT source_group, count(*) AS groups, sum(usage_count) AS usages,
       max(usage_count) AS max_group
FROM gold.reused_photo_group_summary
WHERE is_active
GROUP BY source_group;
```

## Saat group di atas 500 masuk karantina

Jangan menaikkan `MAX_ACCEPTED_GROUP_SIZE` hanya agar group masuk dashboard. Periksa evidence:

```sql
SELECT entity_key AS candidate_group_id, reason, payload
FROM processing.pipeline_exception
WHERE run_id='UUID_RUN'
  AND reason='GROUP_STILL_OVER_500_AFTER_VALIDATION';
```

Kemudian lihat pair score pada `processing.validation_pair_audit`. Jika group besar terbentuk karena background/warna serupa, naikkan `PAIR_SCORE_EDGE` sedikit (misalnya 0.54 menjadi 0.58), buat run baru, lalu bandingkan jumlah removed dan subgroup.

## Rollback tampilan dashboard

Hasil lama tidak dihapus. Untuk mengaktifkan kembali suatu run, lakukan hanya setelah verifikasi dan dalam transaksi:

```sql
BEGIN;
UPDATE gold.reused_photo_group SET is_active=false WHERE source_group='DIN';
UPDATE gold.reused_photo_detail SET is_active=false WHERE source_group='DIN';
UPDATE gold.reused_photo_group_summary SET is_active=false WHERE source_group='DIN';

UPDATE gold.reused_photo_group SET is_active=true WHERE run_id='UUID_LAMA';
UPDATE gold.reused_photo_detail SET is_active=true WHERE run_id='UUID_LAMA';
UPDATE gold.reused_photo_group_summary SET is_active=true WHERE run_id='UUID_LAMA';
COMMIT;
```
