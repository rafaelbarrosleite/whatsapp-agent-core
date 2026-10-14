"""core/fuso.py — fuso horário da clínica.

Cada clínica guarda o fuso em tenants.config.fuso_utc_horas: o deslocamento
em horas em relação ao UTC (-3 Brasília/SP, -4 Mato Grosso do Sul/Amazonas, -5 Acre).
É o campo que o cadastro da clínica preenche. Deslocamento fixo basta porque o
Brasil não tem horário de verão desde 2019, e não depende da base de fusos do
sistema operacional (a imagem Docker não precisa de tzdata).

Legado: antes o fuso só existia em lembrete.utc_offset_horas. Continua aceito
como segunda opção, para clínica já cadastrada não mudar de horário.
"""

import logging
from datetime import timedelta, timezone

from db.client import get_supabase

log = logging.getLogger(__name__)

FUSO_PADRAO_HORAS = -3  # Brasília: o fuso da maior parte das clínicas


def _offset_valido(valor: object) -> bool:
    return isinstance(valor, (int, float)) and not isinstance(valor, bool) and -12 <= valor <= 14


def offset_horas(config: dict | None) -> float:
    """Deslocamento UTC da clínica: fuso_utc_horas, senão o legado do lembrete,
    senão Brasília. Valor inválido (texto, fora de -12..14) é ignorado."""
    config = config or {}
    for valor in (
        config.get("fuso_utc_horas"),
        (config.get("lembrete") or {}).get("utc_offset_horas"),
    ):
        if _offset_valido(valor):
            return valor
    return FUSO_PADRAO_HORAS


def fuso_da_config(config: dict | None) -> timezone:
    """timezone da clínica a partir de tenants.config já carregado."""
    return timezone(timedelta(hours=offset_horas(config)))


def fuso_do_tenant(tenant_id: str) -> timezone:
    """Lê tenants.config e devolve o fuso da clínica. Se a leitura falhar, usa
    o padrão (Brasília) em vez de quebrar o atendimento."""
    try:
        r = get_supabase().table("tenants").select("config").eq("id", tenant_id).limit(1).execute()
        config = (r.data[0].get("config") if r.data else None) or {}
    except Exception:
        log.exception("Falha ao ler o fuso da clínica | tenant=%s", tenant_id)
        config = {}
    return fuso_da_config(config)


def descrever_fuso(fuso: timezone) -> str:
    """'UTC-4' / 'UTC-3' — para textos enviados ao LLM."""
    horas = fuso.utcoffset(None) / timedelta(hours=1)
    return f"UTC{horas:+g}"
