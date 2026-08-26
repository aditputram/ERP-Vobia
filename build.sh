#!/usr/bin/env bash
# Dijalankan Render setiap deploy. Migrasi TIDAK di sini — ada di
# preDeployCommand, supaya kalau migrasi gagal versi lama tetap melayani.
set -o errexit

pip install --upgrade pip
pip install -r requirements.txt
python app/manage.py collectstatic --noinput
