# Mapping sumber DB

Sumber gambar wajib mengikuti kontrak yang sudah Anda miliki:

```sql
SELECT file_uid, source_group, source_root, source_folder, file_path,
       relative_path, file_name, file_name_upper, file_ext, mime_type,
       is_transport, file_size_bytes, file_mtime_epoch, file_sha256,
       image_bytes, bytea_len, load_status, file_exists, zero_byte,
       last_checked_at, loaded_at
FROM silver.image_store_all_files;
```

Default sumber metadata bisnis adalah `silver.production_plantation_clean`. `context.py` mendeteksi nama kolom secara case-insensitive.

Semua kolom evidence yang tersedia diperiksa sebagai exact basename. Urutannya
adalah `ffb_evidence_kp`, `output_file`, lalu alias alternatif. Pipeline tidak
lagi memilih hanya kolom pertama; perilaku ini sama dengan lookup transaksi
Excel lama yang membentuk bulan Januari–Mei.

Jika kolom `account_status` tersedia, hanya baris `ACTUAL` yang digunakan untuk membentuk context GT.

| Target canonical | Kandidat kolom sumber |
|---|---|
| file evidence | `ffb_evidence_kp`, `output_file`, `file_name`, `filename`, `image_name` |
| OPH | `harvest_ticket`, `oph`, `tiket_panen`, `no_bcc` |
| tanggal | `period_date`, `report_date`, `tanggal`, `budat`, `date` |
| estate | `department_code`, `estate`, `kebun`, `department` |
| afdeling | `afdeling_code`, `afdeling_id`, `afdeling`, `afd` |
| blok | `block`, `blok`, `block_code` |
| TPH | `tph_no`, `tph`, `tph_code` |
| pemanen | `name`, `pemanen`, `nama_pemanen`, `employee_name` |
| kerani panen | `name_kerani`, `nama_kerani_panen`, `kerani_panen`, `kerani` |
| JJG | `bunch`, `jjg_kerani_panen`, `jjg`, `qty_jjg`, `bunch_ai_kp` |
| mandor | `name_mandor`, `nama_mandor`, `mandor`, `ename_m` |

Jika nama tabel berbeda, ubah `.env`:

```dotenv
BUSINESS_TABLE=nama_tabel_anda
SAP_TABLE=nama_tabel_sap_anda
```

Transport tidak memerlukan folder. Pipeline membaca baris `is_transport=true` atau filename berpola:

```text
TRANSPORT_DT_<OPH>_<BLOK>_<TPH>....jpg
TRANSPORT_FT_<OPH>_<BLOK>_<TPH>....jpg
```

Jika kolom bisnis Anda memakai nama yang tidak ada pada tabel kandidat di atas, tambahkan alias pada `src/golden_eye/context.py`; tidak perlu mengubah validator atau dashboard.
