"""Pedido de agendamento coletado pelo agente (item 2.3 do plano de consultas).

O agente não marca a consulta: coleta os dados do paciente e registra um PEDIDO
em pedidos_agendamento (banco CRM). A secretária vê o pedido na Agenda do CRM
e cria a consulta com um clique (formulário já preenchido).

Só entra na lista de tools do turno quando o tenant liga
agenda.pedido_pela_bia (padrão desligado) e o agendamento online
(agenda.habilitada) está desligado — ver core/nodes._tools_disponiveis.
Um paciente tem no máximo um pedido pendente: registrar de novo atualiza.
"""

import logging
from datetime import datetime, timezone

from langchain_core.tools import BaseTool, tool

from core.registry import ToolFactory, ToolRegistry
from supabase import Client

log = logging.getLogger(__name__)

TABELA = "pedidos_agendamento"
_TIPOS = {"primeira_consulta", "retorno", "outro"}


def _limpo(valor: str | None, limite: int = 300) -> str | None:
    if valor is None:
        return None
    texto = " ".join(str(valor).split())[:limite]
    return texto or None


def _make_registrar_pedido_agendamento(vertical_client: Client) -> ToolFactory:
    def factory(tenant_id: str) -> BaseTool:
        @tool
        async def registrar_pedido_agendamento(
            contato_id: str,
            nome_paciente: str,
            data_nascimento: str,
            convenio: str,
            tipo_consulta: str,
            preferencia_horario: str,
            observacao: str = "",
        ) -> str:
            """Registra o pedido de agendamento para a secretária confirmar.

            Use DEPOIS de coletar com o paciente, um de cada vez:
            nome_paciente: nome completo de quem vai ser atendido (pode ser
              outra pessoa, ex.: filho).
            data_nascimento: DD/MM/AAAA.
            convenio: nome do convênio ou 'Particular'.
            tipo_consulta: 'primeira_consulta', 'retorno' ou 'outro'.
            preferencia_horario: dias e turno preferidos, em texto
              (ex.: 'terça ou quinta de manhã').
            observacao: motivo da consulta ou outro detalhe útil (opcional).

            Você NÃO marca a consulta nem promete horário: a secretária confirma."""
            nome = _limpo(nome_paciente, 120)
            if not nome:
                return "Falta o nome completo do paciente. Pergunte antes de registrar."
            tipo = tipo_consulta if tipo_consulta in _TIPOS else "outro"

            dados = {
                "nome_paciente": nome,
                "data_nascimento": _limpo(data_nascimento, 20),
                "convenio": _limpo(convenio, 80),
                "tipo_consulta": tipo,
                "preferencia_horario": _limpo(preferencia_horario, 200),
                "observacao": _limpo(observacao, 500),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

            try:
                existente = (
                    vertical_client.table(TABELA)
                    .select("id")
                    .eq("tenant_id", tenant_id)
                    .eq("contato_id", contato_id)
                    .eq("status", "pendente")
                    .limit(1)
                    .execute()
                ).data
                if existente:
                    vertical_client.table(TABELA).update(dados).eq("id", existente[0]["id"]).eq(
                        "tenant_id", tenant_id
                    ).execute()
                else:
                    vertical_client.table(TABELA).insert(
                        {
                            **dados,
                            "tenant_id": tenant_id,
                            "contato_id": contato_id,
                            "status": "pendente",
                        }
                    ).execute()
            except Exception as e:  # noqa: BLE001 — falha não pode travar a conversa
                log.error(
                    "registrar_pedido_agendamento: falha ao gravar | tenant=%s | erro=%s",
                    tenant_id,
                    type(e).__name__,
                )
                return (
                    "Não consegui registrar o pedido agora. Não informe erro técnico ao "
                    "paciente: transfira para a secretária com os dados no resumo."
                )

            log.info("registrar_pedido_agendamento: pedido registrado | tenant=%s", tenant_id)
            return (
                "Pedido registrado para a secretária. Não prometa data nem horário: "
                "diga ao paciente que a equipe vai confirmar o melhor horário disponível."
            )

        return registrar_pedido_agendamento

    return factory


def registrar(registry: ToolRegistry, vertical_client: Client) -> None:
    """Registra a tool no vertical clínica. A disponibilidade por tenant
    (agenda.pedido_pela_bia) é decidida por turno em core/nodes."""
    registry.register_vertical("clinica", _make_registrar_pedido_agendamento(vertical_client))
