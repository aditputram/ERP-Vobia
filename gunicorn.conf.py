"""Setelan server produksi.

Angka pekerja & waktu tunggu dipilih untuk paket kecil: impor Excel bisa
berjalan lama, jadi waktu tunggunya dilonggarkan sampai proses latar belakang
dipasang (lihat docs/DEPLOY-RENDER.md).
"""

import multiprocessing
import os

bind = f"0.0.0.0:{os.getenv('PORT', '10000')}"
workers = int(os.getenv("WEB_CONCURRENCY", max(2, multiprocessing.cpu_count())))
threads = int(os.getenv("WEB_THREADS", "4"))
timeout = int(os.getenv("WEB_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
max_requests = 1000          # daur ulang pekerja, jaga-jaga kebocoran memori
max_requests_jitter = 100
