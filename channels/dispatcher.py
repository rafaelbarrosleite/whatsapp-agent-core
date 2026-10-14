"""channels/dispatcher.py — envia pelo canal WhatsApp da própria clínica
quando a mensagem NÃO é a resposta imediata ao paciente no webhook.

webhook_cloudapi.py já tem o sender da conversa. Quem origina uma mensagem
fora desse fluxo (aviso ao atendente, atendente respondendo pelo CRM,
lembretes, aviso de atraso da recepção, scheduler interno, webhook do médico)
usa este módulo, que escolhe o canal em canais_whatsapp:

- `canal_id` informado (o número em que a conversa chegou) e ativo → esse;
- senão, o canal PRINCIPAL da clínica;
- senão, o canal ativo mais antigo.

Sem fallback global (item 1.8): clínica sem canal ativo, canal de provider
desconhecido ou sem credencial → loga erro e devolve False. Nunca envia pelo
número de outra clínica.
"""

import logging

from channels.cloud_api_sender import CloudApiSender
from channels.credenciais import CredencialCanalAusenteError
from channels.sender_factory import criar_sender
from core.registro_painel import registrar_erro
from supabase import Client

logger = logging.getLogger(__name__)


def escolher_canal(canais: list[dict], canal_id: str | None = None) -> dict | None:
    """Canal da conversa, senão o principal, senão o ativo mais antigo."""
    if not canais:
        return None
    if canal_id:
        for c in canais:
            if c.get("id") == canal_id:
                return c
    ordenados = sorted(
        canais, key=lambda c: (not c.get("principal"), str(c.get("created_at") or ""))
    )
    return ordenados[0]


def sender_do_tenant(
    tenant_id: str, core_db: Client, canal_id: str | None = None
) -> CloudApiSender | None:
    """Sender do canal da clínica, ou None (motivo no log) se não há como enviar."""
    resultado = (
        core_db.table("canais_whatsapp")
        .select(
            "id, provider, credenciais_ref, phone_number_id, token_cifrado, principal, created_at"
        )
        .eq("tenant_id", tenant_id)
        .eq("ativo", True)
        .execute()
    )
    canal = escolher_canal(resultado.data or [], canal_id)
    if canal is None:
        logger.error("Clínica sem canal WhatsApp ativo — nada enviado | tenant=%s", tenant_id)
        registrar_erro(tenant_id, "canal", "Clínica sem canal WhatsApp ativo")
        return None
    try:
        return criar_sender(
            canal["provider"],
            canal.get("credenciais_ref"),
            tenant_id=tenant_id,
            phone_number_id=canal.get("phone_number_id"),
            token_cifrado=canal.get("token_cifrado"),
        )
    except CredencialCanalAusenteError:
        logger.error(
            "Canal WhatsApp sem credencial — nada enviado | tenant=%s canal=%s",
            tenant_id,
            canal.get("id"),
        )
        registrar_erro(tenant_id, "canal", "Canal WhatsApp sem credencial")
    except ValueError:
        logger.error(
            "Canal WhatsApp com provider inválido %r — nada enviado | tenant=%s",
            canal.get("provider"),
            tenant_id,
        )
    return None


def _conferir_envio(tenant_id: str, enviado: bool) -> bool:
    """Envio recusado pela Meta vira erro no painel da clínica."""
    if not enviado:
        registrar_erro(tenant_id, "envio", "A Meta recusou o envio de uma mensagem")
    return enviado


async def enviar_mensagem_tenant(
    tenant_id: str,
    numero_destino: str,
    texto: str,
    core_db: Client,
    canal_id: str | None = None,
) -> bool:
    """Envia texto pelo canal da clínica. False se não há canal utilizável."""
    sender = sender_do_tenant(tenant_id, core_db, canal_id)
    if sender is None:
        return False
    return _conferir_envio(tenant_id, await sender.enviar_texto(numero_destino, texto))


async def enviar_midia_tenant(
    tenant_id: str,
    numero_destino: str,
    link: str,
    tipo: str,
    core_db: Client,
    caption: str | None = None,
    filename: str | None = None,
    canal_id: str | None = None,
) -> bool:
    """Envia imagem/áudio/documento pelo canal da clínica.

    `link` é uma URL pública temporária (signed URL do Storage) que a Meta
    busca no envio. False se não há canal utilizável.
    """
    sender = sender_do_tenant(tenant_id, core_db, canal_id)
    if sender is None:
        return False
    enviado = await sender.enviar_midia(
        numero_destino, link, tipo, caption=caption, filename=filename
    )
    return _conferir_envio(tenant_id, enviado)


async def enviar_template_tenant(
    tenant_id: str,
    numero_destino: str,
    nome: str,
    idioma: str,
    parametros: list[str],
    core_db: Client,
    payloads_botoes: list[str] | None = None,
    canal_id: str | None = None,
) -> bool:
    """Envia um template aprovado pelo canal da clínica. False se não há canal."""
    sender = sender_do_tenant(tenant_id, core_db, canal_id)
    if sender is None:
        return False
    enviado = await sender.enviar_template(
        numero_destino, nome, idioma, parametros, payloads_botoes=payloads_botoes
    )
    return _conferir_envio(tenant_id, enviado)
