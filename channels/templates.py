"""channels/templates.py — templates da Meta configurados por clínica (item 3.1).

Mensagem que a CLÍNICA inicia (lembrete, aviso de atraso) só é entregue fora
da janela de 24 h da Meta se for um template aprovado. Cada clínica aprova os
seus no WhatsApp Manager e registra o nome em tenants.config:

    {"templates": {
        "lembrete_consulta": {"nome": "lembrete_consulta", "idioma": "pt_BR"},
        "aviso_atraso":      {"nome": "aviso_atraso",      "idioma": "pt_BR"}
    }}

Sem template configurado, quem chama usa o texto livre (só entregue a quem
falou com a clínica nas últimas 24 h).
"""

from dataclasses import dataclass

_IDIOMA_PADRAO = "pt_BR"

# Payload dos botões do template de lembrete, na ordem cadastrada na Meta
# (Confirmar presença, Preciso remarcar), seguido do id da consulta. Volta no
# webhook quando o paciente toca (gateway/lembrete_handler.py).
PAYLOAD_CONFIRMAR = "lembrete:confirmar:"
PAYLOAD_REMARCAR = "lembrete:remarcar:"

# Parâmetros da Meta não aceitam quebra de linha, tab nem 4+ espaços seguidos.
_MAX_PARAMETRO = 200

_DIAS_SEMANA = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)


@dataclass(frozen=True)
class TemplateMeta:
    nome: str
    idioma: str = _IDIOMA_PADRAO


def template_da_clinica(config: dict | None, chave: str) -> TemplateMeta | None:
    """Template `chave` registrado na config da clínica, ou None."""
    bloco = ((config or {}).get("templates") or {}).get(chave)
    if not isinstance(bloco, dict):
        return None
    nome = bloco.get("nome")
    if not isinstance(nome, str) or not nome.strip():
        return None
    idioma = bloco.get("idioma")
    if not isinstance(idioma, str) or not idioma.strip():
        idioma = _IDIOMA_PADRAO
    return TemplateMeta(nome=nome.strip(), idioma=idioma.strip())


def parametro(texto: object) -> str:
    """Valor seguro para {{n}}: uma linha, sem espaços repetidos, com limite."""
    limpo = " ".join(str(texto or "").split())
    return (limpo or "-")[:_MAX_PARAMETRO]


def dia_da_semana(indice_iso: int) -> str:
    """1 = segunda ... 7 = domingo (datetime.isoweekday)."""
    return _DIAS_SEMANA[(indice_iso - 1) % 7]
