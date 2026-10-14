FROM python:3.12-slim

# uv copiado diretamente da imagem oficial — sem overhead de pip install
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# UV_COMPILE_BYTECODE: compila .pyc no build, startup mais rápido em produção
# UV_LINK_MODE=copy: evita problemas de hard-link entre layers do Docker
# PYTHONUNBUFFERED: logs aparecem imediatamente (sem buffer de stdout)
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Dependências primeiro — esta layer só é refeita se pyproject.toml ou uv.lock mudarem
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Código da aplicação (copiado por módulo para evitar incluir scripts/, tests/, etc.)
COPY agents/ ./agents/
COPY app/ ./app/
COPY channels/ ./channels/
COPY core/ ./core/
COPY db/ ./db/
COPY gateway/ ./gateway/
COPY integrations/ ./integrations/
COPY jobs/ ./jobs/
COPY tools/ ./tools/
COPY security/ ./security/

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
