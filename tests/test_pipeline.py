"""Testes para core/pipeline.py — pipeline único compartilhado pelos dois
webhooks. Foco: paridade entre providers (sender.enviar_texto vs
enviar_botoes) e propagação do parâmetro provider para o gate de LGPD."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage

from channels.base import MensagemRecebida, TipoMensagem
from core.pipeline import _METADATA_APRESENTACAO_INICIAL, processar_mensagem_entrada
from gateway.consent_handler import ResultadoConsentimento
from gateway.tenant_resolver import TenantInfo

_TENANT = TenantInfo(
    tenant_id="t-1",
    vertical="clinica",
    canal_id="canal-1",
    provider="cloud_api",
    credenciais_ref=None,
)


def _mensagem(texto: str = "Olá") -> MensagemRecebida:
    return MensagemRecebida(
        message_id="msg-1",
        numero_destino="+556733334444",
        numero_remetente="+5567988880000",
        nome_remetente="Maria",
        tipo=TipoMensagem.texto,
        texto=texto,
        provider="cloud_api",
        timestamp=datetime.now(timezone.utc),
        metadata={},
    )


def _mock_sender() -> MagicMock:
    sender = MagicMock()
    sender.enviar_texto = AsyncMock(return_value=True)
    return sender


# ─── Gate LGPD (resultado sem botões → envio por texto) ────────────────────────


async def test_lgpd_pendente_envia_apresentacao_e_lgpd_por_texto() -> None:
    sender = _mock_sender()
    mensagem = _mensagem()

    with (
        patch("core.pipeline.get_supabase", return_value=MagicMock()),
        # contato novo (None) → resolver_conversa_para_entrada não é chamado; o
        # gate LGPD cria o contato/conversa.
        patch("core.pipeline.resolver_contato_id", return_value=None),
        patch(
            "core.pipeline.handle_consentimento",
            new_callable=AsyncMock,
            return_value=ResultadoConsentimento(
                mensagem="Responda *1* — Aceito\n*2* — Não aceito",
                contato_id="novo-contato",
            ),
        ) as mock_consent,
        patch("core.pipeline.obter_ou_criar_conversa", return_value="conversa-1"),
        patch("core.pipeline.salvar_mensagem_db"),
    ):
        await processar_mensagem_entrada(mensagem, _TENANT, sender, "cloud_api")

    assert sender.enviar_texto.call_count == 2
    primeira, segunda = sender.enviar_texto.call_args_list
    assert primeira.args[0] == mensagem.numero_remetente
    assert "Sou a *Assistente*" in primeira.args[1]
    assert segunda.args == (
        mensagem.numero_remetente,
        "Responda *1* — Aceito\n*2* — Não aceito",
    )
    mock_consent.assert_awaited_once()


async def test_lgpd_pendente_com_apresentacao_ja_enviada_so_reenvia_lgpd() -> None:
    sender = _mock_sender()
    mensagem = _mensagem()

    with (
        patch("core.pipeline.get_supabase", return_value=MagicMock()),
        patch("core.pipeline.resolver_contato_id", return_value="contato-1"),
        patch(
            "core.pipeline.resolver_conversa_para_entrada",
            return_value=("conversa-1", True, "reuso_aberta"),
        ),
        patch(
            "core.pipeline.handle_consentimento",
            new_callable=AsyncMock,
            return_value=ResultadoConsentimento(
                mensagem="Responda *1* — Aceito\n*2* — Não aceito",
            ),
        ),
        patch("core.pipeline.obter_ou_criar_conversa", return_value="conversa-1"),
        patch(
            "core.pipeline._buscar_metadata_conversa",
            return_value={_METADATA_APRESENTACAO_INICIAL: True},
        ),
        patch("core.pipeline.salvar_mensagem_db"),
    ):
        await processar_mensagem_entrada(mensagem, _TENANT, sender, "cloud_api")

    sender.enviar_texto.assert_called_once_with(
        mensagem.numero_remetente, "Responda *1* — Aceito\n*2* — Não aceito"
    )


# ─── Fluxo normal: grafo invocado e resposta persistida ────────────────────────


async def test_happy_path_invoca_grafo_e_persiste() -> None:
    sender = _mock_sender()
    mensagem = _mensagem("Quero agendar uma consulta")

    grafo_mock = MagicMock()
    grafo_mock.aget_state = AsyncMock(return_value=MagicMock(values={"ja_iniciado": True}))
    grafo_mock.ainvoke = AsyncMock(
        return_value={
            "messages": [AIMessage(content="Claro, vamos agendar!")],
            "contato_id": "contato-1",
            "tenant_config": {},
        }
    )

    with (
        patch("core.pipeline.get_supabase", return_value=MagicMock()),
        patch("core.pipeline.resolver_contato_id", return_value="contato-1"),
        patch(
            "core.pipeline.resolver_conversa_para_entrada",
            return_value=("conversa-1", True, "reuso_aberta"),
        ),
        patch("core.pipeline.handle_consentimento", new_callable=AsyncMock, return_value=None),
        patch("core.pipeline.handle_resposta_lembrete", new_callable=AsyncMock, return_value=None),
        patch("core.pipeline.handle_resposta_nps", new_callable=AsyncMock, return_value=None),
        patch("core.pipeline.obter_ou_criar_conversa", return_value="conversa-1"),
        patch("core.pipeline.salvar_mensagem_db") as mock_salvar,
        patch("core.pipeline._grafo", grafo_mock),
    ):
        await processar_mensagem_entrada(mensagem, _TENANT, sender, "cloud_api")

    sender.enviar_texto.assert_called_once()
    assert "Claro, vamos agendar" in sender.enviar_texto.call_args.args[1]
    # Persiste mensagem do paciente e da resposta do agente
    assert mock_salvar.call_count == 2


async def test_fluxo_normal_prefere_contato_resolvido_ao_contato_stale_do_grafo() -> None:
    sender = _mock_sender()
    mensagem = _mensagem("Quero agendar uma consulta")
    client = MagicMock()

    grafo_mock = MagicMock()
    grafo_mock.aget_state = AsyncMock(return_value=MagicMock(values={"ja_iniciado": True}))
    grafo_mock.ainvoke = AsyncMock(
        return_value={
            "messages": [AIMessage(content="Claro, vamos agendar!")],
            "contato_id": "contato-stale",
            "tenant_config": {},
        }
    )

    with (
        patch("core.pipeline.get_supabase", return_value=client),
        patch("core.pipeline.resolver_contato_id", return_value="contato-correto"),
        patch(
            "core.pipeline.resolver_conversa_para_entrada",
            return_value=("conversa-1", True, "reuso_aberta"),
        ) as mock_resolver,
        patch("core.pipeline.handle_consentimento", new_callable=AsyncMock, return_value=None),
        patch("core.pipeline.handle_resposta_lembrete", new_callable=AsyncMock, return_value=None),
        patch("core.pipeline.handle_resposta_nps", new_callable=AsyncMock, return_value=None),
        patch("core.pipeline.obter_ou_criar_conversa", return_value="conversa-1"),
        patch("core.pipeline.salvar_mensagem_db"),
        patch("core.pipeline._grafo", grafo_mock),
    ):
        await processar_mensagem_entrada(mensagem, _TENANT, sender, "cloud_api")

    entrada = grafo_mock.ainvoke.call_args.args[0]
    assert entrada["contato_id"] == "contato-correto"
    # A conversa é resolvida cedo, a partir do contato correto (não do stale do grafo).
    mock_resolver.assert_called_with(client, "t-1", "contato-correto", "canal-1")


# ─── Humano no controle (resolver devolve agente_ativo=False) ──────────────────
# A decisão de reúso/criação de conversa vive em resolver_conversa_para_entrada
# (testada em test_conversas_db.py). Aqui validamos só o que o PIPELINE faz com
# o resultado: quando o humano está no controle, registra a mensagem e para.


async def test_humano_no_controle_nao_chama_lgpd_nem_grafo() -> None:
    sender = _mock_sender()
    mensagem = _mensagem("oi")

    with (
        patch("core.pipeline.get_supabase", return_value=MagicMock()),
        patch("core.pipeline.resolver_contato_id", return_value="contato-1"),
        patch(
            "core.pipeline.resolver_conversa_para_entrada",
            return_value=("conversa-humano", False, "reuso_mantem_humano"),
        ),
        patch("core.pipeline.salvar_mensagem_db"),
        patch("core.pipeline.handle_consentimento", new_callable=AsyncMock) as mock_consent,
    ):
        await processar_mensagem_entrada(mensagem, _TENANT, sender, "cloud_api")

    mock_consent.assert_not_called()
    sender.enviar_texto.assert_not_called()


async def test_humano_no_controle_salva_na_conversa_resolvida() -> None:
    """Regressão da duplicação de thread: a mensagem do paciente é salva na
    conversa que o resolver devolveu (a que está com o humano), sem criar outra."""
    sender = _mock_sender()
    mensagem = _mensagem("oi")

    with (
        patch("core.pipeline.get_supabase", return_value=MagicMock()),
        patch("core.pipeline.resolver_contato_id", return_value="contato-1"),
        patch(
            "core.pipeline.resolver_conversa_para_entrada",
            return_value=("conversa-humano", False, "reuso_mantem_humano"),
        ),
        patch("core.pipeline.obter_ou_criar_conversa") as mock_criar,
        patch("core.pipeline.salvar_mensagem_db") as mock_salvar,
    ):
        await processar_mensagem_entrada(mensagem, _TENANT, sender, "cloud_api")

    mock_criar.assert_not_called()
    assert mock_salvar.call_args.args[2] == "conversa-humano"
