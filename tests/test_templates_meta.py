"""channels/templates.py, envio de template (cloud_api_sender) e o botão de
template no adapter (item 3.1)."""

from unittest.mock import AsyncMock, patch

from channels.base import TipoMensagem
from channels.cloud_api import CloudApiAdapter
from channels.cloud_api_sender import CloudApiSender
from channels.templates import TemplateMeta, parametro, template_da_clinica


def test_template_da_clinica() -> None:
    config = {"templates": {"lembrete_consulta": {"nome": " lembrete_consulta "}}}
    assert template_da_clinica(config, "lembrete_consulta") == TemplateMeta(
        "lembrete_consulta", "pt_BR"
    )
    assert template_da_clinica(config, "aviso_atraso") is None
    assert template_da_clinica({"templates": {"x": {"nome": ""}}}, "x") is None
    assert template_da_clinica(None, "x") is None
    en = {"templates": {"x": {"nome": "t", "idioma": "en_US"}}}
    assert template_da_clinica(en, "x") == TemplateMeta("t", "en_US")


def test_parametro_uma_linha_sem_espacos_repetidos() -> None:
    assert parametro("Clínica\n\tOrto    Centro") == "Clínica Orto Centro"
    assert parametro("") == "-"
    assert len(parametro("x" * 500)) == 200


async def test_sender_monta_payload_do_template_com_botoes() -> None:
    sender = CloudApiSender(phone_number_id="111", token="tok")
    with patch(
        "channels.cloud_api_sender._enviar_payload", new_callable=AsyncMock, return_value=True
    ) as envio:
        ok = await sender.enviar_template(
            "+5567988880000",
            "lembrete_consulta",
            "pt_BR",
            ["Ana", "Clínica"],
            payloads_botoes=["a", "b"],
        )
    assert ok is True
    payload = envio.call_args.args[0]
    assert payload["to"] == "5567988880000"
    assert payload["type"] == "template"
    tpl = payload["template"]
    assert tpl["name"] == "lembrete_consulta"
    assert tpl["language"] == {"code": "pt_BR"}
    assert tpl["components"][0] == {
        "type": "body",
        "parameters": [{"type": "text", "text": "Ana"}, {"type": "text", "text": "Clínica"}],
    }
    assert tpl["components"][1] == {
        "type": "button",
        "sub_type": "quick_reply",
        "index": "0",
        "parameters": [{"type": "payload", "payload": "a"}],
    }
    assert tpl["components"][2]["index"] == "1"


def test_adapter_le_o_botao_do_template() -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {
                                "display_phone_number": "556733334444",
                                "phone_number_id": "1",
                            },
                            "messages": [
                                {
                                    "from": "5567988880000",
                                    "id": "wamid.1",
                                    "timestamp": "1719792000",
                                    "type": "button",
                                    "button": {
                                        "payload": "lembrete:confirmar:ag-1",
                                        "text": "Confirmar presença",
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }
    m = CloudApiAdapter().normalizar(payload)
    assert m.tipo == TipoMensagem.botao_resposta
    assert m.texto == "Confirmar presença"
    assert m.metadata["button_id"] == "lembrete:confirmar:ag-1"
