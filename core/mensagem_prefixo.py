"""core/mensagem_prefixo.py — prefixo de remetente nas mensagens enviadas.

Toda mensagem do agente sai como "Nome: ..." (tenants.config.nome_agente, ou
settings.nome_agente_padrao quando o tenant não define); toda mensagem de atendente sai como
"NomeAtendente: ...". Isola essa regra do core (genérico) para não duplicar
a lógica entre o fluxo do agente e o endpoint de mensagem do atendente.
"""

from app.config import settings


def prefixar_mensagem_agente(texto: str, nome_agente: str | None = None) -> str:
    """Adiciona o prefixo "Nome: " se o texto ainda não começa com ele.

    Checar pelo prefixo exato (em vez de um regex genérico tipo "^\\w+:\\s")
    evita falso positivo com nomes de atendente compostos ("Maria Silva: oi"
    tem espaço no nome — um regex de "uma palavra antes dos dois-pontos" não
    reconheceria esse prefixo como já existente e duplicaria).
    """
    prefixo = f"{nome_agente or settings.nome_agente_padrao}: "
    if texto.startswith(prefixo):
        return texto
    return f"{prefixo}{texto}"
