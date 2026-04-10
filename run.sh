#!/bin/bash
# Универсальный скрипт запуска — работает из любой директории
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$SCRIPT_DIR"
exec python3 main.py "$@"
