"""core/pipeline.py — pipeline de negócio do webhook da API oficial da Meta
(Cloud API).

Ordem fixa: transcrição de áudio → agente_ativo (handoff humano) → gate LGPD
→ resposta de lembrete → resposta de NPS → mídia visual → grafo LangGraph →
envio da resposta → persistência. Os dois webhooks resolvem tenant + sender
e chamam processar_mensagem_entrada — nenhuma lógica de negócio vive nos
arquivos de webhook.
"""

import base64
import logging
import uuid

from langchain_core.messages import AIMessage, HumanMessage

from app.config import settings
from channels.audio_transcriber import baixar_midia_meta, transcrever_audio
from channels.base import MensagemRecebida, TipoMensagem
from core.graph import RECURSION_LIMIT, criar_grafo
from core.mensagem_prefixo import prefixar_mensagem_agente
from core.registro_painel import registrar_erro
from db.client import get_supabase
from db.conversas import (
    obter_ou_criar_conversa,
    resolver_contato_id,
    resolver_conversa_para_entrada,
)
from db.conversas import salvar_mensagem as salvar_mensagem_db
from db.vertical_client import get_vertical_supabase
from gateway.consent_handler import handle_consentimento
from gateway.lembrete_handler import handle_resposta_lembrete
from gateway.nps_handler import handle_resposta_nps
from gateway.tenant_resolver import TenantInfo

logger = logging.getLogger(__name__)

_METADATA_APRESENTACAO_INICIAL = "apresentacao_inicial_enviada"


def _apresentacao_padrao(nome_clinica: str, nome_agente: str) -> str:
    """Saudação para clínica que não escreveu a sua (sem nome de médico fixo)."""
    clinica = f" da *{nome_clinica.strip()}*" if (nome_clinica or "").strip() else ""
    return (
        f"Olá! Sou a *{nome_agente}*, assistente virtual{clinica} 😊\n\n"
        "Estou aqui para te ajudar com informações, agendamentos e dúvidas.\n\n"
        "Como posso te ajudar hoje?"
    )


_MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/heic": "heic",
    "image/heif": "heif",
    "application/pdf": "pdf",
}

# Tamanho máximo que passamos ao Claude (10 MB). Arquivos maiores são salvos
# mas não enviados para análise — o agente informa ao paciente.
_MAX_BYTES_CLAUDE = 10 * 1024 * 1024

# Grafo com MemorySaver global — histórico persiste enquanto o servidor estiver
# rodando. Substituído por grafo com PostgresSaver no lifespan do app/main.py.
_grafo = criar_grafo()


def _buscar_metadata_conversa(core, tenant_id: str, conversa_id: str) -> dict:
    result = (
        core.table("conversas")
        .select("metadata")
        .eq("id", conversa_id)
        .eq("tenant_id", tenant_id)
        .limit(1)
        .execute()
    )
    if not isinstance(result.data, list) or not result.data:
        return {}
    return result.data[0].get("metadata") or {}


def _salvar_metadata_conversa(
    core,
    tenant_id: str,
    conversa_id: str,
    metadata: dict,
) -> None:
    (
        core.table("conversas")
        .update({"metadata": metadata})
        .eq("id", conversa_id)
        .eq("tenant_id", tenant_id)
        .execute()
    )


def _mensagem_apresentacao_inicial(core, tenant_id: str) -> str:
    """Retorna apresentação inicial configurável por tenant.

    Sem texto configurado, usa a saudação genérica com o nome da clínica e do
    agente (a clínica que já tinha texto próprio o tem gravado na config).
    """
    result = core.table("tenants").select("nome, config").eq("id", tenant_id).limit(1).execute()
    row = result.data[0] if isinstance(result.data, list) and result.data else {}
    config = row.get("config") or {}
    return (
        config.get("mensagem_apresentacao_inicial")
        or (config.get("atendimento") or {}).get("mensagem_apresentacao_inicial")
        or _apresentacao_padrao(
            row.get("nome") or "", config.get("nome_agente") or settings.nome_agente_padrao
        )
    )


def _conteudo_mensagem_paciente(mensagem: MensagemRecebida, midia_ref: dict | None) -> str:
    """Texto salvo em mensagens.conteudo p/ mensagem do paciente. Usa a legenda
    quando houver; senão, para mídia, um rótulo (nome do arquivo ou [imagem]/
    [documento]) para a lista de conversas mostrar algo legível."""
    if mensagem.texto:
        return mensagem.texto
    if midia_ref:
        return midia_ref.get("nome_original") or (
            "[imagem]" if midia_ref.get("tipo") == "imagem" else "[documento]"
        )
    return ""


def _metadata_mensagem_paciente(message_id: str | None, midia_ref: dict | None) -> dict:
    """Metadata da mensagem do paciente: id do WhatsApp + referência da mídia
    (quando houver) para o CRM renderizar inline no chat."""
    metadata: dict = {"whatsapp_message_id": message_id}
    if midia_ref:
        metadata["midia"] = midia_ref
    return metadata


def _salvar_mensagens_paciente(
    client,
    tenant_id: str,
    conversa_id: str,
    mensagem: MensagemRecebida,
    midia_ref: dict | None = None,
) -> None:
    """Grava a(s) mensagem(ns) do paciente no histórico do CRM.

    Quando a rajada juntou vários textos numa entrada só para o agente
    (gateway/rajada.py → metadata.agrupadas), cada original é gravado
    separado, na ordem, com o próprio whatsapp_message_id.
    """
    agrupadas = mensagem.metadata.get("agrupadas") or []
    if agrupadas:
        for original in agrupadas:
            salvar_mensagem_db(
                client,
                tenant_id,
                conversa_id,
                "paciente",
                original.get("texto") or "",
                _metadata_mensagem_paciente(original.get("message_id"), None),
            )
        return
    salvar_mensagem_db(
        client,
        tenant_id,
        conversa_id,
        "paciente",
        _conteudo_mensagem_paciente(mensagem, midia_ref),
        _metadata_mensagem_paciente(mensagem.message_id, midia_ref),
    )


async def _processar_midia_visual(
    mensagem: MensagemRecebida,
    tenant: TenantInfo,
    contato_id: str | None,
    token: str,
) -> tuple[HumanMessage | None, dict | None]:
    """Baixa imagem/documento da Meta, salva no Supabase Storage e cria registro
    em documentos_paciente. Retorna (HumanMessage multimodal para o agente,
    midia_ref) — o midia_ref ({storage_path, mime_type, nome_original, tipo})
    vai no metadata da mensagem salva, para o CRM exibir a mídia inline no chat.
    Retorna (None, None) se não houver media_id.

    Meta-específico (media_id + GET /{media_id}).
    hoje porque seu adapter não popula media_id para imagem/documento.

    Isolamento multi-tenant garantido pelo path no Storage:
        documentos/{tenant_id}/{contato_id_ou_telefone}/{uuid}.ext
    """
    media_id: str | None = mensagem.metadata.get("media_id")
    mime_type: str = mensagem.metadata.get("mime_type") or "application/octet-stream"
    nome_arquivo: str | None = mensagem.metadata.get("nome_arquivo")

    if not media_id:
        return None, None

    midia_bytes, detected_mime = await baixar_midia_meta(media_id, token)
    mime_type = detected_mime or mime_type
    base_mime = mime_type.split(";")[0].strip()

    eh_imagem = base_mime.startswith("image/")
    eh_pdf = base_mime == "application/pdf"
    tipo_doc = "imagem" if eh_imagem else "documento"
    ext = _MIME_TO_EXT.get(base_mime, "bin")

    # Path isolado por tenant. Usamos o contato_id quando disponível; o número
    # de telefone (digits only) quando o contato ainda não existe no banco.
    pasta_contato = contato_id or mensagem.numero_remetente.lstrip("+")
    storage_path = f"{tenant.tenant_id}/{pasta_contato}/{uuid.uuid4()}.{ext}"

    vertical = get_vertical_supabase()

    vertical.storage.from_("documentos").upload(
        path=storage_path,
        file=midia_bytes,
        file_options={"content-type": base_mime},
    )
    logger.info(
        "Mídia salva no Storage | tenant=%s | tipo=%s | path=%s | bytes=%d",
        tenant.tenant_id,
        tipo_doc,
        storage_path,
        len(midia_bytes),
    )

    doc_payload: dict = {
        "tenant_id": tenant.tenant_id,
        "tipo": tipo_doc,
        "mime_type": mime_type,
        "storage_path": storage_path,
        "nome_original": nome_arquivo,
        "tamanho_bytes": len(midia_bytes),
    }
    if contato_id:
        doc_payload["contato_id"] = contato_id

    vertical.table("documentos_paciente").insert(doc_payload).execute()

    # Referência da mídia p/ o metadata da mensagem (CRM renderiza inline).
    midia_ref: dict = {
        "storage_path": storage_path,
        "mime_type": base_mime,
        "nome_original": nome_arquivo,
        "tipo": tipo_doc,
    }

    # Arquivo muito grande para enviar ao Claude — informa ao agente via texto
    if len(midia_bytes) > _MAX_BYTES_CLAUDE:
        logger.warning(
            "Arquivo muito grande para Claude (%d bytes) | path=%s", len(midia_bytes), storage_path
        )
        tipo_label = "imagem" if eh_imagem else "documento"
        return HumanMessage(
            content=f"[{tipo_label.capitalize()} recebida e salva no prontuário "
            f"({nome_arquivo or base_mime}), mas é grande demais para eu analisar aqui. "
            "O médico poderá visualizá-la no CRM.]"
        ), midia_ref

    b64 = base64.b64encode(midia_bytes).decode()

    if eh_imagem:
        content: list = [
            {"type": "image_url", "image_url": {"url": f"data:{base_mime};base64,{b64}"}},
            {"type": "text", "text": "[Paciente enviou esta imagem via WhatsApp.]"},
        ]
    elif eh_pdf:
        content = [
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
            },
            {"type": "text", "text": "[Paciente enviou este documento PDF via WhatsApp.]"},
        ]
    else:
        # Formato não suportado para visão (docx, xlsx…) — informa ao agente via texto
        content = [
            {
                "type": "text",
                "text": (
                    f"[Paciente enviou um arquivo ({nome_arquivo or base_mime}) "
                    "que foi salvo no prontuário, mas não consigo ler o conteúdo "
                    "deste formato. Informe ao paciente que o médico poderá acessá-lo no CRM.]"
                ),
            }
        ]

    return HumanMessage(content=content), midia_ref


async def processar_mensagem_entrada(
    mensagem: MensagemRecebida,
    tenant: TenantInfo,
    sender,
    provider: str,
) -> None:
    """Executa o pipeline completo e envia a resposta via `sender`.

    Chamada como BackgroundTask pelos webhooks — eles já retornaram 200 antes
    desta função rodar. Qualquer exceção é logada mas não propaga.

    sender: CloudApiSender (ver channels/sender_factory.py),
    expõe enviar_texto(numero, texto) -> bool sempre, e enviar_botoes(numero,
    texto, botoes) -> bool só quando o provider suporta (cloud_api).
    """
    thread_id = f"{tenant.tenant_id}:{mensagem.numero_remetente}"
    logger.info("Iniciando processamento | thread=%s | provider=%s", thread_id, provider)

    client = get_supabase()

    # Transcrever áudio antes de qualquer handler. Meta-específico (media_id +
    # baixar_midia_meta).
    if mensagem.tipo == TipoMensagem.audio and not mensagem.texto:
        media_id = mensagem.metadata.get("media_id")
        mime_type = mensagem.metadata.get("mime_type", "audio/ogg; codecs=opus")
        if media_id and settings.groq_api_key:
            try:
                audio_bytes, detected_mime = await baixar_midia_meta(media_id, sender.token)
                texto_transcrito = await transcrever_audio(
                    audio_bytes, detected_mime or mime_type, settings.groq_api_key
                )
                if texto_transcrito:
                    mensagem = mensagem.model_copy(update={"texto": texto_transcrito})
                    logger.info(
                        "Áudio transcrito | thread=%s | chars=%d",
                        thread_id,
                        len(texto_transcrito),
                    )
                else:
                    logger.warning("Transcrição retornou vazia | thread=%s", thread_id)
                    return
            except Exception:
                logger.exception("Erro na transcrição de áudio | thread=%s", thread_id)
                await sender.enviar_texto(
                    mensagem.numero_remetente,
                    "Não consegui ouvir o áudio. Poderia digitar sua mensagem? 🙏",
                )
                return
        else:
            logger.warning("Áudio recebido mas GROQ_API_KEY não configurada | thread=%s", thread_id)
            await sender.enviar_texto(
                mensagem.numero_remetente,
                "Não consigo ouvir áudios no momento. Poderia digitar sua mensagem? 🙏",
            )
            return

    # Resolve contato + decide conversa/estado ANTES do gate (Fase 2). Para
    # contato existente, resolver_conversa_para_entrada aplica a regra 24h/2h
    # (reutiliza a conversa, devolve ao agente ou mantém com o humano) e já diz se a
    # agente deve responder. Contato novo (None) segue para o gate LGPD, que o cria.
    _contato_id: str | None = None
    _conversa_id: str | None = None
    _agente_deve_responder = True
    try:
        _contato_id = resolver_contato_id(client, tenant.tenant_id, mensagem.numero_remetente)
        if _contato_id:
            _conversa_id, _agente_deve_responder, _acao = resolver_conversa_para_entrada(
                client, tenant.tenant_id, _contato_id, tenant.canal_id
            )
            logger.info(
                "Conversa resolvida | thread=%s | acao=%s | agente=%s",
                thread_id,
                _acao,
                _agente_deve_responder,
            )
    except Exception:
        logger.exception(
            "Erro ao resolver conversa/estado | thread=%s — prosseguindo com agente", thread_id
        )

    # Gate: humano no controle → só registra a mensagem do paciente na conversa
    # existente e sai (não chama LGPD/lembrete/NPS/grafo). Aqui o atendente está
    # atendendo pelo CRM, então a mídia PRECISA ser baixada/salva mesmo sem o
    # agente responder — senão a foto do paciente se perde no atendimento humano.
    if _contato_id and _conversa_id and not _agente_deve_responder:
        logger.info("Humano no controle — agente não responde | thread=%s", thread_id)
        midia_ref_humano: dict | None = None
        if mensagem.tipo in (TipoMensagem.imagem, TipoMensagem.documento):
            try:
                _, midia_ref_humano = await _processar_midia_visual(
                    mensagem=mensagem,
                    tenant=tenant,
                    contato_id=_contato_id,
                    token=sender.token,
                )
            except Exception:
                logger.exception(
                    "Erro ao processar mídia (humano no controle) | thread=%s", thread_id
                )
        try:
            _salvar_mensagens_paciente(
                client, tenant.tenant_id, _conversa_id, mensagem, midia_ref_humano
            )
        except Exception:
            logger.exception("Erro ao salvar mensagem (humano no controle) | thread=%s", thread_id)
        return

    # Gate de consentimento LGPD — intercepta ANTES de lembrete/NPS/agente.
    # (Humano no controle já retornou acima; _contato_id/_conversa_id resolvidos.)
    try:
        resultado_consent = await handle_consentimento(mensagem, tenant, _contato_id)
        if resultado_consent is not None:
            if resultado_consent.contato_id:
                _contato_id = resultado_consent.contato_id

            # Reutiliza a conversa já resolvida; só cria aqui quando o contato
            # acabou de ser criado pelo próprio gate (não havia conversa antes).
            conversa_id_consent: str | None = _conversa_id
            mensagens_agente_consent: list[str] = []

            if _contato_id and conversa_id_consent is None:
                try:
                    conversa_id_consent = obter_ou_criar_conversa(
                        client, tenant.tenant_id, _contato_id, tenant.canal_id
                    )
                except Exception:
                    logger.exception(
                        "Erro ao obter conversa para consentimento LGPD | thread=%s", thread_id
                    )

            if resultado_consent.tipo == "pedido" and conversa_id_consent:
                try:
                    metadata_conversa = _buscar_metadata_conversa(
                        client, tenant.tenant_id, conversa_id_consent
                    )
                    if not metadata_conversa.get(_METADATA_APRESENTACAO_INICIAL):
                        apresentacao = _mensagem_apresentacao_inicial(client, tenant.tenant_id)
                        enviado_apresentacao = await sender.enviar_texto(
                            mensagem.numero_remetente, apresentacao
                        )
                        if not enviado_apresentacao:
                            logger.error(
                                "Falha ao enviar apresentação inicial | thread=%s", thread_id
                            )
                        else:
                            mensagens_agente_consent.append(apresentacao)
                            metadata_conversa[_METADATA_APRESENTACAO_INICIAL] = True
                            _salvar_metadata_conversa(
                                client, tenant.tenant_id, conversa_id_consent, metadata_conversa
                            )
                except Exception:
                    logger.exception(
                        "Erro ao processar apresentação inicial | thread=%s", thread_id
                    )

            if resultado_consent.botoes:
                enviado_consent = await sender.enviar_botoes(
                    mensagem.numero_remetente, resultado_consent.mensagem, resultado_consent.botoes
                )
            else:
                enviado_consent = await sender.enviar_texto(
                    mensagem.numero_remetente, resultado_consent.mensagem
                )
            if not enviado_consent:
                logger.error(
                    "Falha ao enviar mensagem de consentimento LGPD | thread=%s", thread_id
                )
            mensagens_agente_consent.append(resultado_consent.mensagem)

            if _contato_id and conversa_id_consent:
                try:
                    _salvar_mensagens_paciente(
                        client, tenant.tenant_id, conversa_id_consent, mensagem
                    )
                    for mensagem_agente in mensagens_agente_consent:
                        salvar_mensagem_db(
                            client,
                            tenant.tenant_id,
                            conversa_id_consent,
                            "agente",
                            mensagem_agente,
                        )
                except Exception:
                    logger.exception(
                        "Erro ao persistir msgs consentimento LGPD | thread=%s", thread_id
                    )
            return

        if _contato_id and _conversa_id is None:
            _conversa_id = obter_ou_criar_conversa(
                client, tenant.tenant_id, _contato_id, tenant.canal_id
            )
    except Exception:
        logger.exception(
            "Erro no gate de consentimento LGPD | thread=%s — prosseguindo com agente", thread_id
        )

    # Verificar se a mensagem é resposta a um lembrete de consulta
    try:
        resposta_lembrete = await handle_resposta_lembrete(
            mensagem, tenant, _contato_id, _conversa_id
        )
        if resposta_lembrete is not None:
            enviado_lembrete = await sender.enviar_texto(
                mensagem.numero_remetente, resposta_lembrete
            )
            if not enviado_lembrete:
                logger.error("Falha ao enviar resposta de lembrete | thread=%s", thread_id)
            if _contato_id and _conversa_id:
                try:
                    _salvar_mensagens_paciente(client, tenant.tenant_id, _conversa_id, mensagem)
                    salvar_mensagem_db(
                        client,
                        tenant.tenant_id,
                        _conversa_id,
                        "agente",
                        resposta_lembrete,
                    )
                except Exception:
                    logger.exception("Erro ao persistir msgs lembrete | thread=%s", thread_id)
            return
    except Exception:
        logger.exception(
            "Erro no handler de lembrete | thread=%s — prosseguindo com agente", thread_id
        )

    # Verificar se a mensagem é resposta NPS (número 0-10 com registro pendente)
    try:
        resposta_nps = await handle_resposta_nps(mensagem, tenant, _contato_id)
        if resposta_nps is not None:
            enviado_nps = await sender.enviar_texto(mensagem.numero_remetente, resposta_nps)
            if not enviado_nps:
                logger.error("Falha ao enviar resposta NPS | thread=%s", thread_id)
            if _contato_id and _conversa_id:
                try:
                    _salvar_mensagens_paciente(client, tenant.tenant_id, _conversa_id, mensagem)
                    salvar_mensagem_db(
                        client,
                        tenant.tenant_id,
                        _conversa_id,
                        "agente",
                        resposta_nps,
                    )
                except Exception:
                    logger.exception("Erro ao persistir msgs NPS | thread=%s", thread_id)
            return
    except Exception:
        logger.exception("Erro no handler de NPS | thread=%s — prosseguindo com agente", thread_id)

    # Processar imagem ou documento — baixa, salva no Storage e monta HumanMessage multimodal
    human_message: HumanMessage | None = None
    midia_ref: dict | None = None
    if mensagem.tipo in (TipoMensagem.imagem, TipoMensagem.documento):
        try:
            human_message, midia_ref = await _processar_midia_visual(
                mensagem=mensagem,
                tenant=tenant,
                contato_id=_contato_id,
                token=sender.token,
            )
            if human_message is None:
                logger.warning("Mídia sem media_id — ignorada | thread=%s", thread_id)
                return
        except Exception:
            logger.exception("Erro ao processar mídia visual | thread=%s", thread_id)
            await sender.enviar_texto(
                mensagem.numero_remetente,
                "Não consegui processar o arquivo enviado. Poderia tentar novamente? 🙏",
            )
            return

    try:
        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": RECURSION_LIMIT,
        }

        msg = human_message or HumanMessage(content=mensagem.texto or "")

        primeiro_turno = not bool((await _grafo.aget_state(config)).values)
        if primeiro_turno:
            entrada = {
                "messages": [msg],
                "tenant_id": tenant.tenant_id,
                "telefone": mensagem.numero_remetente,
                "contato_id": _contato_id,
                "vertical": "",
                "system_prompt": "",
                "tenant_config": {},
                "intencao": None,
            }
        else:
            entrada = {
                "messages": [msg],
                "tenant_id": tenant.tenant_id,
                "telefone": mensagem.numero_remetente,
                "contato_id": _contato_id,
            }

        resultado = await _grafo.ainvoke(entrada, config=config)

        ultima_ai = next(
            (m for m in reversed(resultado["messages"]) if isinstance(m, AIMessage)),
            None,
        )
        if ultima_ai is None or not ultima_ai.content:
            logger.warning("Agente não gerou resposta | thread=%s", thread_id)
            return

        content = ultima_ai.content
        if isinstance(content, list):
            resposta_texto = " ".join(
                bloco.get("text", "")
                for bloco in content
                if isinstance(bloco, dict) and bloco.get("type") == "text"
            )
        else:
            resposta_texto = str(content)

        if not resposta_texto.strip():
            logger.warning("Resposta do agente está vazia após extração | thread=%s", thread_id)
            return

        nome_agente = (resultado.get("tenant_config") or {}).get("nome_agente")
        resposta_texto = prefixar_mensagem_agente(resposta_texto, nome_agente)

        logger.info(
            "Resposta gerada | thread=%s | chars=%d",
            thread_id,
            len(resposta_texto),
        )

        enviado = await sender.enviar_texto(mensagem.numero_remetente, resposta_texto)

        if not enviado:
            logger.error(
                "Falha no envio da resposta | thread=%s | para=%s",
                thread_id,
                mensagem.numero_remetente,
            )
            registrar_erro(
                tenant.tenant_id, "envio", "Falha ao enviar a resposta do agente ao paciente"
            )

        # Persistir conversa e mensagens no banco CORE
        # contato_id sempre disponível: identificar_contato é o 2º nó do grafo
        contato_id = _contato_id or resultado.get("contato_id")
        if contato_id:
            try:
                conversa_id = _conversa_id or obter_ou_criar_conversa(
                    client, tenant.tenant_id, contato_id, tenant.canal_id
                )
                _salvar_mensagens_paciente(
                    client, tenant.tenant_id, conversa_id, mensagem, midia_ref
                )
                salvar_mensagem_db(
                    client,
                    tenant.tenant_id,
                    conversa_id,
                    "agente",
                    resposta_texto,
                )
            except Exception:
                logger.exception("Erro ao persistir conversa/mensagens | thread=%s", thread_id)
        else:
            logger.warning(
                "contato_id não resolvido — mensagens não persistidas | thread=%s", thread_id
            )

    except Exception as e:
        logger.exception("Erro no processamento do agente | thread=%s", thread_id)
        registrar_erro(
            tenant.tenant_id, "agente", f"Erro no processamento do agente ({type(e).__name__})"
        )
