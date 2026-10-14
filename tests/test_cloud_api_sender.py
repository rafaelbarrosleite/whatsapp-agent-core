from unittest.mock import AsyncMock, MagicMock, call, patch

import httpx
import pytest

from channels.cloud_api_sender import CloudApiSender, enviar_mensagem, enviar_mensagem_com_botoes

NUMERO = "+5567999991234"
TEXTO = "Olá, teste."
TOKEN = "tok-abc"
PHONE_ID = "123456789"


def _resp(status: int, body: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = body
    return r


def _mock_client(*respostas) -> MagicMock:
    """Cria mock de httpx.AsyncClient com respostas sequenciais (ou exceções) em .post."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(side_effect=list(respostas))
    return client


# ─── Sucesso ──────────────────────────────────────────────────────────────────


async def test_sucesso_na_primeira_tentativa():
    mock = _mock_client(_resp(200))

    with (
        patch("channels.cloud_api_sender.httpx.AsyncClient", return_value=mock),
        patch("channels.cloud_api_sender.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        result = await enviar_mensagem(NUMERO, TEXTO, TOKEN, PHONE_ID)

    assert result is True
    assert mock.post.call_count == 1
    mock_sleep.assert_not_called()


async def test_texto_envia_com_preview_url_para_link_gerar_miniatura():
    """preview_url=True no payload → WhatsApp gera preview de links (ex.: o Maps
    na mensagem de localização)."""
    mock = _mock_client(_resp(200))
    with (
        patch("channels.cloud_api_sender.httpx.AsyncClient", return_value=mock),
        patch("channels.cloud_api_sender.asyncio.sleep", new_callable=AsyncMock),
    ):
        await enviar_mensagem(NUMERO, TEXTO, TOKEN, PHONE_ID)

    payload = mock.post.call_args.kwargs["json"]
    assert payload["text"]["preview_url"] is True
    assert payload["text"]["body"] == TEXTO


async def test_sucesso_na_segunda_tentativa_apos_timeout():
    mock = _mock_client(httpx.TimeoutException("timeout"), _resp(200))

    with (
        patch("channels.cloud_api_sender.httpx.AsyncClient", return_value=mock),
        patch("channels.cloud_api_sender.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        result = await enviar_mensagem(NUMERO, TEXTO, TOKEN, PHONE_ID)

    assert result is True
    assert mock.post.call_count == 2
    mock_sleep.assert_called_once_with(2)


async def test_sucesso_na_terceira_tentativa_apos_5xx():
    mock = _mock_client(_resp(503), _resp(502), _resp(200))

    with (
        patch("channels.cloud_api_sender.httpx.AsyncClient", return_value=mock),
        patch("channels.cloud_api_sender.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        result = await enviar_mensagem(NUMERO, TEXTO, TOKEN, PHONE_ID)

    assert result is True
    assert mock.post.call_count == 3
    assert mock_sleep.call_args_list == [call(2), call(4)]


async def test_201_tambem_e_sucesso():
    mock = _mock_client(_resp(201))

    with patch("channels.cloud_api_sender.httpx.AsyncClient", return_value=mock):
        result = await enviar_mensagem(NUMERO, TEXTO, TOKEN, PHONE_ID)

    assert result is True


# ─── Erros permanentes (sem retry) ───────────────────────────────────────────


async def test_4xx_retorna_false_sem_retry():
    mock = _mock_client(_resp(401, "Unauthorized"))

    with (
        patch("channels.cloud_api_sender.httpx.AsyncClient", return_value=mock),
        patch("channels.cloud_api_sender.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        result = await enviar_mensagem(NUMERO, TEXTO, TOKEN, PHONE_ID)

    assert result is False
    assert mock.post.call_count == 1
    mock_sleep.assert_not_called()


async def test_400_sem_retry():
    mock = _mock_client(_resp(400, "Bad Request"))
    with (
        patch("channels.cloud_api_sender.httpx.AsyncClient", return_value=mock),
        patch("channels.cloud_api_sender.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        result = await enviar_mensagem(NUMERO, TEXTO, TOKEN, PHONE_ID)
    assert result is False
    mock_sleep.assert_not_called()


async def test_excecao_inesperada_retorna_false_sem_retry():
    mock = _mock_client(RuntimeError("boom"))

    with (
        patch("channels.cloud_api_sender.httpx.AsyncClient", return_value=mock),
        patch("channels.cloud_api_sender.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        result = await enviar_mensagem(NUMERO, TEXTO, TOKEN, PHONE_ID)

    assert result is False
    assert mock.post.call_count == 1
    mock_sleep.assert_not_called()


# ─── Esgotamento de tentativas ────────────────────────────────────────────────


async def test_3_timeouts_retorna_false():
    mock = _mock_client(
        httpx.TimeoutException("t"),
        httpx.TimeoutException("t"),
        httpx.TimeoutException("t"),
    )

    with (
        patch("channels.cloud_api_sender.httpx.AsyncClient", return_value=mock),
        patch("channels.cloud_api_sender.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        result = await enviar_mensagem(NUMERO, TEXTO, TOKEN, PHONE_ID)

    assert result is False
    assert mock.post.call_count == 3
    assert mock_sleep.call_args_list == [call(2), call(4)]


async def test_3_connect_errors_retorna_false():
    mock = _mock_client(
        httpx.ConnectError("conn"),
        httpx.ConnectError("conn"),
        httpx.ConnectError("conn"),
    )

    with (
        patch("channels.cloud_api_sender.httpx.AsyncClient", return_value=mock),
        patch("channels.cloud_api_sender.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        result = await enviar_mensagem(NUMERO, TEXTO, TOKEN, PHONE_ID)

    assert result is False
    assert mock.post.call_count == 3
    assert mock_sleep.call_args_list == [call(2), call(4)]


async def test_3x_5xx_retorna_false():
    mock = _mock_client(_resp(500), _resp(500), _resp(500))

    with (
        patch("channels.cloud_api_sender.httpx.AsyncClient", return_value=mock),
        patch("channels.cloud_api_sender.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        result = await enviar_mensagem(NUMERO, TEXTO, TOKEN, PHONE_ID)

    assert result is False
    assert mock.post.call_count == 3
    assert mock_sleep.call_args_list == [call(2), call(4)]


# ─── 429 é recuperável ────────────────────────────────────────────────────────


async def test_429_faz_retry():
    mock = _mock_client(_resp(429), _resp(200))

    with (
        patch("channels.cloud_api_sender.httpx.AsyncClient", return_value=mock),
        patch("channels.cloud_api_sender.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        result = await enviar_mensagem(NUMERO, TEXTO, TOKEN, PHONE_ID)

    assert result is True
    assert mock.post.call_count == 2
    mock_sleep.assert_called_once_with(2)


# ─── Payload correto ──────────────────────────────────────────────────────────


async def test_numero_enviado_sem_sinal_de_mais():
    mock = _mock_client(_resp(200))

    with patch("channels.cloud_api_sender.httpx.AsyncClient", return_value=mock):
        await enviar_mensagem("+5567999991234", TEXTO, TOKEN, PHONE_ID)

    payload = mock.post.call_args.kwargs["json"]
    assert payload["to"] == "5567999991234"


# ─── enviar_mensagem_com_botoes ────────────────────────────────────────────────


async def test_botoes_monta_payload_interactive_button() -> None:
    mock = _mock_client(_resp(200))
    botoes = [("lgpd_aceitar", "Aceito"), ("lgpd_negar", "Não aceito")]

    with patch("channels.cloud_api_sender.httpx.AsyncClient", return_value=mock):
        result = await enviar_mensagem_com_botoes(NUMERO, TEXTO, botoes, TOKEN, PHONE_ID)

    assert result is True
    payload = mock.post.call_args.kwargs["json"]
    assert payload["type"] == "interactive"
    assert payload["interactive"]["type"] == "button"
    assert payload["interactive"]["body"]["text"] == TEXTO
    assert payload["interactive"]["action"]["buttons"] == [
        {"type": "reply", "reply": {"id": "lgpd_aceitar", "title": "Aceito"}},
        {"type": "reply", "reply": {"id": "lgpd_negar", "title": "Não aceito"}},
    ]


async def test_botoes_mais_de_tres_levanta_erro() -> None:
    botoes = [("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")]
    with pytest.raises(ValueError):
        await enviar_mensagem_com_botoes(NUMERO, TEXTO, botoes, TOKEN, PHONE_ID)


async def test_botoes_lista_vazia_levanta_erro() -> None:
    with pytest.raises(ValueError):
        await enviar_mensagem_com_botoes(NUMERO, TEXTO, [], TOKEN, PHONE_ID)


async def test_botoes_reaproveita_retry_em_5xx() -> None:
    mock = _mock_client(_resp(503), _resp(200))
    botoes = [("id1", "Sim")]

    with (
        patch("channels.cloud_api_sender.httpx.AsyncClient", return_value=mock),
        patch("channels.cloud_api_sender.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        result = await enviar_mensagem_com_botoes(NUMERO, TEXTO, botoes, TOKEN, PHONE_ID)

    assert result is True
    assert mock.post.call_count == 2
    mock_sleep.assert_called_once_with(2)


# ─── CloudApiSender (credenciais no construtor) ────────────────────────────────


async def test_cloud_api_sender_enviar_texto() -> None:
    mock = _mock_client(_resp(200))
    sender = CloudApiSender(phone_number_id=PHONE_ID, token=TOKEN)

    with patch("channels.cloud_api_sender.httpx.AsyncClient", return_value=mock):
        result = await sender.enviar_texto(NUMERO, TEXTO)

    assert result is True
    payload = mock.post.call_args.kwargs["json"]
    assert payload["text"]["body"] == TEXTO


async def test_cloud_api_sender_enviar_botoes() -> None:
    mock = _mock_client(_resp(200))
    sender = CloudApiSender(phone_number_id=PHONE_ID, token=TOKEN)
    botoes = [("lgpd_aceitar", "Aceito"), ("lgpd_negar", "Não aceito")]

    with patch("channels.cloud_api_sender.httpx.AsyncClient", return_value=mock):
        result = await sender.enviar_botoes(NUMERO, TEXTO, botoes)

    assert result is True
    payload = mock.post.call_args.kwargs["json"]
    assert payload["type"] == "interactive"


# ─── Mídia (imagem / áudio / documento) ───────────────────────────────────────

LINK = "https://storage.exemplo/signed/arquivo"


async def test_enviar_midia_imagem_usa_type_image_com_caption() -> None:
    mock = _mock_client(_resp(200))
    sender = CloudApiSender(phone_number_id=PHONE_ID, token=TOKEN)

    with patch("channels.cloud_api_sender.httpx.AsyncClient", return_value=mock):
        result = await sender.enviar_midia(NUMERO, LINK, "imagem", caption="Raio-x")

    assert result is True
    payload = mock.post.call_args.kwargs["json"]
    assert payload["type"] == "image"
    assert payload["image"] == {"link": LINK, "caption": "Raio-x"}


async def test_enviar_midia_audio_usa_type_audio_sem_caption_nem_filename() -> None:
    """Mensagem de voz da atendente: a Meta não aceita caption/filename em
    `audio` — o payload leva só o link."""
    mock = _mock_client(_resp(200))
    sender = CloudApiSender(phone_number_id=PHONE_ID, token=TOKEN)

    with patch("channels.cloud_api_sender.httpx.AsyncClient", return_value=mock):
        result = await sender.enviar_midia(
            NUMERO, LINK, "audio", caption="ignorado", filename="audio.ogg"
        )

    assert result is True
    payload = mock.post.call_args.kwargs["json"]
    assert payload["type"] == "audio"
    assert payload["audio"] == {"link": LINK}
    assert "caption" not in payload["audio"]
    assert "filename" not in payload["audio"]


async def test_enviar_midia_documento_mantem_filename() -> None:
    mock = _mock_client(_resp(200))
    sender = CloudApiSender(phone_number_id=PHONE_ID, token=TOKEN)

    with patch("channels.cloud_api_sender.httpx.AsyncClient", return_value=mock):
        result = await sender.enviar_midia(NUMERO, LINK, "documento", filename="orcamento.pdf")

    assert result is True
    payload = mock.post.call_args.kwargs["json"]
    assert payload["type"] == "document"
    assert payload["document"] == {"link": LINK, "filename": "orcamento.pdf"}
