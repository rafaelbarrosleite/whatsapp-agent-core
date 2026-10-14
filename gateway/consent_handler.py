"""Gate de consentimento LGPD — intercepta toda mensagem de paciente antes do LangGraph.

Isolado do webhook principal para manter `webhook_cloudapi.py` legível, no
mesmo padrão de `nps_handler.py` e `lembrete_handler.py`. Retorna None quando
o paciente já aceitou e não revogou (segue fluxo normal); retorna
ResultadoConsentimento quando a mensagem deve ser interceptada — pedido de
consentimento pendente, ou confirmação de aceite/recusa via botão.

contatos (lgpd_aceito, lgpd_revogado, ...) vive no banco CORE; lgpd_termos e
lgpd_audit_log vivem no banco VERTICAL — os dois clients são usados aqui.
"""

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import settings
from channels.base import MensagemRecebida
from core.telefone import normalizar_telefone
from db.client import get_supabase
from db.privacy import communication_allowed, get_active_term, record_preference
from db.vertical_client import get_vertical_supabase
from gateway.tenant_resolver import TenantInfo
from integrations.crm_leads import notificar_contato_vinculado

log = logging.getLogger(__name__)

_BOTAO_ACEITAR = "lgpd_aceitar"
_BOTAO_NEGAR = "lgpd_negar"

_MSG_ACEITO = (
    "Consentimento registrado! ✅ Agora posso continuar te ajudando por aqui. Como posso te ajudar?"
)
_MSG_NEGADO = (
    "Entendido. Sem o seu consentimento, não posso continuar o atendimento "
    "automatizado por aqui, para proteger seus dados. 🔒\n\n"
    "Por favor, ligue diretamente para a clínica para ser atendido. 📞"
)
_MSG_ERRO = (
    "Tivemos um problema técnico ao registrar sua resposta. Pode tentar de novo "
    "em alguns instantes, ou ligar para a clínica."
)


@dataclass(frozen=True)
class ResultadoConsentimento:
    """Resultado do gate quando a mensagem deve ser interceptada (não None).

    botoes presente → webhook envia via enviar_mensagem_com_botoes; ausente →
    envio de texto simples. contato_id preenchido apenas quando um contato
    novo foi criado aqui (webhook não tinha esse id antes da chamada).
    """

    mensagem: str
    botoes: list[tuple[str, str]] | None = None
    contato_id: str | None = None
    tipo: str = "pedido"


def _buscar_termo_ativo(vertical, tenant_id: str) -> dict | None:
    return get_active_term(vertical, tenant_id)


_VALIDADE_LINK = timedelta(days=7)


def _link_consentimento(vertical, tenant_id: str, contato_id: str) -> str | None:
    """Link público para ler o termo e decidir pelo navegador.

    O código é aleatório (32 bytes), vale 7 dias e só o SHA-256 dele vai para o
    banco (links_consentimento). Nunca o id do contato: quem o visse poderia ler
    o nome do paciente e mudar a decisão dele. Se não der para gravar o código,
    devolve None e o pedido segue só com os botões do WhatsApp.
    """
    base = (settings.crm_url or "").rstrip("/")
    if not base:
        return None
    codigo = secrets.token_urlsafe(32)
    agora = datetime.now(timezone.utc)
    try:
        tabela = vertical.table("links_consentimento")
        # Limpeza: links vencidos deste paciente não servem mais para nada.
        tabela.delete().eq("tenant_id", tenant_id).eq("contato_id", contato_id).lt(
            "expira_em", agora.isoformat()
        ).execute()
        tabela.insert(
            {
                "tenant_id": tenant_id,
                "contato_id": contato_id,
                "codigo_hash": hashlib.sha256(codigo.encode()).hexdigest(),
                "expira_em": (agora + _VALIDADE_LINK).isoformat(),
            }
        ).execute()
    except Exception:
        log.warning("[LGPD] Não foi possível gerar o link de consentimento | tenant=%s", tenant_id)
        return None
    return f"{base}/lgpd/consentimento/{codigo}"


def _montar_pedido_consentimento(
    vertical, tenant_id: str, contato_id: str
) -> ResultadoConsentimento:
    termo = _buscar_termo_ativo(vertical, tenant_id)
    titulo_termo = termo["titulo"] if termo else "nossos Termos de Consentimento"
    link = _link_consentimento(vertical, tenant_id, contato_id)
    leia = f"📄 Leia {titulo_termo}: {link}\n\n" if link else ""
    preambulo = (
        "Antes de continuar, precisamos do seu consentimento para tratar seus dados "
        "de acordo com a LGPD (Lei Geral de Proteção de Dados).\n\n" + leia
    )

    return ResultadoConsentimento(
        mensagem=preambulo + "Você aceita?",
        botoes=[(_BOTAO_ACEITAR, "Aceito"), (_BOTAO_NEGAR, "Não aceito")],
    )


def _nome_remetente_whatsapp(mensagem: MensagemRecebida) -> str | None:
    nome = (mensagem.nome_remetente or "").strip()
    return nome or None


def _registrar_audit_log(vertical, tenant_id: str, contato_id: str, acao: str) -> None:
    vertical.table("lgpd_audit_log").insert(
        {
            "tenant_id": tenant_id,
            "entidade": "contato",
            "entidade_id": contato_id,
            "acao": acao,
            "canal": "whatsapp",
            "realizado_por": None,
        }
    ).execute()


def _processar_aceite(core, vertical, tenant_id: str, contato_id: str) -> ResultadoConsentimento:
    termo = _buscar_termo_ativo(vertical, tenant_id)
    if not termo or not termo.get("conteudo_hash"):
        log.error(
            "[LGPD] Termo ativo versionado não encontrado — aceite bloqueado | tenant=%s",
            tenant_id,
        )
        return ResultadoConsentimento(mensagem=_MSG_ERRO, tipo="erro")
    try:
        core.table("contatos").update(
            {
                "lgpd_aceito": True,
                "lgpd_aceito_at": datetime.now(timezone.utc).isoformat(),
                "lgpd_revogado": False,
                "lgpd_ip": None,
                "lgpd_canal": "whatsapp",
            }
        ).eq("id", contato_id).eq("tenant_id", tenant_id).execute()
        record_preference(
            vertical,
            tenant_id=tenant_id,
            contato_id=contato_id,
            channel="whatsapp",
            purpose="atendimento_transacional",
            decision="aceito",
            origin="whatsapp_button",
            term=termo,
        )
    except Exception:
        log.exception(
            "[LGPD] Erro ao registrar aceite — audit_log NÃO inserido | tenant=%s | contato=%s",
            tenant_id,
            contato_id,
        )
        return ResultadoConsentimento(mensagem=_MSG_ERRO, tipo="erro")

    try:
        _registrar_audit_log(vertical, tenant_id, contato_id, "consentimento_dado")
    except Exception:
        log.exception(
            "[LGPD] Aceite gravado mas audit_log falhou | tenant=%s | contato=%s",
            tenant_id,
            contato_id,
        )
    return ResultadoConsentimento(mensagem=_MSG_ACEITO, tipo="aceito")


def _processar_negativa(core, vertical, tenant_id: str, contato_id: str) -> ResultadoConsentimento:
    termo = _buscar_termo_ativo(vertical, tenant_id)
    try:
        core.table("contatos").update(
            {
                "lgpd_aceito": False,
                "lgpd_revogado": True,
                "lgpd_revogado_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", contato_id).eq("tenant_id", tenant_id).execute()
        record_preference(
            vertical,
            tenant_id=tenant_id,
            contato_id=contato_id,
            channel="whatsapp",
            purpose="atendimento_transacional",
            decision="recusado",
            origin="whatsapp_button",
            term=termo,
        )
    except Exception:
        log.exception(
            "[LGPD] Erro ao registrar negativa — audit_log NÃO inserido | tenant=%s | contato=%s",
            tenant_id,
            contato_id,
        )
        return ResultadoConsentimento(mensagem=_MSG_ERRO, tipo="erro")

    try:
        _registrar_audit_log(vertical, tenant_id, contato_id, "consentimento_negado")
    except Exception:
        log.exception(
            "[LGPD] Negativa gravada mas audit_log falhou | tenant=%s | contato=%s",
            tenant_id,
            contato_id,
        )
    return ResultadoConsentimento(mensagem=_MSG_NEGADO, tipo="negado")


async def handle_consentimento(
    mensagem: MensagemRecebida,
    tenant: TenantInfo,
    contato_id: str | None,
) -> ResultadoConsentimento | None:
    """Gate de consentimento LGPD. None = segue fluxo normal (webhook continua
    como hoje). Não-None = webhook deve enviar a mensagem e RETORNAR sem
    chamar o LangGraph.

    O pedido usa botões interativos da API oficial da Meta; a confirmação chega
    como button_id.
    """
    core = get_supabase()
    vertical = get_vertical_supabase()

    contato_novo = contato_id is None
    if contato_novo:
        telefone_canonico = normalizar_telefone(mensagem.numero_remetente)
        nome_contato = _nome_remetente_whatsapp(mensagem)
        inserted = (
            core.table("contatos")
            .insert(
                {
                    "tenant_id": tenant.tenant_id,
                    "telefone": telefone_canonico,
                    "nome": nome_contato,
                    "lgpd_aceito": False,
                }
            )
            .execute()
        )
        contato_id = inserted.data[0]["id"]
        log.info(
            "[LGPD] Contato novo criado pelo gate de consentimento | tenant=%s | contato=%s",
            tenant.tenant_id,
            contato_id,
        )
        # Best-effort, fire-and-forget: avisa o CRM para vincular este contato a
        # um lead existente. Nunca bloqueia nem quebra a resposta ao paciente.
        notificar_contato_vinculado(
            tenant.tenant_id,
            contato_id,
            telefone_canonico,
            nome_contato,
        )
        pedido = _montar_pedido_consentimento(vertical, tenant.tenant_id, contato_id)
        return ResultadoConsentimento(
            mensagem=pedido.mensagem,
            botoes=pedido.botoes,
            contato_id=contato_id,
        )

    button_id = mensagem.metadata.get("button_id")

    if button_id == _BOTAO_ACEITAR:
        return _processar_aceite(core, vertical, tenant.tenant_id, contato_id)
    if button_id == _BOTAO_NEGAR:
        return _processar_negativa(core, vertical, tenant.tenant_id, contato_id)

    termo = _buscar_termo_ativo(vertical, tenant.tenant_id)
    if communication_allowed(
        vertical,
        tenant_id=tenant.tenant_id,
        contato_id=contato_id,
        channel="whatsapp",
        purpose="atendimento_transacional",
        current_term=termo,
    ):
        return None

    return _montar_pedido_consentimento(vertical, tenant.tenant_id, contato_id)
