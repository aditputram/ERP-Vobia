#!/bin/sh
set -eu

PROJECT_DIR="/Users/aditya/Documents/VOBIA ERP"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"

if [ ! -x "$PYTHON_BIN" ]; then
    echo "Virtual environment belum tersedia di $PROJECT_DIR/.venv"
    echo "Hubungi Samuel untuk menjalankan setup dependency."
    exit 1
fi

cd "$PROJECT_DIR/app"
export VOBIA_USE_SQLITE=1
export DJANGO_DEBUG=1

"$PYTHON_BIN" manage.py migrate --noinput

if ! "$PYTHON_BIN" manage.py shell -c "from django.contrib.auth import get_user_model; raise SystemExit(0 if get_user_model().objects.filter(username='vobiasuperadmin').exists() else 1)"; then
    echo "Setup akun pertama. Password tidak akan terlihat saat diketik."
    "$PYTHON_BIN" manage.py setup_superadmin
fi

echo "Vobia ERP berjalan di http://127.0.0.1:8000/"
"$PYTHON_BIN" manage.py runserver 127.0.0.1:8000

