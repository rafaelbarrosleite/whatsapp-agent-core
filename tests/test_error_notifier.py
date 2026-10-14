import logging
import sys
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

from core.error_notifier import WhatsAppErrorHandler, notificar_erro

# ---------------------------------------------------------------------------
# notificar_erro — credenciais
# ---------------------------------------------------------------------------


async def test_notificar_erro_nao_propaga_excecao():
    """Se o envio HTTP falhar, notificar_erro nunca levanta exceção."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=RuntimeError("conexão recusada"))

    with (
        patch("core.error_notifier.httpx.AsyncClient", return_value=mock_client),
        patch("core.error_notifier.settings") as mock_settings,
    ):
        mock_settings.whatsapp_cloud_token = "tok"
        mock_settings.whatsapp_phone_number_id = "ph-id"
        mock_settings.monitor_phone = "5567999991234"

        await notificar_erro("modulo | funcao", "algo deu errado", "traceback aqui")


async def test_notificar_erro_silencioso_sem_credenciais():
    """Sem token configurado, a função retorna sem fazer chamada HTTP."""
    with patch("core.error_notifier.settings") as mock_settings:
        mock_settings.whatsapp_cloud_token = None
        mock_settings.whatsapp_phone_number_id = None

        with patch("core.error_notifier.httpx.AsyncClient") as mock_http:
            await notificar_erro("ctx", "err")
            mock_http.assert_not_called()


async def test_notificar_erro_inclui_detalhes_na_mensagem():
    """Detalhes (traceback) aparecem no corpo da mensagem enviada."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=MagicMock())

    with (
        patch("core.error_notifier.httpx.AsyncClient", return_value=mock_client),
        patch("core.error_notifier.settings") as mock_settings,
    ):
        mock_settings.whatsapp_cloud_token = "tok"
        mock_settings.whatsapp_phone_number_id = "ph-id"
        mock_settings.monitor_phone = "5567999991234"

        await notificar_erro("ctx", "erro", detalhes="Traceback (most recent call last)...")

    body = mock_client.post.call_args.kwargs["json"]
    assert "Traceback" in body["text"]["body"]
    assert "ERRO NO AGENTE" in body["text"]["body"]


# ---------------------------------------------------------------------------
# notificar_erro — monitor_phone
# ---------------------------------------------------------------------------


async def test_notificar_erro_monitor_phone_none_nao_envia():
    """monitor_phone=None → não chama HTTP."""
    with (
        patch("core.error_notifier.settings") as mock_settings,
        patch("core.error_notifier.httpx.AsyncClient") as mock_http,
    ):
        mock_settings.whatsapp_cloud_token = "tok"
        mock_settings.whatsapp_phone_number_id = "ph-id"
        mock_settings.monitor_phone = None

        await notificar_erro("ctx", "err")

    mock_http.assert_not_called()


async def test_notificar_erro_monitor_phone_none_escreve_stderr():
    """monitor_phone=None → aviso em stderr (não log.error para evitar recursão)."""
    buf = StringIO()
    with (
        patch("core.error_notifier.settings") as mock_settings,
        patch("core.error_notifier.sys.stderr", buf),
    ):
        mock_settings.whatsapp_cloud_token = "tok"
        mock_settings.whatsapp_phone_number_id = "ph-id"
        mock_settings.monitor_phone = None

        await notificar_erro("ctx", "err")

    assert "MONITOR_PHONE" in buf.getvalue()


async def test_notificar_erro_destino_e_monitor_phone():
    """Mensagem é enviada para settings.monitor_phone, não para número do médico."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=MagicMock())

    with (
        patch("core.error_notifier.httpx.AsyncClient", return_value=mock_client),
        patch("core.error_notifier.settings") as mock_settings,
    ):
        mock_settings.whatsapp_cloud_token = "tok"
        mock_settings.whatsapp_phone_number_id = "ph-id"
        mock_settings.monitor_phone = "5567MONITOR"

        await notificar_erro("ctx", "err")

    body = mock_client.post.call_args.kwargs["json"]
    assert body["to"] == "5567MONITOR"


# ---------------------------------------------------------------------------
# WhatsAppErrorHandler — níveis
# ---------------------------------------------------------------------------


def test_handler_so_processa_errors():
    """Logs INFO e WARNING não disparam notificação."""
    handler = WhatsAppErrorHandler()

    with patch("core.error_notifier.asyncio.get_running_loop") as mock_loop:
        mock_loop.return_value = MagicMock()

        for level in (logging.DEBUG, logging.INFO, logging.WARNING):
            record = logging.LogRecord(
                name="teste",
                level=level,
                pathname="",
                lineno=0,
                msg="mensagem",
                args=(),
                exc_info=None,
            )
            handler.emit(record)

        mock_loop.assert_not_called()


def test_handler_processa_error():
    """Log de ERROR dispara create_task com notificar_erro."""
    handler = WhatsAppErrorHandler()
    mock_loop = MagicMock()
    captured: list = []
    mock_loop.create_task.side_effect = captured.append

    with patch("core.error_notifier.asyncio.get_running_loop", return_value=mock_loop):
        record = logging.LogRecord(
            name="app.modulo",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="falha crítica",
            args=(),
            exc_info=None,
        )
        handler.emit(record)

    assert len(captured) == 1
    captured[0].close()


# ---------------------------------------------------------------------------
# WhatsAppErrorHandler — sem event loop (PARTE D)
# ---------------------------------------------------------------------------


def test_handler_sem_event_loop_chama_asyncio_run():
    """Sem loop ativo, o handler tenta envio síncrono via asyncio.run."""
    handler = WhatsAppErrorHandler()

    with (
        patch(
            "core.error_notifier.asyncio.get_running_loop",
            side_effect=RuntimeError("no running event loop"),
        ),
        patch("core.error_notifier.asyncio.run") as mock_run,
    ):
        record = logging.LogRecord(
            name="mod",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="erro standalone",
            args=(),
            exc_info=None,
        )
        handler.emit(record)

    mock_run.assert_called_once()


def test_handler_sem_event_loop_nao_levanta_mesmo_se_asyncio_run_falhar():
    """RuntimeError de get_running_loop + asyncio.run levanta = handler silencia tudo."""
    handler = WhatsAppErrorHandler()

    with (
        patch(
            "core.error_notifier.asyncio.get_running_loop",
            side_effect=RuntimeError("no running event loop"),
        ),
        patch(
            "core.error_notifier.asyncio.run",
            side_effect=Exception("run também falhou"),
        ),
    ):
        record = logging.LogRecord(
            name="mod",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="erro",
            args=(),
            exc_info=None,
        )
        handler.emit(record)  # não deve levantar


def test_handler_inclui_traceback_em_detalhes():
    """Quando exc_info está presente, detalhes do traceback são passados."""
    handler = WhatsAppErrorHandler()
    mock_loop = MagicMock()
    captured: list = []
    mock_loop.create_task.side_effect = captured.append

    with patch("core.error_notifier.asyncio.get_running_loop", return_value=mock_loop):
        try:
            raise ValueError("erro de teste")
        except ValueError:
            record = logging.LogRecord(
                name="mod",
                level=logging.ERROR,
                pathname="",
                lineno=0,
                msg="falha",
                args=(),
                exc_info=sys.exc_info(),
            )
            handler.emit(record)

    assert len(captured) == 1
    captured[0].close()
