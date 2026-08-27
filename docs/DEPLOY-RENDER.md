# Deploy ke Render — panduan langkah demi langkah

Berkas di repo ini (`render.yaml`, `build.sh`, `gunicorn.conf.py`,
`.python-version`) sudah menyiapkan semuanya. Yang tersisa hanya klik.

## Sekali di awal

1. Daftar/masuk ke Render, hubungkan akun GitHub, pilih repo ini.
2. Pilih **Blueprint** (Render membaca `render.yaml`), bukan "Web Service" manual.
3. Periksa dua hal sebelum menekan Apply:
   - region **Singapore** untuk web service *dan* database;
   - paket **berbayar** (starter / basic-256mb). Paket gratis tidur setelah 15
     menit dan database gratis kedaluwarsa 30 hari.
4. Apply. Render membuat database, mengisi `DATABASE_URL`, membuat
   `DJANGO_SECRET_KEY` acak, memasang disk 10 GB di `/var/data`, menjalankan
   `build.sh`, lalu `migrate` sebelum versi baru menerima lalu lintas.

## Setelah deploy pertama

Buka **Shell** di dashboard Render:

```bash
python app/manage.py setup_superadmin          # akun pertama, password diketik tersembunyi
python app/manage.py create_team_user budi --role gudang --full-name "Budi"
```

Peran yang tersedia: `owner`, `merchandising`, `purchasing`, `gudang`,
`finance`, `viewer`. Password anggota tim diambil dari environment
`DJANGO_NEW_USER_PASSWORD` saat perintah dijalankan.

> Halaman `/account/setup/` sengaja dimatikan di server
> (`VOBIA_ALLOW_INITIAL_SETUP=0`). Penjaganya mengandalkan alamat pengirim, dan
> di balik proxy hosting alamat itu bisa terbaca sebagai localhost.

## Memastikan sehat

- `https://<host>/healthz` → `{"status": "ok"}`. Render memakai alamat ini
  untuk tahu aplikasi hidup; pasang juga di alat pemantauan.
- Log ada di tab **Logs**; error 5xx dan kegagalan impor muncul di sana.

## Domain sendiri

Tambahkan domain di Render, arahkan DNS-nya, tunggu sertifikat terbit. Setelah
domain final:

```
DJANGO_ALLOWED_HOSTS=erp.vobia.co.id
DJANGO_CSRF_TRUSTED_ORIGINS=https://erp.vobia.co.id
DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=1   # hanya jika semua subdomain HTTPS
```

## Yang masih harus dikerjakan sebelum disebut produksi

- **Backup + uji pulihkan.** Render mem-backup database; yang wajib dibuktikan
  adalah pemulihannya pernah dicoba dan berhasil, lengkap dengan catatan waktu.
- **Bukti impor ke penyimpanan objek.** Disk 10 GB cukup untuk UAT, tapi
  terikat pada satu mesin. Untuk produksi pindahkan ke penyimpanan objek
  (Cloudflare R2 / S3) yang punya versi dan replikasi.
- **Impor Excel sebagai proses latar belakang.** Sekarang impor berjalan di
  dalam permintaan web; file besar bisa menabrak batas waktu. Tambahkan
  Background Worker Render + antrean, dengan status yang bisa dipantau.
- **Peran belum menjaga halaman.** Perintah `create_team_user` sudah memberi
  akun per orang dan label peran, sehingga jejak audit bernama orangnya. Tetapi
  pembatasan "peran X tidak boleh membuka halaman Y" belum dipasang — itu
  keputusan proses bisnis, dibahas bersama pemilik produk.
