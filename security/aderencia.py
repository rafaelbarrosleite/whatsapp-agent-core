"""security/aderencia.py — número na resposta do agente precisa ter fonte.

Adaptado do Tech Challenge FIAP Fase 3 (safety/guardrails.py → grounding,
"números sem suporte"). Lá, o modelo inventava "K ≥ 3,3" e "72 h" com a
citação certa. Aqui o equivalente é o agente dizer que a consulta custa R$ 200,
que o jejum é de 8 horas ou que atende às 7h, sem nada disso estar na base de
conhecimento, no resultado de uma tool, no prompt da clínica ou no que o
próprio paciente escreveu.

A checagem é por GRUPO DE DÍGITOS: "R$ 1.250,00" vira {"1", "250", "00"} e só
tem fonte se "250" aparecer em alguma referência ("1" e "0" são ignorados —
aparecem em tudo). É tolerante de propósito: "14/10" casa com "2026-10-14"
vindo de uma tool de agenda. O objetivo é pegar número inventado, não formato.

Fora da checagem do vocabulário da FIAP (sobreposição de palavras < 0,5): em
conversa de WhatsApp o agente parafraseia e cumprimenta, e o corte calibrado para
protocolo clínico bloquearia resposta boa.
"""

import re

# "1. Opção", "2) Opção" e emojis-número (1️⃣) no início de linha são marcador
# de lista, não informação.
_MARCADOR_LISTA = re.compile(r"(?m)^\s*\d{1,2}(?:[.)]|️?⃣)\s*")
_EMOJI_NUMERO = re.compile(r"\d️?⃣")
_NUMERO = re.compile(r"\d+(?:[.,:/h-]\d+)*")
_GRUPO = re.compile(r"\d+")
_IGNORADOS = frozenset({"0", "1"})


def _grupos(texto: str) -> set[str]:
    return {g.lstrip("0") or "0" for g in _GRUPO.findall(texto or "")}


def numeros_sem_fonte(resposta: str, referencias: list[str]) -> list[str]:
    """Números da resposta sem nenhuma referência que os sustente, na ordem em
    que aparecem. Lista vazia = resposta aderente."""
    texto = _EMOJI_NUMERO.sub(" ", _MARCADOR_LISTA.sub("", resposta or ""))
    com_fonte = set().union(*(_grupos(r) for r in referencias)) if referencias else set()
    sem_fonte: list[str] = []
    for numero in _NUMERO.findall(texto):
        relevantes = _grupos(numero) - _IGNORADOS
        if relevantes and not relevantes <= com_fonte and numero not in sem_fonte:
            sem_fonte.append(numero)
    return sem_fonte


MENSAGEM_CONFIRMAR_COM_EQUIPE = (
    "Para não te passar uma informação errada, vou confirmar esse detalhe com a "
    "nossa equipe. Assim que tiver a confirmação, te retornamos por aqui."
)
MENSAGEM_CONFIRMAR_SEM_HANDOFF = (
    "Para não te passar uma informação errada, prefiro que você confirme esse "
    "detalhe direto com a nossa equipe, pelo telefone da clínica."
)
