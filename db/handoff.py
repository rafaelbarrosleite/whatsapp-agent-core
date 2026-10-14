"""db/handoff.py — fila de transferência agente → atendente humano (round-robin).

handoff_fila e perfis.ultimo_handoff_em vivem no banco VERTICAL (painel CRM).
conversas vive no banco CORE — devolver_para_agente é a única função que toca
os dois bancos. Nenhuma função aqui confia em tenant_id vindo do LLM: todo
chamador (a tool transferir_para_atendente) já recebe tenant_id lacrado em
closure, igual às demais tools do projeto.
"""

import logging
from datetime import datetime, timezone

from core.telefone import normalizar_telefone
from supabase import Client

logger = logging.getLogger(__name__)


async def selecionar_proximo_atendente(
    tenant_id: str,
    role: str,
    vertical_db: Client,
) -> dict | None:
    """Seleciona o próximo atendente ativo por round-robin.

    Critério: perfis WHERE tenant_id = ? AND role = ? AND ativo = true.
    Ordenado por ultimo_handoff_em ASC NULLS FIRST — quem nunca atendeu vai
    primeiro; depois quem atendeu há mais tempo. Retorna o perfil completo
    ou None se não houver ninguém disponível.
    """
    resultado = (
        vertical_db.table("perfis")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("role", role)
        .eq("ativo", True)
        .order("ultimo_handoff_em", desc=False, nullsfirst=True)
        .limit(1)
        .execute()
    )
    return resultado.data[0] if resultado.data else None


async def registrar_handoff(
    tenant_id: str,
    conversa_id: str,
    contato_id: str,
    perfil: dict,
    motivo: str,
    resumo_conversa: str,
    vertical_db: Client,
) -> dict:
    """Insere registro em handoff_fila com status='aguardando' e marca o
    perfil selecionado como tendo recebido um handoff agora (avança seu lugar
    no round-robin). Retorna o registro de handoff_fila criado."""
    agora = datetime.now(timezone.utc).isoformat()

    inserido = (
        vertical_db.table("handoff_fila")
        .insert(
            {
                "tenant_id": tenant_id,
                "conversa_id": conversa_id,
                "contato_id": contato_id,
                "perfil_id": perfil["id"],
                "perfil_nome": perfil["nome"],
                "perfil_role": perfil["role"],
                "perfil_telefone": perfil.get("telefone"),
                "status": "aguardando",
                "motivo": motivo,
                "resumo_conversa": resumo_conversa,
                "ultimo_handoff_em": agora,
            }
        )
        .execute()
    )

    (
        vertical_db.table("perfis")
        .update({"ultimo_handoff_em": agora})
        .eq("id", perfil["id"])
        .eq("tenant_id", tenant_id)
        .execute()
    )

    return inserido.data[0]


async def assumir_handoff(
    handoff_id: str,
    tenant_id: str,
    vertical_db: Client,
) -> None:
    """Marca o handoff como assumido pelo atendente (o painel CRM chama isso
    quando o atendente abre a conversa)."""
    (
        vertical_db.table("handoff_fila")
        .update(
            {
                "status": "em_atendimento",
                "assumido_em": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("id", handoff_id)
        .eq("tenant_id", tenant_id)
        .execute()
    )


async def devolver_para_agente(
    conversa_id: str,
    tenant_id: str,
    core_db: Client,
    vertical_db: Client,
) -> None:
    """Devolve a conversa para o agente e encerra o handoff ativo.

    1. CORE: agente_ativo=True, estado='aberta' — agente_ativo/estado são as
       colunas usadas hoje por db/conversas.agente_ativo()/obter_ou_criar_conversa()
       e por isso são escritas de forma incondicional (qualquer falha aqui deve
       propagar — sem isso o gate de agente_ativo trava a conversa).
       status/atribuido_para são escritas em melhor esforço: existem na CRM
       (painel CRM) mas não são lidas por este backend — uma falha aqui
       (ex.: coluna ainda não propagada para este ambiente) não deve impedir
       a devolução real do controle ao agente.
    2. VERTICAL: handoff_fila SET status='devolvido', encerrado_em=now()
       para os handoffs ainda abertos dessa conversa.
    """
    core_db.table("conversas").update(
        {
            "agente_ativo": True,
            "estado": "aberta",
        }
    ).eq("id", conversa_id).eq("tenant_id", tenant_id).execute()

    try:
        core_db.table("conversas").update(
            {
                "status": "aberta",
                "atribuido_para": None,
            }
        ).eq("id", conversa_id).eq("tenant_id", tenant_id).execute()
    except Exception:
        logger.warning(
            "devolver_para_agente: não foi possível atualizar status/atribuido_para "
            "(colunas podem não existir neste ambiente) | conversa=%s",
            conversa_id,
            exc_info=True,
        )

    (
        vertical_db.table("handoff_fila")
        .update(
            {
                "status": "devolvido",
                "encerrado_em": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("conversa_id", conversa_id)
        .eq("tenant_id", tenant_id)
        .in_("status", ["aguardando", "em_atendimento"])
        .execute()
    )


async def buscar_lead_por_contato(
    vertical_db: Client,
    tenant_id: str,
    contato_id: str,
    telefone: str | None = None,
) -> dict | None:
    """Retorna o lead do contato (id, created_at) ou None. Busca por contato_id
    e, se não achar, por telefone canônico — assim pega o lead criado no CRM (que
    pode não ter contato_id) e trata '67999991234' == '+5567999991234'."""
    r = (
        vertical_db.table("leads")
        .select("id, created_at")
        .eq("tenant_id", tenant_id)
        .eq("contato_id", contato_id)
        .limit(1)
        .execute()
    )
    if r.data:
        return r.data[0]
    if telefone:
        r = (
            vertical_db.table("leads")
            .select("id, created_at")
            .eq("tenant_id", tenant_id)
            .eq("telefone", normalizar_telefone(telefone))
            .limit(1)
            .execute()
        )
        if r.data:
            return r.data[0]
    return None


async def atribuir_lead_por_contato(
    tenant_id: str,
    contato_id: str,
    contato_nome: str | None,
    contato_telefone: str | None,
    contato_tipo: str | None,
    perfil_id: str,
    vertical_db: Client,
) -> str | None:
    """Atribui o lead do contato ao perfil destino, preservando a regra de
    promoção irreversível lead→paciente:

      1. Existe lead p/ o contato_id  → seta leads.atribuido_a nesse lead.
      2. Não existe lead E tipo='paciente' → NÃO cria lead. A atribuição já
         vale sobre o atendimento (conversa pausada + handoff_fila +
         notificação). Retorna None.
      3. Não existe lead E tipo != 'paciente' (lead) → cria lead e atribui.

    leads.atribuido_a alimenta o bloco "Meus leads" do Inbox. Retorna o
    lead_id afetado, ou None quando é paciente sem lead.
    """
    agora = datetime.now(timezone.utc).isoformat()

    existente = (
        vertical_db.table("leads")
        .select("id")
        .eq("tenant_id", tenant_id)
        .eq("contato_id", contato_id)
        .limit(1)
        .execute()
    )
    if existente.data:
        lead_id = existente.data[0]["id"]
        vertical_db.table("leads").update({"atribuido_a": perfil_id, "atribuido_em": agora}).eq(
            "id", lead_id
        ).eq("tenant_id", tenant_id).execute()
        return lead_id

    if contato_tipo == "paciente":
        logger.info(
            "atribuir_lead_por_contato: contato %s é paciente sem lead — "
            "não cria lead; atribuição vale sobre o atendimento | tenant=%s",
            contato_id,
            tenant_id,
        )
        return None

    criado = (
        vertical_db.table("leads")
        .insert(
            {
                "tenant_id": tenant_id,
                "contato_id": contato_id,
                "nome": contato_nome,
                "telefone": contato_telefone,
                "origem": "whatsapp",
                "estagio": "em_conversa",
                "atribuido_a": perfil_id,
                "atribuido_em": agora,
            }
        )
        .execute()
    )
    return criado.data[0]["id"] if criado.data else None


def notificar_handoff(
    vertical_db: Client,
    tenant_id: str,
    perfil_ids: list[str],
    contato_id: str,
    conversa_id: str,
    titulo: str,
    corpo: str | None,
    tipo: str = "handoff_solicitado",
) -> None:
    """Insere notificações em crm_notificacoes (banco VERTICAL) para cada perfil
    destino. Ponto ÚNICO de notificação de handoff/atribuição — reutilizado pelo
    handoff automático (tool transferir_para_atendente) e pelo manual (painel CRM
    → app/atendente_router.atribuir), para os dois caminhos notificarem igual.

    Best-effort por perfil: uma falha de insert é logada mas não interrompe os
    demais nem a operação de negócio que chamou (a atribuição já foi feita). O
    frontend do CRM reage em tempo real ao INSERT (perfil_id=eq.<perfil>)."""
    for perfil_id in perfil_ids:
        try:
            vertical_db.table("crm_notificacoes").insert(
                {
                    "tenant_id": tenant_id,
                    "perfil_id": perfil_id,
                    "tipo": tipo,
                    "titulo": titulo,
                    "corpo": corpo,
                    "contato_id": contato_id,
                    "conversa_id": conversa_id,
                    "lida": False,
                }
            ).execute()
        except Exception:
            logger.error(
                "notificar_handoff: falha ao gravar crm_notificacoes | perfil=%s | "
                "conversa=%s | tipo=%s",
                perfil_id,
                conversa_id,
                tipo,
                exc_info=True,
            )


def mover_lead_para_com_atendente(vertical_db: Client, tenant_id: str, contato_id: str) -> None:
    """Move o lead do contato de 'em_conversa' para 'com_atendente' quando um
    humano assume a conversa (assumir/pausar) — reflete no funil do CRM que já
    tem gente atendendo. Só avança a partir de 'em_conversa' (não retrocede
    estágios mais avançados como consulta_agendada). Best-effort."""
    try:
        (
            vertical_db.table("leads")
            .update({"estagio": "com_atendente"})
            .eq("tenant_id", tenant_id)
            .eq("contato_id", contato_id)
            .eq("estagio", "em_conversa")
            .execute()
        )
    except Exception:
        logger.warning(
            "mover_lead_para_com_atendente: falha ao mover lead | contato=%s | tenant=%s",
            contato_id,
            tenant_id,
            exc_info=True,
        )


async def buscar_handoff_ativo(
    conversa_id: str,
    tenant_id: str,
    vertical_db: Client,
) -> dict | None:
    """Retorna o handoff_fila mais recente com status IN ('aguardando',
    'em_atendimento') para a conversa, ou None se não houver handoff ativo."""
    resultado = (
        vertical_db.table("handoff_fila")
        .select("*")
        .eq("conversa_id", conversa_id)
        .eq("tenant_id", tenant_id)
        .in_("status", ["aguardando", "em_atendimento"])
        .order("criado_em", desc=True)
        .limit(1)
        .execute()
    )
    return resultado.data[0] if resultado.data else None
