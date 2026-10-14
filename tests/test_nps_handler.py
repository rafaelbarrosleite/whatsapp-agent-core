from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.nps_handler import _buscar_nps_pendente, _registrar_resposta

TELEFONE = "+5567999991234"
MENSAGEM = "9"
TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CONTATO = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _resp(status: int, body: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = body
    return r


def _mock_client(resposta) -> MagicMock:
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=resposta)
    return client


def _mock_vertical_query(data: list) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Mock da cadeia .table().select().eq().eq().is_().gte().order().limit().execute().

    Retorna (vertical, is_mock, gte_mock) para inspecionar as colunas usadas
    nos filtros .is_/.gte/.order além do resultado.
    """
    vertical = MagicMock()
    select_mock = vertical.table.return_value.select.return_value
    is_mock = select_mock.eq.return_value.eq.return_value
    gte_mock = is_mock.is_.return_value
    order_mock = gte_mock.gte.return_value
    order_mock.order.return_value.limit.return_value.execute.return_value.data = data
    return vertical, is_mock, gte_mock


async def test_sem_crm_url_loga_error_e_nao_chama_http():
    with (
        patch("gateway.nps_handler.httpx.AsyncClient") as mock_client_cls,
        patch("gateway.nps_handler.settings") as mock_settings,
        patch("gateway.nps_handler.log") as mock_log,
    ):
        mock_settings.crm_url = None

        await _registrar_resposta(TELEFONE, MENSAGEM, TENANT, CONTATO)

    mock_client_cls.assert_not_called()
    mensagem_log = mock_log.error.call_args.args[0]
    assert "crm_url" in mensagem_log
    assert "CRM_URL" in mensagem_log
    mock_log.warning.assert_not_called()


async def test_inclui_header_x_internal_key():
    mock = _mock_client(_resp(200))

    with (
        patch("gateway.nps_handler.httpx.AsyncClient", return_value=mock),
        patch("gateway.nps_handler.settings") as mock_settings,
    ):
        mock_settings.crm_url = "https://crm.exemplo.com"
        mock_settings.internal_api_key = "chave-secreta"

        await _registrar_resposta(TELEFONE, MENSAGEM, TENANT, CONTATO)

    headers = mock.post.call_args.kwargs["headers"]
    assert headers == {"x-internal-key": "chave-secreta"}


async def test_envia_clinica_e_contato_alem_do_telefone():
    """Telefone sozinho não identifica o paciente: vai a identidade (tenant + contato)."""
    mock = _mock_client(_resp(200))

    with (
        patch("gateway.nps_handler.httpx.AsyncClient", return_value=mock),
        patch("gateway.nps_handler.settings") as mock_settings,
    ):
        mock_settings.crm_url = "https://crm.exemplo.com"
        mock_settings.internal_api_key = "chave-secreta"

        await _registrar_resposta(TELEFONE, MENSAGEM, TENANT, CONTATO)

    assert mock.post.call_args.kwargs["json"] == {
        "telefone": TELEFONE,
        "mensagem": MENSAGEM,
        "tenant_id": TENANT,
        "contato_id": CONTATO,
    }


async def test_sem_internal_api_key_loga_error_antes_da_chamada():
    mock = _mock_client(_resp(401, "Unauthorized"))

    with (
        patch("gateway.nps_handler.httpx.AsyncClient", return_value=mock),
        patch("gateway.nps_handler.settings") as mock_settings,
        patch("gateway.nps_handler.log") as mock_log,
    ):
        mock_settings.crm_url = "https://crm.exemplo.com"
        mock_settings.internal_api_key = None

        await _registrar_resposta(TELEFONE, MENSAGEM, TENANT, CONTATO)

    primeira_chamada = mock_log.error.call_args_list[0]
    assert "internal_api_key" in primeira_chamada.args[0]
    # o log de configuração ausente precisa ocorrer antes do POST
    assert mock_log.error.call_count >= 1
    assert mock.post.call_args.kwargs["headers"] == {"x-internal-key": ""}


async def test_401_loga_contexto_especifico():
    mock = _mock_client(_resp(401, "Unauthorized"))

    with (
        patch("gateway.nps_handler.httpx.AsyncClient", return_value=mock),
        patch("gateway.nps_handler.settings") as mock_settings,
        patch("gateway.nps_handler.log") as mock_log,
    ):
        mock_settings.crm_url = "https://crm.exemplo.com"
        mock_settings.internal_api_key = "chave-secreta"

        await _registrar_resposta(TELEFONE, MENSAGEM, TENANT, CONTATO)

    mensagem_log = mock_log.error.call_args.args[0]
    assert "401" in mensagem_log
    assert "INTERNAL_API_KEY" in mensagem_log


async def test_outro_status_diferente_de_401_mantem_log_generico():
    mock = _mock_client(_resp(500, "Internal Server Error"))

    with (
        patch("gateway.nps_handler.httpx.AsyncClient", return_value=mock),
        patch("gateway.nps_handler.settings") as mock_settings,
        patch("gateway.nps_handler.log") as mock_log,
    ):
        mock_settings.crm_url = "https://crm.exemplo.com"
        mock_settings.internal_api_key = "chave-secreta"

        await _registrar_resposta(TELEFONE, MENSAGEM, TENANT, CONTATO)

    mensagem_log = mock_log.error.call_args.args[0]
    assert "INTERNAL_API_KEY" not in mensagem_log
    assert 500 in mock_log.error.call_args.args


async def test_sucesso_nao_loga_error():
    mock = _mock_client(_resp(200))

    with (
        patch("gateway.nps_handler.httpx.AsyncClient", return_value=mock),
        patch("gateway.nps_handler.settings") as mock_settings,
        patch("gateway.nps_handler.log") as mock_log,
    ):
        mock_settings.crm_url = "https://crm.exemplo.com"
        mock_settings.internal_api_key = "chave-secreta"

        await _registrar_resposta(TELEFONE, MENSAGEM, TENANT, CONTATO)

    mock_log.error.assert_not_called()


# ─── _buscar_nps_pendente ─────────────────────────────────────────────────────


def test_buscar_nps_pendente_encontra_registro_recente():
    """Registro com score null e criado_em recente é encontrado."""
    vertical, is_mock, gte_mock = _mock_vertical_query([{"id": "abc-123"}])

    resultado = _buscar_nps_pendente(vertical, "tenant-1", "contato-1")

    assert resultado == {"id": "abc-123"}
    is_mock.is_.assert_called_once_with("score", "null")
    gte_mock.gte.assert_called_once()
    assert gte_mock.gte.call_args.args[0] == "criado_em"
    gte_mock.gte.return_value.order.assert_called_once_with("criado_em", desc=True)


def test_buscar_nps_pendente_nao_encontra_score_ja_preenchido():
    """Sem linhas pendentes (score já preenchido é filtrado no banco) → None + log.info."""
    vertical, is_mock, _ = _mock_vertical_query([])

    with patch("gateway.nps_handler.log") as mock_log:
        resultado = _buscar_nps_pendente(vertical, "tenant-1", "contato-1")

    assert resultado is None
    is_mock.is_.assert_called_once_with("score", "null")
    mock_log.info.assert_called_once()
    mensagem_log = mock_log.info.call_args.args[0]
    assert "tenant-1" in mock_log.info.call_args.args
    assert "contato-1" in mock_log.info.call_args.args
    assert "NPS" in mensagem_log


def test_buscar_nps_pendente_nao_encontra_mais_de_24h():
    """Cutoff enviado ao .gte() é ~24h atrás de agora.

    Registro fora da janela é filtrado no próprio banco.
    """
    vertical, _, gte_mock = _mock_vertical_query([])

    antes = datetime.now(timezone.utc) - timedelta(hours=24, minutes=1)
    resultado = _buscar_nps_pendente(vertical, "tenant-1", "contato-1")
    depois = datetime.now(timezone.utc) - timedelta(hours=23, minutes=59)

    assert resultado is None
    cutoff_enviado = datetime.fromisoformat(gte_mock.gte.call_args.args[1])
    assert antes <= cutoff_enviado <= depois
