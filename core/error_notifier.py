"""core/error_notifier.py — alertas de erro do sistema via WhatsApp.

Canal de operação/devops (settings.monitor_phone), NÃO um canal de tenant —
não passa pelo roteamento multi-tenant (channels/dispatcher.py). Atrelar
esses alertas ao canal de um tenant específico criaria uma dependência
indesejada: se aquele tenant trocasse de provider ou desativasse o canal, os
alertas de erro do sistema inteiro parariam de chegar. Por isso usa Cloud API
com credenciais globais (settings) explicitamente, sempre — decisão
confirmada com Rafael na sessão do M2.
"""

import asyncio
import logging
import sys

import httpx

from app.config import settings
from core.observability import formatar_excecao_sem_conteudo, mascarar_dados_pessoais


async def notificar_erro(
    contexto: str,
    erro: str,
    detalhes: str | None = None,
) -> None:
    """Envia alerta de erro no WhatsApp do monitor. Nunca propaga exceção."""
    if not settings.whatsapp_cloud_token or not settings.whatsapp_phone_number_id:
        return

    if not settings.monitor_phone:
        print(
            "ERROR_NOTIFIER: MONITOR_PHONE não configurado — alerta descartado",
            file=sys.stderr,
        )
        return

    mensagem = f"🚨 *ERRO NO AGENTE*\n\n*Contexto:* {contexto}\n*Erro:* {erro}"

    if detalhes:
        mensagem += f"\n*Detalhes:*\n```{detalhes[:800]}```"

    # O alerta sai do servidor pela Meta até um celular: nenhum telefone de
    # paciente pode ir junto, mesmo que um chamador passe texto cru.
    mensagem = mascarar_dados_pessoais(mensagem)

    mensagem += "\n\n_Copie e mande para o Claude Code analisar._"

    try:
        async with httpx.AsyncClient(timeout=10) as http:
            await http.post(
                f"https://graph.facebook.com/v25.0/{settings.whatsapp_phone_number_id}/messages",
                headers={"Authorization": f"Bearer {settings.whatsapp_cloud_token}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": settings.monitor_phone,
                    "type": "text",
                    "text": {"body": mensagem},
                },
            )
    except Exception:
        pass  # Silencioso — notificador nunca quebra o sistema


class WhatsAppErrorHandler(logging.Handler):
    """Logging handler que envia alertas de ERROR para o WhatsApp do monitor.

    LGPD: o alerta leva só o MODELO da mensagem de log (sem os argumentos, que
    podem conter telefone/thread/ids de paciente) e a pilha sem as mensagens
    das exceções. Diagnóstico completo fica nos logs do servidor.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.ERROR:
            return

        contexto = f"{record.name} | {record.funcName}"
        erro = str(record.msg)

        detalhes = None
        if record.exc_info:
            detalhes = formatar_excecao_sem_conteudo(record.exc_info)

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(notificar_erro(contexto, erro, detalhes))
        except RuntimeError:
            # Sem event loop ativo (jobs standalone via python -m) → envio síncrono
            try:
                asyncio.run(notificar_erro(contexto, erro, detalhes))
            except Exception:
                pass  # notificador nunca quebra o sistema
