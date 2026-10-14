from app.config import settings
from core.mensagem_prefixo import prefixar_mensagem_agente


def test_prefixa_com_nome_padrao():
    assert prefixar_mensagem_agente("Posso ajudar?") == "Assistente: Posso ajudar?"


def test_nome_padrao_vem_da_config_e_nao_do_core(monkeypatch):
    """Tenant sem nome_agente usa NOME_AGENTE_PADRAO — o core não fixa persona."""
    monkeypatch.setattr(settings, "nome_agente_padrao", "Ana")
    assert prefixar_mensagem_agente("Oi") == "Ana: Oi"


def test_prefixa_com_nome_agente_customizado():
    assert prefixar_mensagem_agente("Olá!", "Sofia") == "Sofia: Olá!"


def test_nao_duplica_prefixo_em_reenvio():
    texto = prefixar_mensagem_agente("Posso ajudar?")
    assert prefixar_mensagem_agente(texto) == texto


def test_nao_duplica_prefixo_com_nome_composto():
    """Nome de atendente com espaço ('Maria Silva') não deveria ser duplicado
    — esse é o caso que um regex de 'uma palavra antes dos dois-pontos'
    deixaria passar batido."""
    texto = prefixar_mensagem_agente("Oi, tudo bem?", "Maria Silva")
    assert texto == "Maria Silva: Oi, tudo bem?"
    assert prefixar_mensagem_agente(texto, "Maria Silva") == texto


def test_nomes_diferentes_nao_sao_tratados_como_mesmo_prefixo():
    """Prefixo do nome padrão não deve ser reconhecido como já-prefixado por 'Sofia'."""
    texto_padrao = prefixar_mensagem_agente("Olá")
    texto_sofia = prefixar_mensagem_agente(texto_padrao, "Sofia")
    assert texto_sofia == f"Sofia: {texto_padrao}"
