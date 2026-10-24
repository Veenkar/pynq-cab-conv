#!/bin/sh
# Create .venv from the python3 on PATH and install requirements.txt.
#   ./setup_venv.sh              create if missing (recreated if the Python version changed)
#   ./setup_venv.sh --reinstall  delete and recreate .venv
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
VENV=.venv

case "${1:-}" in
--reinstall) rm -rf "$VENV" ;;
"") ;;
*) sed -n '2,4p' "$0"; exit 1 ;;
esac

# recreate when the base Python version differs from the one the venv was built with
want=$("$PY" -c 'import platform; print(platform.python_version())')
if [ -f "$VENV/pyvenv.cfg" ]; then
    have=$(sed -n 's/^version[_info]* *= *//p' "$VENV/pyvenv.cfg" | head -n 1)
    if [ "$have" != "$want" ]; then
        echo "venv Python $have != $want, recreating"
        rm -rf "$VENV"
    fi
fi

[ -d "$VENV" ] || "$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -r requirements.txt
echo "ready: $VENV/bin/python (Python $want)"
