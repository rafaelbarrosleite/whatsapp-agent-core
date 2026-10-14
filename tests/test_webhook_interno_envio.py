"""Teste para app/webhook_interno.py — envio da resposta ao médico passa a
usar enviar_mensagem_tenant (resolve canal/credenciais por tenant), não mais
Cloud API hardcoded com settings globais."""

from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage

from app.webhook_interno import _processar_mensagem_medico

_TENANT = {
    "id": "tenant-1",
    "config": {"medico_whatsapp": "+5567999990000"},
}


async def test_resposta_ao_medico_usa_enviar_mensagem_tenant() -> None:
    grafo_mock = MagicMock()
    grafo_mock.aget_state = AsyncMock(return_value=MagicMock(values={}))
    grafo_mock.ainvoke = AsyncMock(
        return_value={
            "messages": [AIMessage(content="Olá, doutor!")],
        }
    )

    with (
        patch("app.webhook_interno._grafo_interno", grafo_mock),
        patch("app.webhook_interno.get_supabase", return_value=MagicMock()),
        patch(
            "app.webhook_interno.enviar_mensagem_tenant",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_enviar,
    ):
        await _processar_mensagem_medico("oi", _TENANT)

    mock_enviar.assert_called_once_with(
        tenant_id="tenant-1",
        numero_destino="+5567999990000",
        texto="Olá, doutor!",
        core_db=mock_enviar.call_args.kwargs["core_db"],
    )
