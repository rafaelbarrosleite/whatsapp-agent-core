from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

# A lógica de normalização vive em core.telefone (fonte única de verdade). Este
# alias preserva os imports históricos (`from channels.base import
# _normalizar_numero_br`) usados pelos adapters e pelo tenant_resolver.
from core.telefone import normalizar_telefone as _normalizar_numero_br  # noqa: F401 (reexport)


class TipoMensagem(str, Enum):
    texto = "texto"
    imagem = "imagem"
    audio = "audio"
    documento = "documento"
    sticker = "sticker"
    botao_resposta = "botao_resposta"
    desconhecido = "desconhecido"


class ProviderWhatsApp(str, Enum):
    cloud_api = "cloud_api"  # único canal suportado: API oficial da Meta


class MensagemRecebida(BaseModel):
    message_id: str  # ID original do provider — usado para deduplicação
    numero_destino: str  # número que RECEBEU a mensagem (E.164) → identifica o tenant
    numero_remetente: str  # número que ENVIOU a mensagem (E.164) → identifica o contato
    nome_remetente: str | None = None  # nome do contato, se o provider fornece
    tipo: TipoMensagem
    texto: str | None = None  # None para mensagens de mídia
    provider: ProviderWhatsApp
    timestamp: datetime  # horário em que o usuário enviou (do provider, não nosso relógio)
    metadata: dict = {}  # campos brutos extras por provider (ex: caption, media_id)


class EcoMensagemClinica(BaseModel):
    """Mensagem que a CLÍNICA enviou pelo app WhatsApp Business no celular
    (coexistência). Chega no webhook smb_message_echoes; o paciente é o `to`."""

    message_id: str
    numero_negocio: str  # número da clínica (E.164) → identifica o tenant
    numero_cliente: str  # número do paciente (E.164) → identifica o contato
    tipo: str  # tipo bruto da Meta (text, image, document...)
    texto: str  # texto, legenda ou rótulo da mídia ("[imagem]")
    timestamp: datetime


class MensagemHistorico(BaseModel):
    """Uma mensagem do histórico do app WhatsApp Business, enviada pela Meta
    uma única vez quando a clínica conecta o número (coexistência, webhook
    `history`). Pode ter sido escrita pelo paciente ou pela clínica."""

    message_id: str
    do_paciente: bool  # True = paciente escreveu; False = clínica pelo celular
    tipo: str  # tipo bruto da Meta (text, image, document...)
    texto: str  # texto, legenda ou rótulo da mídia ("[imagem]")
    timestamp: datetime


class ConversaHistorico(BaseModel):
    """Mensagens antigas trocadas com UM paciente (thread do webhook `history`)."""

    numero_negocio: str  # número da clínica (E.164) → identifica o tenant
    numero_cliente: str  # número do paciente (E.164)
    mensagens: list[MensagemHistorico]


class ContatoAgenda(BaseModel):
    """Contato da agenda do app WhatsApp Business (webhook smb_app_state_sync)."""

    numero_negocio: str
    numero_cliente: str
    nome: str | None  # ausente quando acao == "remove"
    acao: str  # "add" (adicionado/editado) | "remove"


class PayloadInvalidoError(ValueError):
    """Levantada quando o payload do provider não pode ser normalizado."""


@runtime_checkable
class AdapterCanal(Protocol):
    """Interface estrutural para adapters de canal WhatsApp.

    O adapter da Cloud API (channels/cloud_api.py) implementa este protocolo sem
    herdar dele — duck typing puro.
    """

    @property
    def provider(self) -> ProviderWhatsApp: ...

    def eh_mensagem_entrada(self, payload: dict) -> bool:
        """Retorna True apenas para mensagens recebidas de usuários finais.

        Deve retornar False para: status de entrega, leitura, eventos de conexão,
        QR code, e — CRÍTICO — mensagens enviadas pelo próprio bot (fromMe=True).
        Processar mensagens do bot causaria loop infinito queimando tokens.
        """
        ...

    def normalizar(self, payload: dict) -> MensagemRecebida:
        """Converte payload bruto do provider para MensagemRecebida.

        Pré-condição: eh_mensagem_entrada(payload) is True.
        Lança PayloadInvalidoError se os campos obrigatórios estiverem ausentes.
        """
        ...
