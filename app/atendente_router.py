"""app/atendente_router.py — endpoint chamado pelo painel CRM (Next.js, repositório separado)
quando um atendente humano responde a um paciente pelo painel, não pelo
WhatsApp dele mesmo.

Não confundir com gateway/webhook*.py: aqueles recebem mensagens DO paciente
via WhatsApp; este recebe a resposta DO atendente via HTTP do CRM e a envia
ao paciente pelo canal WhatsApp do tenant.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from channels.dispatcher import enviar_mensagem_tenant, enviar_midia_tenant, sender_do_tenant
from channels.templates import parametro, template_da_clinica
from core.mensagem_prefixo import prefixar_mensagem_agente
from db.client import get_supabase
from db.conversas import salvar_mensagem
from db.handoff import (
    assumir_handoff,
    atribuir_lead_por_contato,
    buscar_handoff_ativo,
    devolver_para_agente,
    mover_lead_para_com_atendente,
    notificar_handoff,
)
from db.vertical_client import get_vertical_supabase
from security.token_compartilhado import token_valido

log = logging.getLogger(__name__)
router = APIRouter()


def _verificar_auth(authorization: str | None) -> None:
    # Tempo constante (token_valido): `!=` deixaria medir a chave pelo tempo.
    esquema, _, recebido = (authorization or "").partition(" ")
    if esquema != "Bearer" or not token_valido(recebido or None, settings.internal_api_key):
        raise HTTPException(status_code=401, detail="Não autorizado")


def _buscar_perfil(
    vertical,
    perfil_id: str,
    tenant_id: str,
    roles: list[str] | None = None,
) -> dict:
    query = (
        vertical.table("perfis")
        .select("id, nome, role, tenant_id")
        .eq("id", perfil_id)
        .eq("tenant_id", tenant_id)
    )
    if roles:
        query = query.in_("role", roles)
    resultado = query.limit(1).execute()
    if not resultado.data:
        raise HTTPException(status_code=404, detail="Perfil não encontrado para este tenant")
    return resultado.data[0]


def _buscar_conversa(core, conversa_id: str, tenant_id: str) -> dict:
    resultado = (
        core.table("conversas")
        .select("id, tenant_id, contato_id, agente_ativo, atribuido_para, canal_id")
        .eq("id", conversa_id)
        .eq("tenant_id", tenant_id)
        .limit(1)
        .execute()
    )
    if not resultado.data:
        raise HTTPException(status_code=404, detail="Conversa não encontrada para este tenant")
    return resultado.data[0]


def _atualizar_atribuicao_melhor_esforco(
    core,
    conversa_id: str,
    tenant_id: str,
    perfil: dict | None,
) -> None:
    """atribuido_para/atribuido_nome são melhor esforço — mesma observação de
    db.handoff.devolver_para_agente: existem na CRM mas uma falha aqui (coluna
    ainda não propagada para este ambiente) não deve quebrar a operação real."""
    try:
        core.table("conversas").update(
            {
                "atribuido_para": perfil["id"] if perfil else None,
                "atribuido_nome": perfil["nome"] if perfil else None,
            }
        ).eq("id", conversa_id).eq("tenant_id", tenant_id).execute()
    except Exception:
        log.warning(
            "atendente_router: não foi possível atualizar atribuido_para/atribuido_nome "
            "(colunas podem não existir neste ambiente) | conversa=%s",
            conversa_id,
            exc_info=True,
        )


class MensagemAtendenteBody(BaseModel):
    perfil_id: str
    texto: str
    tenant_id: str


class EnviarArquivoBody(BaseModel):
    perfil_id: str
    tenant_id: str
    link: str  # signed URL (Storage) que a Meta busca no envio
    storage_path: str
    mime_type: str
    tipo: str  # 'imagem' | 'audio' | 'documento'
    nome_original: str | None = None
    caption: str | None = None
    tamanho_bytes: int | None = None


class AvisoAtraso(BaseModel):
    contato_id: str
    texto: str = Field(min_length=1, max_length=1000)
    # {{1}} primeiro nome, {{2}} clínica, {{3}} minutos de atraso, {{4}} horário —
    # usados quando a clínica tem o template "aviso_atraso" aprovado na Meta.
    parametros: list[str] | None = Field(default=None, max_length=4)


class AvisoAtrasoBody(BaseModel):
    tenant_id: str
    perfil_id: str
    avisos: list[AvisoAtraso] = Field(min_length=1, max_length=50)


class PausarBody(BaseModel):
    tenant_id: str
    perfil_id: str


class AtribuirBody(BaseModel):
    tenant_id: str
    perfil_id: str


class AssumirBody(BaseModel):
    tenant_id: str
    perfil_id: str


class DevolverBody(BaseModel):
    tenant_id: str


@router.post("/api/conversas/{conversa_id}/pausar")
async def pausar(
    conversa_id: str,
    body: PausarBody,
    authorization: str | None = Header(None),
) -> dict:
    """Pausa o agente para esta conversa — chamado quando um admin/gestor assume
    manualmente pelo painel (fora do fluxo de round-robin de
    transferir_para_atendente). Quem pausou vira o responsável imediato."""
    _verificar_auth(authorization)

    vertical = get_vertical_supabase()
    perfil = _buscar_perfil(vertical, body.perfil_id, body.tenant_id)

    core = get_supabase()
    conversa = _buscar_conversa(core, conversa_id, body.tenant_id)

    core.table("conversas").update(
        {
            "agente_ativo": False,
            "estado": "aguardando_humano",
        }
    ).eq("id", conversa_id).eq("tenant_id", body.tenant_id).execute()

    _atualizar_atribuicao_melhor_esforco(core, conversa_id, body.tenant_id, perfil)
    # Humano assumiu → move o lead para "com_atendente" no funil (best-effort).
    mover_lead_para_com_atendente(vertical, body.tenant_id, conversa["contato_id"])

    return {"sucesso": True}


@router.post("/api/conversas/{conversa_id}/atribuir")
async def atribuir(
    conversa_id: str,
    body: AtribuirBody,
    authorization: str | None = Header(None),
) -> dict:
    """Atribui/reatribui uma conversa já pausada a um atendente ou concierge."""
    _verificar_auth(authorization)

    vertical = get_vertical_supabase()
    perfil = _buscar_perfil(
        vertical,
        body.perfil_id,
        body.tenant_id,
        roles=["atendente", "concierge"],
    )

    core = get_supabase()
    conversa = _buscar_conversa(core, conversa_id, body.tenant_id)

    if conversa.get("agente_ativo"):
        raise HTTPException(
            status_code=400,
            detail="Conversa ainda está com o agente ativo — pause o agente antes de atribuir.",
        )

    _atualizar_atribuicao_melhor_esforco(core, conversa_id, body.tenant_id, perfil)

    try:
        vertical.table("handoff_fila").update(
            {
                "perfil_id": perfil["id"],
                "perfil_nome": perfil["nome"],
                "perfil_role": perfil["role"],
            }
        ).eq("conversa_id", conversa_id).eq("tenant_id", body.tenant_id).in_(
            "status", ["aguardando", "em_atendimento"]
        ).execute()
    except Exception:
        log.warning(
            "atendente_router.atribuir: não foi possível reatribuir handoff_fila ativo "
            "| conversa=%s",
            conversa_id,
            exc_info=True,
        )

    # Notifica o perfil recém-atribuído — MESMO ponto único usado pelo handoff
    # automático (db.handoff.notificar_handoff). Antes o caminho manual não
    # notificava ninguém: o atendente reatribuído não sabia que tinha conversa.
    nome_paciente = "um paciente"
    try:
        contato_res = (
            core.table("contatos")
            .select("nome")
            .eq("id", conversa["contato_id"])
            .eq("tenant_id", body.tenant_id)
            .limit(1)
            .execute()
        )
        if contato_res.data and contato_res.data[0].get("nome"):
            nome_paciente = contato_res.data[0]["nome"]
    except Exception:
        log.warning(
            "atendente_router.atribuir: não foi possível buscar nome do contato p/ "
            "notificação | conversa=%s",
            conversa_id,
            exc_info=True,
        )

    notificar_handoff(
        vertical_db=vertical,
        tenant_id=body.tenant_id,
        perfil_ids=[perfil["id"]],
        contato_id=conversa["contato_id"],
        conversa_id=conversa_id,
        titulo="Uma conversa foi atribuída a você",
        corpo=f"Paciente: {nome_paciente}",
        tipo="conversa_atribuida",
    )

    return {"sucesso": True}


@router.post("/api/conversas/{conversa_id}/assumir")
async def assumir(
    conversa_id: str,
    body: AssumirBody,
    authorization: str | None = Header(None),
) -> dict:
    """Assume um handoff aguardando. É AQUI que o agente é pausado (mudança de
    fluxo 2026-07-12): a transferência não pausa mais — o agente segue respondendo
    até alguém assumir. O primeiro a assumir vence (lock atômico em handoff_fila);
    os demais recebem 409, evitando dois humanos ativos na mesma conversa."""
    _verificar_auth(authorization)

    vertical = get_vertical_supabase()
    perfil = _buscar_perfil(
        vertical,
        body.perfil_id,
        body.tenant_id,
        roles=["atendente", "concierge"],
    )

    core = get_supabase()
    conversa = _buscar_conversa(core, conversa_id, body.tenant_id)

    handoff_ativo = await buscar_handoff_ativo(conversa_id, body.tenant_id, vertical)
    if not handoff_ativo:
        raise HTTPException(
            status_code=404,
            detail="Handoff ativo não encontrado para esta conversa",
        )

    # Lock atômico: só o 1º que transiciona 'aguardando' → 'em_atendimento'
    # vence. UPDATE ... WHERE status='aguardando' é atômico no Postgres; se
    # outro atendente já assumiu, .data volta vazio → 409.
    if handoff_ativo["status"] != "aguardando":
        raise HTTPException(
            status_code=409,
            detail="Esta conversa já foi assumida por outro atendente.",
        )

    lock = (
        vertical.table("handoff_fila")
        .update(
            {
                "status": "em_atendimento",
                "assumido_em": datetime.now(timezone.utc).isoformat(),
                "perfil_id": perfil["id"],
                "perfil_nome": perfil["nome"],
                "perfil_role": perfil["role"],
            }
        )
        .eq("id", handoff_ativo["id"])
        .eq("tenant_id", body.tenant_id)
        .eq("status", "aguardando")
        .execute()
    )
    if not lock.data:
        raise HTTPException(
            status_code=409,
            detail="Esta conversa já foi assumida por outro atendente.",
        )

    # Agora sim pausa o agente para esta conversa.
    core.table("conversas").update(
        {
            "agente_ativo": False,
            "estado": "aguardando_humano",
        }
    ).eq("id", conversa_id).eq("tenant_id", body.tenant_id).execute()
    _atualizar_atribuicao_melhor_esforco(core, conversa_id, body.tenant_id, perfil)

    # Confirma/atualiza leads.atribuido_a p/ quem assumiu — cobre a fila
    # genérica de atendente (que não tinha destinatário fixo na transferência).
    try:
        contato_res = (
            core.table("contatos")
            .select("nome, telefone, tipo")
            .eq("id", conversa["contato_id"])
            .eq("tenant_id", body.tenant_id)
            .limit(1)
            .execute()
        )
        c = contato_res.data[0] if contato_res.data else {}
        await atribuir_lead_por_contato(
            tenant_id=body.tenant_id,
            contato_id=conversa["contato_id"],
            contato_nome=c.get("nome"),
            contato_telefone=c.get("telefone"),
            contato_tipo=c.get("tipo"),
            perfil_id=perfil["id"],
            vertical_db=vertical,
        )
    except Exception:
        log.error(
            "assumir: falha ao atribuir lead a quem assumiu | conversa=%s",
            conversa_id,
            exc_info=True,
        )

    # Humano assumiu → move o lead para "com_atendente" no funil (best-effort).
    mover_lead_para_com_atendente(vertical, body.tenant_id, conversa["contato_id"])

    return {"sucesso": True, "status": "em_atendimento"}


@router.post("/api/conversas/{conversa_id}/devolver")
async def devolver(
    conversa_id: str,
    body: DevolverBody,
    authorization: str | None = Header(None),
) -> dict:
    """Devolve a conversa ao agente — encerra o handoff ativo, se houver, e
    reativa o agente automático."""
    _verificar_auth(authorization)

    core = get_supabase()
    _buscar_conversa(core, conversa_id, body.tenant_id)

    vertical = get_vertical_supabase()
    await devolver_para_agente(conversa_id, body.tenant_id, core, vertical)

    return {"sucesso": True}


@router.post("/api/conversas/{conversa_id}/mensagem-atendente")
async def mensagem_atendente(
    conversa_id: str,
    body: MensagemAtendenteBody,
    authorization: str | None = Header(None),
) -> dict:
    _verificar_auth(authorization)

    if not body.tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id é obrigatório")

    vertical = get_vertical_supabase()
    perfil = _buscar_perfil(vertical, body.perfil_id, body.tenant_id)

    core = get_supabase()
    conversa = _buscar_conversa(core, conversa_id, body.tenant_id)

    if conversa.get("agente_ativo"):
        raise HTTPException(
            status_code=400,
            detail="Conversa ainda está com o agente ativo — pause o agente antes de responder.",
        )

    # Só quem assumiu (atribuido_para) pode responder. Sem bypass silencioso p/
    # admin: intervir em conversa de outro exige ação explícita (assumir/
    # reatribuir p/ si), não mandar mensagem por cima. Regra também no frontend,
    # mas a de negócio vive aqui — cliente manipulado não fura.
    if conversa.get("atribuido_para") != perfil["id"]:
        raise HTTPException(
            status_code=403,
            detail="Só quem assumiu a conversa pode responder. Assuma a conversa primeiro.",
        )

    contato_resultado = (
        core.table("contatos")
        .select("telefone")
        .eq("id", conversa["contato_id"])
        .eq("tenant_id", body.tenant_id)
        .limit(1)
        .execute()
    )
    if not contato_resultado.data or not contato_resultado.data[0].get("telefone"):
        raise HTTPException(status_code=404, detail="Telefone do paciente não encontrado")
    telefone_paciente = contato_resultado.data[0]["telefone"]

    # Primeira resposta do atendente nesta conversa: transita o handoff de
    # 'aguardando' para 'em_atendimento'. Chamadas seguintes encontram o
    # handoff já em 'em_atendimento' e não fazem nada (idempotente).
    handoff_ativo = await buscar_handoff_ativo(conversa_id, body.tenant_id, vertical)
    if handoff_ativo and handoff_ativo["status"] == "aguardando":
        await assumir_handoff(handoff_ativo["id"], body.tenant_id, vertical)

    texto_prefixado = prefixar_mensagem_agente(body.texto, perfil["nome"])

    enviado = await enviar_mensagem_tenant(
        tenant_id=body.tenant_id,
        numero_destino=telefone_paciente,
        texto=texto_prefixado,
        core_db=core,
        canal_id=conversa.get("canal_id"),
    )
    if not enviado:
        raise HTTPException(status_code=502, detail="Falha ao enviar mensagem via WhatsApp")

    try:
        salvar_mensagem(
            core,
            body.tenant_id,
            conversa_id,
            "atendente",
            texto_prefixado,
            perfil_id=perfil["id"],
        )
    except Exception:
        log.error(
            "mensagem_atendente: mensagem enviada mas falhou ao persistir | conversa=%s",
            conversa_id,
            exc_info=True,
        )

    return {"sucesso": True, "mensagem_enviada": texto_prefixado}


@router.post("/api/conversas/{conversa_id}/enviar-arquivo")
async def enviar_arquivo(
    conversa_id: str,
    body: EnviarArquivoBody,
    authorization: str | None = Header(None),
) -> dict:
    """Envia uma imagem/áudio/documento (que a atendente anexou ou gravou no
    CRM) ao paciente via WhatsApp. Regras do mensagem-atendente: só quem assumiu
    responde.
    O arquivo já foi salvo no Storage pelo CRM; aqui só enviamos via Meta (por
    `link` = signed URL), persistimos a mensagem com a referência da mídia e
    registramos em documentos_paciente (origem='clinica') para a aba Arquivos."""
    _verificar_auth(authorization)

    if not body.tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id é obrigatório")

    vertical = get_vertical_supabase()
    perfil = _buscar_perfil(vertical, body.perfil_id, body.tenant_id)

    core = get_supabase()
    conversa = _buscar_conversa(core, conversa_id, body.tenant_id)

    if conversa.get("agente_ativo"):
        raise HTTPException(
            status_code=400,
            detail="Conversa ainda está com o agente ativo — pause o agente antes de responder.",
        )
    if conversa.get("atribuido_para") != perfil["id"]:
        raise HTTPException(
            status_code=403,
            detail="Só quem assumiu a conversa pode responder. Assuma a conversa primeiro.",
        )

    contato_resultado = (
        core.table("contatos")
        .select("telefone")
        .eq("id", conversa["contato_id"])
        .eq("tenant_id", body.tenant_id)
        .limit(1)
        .execute()
    )
    if not contato_resultado.data or not contato_resultado.data[0].get("telefone"):
        raise HTTPException(status_code=404, detail="Telefone do paciente não encontrado")
    telefone_paciente = contato_resultado.data[0]["telefone"]

    # Idempotente: 1ª ação do atendente transita o handoff aguardando→em_atendimento.
    handoff_ativo = await buscar_handoff_ativo(conversa_id, body.tenant_id, vertical)
    if handoff_ativo and handoff_ativo["status"] == "aguardando":
        await assumir_handoff(handoff_ativo["id"], body.tenant_id, vertical)

    # Áudio (mensagem de voz da atendente): a Meta ignora caption/filename em
    # mensagens do tipo `audio` — nem enviamos. O CRM e o app não oferecem
    # legenda ao gravar; texto vai como mensagem própria.
    eh_audio = body.tipo == "audio"
    caption = (
        prefixar_mensagem_agente(body.caption, perfil["nome"])
        if body.caption and not eh_audio
        else None
    )

    enviado = await enviar_midia_tenant(
        tenant_id=body.tenant_id,
        numero_destino=telefone_paciente,
        link=body.link,
        tipo=body.tipo,
        core_db=core,
        caption=caption,
        filename=body.nome_original,
        canal_id=conversa.get("canal_id"),
    )
    if not enviado:
        raise HTTPException(status_code=502, detail="Falha ao enviar arquivo via WhatsApp")

    midia_ref = {
        "storage_path": body.storage_path,
        "mime_type": body.mime_type,
        "nome_original": body.nome_original,
        "tipo": body.tipo,
    }
    if eh_audio:
        # Rótulo fixo: o nome do arquivo gravado ("audio-1753…​.ogg") não diz
        # nada no histórico — o CRM/app renderizam o player a partir da mídia.
        conteudo = "[áudio]"
    else:
        conteudo = body.caption or (
            body.nome_original or ("[imagem]" if body.tipo == "imagem" else "[documento]")
        )
    try:
        salvar_mensagem(
            core,
            body.tenant_id,
            conversa_id,
            "atendente",
            conteudo,
            metadata={"midia": midia_ref},
            perfil_id=perfil["id"],
        )
    except Exception:
        log.error(
            "enviar_arquivo: arquivo enviado mas falhou ao persistir mensagem | conversa=%s",
            conversa_id,
            exc_info=True,
        )

    # Registra na aba Arquivos do cadastro (documentos_paciente), marcado como
    # enviado pela clínica. Best-effort: falha aqui não desfaz o envio já feito.
    try:
        doc_payload: dict = {
            "tenant_id": body.tenant_id,
            "contato_id": conversa["contato_id"],
            "tipo": body.tipo,
            "mime_type": body.mime_type,
            "storage_path": body.storage_path,
            "nome_original": body.nome_original,
            "tamanho_bytes": body.tamanho_bytes,
            "origem": "clinica",
            "enviado_por": perfil["id"],
        }
        vertical.table("documentos_paciente").insert(doc_payload).execute()
    except Exception:
        log.error(
            "enviar_arquivo: falha ao registrar documento em documentos_paciente | conversa=%s",
            conversa_id,
            exc_info=True,
        )

    return {"sucesso": True}


# Papéis que podem avisar atraso (mesma regra de podeVerClinico no CRM).
_ROLES_RECEPCAO = ["admin", "medico", "gestor", "atendente", "concierge"]


@router.post("/api/recepcao/aviso-atraso")
async def aviso_atraso(
    body: AvisoAtrasoBody,
    authorization: str | None = Header(default=None),
) -> dict:
    """Aviso de atraso da recepção (item 4.1), pelo canal da própria clínica.

    O CRM já escolheu as consultas de hoje que ainda aguardam o paciente e
    montou o texto. Aqui o telefone sai do banco pelo contato_id, filtrado pela
    clínica: um contato de outra clínica (ou inexistente) conta como falha e
    não recebe nada. Texto livre: fora da janela de 24 h da Meta a entrega pode
    falhar até existir o template (item 3.1).
    """
    _verificar_auth(authorization)

    vertical = get_vertical_supabase()
    _buscar_perfil(vertical, body.perfil_id, body.tenant_id, roles=_ROLES_RECEPCAO)

    core = get_supabase()
    ids = list({a.contato_id for a in body.avisos})
    resultado = (
        core.table("contatos")
        .select("id, telefone")
        .eq("tenant_id", body.tenant_id)
        .in_("id", ids)
        .execute()
    )
    telefones = {c["id"]: c.get("telefone") for c in (resultado.data or [])}

    tenant_row = core.table("tenants").select("config").eq("id", body.tenant_id).limit(1).execute()
    config = (tenant_row.data[0].get("config") if tenant_row.data else None) or {}
    template = template_da_clinica(config, "aviso_atraso")

    sender = sender_do_tenant(body.tenant_id, core)
    if sender is None:
        raise HTTPException(
            status_code=503,
            detail="O canal WhatsApp da clínica não está configurado. Fale com o suporte.",
        )

    enviados = 0
    falhas = 0
    for aviso in body.avisos:
        telefone = telefones.get(aviso.contato_id)
        if not telefone:
            falhas += 1
            continue
        if template and aviso.parametros and len(aviso.parametros) == 4:
            ok = await sender.enviar_template(
                telefone, template.nome, template.idioma, [parametro(p) for p in aviso.parametros]
            )
        else:
            ok = await sender.enviar_texto(telefone, aviso.texto)
        if ok:
            enviados += 1
        else:
            falhas += 1

    log.info(
        "Aviso de atraso | tenant=%s | perfil=%s | enviados=%d | falhas=%d",
        body.tenant_id,
        body.perfil_id,
        enviados,
        falhas,
    )
    return {"enviados": enviados, "falhas": falhas}
