"""Testes para channels/cloud_api.py — em especial suporte a botões (interactive)."""

from channels.base import TipoMensagem
from channels.cloud_api import CloudApiAdapter


def _payload_base(message: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {
                                "display_phone_number": "5511888880000",
                                "phone_number_id": "PHONE-1",
                            },
                            "contacts": [{"profile": {"name": "João Silva"}}],
                            "messages": [message],
                        }
                    }
                ]
            }
        ],
    }


def _payload_botao(button_id: str = "lgpd_aceitar", title: str = "Aceito") -> dict:
    return _payload_base(
        {
            "from": "5567988880000",
            "id": "MSG-BTN-1",
            "timestamp": "1719792000",
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": button_id, "title": title},
            },
        }
    )


def test_eh_mensagem_entrada_aceita_payload_interactive() -> None:
    adapter = CloudApiAdapter()
    assert adapter.eh_mensagem_entrada(_payload_botao()) is True


def test_normalizar_botao_define_tipo_botao_resposta() -> None:
    adapter = CloudApiAdapter()
    mensagem = adapter.normalizar(_payload_botao())
    assert mensagem.tipo == TipoMensagem.botao_resposta


def test_normalizar_botao_extrai_button_id_em_metadata() -> None:
    adapter = CloudApiAdapter()
    mensagem = adapter.normalizar(_payload_botao(button_id="lgpd_negar"))
    assert mensagem.metadata["button_id"] == "lgpd_negar"


def test_normalizar_botao_usa_title_como_texto() -> None:
    adapter = CloudApiAdapter()
    mensagem = adapter.normalizar(_payload_botao(title="Não aceito"))
    assert mensagem.texto == "Não aceito"


def test_normalizar_texto_comum_nao_tem_button_id() -> None:
    adapter = CloudApiAdapter()
    payload = _payload_base(
        {
            "from": "5567988880000",
            "id": "MSG-TXT-1",
            "timestamp": "1719792000",
            "type": "text",
            "text": {"body": "Olá"},
        }
    )
    mensagem = adapter.normalizar(payload)
    assert mensagem.metadata["button_id"] is None
    assert mensagem.tipo == TipoMensagem.texto


def test_normalizar_payload_interactive_malformado_levanta_erro() -> None:
    adapter = CloudApiAdapter()
    payload = _payload_base(
        {
            "from": "5567988880000",
            "id": "MSG-BAD-1",
            "timestamp": "1719792000",
            "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {}},
        }
    )
    # button_id/title ausentes não devem levantar exceção — ficam None
    mensagem = adapter.normalizar(payload)
    assert mensagem.metadata["button_id"] is None
    assert mensagem.texto is None
