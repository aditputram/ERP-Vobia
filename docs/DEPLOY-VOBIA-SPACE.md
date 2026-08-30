# Deploy Vobia Space

Target production:

- Domain: `https://space.vobia.id`
- Region: Singapore
- Web service: 1 CPU / 2 GB RAM
- PostgreSQL: 0.5 CPU / 1 GB RAM, storage awal 5 GB
- Private persistent disk: 10 GB

## Urutan aman

1. Pastikan seluruh test dan `check --deploy` lulus.
2. Buat backup konsisten SQLite dan seluruh `data/private_uploads`.
3. Buat Blueprint Render dari `render.yaml`, tetapi perlakukan deploy pertama sebagai staging.
4. Jalankan migrasi schema PostgreSQL.
5. Pindahkan **seluruh data SQLite yang sekarang** ke PostgreSQL, termasuk akun, audit, Sales, Operation, Marketing, dan data UAT.
6. Salin seluruh private upload ke `/var/data/private_uploads` dengan path relatif tetap.
7. Rekonsiliasi jumlah row per model, total utama, file count, dan checksum.
8. Uji login, permission, upload/download, Instagram, campaign, partnership, Sales, dan Operation.
9. Setelah UAT lolos, hubungkan `space.vobia.id` dan update callback integrasi.

## Data baseline saat persiapan

- SQLite lokal: `data/vobia_erp.sqlite3`
- Private upload lokal: `data/private_uploads`
- Data lokal tidak boleh dihapus atau ditimpa ketika staging dibuat.
- Token, password, secret key, database URL, dan backup tidak boleh masuk Git.

Snapshot ukuran/count harus diambil ulang tepat sebelum migrasi karena data UAT masih berubah. Cutover wajib memakai backup bertimestamp, manifest jumlah row per tabel, daftar file beserta checksum, dan hasil rekonsiliasi sebelum staging diterima.

## Akun pertama

Halaman `/account/setup/` mati di production. Setelah data dipindahkan, akun existing ikut terbawa. Jika database staging benar-benar kosong, buat akun awal melalui Render Shell:

```sh
python app/manage.py setup_superadmin
```

## Health check

`https://<host>/healthz` harus mengembalikan `{"status": "ok"}`. Respons degraded berarti koneksi database bermasalah dan deploy tidak boleh diterima.
