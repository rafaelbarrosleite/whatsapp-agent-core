import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

_GRAPH_API_BASE = "https://graph.facebook.com/v25.0"
_TIMEOUT = 30.0
_MAX_TENTATIVAS = 3
# Segundos de espera antes da 2ª e 3ª tentativas (backoff exponencial).
# A 1ª tentativa é sempre imediata — este tuple tem len(_MAX_TENTATIVAS - 1).
_BACKOFF_S = (2, 4)


async def enviar_mensagem(
    numero_remetente: str,
    texto: str,
    token: str,
    phone_number_id: str,
) -> bool:
    """Envia uma mensagem de texto via Cloud API do WhatsApp (Meta).

    Retorna True se o envio foi aceito (HTTP 200/201). Faz até 3 tentativas
    com backoff exponencial (0s, 2s, 4s) apenas em erros recuperáveis:
    timeout, erro de conexão, status 429 e 5xx. Erros 4xx (exceto 429) e
    exceções inesperadas retornam False imediatamente sem retry.
    """
    # Cloud API espera o número sem o '+' no campo 'to'.
    # preview_url=True: o WhatsApp gera automaticamente o preview de links no
    # texto (ex.: o link do Maps na mensagem de localização mostra a miniatura
    # do local). Sem custo para mensagens sem link.
    payload = {
        "messaging_product": "whatsapp",
        "to": numero_remetente.lstrip("+"),
        "type": "text",
        "text": {"body": texto, "preview_url": True},
    }
    return await _enviar_payload(payload, numero_remetente, token, phone_number_id)


async def enviar_mensagem_com_botoes(
    numero_remetente: str,
    texto: str,
    botoes: list[tuple[str, str]],
    token: str,
    phone_number_id: str,
) -> bool:
    """Envia uma mensagem interativa com até 3 reply buttons via Cloud API.

    `botoes` é uma lista de (id, label) — id volta em metadata.button_id quando
    o paciente clica (ver CloudApiAdapter.normalizar). Mesma política de
    retry/backoff de enviar_mensagem.
    """
    if not botoes or len(botoes) > 3:
        raise ValueError("enviar_mensagem_com_botoes aceita de 1 a 3 botões")

    payload = {
        "messaging_product": "whatsapp",
        "to": numero_remetente.lstrip("+"),
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": texto},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": id_, "title": label}} for id_, label in botoes
                ]
            },
        },
    }
    return await _enviar_payload(payload, numero_remetente, token, phone_number_id)


async def enviar_template(
    numero_remetente: str,
    nome: str,
    idioma: str,
    parametros: list[str],
    token: str,
    phone_number_id: str,
    payloads_botoes: list[str] | None = None,
) -> bool:
    """Envia um template aprovado na Meta (único tipo entregue fora da janela de
    24 h). `parametros` preenchem {{1}}, {{2}}... do corpo, na ordem.
    `payloads_botoes` preenchem os botões de resposta rápida, na ordem em que
    foram cadastrados no template; o payload volta no webhook quando o paciente
    toca o botão (ver CloudApiAdapter: mensagem do tipo "button")."""
    componentes: list[dict] = []
    if parametros:
        componentes.append(
            {
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in parametros],
            }
        )
    for indice, payload_botao in enumerate(payloads_botoes or []):
        componentes.append(
            {
                "type": "button",
                "sub_type": "quick_reply",
                "index": str(indice),
                "parameters": [{"type": "payload", "payload": payload_botao}],
            }
        )
    payload = {
        "messaging_product": "whatsapp",
        "to": numero_remetente.lstrip("+"),
        "type": "template",
        "template": {"name": nome, "language": {"code": idioma}, "components": componentes},
    }
    return await _enviar_payload(payload, numero_remetente, token, phone_number_id)


async def enviar_midia(
    numero_remetente: str,
    link: str,
    tipo: str,
    token: str,
    phone_number_id: str,
    caption: str | None = None,
    filename: str | None = None,
) -> bool:
    """Envia uma imagem, áudio ou documento via Cloud API usando `link` (URL
    pública temporária — a Meta busca o arquivo no momento do envio; usamos
    signed URL do Supabase Storage). `tipo` = 'imagem' → image, 'audio' →
    audio, qualquer outro → document. Mesma política de retry/backoff de
    enviar_mensagem."""
    to = numero_remetente.lstrip("+")
    if tipo == "imagem":
        midia: dict = {"link": link}
        if caption:
            midia["caption"] = caption
        payload = {"messaging_product": "whatsapp", "to": to, "type": "image", "image": midia}
    elif tipo == "audio":
        # Mensagem de voz da atendente. A Meta NÃO aceita caption nem filename
        # em `audio` (ignora silenciosamente) — quem chama manda o texto à parte
        # se precisar. Formatos aceitos: aac, amr, mpeg, mp4 e ogg/opus.
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "audio",
            "audio": {"link": link},
        }
    else:
        midia = {"link": link}
        if caption:
            midia["caption"] = caption
        if filename:
            midia["filename"] = filename
        payload = {"messaging_product": "whatsapp", "to": to, "type": "document", "document": midia}
    return await _enviar_payload(payload, numero_remetente, token, phone_number_id)


async def _enviar_payload(
    payload: dict,
    numero_remetente: str,
    token: str,
    phone_number_id: str,
) -> bool:
    """Lógica de envio com retry/backoff compartilhada por enviar_mensagem
    e enviar_mensagem_com_botoes — só o payload muda entre as duas."""
    url = f"{_GRAPH_API_BASE}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    for tentativa in range(1, _MAX_TENTATIVAS + 1):
        logger.info("Tentativa %d/%d para %s", tentativa, _MAX_TENTATIVAS, numero_remetente)

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(url, json=payload, headers=headers)

            if response.status_code in (200, 201):
                logger.info(
                    "Mensagem enviada na tentativa %d/%d | para=%s | status=%d",
                    tentativa,
                    _MAX_TENTATIVAS,
                    numero_remetente,
                    response.status_code,
                )
                return True

            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and tentativa < _MAX_TENTATIVAS:
                delay = _BACKOFF_S[tentativa - 1]
                logger.warning(
                    "Status %d na tentativa %d/%d para %s — aguardando %ds...",
                    response.status_code,
                    tentativa,
                    _MAX_TENTATIVAS,
                    numero_remetente,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            # Erros 4xx permanentes ou última tentativa com status recuperável
            logger.error(
                "Falha no envio | para=%s | status=%d | tentativa=%d/%d | body=%s",
                numero_remetente,
                response.status_code,
                tentativa,
                _MAX_TENTATIVAS,
                response.text[:300],
            )
            return False

        except httpx.TimeoutException:
            if tentativa < _MAX_TENTATIVAS:
                delay = _BACKOFF_S[tentativa - 1]
                logger.warning(
                    "Timeout na tentativa %d/%d, aguardando %ds...",
                    tentativa,
                    _MAX_TENTATIVAS,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "Timeout na tentativa %d/%d — esgotadas as tentativas para %s",
                    tentativa,
                    _MAX_TENTATIVAS,
                    numero_remetente,
                )

        except httpx.ConnectError:
            if tentativa < _MAX_TENTATIVAS:
                delay = _BACKOFF_S[tentativa - 1]
                logger.warning(
                    "Erro de conexão na tentativa %d/%d, aguardando %ds...",
                    tentativa,
                    _MAX_TENTATIVAS,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "Erro de conexão na tentativa %d/%d — esgotadas as tentativas para %s",
                    tentativa,
                    _MAX_TENTATIVAS,
                    numero_remetente,
                )

        except Exception:
            logger.exception(
                "Erro inesperado ao enviar mensagem | para=%s | tentativa=%d/%d",
                numero_remetente,
                tentativa,
                _MAX_TENTATIVAS,
            )
            return False

    logger.error(
        "Todas as %d tentativas falharam para %s",
        _MAX_TENTATIVAS,
        numero_remetente,
    )
    return False


class CloudApiSender:
    """Wrapper fino sobre enviar_mensagem/enviar_mensagem_com_botoes com
    credenciais no construtor — interface usada no
    pipeline unificado (core/pipeline.py)."""

    def __init__(self, phone_number_id: str, token: str) -> None:
        self._phone_number_id = phone_number_id
        self._token = token

    @property
    def token(self) -> str:
        """Token do canal — também baixa a mídia recebida por este número."""
        return self._token

    async def enviar_texto(self, numero_remetente: str, texto: str) -> bool:
        return await enviar_mensagem(
            numero_remetente=numero_remetente,
            texto=texto,
            token=self._token,
            phone_number_id=self._phone_number_id,
        )

    async def enviar_botoes(
        self, numero_remetente: str, texto: str, botoes: list[tuple[str, str]]
    ) -> bool:
        return await enviar_mensagem_com_botoes(
            numero_remetente=numero_remetente,
            texto=texto,
            botoes=botoes,
            token=self._token,
            phone_number_id=self._phone_number_id,
        )

    async def enviar_template(
        self,
        numero_remetente: str,
        nome: str,
        idioma: str,
        parametros: list[str],
        payloads_botoes: list[str] | None = None,
    ) -> bool:
        return await enviar_template(
            numero_remetente=numero_remetente,
            nome=nome,
            idioma=idioma,
            parametros=parametros,
            token=self._token,
            phone_number_id=self._phone_number_id,
            payloads_botoes=payloads_botoes,
        )

    async def enviar_midia(
        self,
        numero_remetente: str,
        link: str,
        tipo: str,
        caption: str | None = None,
        filename: str | None = None,
    ) -> bool:
        return await enviar_midia(
            numero_remetente=numero_remetente,
            link=link,
            tipo=tipo,
            token=self._token,
            phone_number_id=self._phone_number_id,
            caption=caption,
            filename=filename,
        )

    async def solicitar_sincronizacao_app(self, tipo: str) -> tuple[bool, str]:
        """Coexistência: pede à Meta o histórico (`history`) ou a agenda
        (`smb_app_state_sync`) do app da clínica. A Meta responde pelos
        webhooks de mesmo nome. Retorna (aceito, detalhe da Meta)."""
        if tipo not in ("history", "smb_app_state_sync"):
            raise ValueError("tipo deve ser 'history' ou 'smb_app_state_sync'")
        url = f"{_GRAPH_API_BASE}/{self._phone_number_id}/smb_app_data"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resposta = await client.post(
                    url,
                    json={"messaging_product": "whatsapp", "sync_type": tipo},
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except httpx.HTTPError as exc:
            return False, f"erro de rede: {type(exc).__name__}"
        return resposta.status_code in (200, 201), resposta.text[:500]
