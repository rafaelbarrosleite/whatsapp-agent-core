"""Agenda de demonstração do vertical clínica.

Agenda simples que vive no próprio banco (tabela agendamentos): seg–sex,
08h–12h e 14h–18h, consultas de 30 min, no fuso da clínica. Serve para rodar o
agente de ponta a ponta sem integrar um sistema de agenda externo. Uma clínica
real troca este módulo por um provider da agenda dela, com os mesmos nomes de
tool (core/nodes.AGENDA_TOOLS), sem mexer no core.

Só entra na lista de tools quando o tenant liga agenda.habilitada.
Toda consulta filtra por tenant_id no código, nunca pelo que o LLM mandou.
"""

import logging
from datetime import date, datetime, time, timedelta, timezone

from langchain_core.tools import BaseTool, tool

from core.fuso import fuso_do_tenant
from core.registry import ToolFactory, ToolRegistry
from supabase import Client

log = logging.getLogger(__name__)

_DIAS_PT = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
_TURNOS = ((time(8, 0), time(12, 0)), (time(14, 0), time(18, 0)))
_DURACAO = timedelta(minutes=30)
_MAX_SLOTS = 10
_MAX_DIAS = 60
_STATUS_OCUPA = ("agendado", "confirmado", "confirmado_paciente")


def _fmt(dt: datetime) -> str:
    return f"{_DIAS_PT[dt.weekday()]} {dt.strftime('%d/%m/%Y')} às {dt.strftime('%Hh%M')}"


def _horarios_do_dia(dia: date, fuso: timezone) -> list[datetime]:
    if dia.weekday() >= 5:
        return []
    slots = []
    for inicio, fim in _TURNOS:
        atual = datetime.combine(dia, inicio, tzinfo=fuso)
        limite = datetime.combine(dia, fim, tzinfo=fuso)
        while atual + _DURACAO <= limite:
            slots.append(atual)
            atual += _DURACAO
    return slots


def _ocupados(client: Client, tenant_id: str, inicio: datetime, fim: datetime) -> set[datetime]:
    r = (
        client.table("agendamentos")
        .select("data_hora")
        .eq("tenant_id", tenant_id)
        .in_("status", list(_STATUS_OCUPA))
        .gte("data_hora", inicio.isoformat())
        .lt("data_hora", fim.isoformat())
        .execute()
    )
    return {datetime.fromisoformat(a["data_hora"].replace("Z", "+00:00")) for a in r.data or []}


def _parse_data(valor: str) -> date | None:
    try:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    except ValueError:
        return None


def _make_buscar_slots_disponiveis(client: Client) -> ToolFactory:
    def factory(tenant_id: str) -> BaseTool:
        @tool
        def buscar_slots_disponiveis(data_inicio: str, data_fim: str) -> str:
            """Lista horários livres para consulta entre data_inicio e data_fim
            (YYYY-MM-DD). Use quando o paciente quiser agendar ou perguntar por
            vagas. Sem datas do paciente, use hoje até 14 dias à frente."""
            inicio, fim = _parse_data(data_inicio), _parse_data(data_fim)
            if not inicio or not fim or fim < inicio:
                return "Datas inválidas. Use YYYY-MM-DD, com data_fim depois de data_inicio."
            fim = min(fim, inicio + timedelta(days=_MAX_DIAS))

            fuso = fuso_do_tenant(tenant_id)
            agora = datetime.now(fuso)
            janela_ini = datetime.combine(inicio, time.min, tzinfo=fuso)
            janela_fim = datetime.combine(fim + timedelta(days=1), time.min, tzinfo=fuso)
            ocupados = _ocupados(client, tenant_id, janela_ini, janela_fim)

            livres: list[datetime] = []
            dia = inicio
            while dia <= fim and len(livres) < _MAX_SLOTS:
                for slot in _horarios_do_dia(dia, fuso):
                    if slot > agora and slot not in ocupados:
                        livres.append(slot)
                        if len(livres) == _MAX_SLOTS:
                            break
                dia += timedelta(days=1)

            if not livres:
                return "Não há horários livres nesse período. Quer que eu veja outras datas?"
            return "Horários disponíveis:\n" + "\n".join(f"• {_fmt(s)}" for s in livres)

        return buscar_slots_disponiveis

    return factory


def _make_criar_agendamento(client: Client, core: Client) -> ToolFactory:
    def factory(tenant_id: str) -> BaseTool:
        @tool
        def criar_agendamento(
            contato_id: str, data: str, horario: str, convenio_nome: str = "Particular"
        ) -> str:
            """Marca a consulta do paciente. Use só depois de o paciente escolher
            um horário de buscar_slots_disponiveis e dizer para quem é. data em
            YYYY-MM-DD, horario em HH:MM. A consulta fica no cadastro deste
            WhatsApp: não peça nome, e-mail nem documento para marcar."""
            fuso = fuso_do_tenant(tenant_id)
            try:
                quando = datetime.strptime(f"{data} {horario}", "%Y-%m-%d %H:%M").replace(
                    tzinfo=fuso
                )
            except ValueError:
                return "Data ou horário inválido. Use data YYYY-MM-DD e horario HH:MM."

            if quando not in _horarios_do_dia(quando.date(), fuso):
                return (
                    "Esse horário não faz parte da agenda. Ofereça um de buscar_slots_disponiveis."
                )
            if quando <= datetime.now(fuso):
                return "Esse horário já passou. Ofereça um horário futuro."

            # O contato tem que ser desta clínica: o LLM não escolhe o tenant.
            contato = (
                core.table("contatos")
                .select("id")
                .eq("id", contato_id)
                .eq("tenant_id", tenant_id)
                .limit(1)
                .execute()
            )
            if not contato.data:
                return "Contato não encontrado nesta clínica."

            if quando in _ocupados(client, tenant_id, quando, quando + _DURACAO):
                return "Esse horário acabou de ser ocupado. Ofereça outro."

            client.table("agendamentos").insert(
                {
                    "tenant_id": tenant_id,
                    "contato_id": contato_id,
                    "data_hora": quando.isoformat(),
                    "tipo": "Consulta",
                    "status": "agendado",
                    "convenio": convenio_nome.strip() or "Particular",
                    "observacao": "Agendado pelo assistente virtual (WhatsApp).",
                }
            ).execute()
            log.info("agenda_demo: consulta criada | tenant=%s | contato=%s", tenant_id, contato_id)
            return f"Consulta marcada: {_fmt(quando)}."

        return criar_agendamento

    return factory


def _make_buscar_agendamentos(client: Client) -> ToolFactory:
    def factory(tenant_id: str) -> BaseTool:
        @tool
        def buscar_agendamentos(contato_id: str) -> str:
            """Lista as próximas consultas do paciente desta conversa. Use direto
            quando ele perguntar pelas consultas marcadas, sem perguntar para
            quem é: só mostra as do próprio WhatsApp."""
            fuso = fuso_do_tenant(tenant_id)
            r = (
                client.table("agendamentos")
                .select("data_hora, tipo, status")
                .eq("tenant_id", tenant_id)
                .eq("contato_id", contato_id)
                .in_("status", list(_STATUS_OCUPA))
                .gte("data_hora", datetime.now(timezone.utc).isoformat())
                .order("data_hora")
                .execute()
            )
            if not r.data:
                return "Nenhuma consulta marcada."
            linhas = []
            for ag in r.data:
                dt = datetime.fromisoformat(ag["data_hora"].replace("Z", "+00:00")).astimezone(fuso)
                linhas.append(f"📅 {_fmt(dt)} — {ag['tipo']}")
            return "\n".join(linhas)

        return buscar_agendamentos

    return factory


def registrar(registry: ToolRegistry, vertical_client: Client, core_client: Client) -> None:
    """Registra a agenda de demonstração como tools do vertical clínica."""
    registry.register_vertical("clinica", _make_buscar_slots_disponiveis(vertical_client))
    registry.register_vertical("clinica", _make_criar_agendamento(vertical_client, core_client))
    registry.register_vertical("clinica", _make_buscar_agendamentos(vertical_client))
