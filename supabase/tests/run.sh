#!/usr/bin/env bash
# Roda um arquivo de teste SQL num Postgres 17 descartável (Docker), com
# supabase/ montado em /supabase. Nunca toca produção.
# Uso: supabase/tests/run.sh supabase/tests/<arquivo>.sql
set -euo pipefail
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
ARQ="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
REL="${ARQ#"$RAIZ"/}"
NOME="teste-sql-$$"
docker run -d --rm --name "$NOME" -e POSTGRES_PASSWORD=local -v "$RAIZ":/supabase:ro postgres:17-alpine >/dev/null
trap 'docker stop "$NOME" >/dev/null' EXIT
until docker exec "$NOME" pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
sleep 1
docker exec "$NOME" psql -U postgres -v ON_ERROR_STOP=1 -q -f "/supabase/$REL"
