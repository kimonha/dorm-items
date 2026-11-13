#!/bin/zsh
set -eu
cd "$(dirname "$0")"
port=${1:-8000}
if [[ $# -gt 1 || "$port" != <1-65535> ]]; then
  printf '사용법: ./start.command [포트 번호 1–65535]\n' >&2
  exit 2
fi
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import fastapi, uvicorn, pydantic, psycopg, werkzeug' 2>/dev/null; then
  .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port "$port"
