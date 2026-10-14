"""agents/internal/tools.py — Ferramentas do Internal Agent (uso exclusivo do médico).

USO EXCLUSIVO DO INTERNAL AGENT. Nunca importar de módulos do agente (agente paciente).
"""

import logging
from datetime import date, datetime, timedelta, timezone

from agents.internal.briefs import gerar_patient_brief
from db.client import get_supabase
from db.vertical_client import get_vertical_supabase

log = logging.getLogger(__name__)

# Clínica piloto em UTC-4 o ano todo (Brasil sem horário de verão desde 2019)
_BR_TZ = timezone(timedelta(hours=-4))


def _intervalo_dia_br(data: date) -> tuple[str, str]:
    """Retorna (inicio, fim) do dia no fuso UTC-4 da clínica piloto como strings ISO 8601."""
    inicio = datetime(data.year, data.month, data.day, tzinfo=_BR_TZ)
    fim = inicio + timedelta(days=1)
    return inicio.isoformat(), fim.isoformat()


async def get_agenda_do_dia(
    tenant_id: str,
    data: date,
    *,
    perfil_medico: dict | None = None,
) -> list[dict]:
    """Retorna os agendamentos do dia com dados do paciente e brief clínico.

    Busca agendamentos no Vertical DB, enriquece com nome/telefone do Core DB
    e gera um brief via LLM para cada paciente.

    Args:
        tenant_id:     UUID do tenant — primeiro filtro em toda query.
        data:          Data do dia a consultar.
        perfil_medico: Perfil do médico (config["perfil_medico"]) repassado ao
                       gerador de briefs para personalizar o raciocínio clínico.

    Returns:
        Lista de dicts com os campos do agendamento + "nome", "telefone" e "brief".
    """
    vertical = get_vertical_supabase()
    core = get_supabase()

    inicio_dia, fim_dia = _intervalo_dia_br(data)

    # Agendamentos do dia (Vertical DB)
    agendamentos_result = (
        vertical.table("agendamentos")
        .select("*")
        .eq("tenant_id", tenant_id)
        .gte("data_hora", inicio_dia)
        .lt("data_hora", fim_dia)
        .order("data_hora", desc=False)
        .execute()
    )
    agendamentos = agendamentos_result.data or []

    if not agendamentos:
        return []

    # Buscar contatos em lote no Core DB (evita N queries)
    contato_ids = list({ag["contato_id"] for ag in agendamentos})
    contatos_result = (
        core.table("contatos")
        .select("id, nome, telefone")
        .eq("tenant_id", tenant_id)
        .in_("id", contato_ids)
        .execute()
    )
    contatos_por_id: dict[str, dict] = {c["id"]: c for c in (contatos_result.data or [])}

    # Montar resultado: agendamento + contato + brief
    resultado: list[dict] = []
    for ag in agendamentos:
        contato_id = ag["contato_id"]
        contato = contatos_por_id.get(contato_id, {})

        try:
            brief = await gerar_patient_brief(
                contato_id=contato_id, tenant_id=tenant_id, perfil_medico=perfil_medico
            )
        except Exception as e:
            log.error("Erro ao gerar brief para contato %s: %s", contato_id, e, exc_info=True)
            brief = {"erro": True}

        resultado.append(
            {
                **ag,
                "nome": contato.get("nome"),
                "telefone": contato.get("telefone"),
                "brief": brief,
            }
        )

    return resultado
