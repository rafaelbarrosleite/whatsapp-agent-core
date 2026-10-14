"""tools/verticals/clinica/handoff.py — tool transferir_para_atendente.

Quando o agente detecta que o paciente precisa de atendimento humano (depois de
confirmar com o paciente — isso é responsabilidade do system prompt, não da
tool), ele chama esta tool para executar o handoff completo: seleciona o
próximo atendente disponível por round-robin, registra o handoff, pausa o
agente para a conversa, notifica o atendente (CRM + WhatsApp pessoal) e
devolve ao agente o texto para informar o paciente.

Sobre os parâmetros e o RunnableConfig: diferente do que pode parecer natural
à primeira vista, esta tool NÃO recebe conversa_id/contato_id por
RunnableConfig. core/nodes.executar_tools (agente) hoje invoca as tools com
`tool.ainvoke(args)`, sem propagar `config` — só o Internal Agent
(agents/internal/agent.py) faz essa propagação. Replicar o padrão do Internal
Agent aqui exigiria reordenar todo o pipeline de webhook_cloudapi.py (resolver
conversa_id ANTES de invocar o grafo, hoje resolvido depois) — uma mudança de
alto raio de impacto em um pipeline que atende toda conversa, não só handoff.
Em vez disso, seguimos o padrão já usado por criar_agendamento e
buscar_agendamentos: contato_id chega como argumento explícito do LLM (ele
já o tem em contexto, obtido via buscar_contato ou identificar_contato), e a
tool resolve conversa_id sozinha com o helper já existente
db.conversas.obter_ou_criar_conversa — zero mudança no grafo ou no webhook.
"""

import logging
import re
from datetime import datetime, timezone

from langchain_core.tools import BaseTool, tool

from channels.dispatcher import enviar_mensagem_tenant
from core.registry import ToolFactory, ToolRegistry
from db.client import get_supabase
from db.conversas import obter_ou_criar_conversa
from db.handoff import (
    atribuir_lead_por_contato,
    buscar_handoff_ativo,
    buscar_lead_por_contato,
    notificar_handoff,
    registrar_handoff,
    selecionar_proximo_atendente,
)
from supabase import Client

log = logging.getLogger(__name__)

# Janela para considerar um lead "criado nesta conversa" (vs. já existente na
# base). Um handoff acontece dentro da mesma sessão de atendimento; um lead
# criado há mais que isso é de uma interação anterior (retornante).
_JANELA_LEAD_NOVO_S = 3 * 3600  # 3 horas


def _status_lead(lead: dict | None) -> str:
    """Rótulo curto para a notificação: lead novo (criado nesta conversa) vs.
    já existente na base. Baseado em created_at para não depender de qual
    caminho criou o lead (criar_lead do agente ou fallback do handoff)."""
    if not lead:
        return "🆕 Lead novo"
    try:
        criado = datetime.fromisoformat(str(lead.get("created_at")).replace("Z", "+00:00"))
        if criado.tzinfo is None:
            criado = criado.replace(tzinfo=timezone.utc)
        recente = (datetime.now(timezone.utc) - criado).total_seconds() < _JANELA_LEAD_NOVO_S
    except (ValueError, TypeError):
        recente = False
    return "🆕 Lead novo (cadastrado nesta conversa)" if recente else "🔁 Lead já existente"


_ROLES_DESTINO = {"atendente", "concierge"}

_MSG_SEM_ATENDENTE = (
    "No momento não há ninguém da equipe disponível para te atender "
    "diretamente, mas posso continuar te ajudando por aqui. O que você precisa?"
)


def _make_transferir_para_atendente(vertical_client: Client) -> ToolFactory:
    def factory(tenant_id: str) -> BaseTool:
        @tool
        async def transferir_para_atendente(
            contato_id: str,
            role_destino: str,
            motivo: str,
            resumo: str,
        ) -> str:
            """Transfere a conversa para um atendente humano. Use SOMENTE depois
            de confirmar com o paciente que ele concorda em ser transferido.

            role_destino: 'atendente' (agendamento, financeiro, convênio, dúvidas
            gerais) ou 'concierge' (pós-operatório, suporte operacional).
            motivo: descrição curta (1 linha) da intenção do paciente.
            resumo: contexto coletado na conversa — nome, telefone, email (se
            coletado) e o que o paciente relatou — para o atendente não ter que
            perguntar tudo de novo.

            Retorna o texto que você deve enviar ao paciente a seguir. Se não
            houver ninguém disponível, a conversa NÃO é pausada — você continua
            atendendo normalmente."""
            if role_destino not in _ROLES_DESTINO:
                return (
                    f"Erro interno: role_destino deve ser 'atendente' ou 'concierge' "
                    f"(recebido: {role_destino!r}). Não informe isso ao paciente, "
                    "apenas tente novamente com o valor correto."
                )

            core_db = get_supabase()

            contato_resultado = (
                core_db.table("contatos")
                .select("nome, telefone, tipo")
                .eq("id", contato_id)
                .eq("tenant_id", tenant_id)
                .limit(1)
                .execute()
            )
            contato = contato_resultado.data[0] if contato_resultado.data else {}
            nome_paciente = contato.get("nome") or "Paciente sem nome"
            contato_telefone = contato.get("telefone")
            contato_tipo = contato.get("tipo")

            conversa_id = obter_ou_criar_conversa(core_db, tenant_id, contato_id)

            handoff_ativo = await buscar_handoff_ativo(conversa_id, tenant_id, vertical_client)
            if handoff_ativo:
                nome_atendente = handoff_ativo.get("perfil_nome") or "nossa equipe"
                log.info(
                    "transferir_para_atendente: handoff ativo reutilizado | tenant=%s | "
                    "conversa=%s | handoff=%s | status=%s",
                    tenant_id,
                    conversa_id,
                    handoff_ativo.get("id"),
                    handoff_ativo.get("status"),
                )
                return (
                    f"Já conectei você com {nome_atendente}. "
                    "A equipe já foi notificada e vai continuar seu atendimento por aqui."
                )

            perfil = await selecionar_proximo_atendente(tenant_id, role_destino, vertical_client)
            if perfil is None:
                log.warning(
                    "transferir_para_atendente: nenhum %s disponível | tenant=%s",
                    role_destino,
                    tenant_id,
                )
                return _MSG_SEM_ATENDENTE

            # Status do lead ANTES de qualquer criação neste handoff (concierge
            # cria via atribuir_lead abaixo) → avisa o humano se é lead novo
            # (cadastrado nesta conversa) ou já existente na base.
            lead_existente = await buscar_lead_por_contato(
                vertical_client, tenant_id, contato_id, contato_telefone
            )
            status_lead = _status_lead(lead_existente)

            handoff = await registrar_handoff(
                tenant_id=tenant_id,
                conversa_id=conversa_id,
                contato_id=contato_id,
                perfil=perfil,
                motivo=motivo,
                resumo_conversa=resumo,
                vertical_db=vertical_client,
            )

            # NÃO pausa o agente aqui (mudança de fluxo 2026-07-12): o agente
            # continua respondendo normalmente após a transferência. A pausa
            # (agente_ativo=False, estado='aguardando_humano') acontece só
            # quando um atendente clica "Assumir" (app/atendente_router.assumir).
            # Até lá, a conversa fica com handoff_fila status='aguardando' e a
            # tela de mensagens do CRM bloqueia o envio manual — assim ninguém
            # responde em conflito com o agente e não precisa de timeout.

            # ── Atribuição de lead (só concierge; atendente é fila) ──────────
            # concierge = pessoa específica (ex.: Laís) → atribui o lead direto.
            # atendente = fila genérica → NÃO pré-atribui; o 1º que assumir no
            # Inbox vira o atribuido_a (ver notificação p/ todos abaixo).
            if role_destino == "concierge":
                try:
                    await atribuir_lead_por_contato(
                        tenant_id=tenant_id,
                        contato_id=contato_id,
                        contato_nome=nome_paciente,
                        contato_telefone=contato_telefone,
                        contato_tipo=contato_tipo,
                        perfil_id=perfil["id"],
                        vertical_db=vertical_client,
                    )
                except Exception:
                    log.error(
                        "transferir_para_atendente: falha ao atribuir lead | contato=%s | "
                        "handoff=%s",
                        contato_id,
                        handoff["id"],
                        exc_info=True,
                    )

                # Concierge = pessoa específica: já marca a conversa como atribuída
                # a ela no CORE (agente_ativo continua True — o agente segue até ela
                # assumir). Assim a conversa aparece na lista dela como "aguardando
                # você" antes de assumir. (Fila de atendente NÃO faz isso: fica
                # atribuido_para=null até o 1º assumir.) Best-effort: colunas
                # atribuido_para/atribuido_nome podem não existir em todo ambiente.
                try:
                    core_db.table("conversas").update(
                        {
                            "atribuido_para": perfil["id"],
                            "atribuido_nome": perfil["nome"],
                        }
                    ).eq("id", conversa_id).eq("tenant_id", tenant_id).execute()
                except Exception:
                    log.warning(
                        "transferir_para_atendente: não foi possível pré-atribuir "
                        "conversa ao concierge | conversa=%s | handoff=%s",
                        conversa_id,
                        handoff["id"],
                        exc_info=True,
                    )

            # ── Notificação no CRM (crm_notificacoes) ────────────────────────
            # concierge → notifica só ela. atendente → notifica TODOS os
            # atendentes ativos do tenant (fila: quem assumir primeiro pega).
            if role_destino == "atendente":
                destinatarios = (
                    vertical_client.table("perfis")
                    .select("id")
                    .eq("tenant_id", tenant_id)
                    .eq("role", "atendente")
                    .eq("ativo", True)
                    .execute()
                ).data or [{"id": perfil["id"]}]
            else:
                destinatarios = [{"id": perfil["id"]}]

            notificar_handoff(
                vertical_db=vertical_client,
                tenant_id=tenant_id,
                perfil_ids=[dest["id"] for dest in destinatarios],
                contato_id=contato_id,
                conversa_id=conversa_id,
                titulo="Novo paciente aguardando",
                corpo=f"{status_lead} · {nome_paciente} — {motivo}",
            )

            # ── Telefone que o PACIENTE recebe na confirmação ────────────────
            # atendente → recepção genérica (tenants.telefone_atendente).
            # concierge → telefone do cadastro do concierge (nunca hardcode).
            if role_destino == "concierge":
                telefone_destino = perfil.get("telefone")
            else:
                tenant_row = (
                    core_db.table("tenants")
                    .select("telefone_atendente")
                    .eq("id", tenant_id)
                    .limit(1)
                    .execute()
                )
                telefone_destino = (
                    tenant_row.data[0].get("telefone_atendente") if tenant_row.data else None
                )

            # ── Alerta WhatsApp pessoal ao perfil designado (interno) ────────
            telefone_perfil = perfil.get("telefone")
            if telefone_perfil:
                texto_whatsapp = (
                    "🔔 *Nova conversa atribuída*\n\n"
                    f"{status_lead}\n"
                    f"*Paciente:* {nome_paciente}\n"
                    f"*Assunto:* {motivo}\n\n"
                    "*Resumo:*\n"
                    f"{resumo}"
                )
                # A concierge (Laís) inicia a conversa pelo WhatsApp DELA, fora do
                # CRM — então mandamos um link wa.me clicável para o número do
                # paciente. Atendente responde pelo CRM, não precisa do link.
                if role_destino == "concierge" and contato_telefone:
                    wa_link = "https://wa.me/" + re.sub(r"\D", "", contato_telefone)
                    texto_whatsapp += f"\n\n*Iniciar conversa com o paciente:*\n{wa_link}"
                try:
                    enviado = await enviar_mensagem_tenant(
                        tenant_id=tenant_id,
                        numero_destino=telefone_perfil,
                        texto=texto_whatsapp,
                        core_db=core_db,
                    )
                    if not enviado:
                        log.warning(
                            "transferir_para_atendente: falha ao notificar WhatsApp pessoal "
                            "| perfil=%s | handoff=%s",
                            perfil["id"],
                            handoff["id"],
                        )
                except Exception:
                    log.error(
                        "transferir_para_atendente: erro ao notificar WhatsApp pessoal "
                        "| perfil=%s | handoff=%s",
                        perfil["id"],
                        handoff["id"],
                        exc_info=True,
                    )

            # ── Texto de confirmação ao paciente (com o telefone de destino) ──
            contato_str = f" O contato é: {telefone_destino}." if telefone_destino else ""
            if role_destino == "concierge":
                return (
                    f"Entendido! Vou te conectar com {perfil['nome']}, nossa concierge. "
                    f"Ela já foi notificada e entrará em contato em breve.{contato_str} "
                    "Se precisar de mais alguma coisa, é só me chamar aqui! 😊"
                )
            return (
                "Perfeito! Encaminhei você para a nossa equipe de atendimento. "
                f"Em breve alguém entra em contato.{contato_str} "
                "Se precisar de mais alguma coisa, pode falar comigo por aqui! 😊"
            )

        return transferir_para_atendente

    return factory


# ─── Registro ────────────────────────────────────────────────────────────────


def registrar(registry: ToolRegistry, vertical_client: Client) -> None:
    """Registra transferir_para_atendente como exclusiva do vertical clínica.

    A disponibilidade real por tenant (tenant_config.handoff.habilitado) é
    filtrada em core/nodes — não aqui. Aqui é só "este vertical tem essa
    capacidade"; "este tenant pode usá-la" é decidido por turno, não por
    registro global no startup.
    """
    registry.register_vertical("clinica", _make_transferir_para_atendente(vertical_client))
