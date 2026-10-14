from core.modelos import PADRAO, modelo_do_tenant


def test_sem_config_usa_o_padrao() -> None:
    assert modelo_do_tenant(None, "atendimento") == PADRAO["atendimento"]
    assert modelo_do_tenant({}, "interno") == PADRAO["interno"]


def test_modelo_escolhido_pela_clinica() -> None:
    config = {"modelos_agente": {"atendimento": "claude-haiku-4-5", "interno": "claude-sonnet-4-6"}}
    assert modelo_do_tenant(config, "atendimento") == "claude-haiku-4-5"
    assert modelo_do_tenant(config, "interno") == "claude-sonnet-4-6"


def test_modelo_fora_da_lista_cai_no_padrao() -> None:
    config = {"modelos_agente": {"atendimento": "modelo-inventado", "interno": None}}
    assert modelo_do_tenant(config, "atendimento") == PADRAO["atendimento"]
    assert modelo_do_tenant(config, "interno") == PADRAO["interno"]
