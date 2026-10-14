import logging
import re
from datetime import datetime, timezone

from channels.base import (
    ContatoAgenda,
    ConversaHistorico,
    EcoMensagemClinica,
    MensagemHistorico,
    MensagemRecebida,
    PayloadInvalidoError,
    ProviderWhatsApp,
    TipoMensagem,
    _normalizar_numero_br,
)

logger = logging.getLogger(__name__)

# Rótulo da mídia que a clínica manda pelo celular, quando não há legenda.
_ROTULO_MIDIA_ECO: dict[str, str] = {
    "image": "[imagem]",
    "document": "[documento]",
    "audio": "[áudio]",
    "video": "[vídeo]",
    "sticker": "[figurinha]",
}

_TIPO_MAP: dict[str, TipoMensagem] = {
    "text": TipoMensagem.texto,
    "image": TipoMensagem.imagem,
    "audio": TipoMensagem.audio,
    "document": TipoMensagem.documento,
    "sticker": TipoMensagem.sticker,
    "interactive": TipoMensagem.botao_resposta,
    # Botão de resposta rápida de TEMPLATE (ex.: Confirmar / Preciso remarcar no
    # lembrete): chega como type "button", com o payload definido no envio.
    "button": TipoMensagem.botao_resposta,
}


class CloudApiAdapter:
    """Adapter para webhooks da Cloud API do WhatsApp (Meta oficial).

    Normaliza o payload bruto para MensagemRecebida sem nenhum conhecimento
    de tenant, core ou banco de dados.
    """

    @property
    def provider(self) -> ProviderWhatsApp:
        return ProviderWhatsApp.cloud_api

    @staticmethod
    def _changes(payload: dict, field: str, *, exigir_objeto: bool = True):
        """Percorre TODAS as entradas e mudanças do payload com o `field` pedido.

        A Meta pode agrupar vários eventos num único POST (várias `entry`, várias
        `changes`, várias `messages`). Ler só o índice [0] perdia o resto em
        silêncio: o webhook respondia 200 e a Meta não reenviava. Mudança sem
        `field` conta como "messages" (formato dos testes antigos).
        """
        if exigir_objeto and payload.get("object") != "whatsapp_business_account":
            return
        for entry in payload.get("entry") or []:
            if not isinstance(entry, dict):
                continue
            for change in entry.get("changes") or []:
                if not isinstance(change, dict):
                    continue
                if change.get("field", "messages") != field:
                    continue
                value = change.get("value")
                if isinstance(value, dict):
                    yield value

    def eh_mensagem_entrada(self, payload: dict) -> bool:
        """Retorna True quando o payload tem ao menos uma mensagem recebida.

        Descarta silenciosamente:
        - Payloads que não sejam de whatsapp_business_account
        - Eventos de status (statuses) sem campo messages
        - Payloads malformados
        """
        for value in self._changes(payload, "messages"):
            messages = value.get("messages")
            # Payloads de status têm "statuses" mas não têm "messages".
            if isinstance(messages, list) and messages:
                return True
        return False

    def normalizar_todas(self, payload: dict) -> list[MensagemRecebida]:
        """Todas as mensagens recebidas do payload, na ordem em que vieram.

        Item malformado é descartado (e logado), não derruba o lote.
        """
        mensagens: list[MensagemRecebida] = []
        for value in self._changes(payload, "messages", exigir_objeto=False):
            for message in value.get("messages") or []:
                try:
                    mensagens.append(self._normalizar_mensagem(value, message))
                except PayloadInvalidoError:
                    logger.warning(
                        "Mensagem malformada descartada do payload | id=%s",
                        message.get("id") if isinstance(message, dict) else None,
                    )
        return mensagens

    def normalizar(self, payload: dict) -> MensagemRecebida:
        """Converte a PRIMEIRA mensagem do payload para MensagemRecebida.

        Lança PayloadInvalidoError se não houver mensagem válida. O webhook usa
        normalizar_todas(); este método fica para quem precisa de uma só.
        """
        for value in self._changes(payload, "messages", exigir_objeto=False):
            for message in value.get("messages") or []:
                return self._normalizar_mensagem(value, message)
        raise PayloadInvalidoError("Payload da Cloud API sem mensagens")

    @staticmethod
    def _nome_do_contato(value: dict, wa_id: str) -> str | None:
        """Nome do perfil do remetente em contacts[] (casado pelo wa_id; se o
        payload tem um contato só e sem wa_id, é dele)."""
        contacts = [c for c in value.get("contacts") or [] if isinstance(c, dict)]
        contato = next((c for c in contacts if c.get("wa_id") == wa_id), None)
        if contato is None and len(contacts) == 1 and not contacts[0].get("wa_id"):
            contato = contacts[0]
        if contato is None:
            return None
        return (contato.get("profile") or {}).get("name") or None

    def _normalizar_mensagem(self, value: dict, message: dict) -> MensagemRecebida:
        try:
            metadata = value["metadata"]

            message_id: str = message["id"]
            numero_remetente = _normalizar_numero_br(message["from"])
            numero_destino = f"+{re.sub(r'\D', '', metadata['display_phone_number'])}"

            # Nome do contato vem em contacts[].profile.name, quando disponível.
            nome_remetente = self._nome_do_contato(value, message["from"])

            message_type: str = message.get("type", "")
            tipo = _TIPO_MAP.get(message_type, TipoMensagem.desconhecido)

            texto: str | None = None
            if tipo == TipoMensagem.texto:
                texto = message.get("text", {}).get("body")

            # Resposta de botão (reply button): id vai para metadata, title como texto
            # (mesmo padrão de media_id — webhook usa metadata.button_id para identificar).
            button_id: str | None = None
            if tipo == TipoMensagem.botao_resposta:
                if message_type == "button":
                    botao = message.get("button", {})
                    button_id = botao.get("payload")
                    texto = botao.get("text")
                else:
                    button_reply = message.get("interactive", {}).get("button_reply", {})
                    button_id = button_reply.get("id")
                    texto = button_reply.get("title")

            # Extrai media_id e mime_type para mídias que precisam de download
            media_id: str | None = None
            mime_type: str | None = None
            nome_arquivo: str | None = None
            if tipo == TipoMensagem.audio:
                bloco = message.get("audio", {})
                media_id = bloco.get("id")
                mime_type = bloco.get("mime_type", "audio/ogg; codecs=opus")
            elif tipo == TipoMensagem.imagem:
                bloco = message.get("image", {})
                media_id = bloco.get("id")
                mime_type = bloco.get("mime_type", "image/jpeg")
            elif tipo == TipoMensagem.documento:
                bloco = message.get("document", {})
                media_id = bloco.get("id")
                mime_type = bloco.get("mime_type", "application/octet-stream")
                nome_arquivo = bloco.get("filename")

            ts = int(message.get("timestamp", 0))
            timestamp = datetime.fromtimestamp(ts, tz=timezone.utc)

            return MensagemRecebida(
                message_id=message_id,
                numero_destino=numero_destino,
                numero_remetente=numero_remetente,
                nome_remetente=nome_remetente,
                tipo=tipo,
                texto=texto,
                provider=ProviderWhatsApp.cloud_api,
                timestamp=timestamp,
                metadata={
                    "phone_number_id": metadata.get("phone_number_id"),
                    "message_type_raw": message_type,
                    "media_id": media_id,
                    "mime_type": mime_type,
                    "nome_arquivo": nome_arquivo,
                    "button_id": button_id,
                },
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise PayloadInvalidoError(f"Payload da Cloud API inválido: {exc}") from exc

    def eh_eco_celular(self, payload: dict) -> bool:
        """True para smb_message_echoes: mensagens que a clínica enviou pelo
        app WhatsApp Business no celular (coexistência)."""
        if payload.get("object") != "whatsapp_business_account":
            return False
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                if change.get("field") == "smb_message_echoes" and (
                    (change.get("value") or {}).get("message_echoes")
                ):
                    return True
        return False

    def normalizar_ecos(self, payload: dict) -> list[EcoMensagemClinica]:
        """Ecos válidos do payload. Item malformado é descartado, não derruba o lote."""
        ecos: list[EcoMensagemClinica] = []
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                if change.get("field") != "smb_message_echoes":
                    continue
                value = change.get("value") or {}
                metadata = value.get("metadata") or {}
                for eco in value.get("message_echoes") or []:
                    try:
                        tipo = eco.get("type", "")
                        if tipo == "text":
                            texto = (eco.get("text") or {}).get("body") or ""
                        else:
                            bloco = eco.get(tipo) or {}
                            texto = bloco.get("caption") or _ROTULO_MIDIA_ECO.get(tipo, f"[{tipo}]")
                        digitos_negocio = re.sub(
                            r"\D", "", metadata.get("display_phone_number") or eco["from"]
                        )
                        ecos.append(
                            EcoMensagemClinica(
                                message_id=eco["id"],
                                numero_negocio=f"+{digitos_negocio}",
                                numero_cliente=_normalizar_numero_br(eco["to"]),
                                tipo=tipo,
                                texto=texto,
                                timestamp=datetime.fromtimestamp(
                                    int(eco.get("timestamp", 0)), tz=timezone.utc
                                ),
                            )
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
        return ecos

    # ─── Coexistência: histórico e agenda do app (onboarding) ────────────────

    @staticmethod
    def _numero_negocio(value: dict) -> str | None:
        digitos = re.sub(r"\D", "", (value.get("metadata") or {}).get("display_phone_number") or "")
        return f"+{digitos}" if digitos else None

    def eh_sincronizacao_app(self, payload: dict) -> bool:
        """True para `history` ou `smb_app_state_sync` (coexistência)."""
        return any(True for _ in self._changes(payload, "history")) or any(
            True for _ in self._changes(payload, "smb_app_state_sync")
        )

    def normalizar_historico(self, payload: dict) -> tuple[list[ConversaHistorico], list[str]]:
        """Conversas antigas do webhook `history` + erros informados pela Meta
        (ex.: a clínica desligou o compartilhamento do histórico no app).

        Mensagem malformada é descartada, não derruba o lote.
        """
        conversas: list[ConversaHistorico] = []
        erros: list[str] = []
        for value in self._changes(payload, "history"):
            numero_negocio = self._numero_negocio(value)
            for bloco in value.get("history") or []:
                if not isinstance(bloco, dict):
                    continue
                for erro in bloco.get("errors") or []:
                    if isinstance(erro, dict):
                        erros.append(f"{erro.get('code')}: {erro.get('title') or ''}".strip())
                if not numero_negocio:
                    continue
                for thread in bloco.get("threads") or []:
                    if not isinstance(thread, dict) or not thread.get("id"):
                        continue
                    numero_cliente = _normalizar_numero_br(str(thread["id"]))
                    mensagens: list[MensagemHistorico] = []
                    for msg in thread.get("messages") or []:
                        try:
                            tipo = msg.get("type", "")
                            if tipo == "text":
                                texto = (msg.get("text") or {}).get("body") or ""
                            else:
                                bloco_midia = msg.get(tipo) or {}
                                texto = (
                                    bloco_midia.get("caption")
                                    if isinstance(bloco_midia, dict)
                                    else None
                                ) or _ROTULO_MIDIA_ECO.get(tipo, f"[{tipo}]")
                            if not texto:
                                continue
                            mensagens.append(
                                MensagemHistorico(
                                    message_id=msg["id"],
                                    do_paciente=(
                                        _normalizar_numero_br(str(msg["from"])) == numero_cliente
                                    ),
                                    tipo=tipo,
                                    texto=texto,
                                    timestamp=datetime.fromtimestamp(
                                        int(msg.get("timestamp", 0)), tz=timezone.utc
                                    ),
                                )
                            )
                        except (AttributeError, KeyError, TypeError, ValueError):
                            continue
                    if mensagens:
                        conversas.append(
                            ConversaHistorico(
                                numero_negocio=numero_negocio,
                                numero_cliente=numero_cliente,
                                mensagens=mensagens,
                            )
                        )
        return conversas, erros

    def normalizar_agenda(self, payload: dict) -> list[ContatoAgenda]:
        """Contatos da agenda do app (webhook smb_app_state_sync)."""
        contatos: list[ContatoAgenda] = []
        for value in self._changes(payload, "smb_app_state_sync"):
            numero_negocio = self._numero_negocio(value)
            if not numero_negocio:
                continue
            for item in value.get("state_sync") or []:
                try:
                    if item.get("type") != "contact":
                        continue
                    contato = item.get("contact") or {}
                    nome = (contato.get("full_name") or contato.get("first_name") or "").strip()
                    contatos.append(
                        ContatoAgenda(
                            numero_negocio=numero_negocio,
                            numero_cliente=_normalizar_numero_br(str(contato["phone_number"])),
                            nome=nome or None,
                            acao=str(item.get("action") or ""),
                        )
                    )
                except (AttributeError, KeyError, TypeError, ValueError):
                    continue
        return contatos
