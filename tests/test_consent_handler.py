"""Testes para gateway/consent_handler.py — gate de consentimento LGPD."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from channels.base import MensagemRecebida, TipoMensagem
from gateway.consent_handler import (
    _BOTAO_ACEITAR,
    _BOTAO_NEGAR,
    ResultadoConsentimento,
    _link_consentimento,
    handle_consentimento,
)
from gateway.tenant_resolver import TenantInfo

_TENANT_A = TenantInfo(tenant_id="tenant-a", vertical="clinica", canal_id="canal-a")
_TENANT_B = TenantInfo(tenant_id="tenant-b", vertical="clinica", canal_id="canal-b")

_PATCH_CORE = "gateway.consent_handler.get_supabase"
_PATCH_VERTICAL = "gateway.consent_handler.get_vertical_supabase"


def _mensagem(
    texto: str = "Olá",
    button_id: str | None = None,
    nome_remetente: str | None = "João Silva",
) -> MensagemRecebida:
    return MensagemRecebida(
        message_id="msg-1",
        numero_destino="+551199990000",
        numero_remetente="+5567988880000",
        nome_remetente=nome_remetente,
        tipo=TipoMensagem.botao_resposta if button_id else TipoMensagem.texto,
        texto=texto,
        provider="cloud_api",
        timestamp=datetime.now(timezone.utc),
        metadata={"button_id": button_id} if button_id else {},
    )


def _mock_core(
    *,
    insert_id: str | None = None,
    select_data: list[dict] | None = None,
    update_side_effect: Exception | None = None,
) -> MagicMock:
    core = MagicMock()
    if insert_id is not None:
        core.table.return_value.insert.return_value.execute.return_value.data = [{"id": insert_id}]
    core.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data = (
        select_data or []
    )
    if update_side_effect is not None:
        core.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.side_effect = update_side_effect
    return core


def _mock_vertical(termo: dict | None = None) -> MagicMock:
    vertical = MagicMock()
    vertical.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data = (
        [termo] if termo else []
    )
    return vertical


# ─── Paciente novo (contato_id=None) ───────────────────────────────────────────


async def test_paciente_novo_cria_contato_com_lgpd_aceito_false_e_bloqueia() -> None:
    core = _mock_core(insert_id="novo-contato-id")
    vertical = _mock_vertical()

    with patch(_PATCH_CORE, return_value=core), patch(_PATCH_VERTICAL, return_value=vertical):
        resultado = await handle_consentimento(_mensagem(), _TENANT_A, None)

    assert isinstance(resultado, ResultadoConsentimento)
    assert resultado.contato_id == "novo-contato-id"
    assert resultado.botoes is not None
    assert {b[0] for b in resultado.botoes} == {_BOTAO_ACEITAR, _BOTAO_NEGAR}

    insert_payload = core.table.return_value.insert.call_args.args[0]
    assert insert_payload["lgpd_aceito"] is False
    assert insert_payload["tenant_id"] == "tenant-a"
    assert insert_payload["telefone"] == "+5567988880000"
    assert insert_payload["nome"] == "João Silva"


async def test_paciente_novo_nome_vazio_salva_nome_none() -> None:
    core = _mock_core(insert_id="novo-contato-id")
    vertical = _mock_vertical()

    with patch(_PATCH_CORE, return_value=core), patch(_PATCH_VERTICAL, return_value=vertical):
        await handle_consentimento(_mensagem(nome_remetente="   "), _TENANT_A, None)

    insert_payload = core.table.return_value.insert.call_args.args[0]
    assert insert_payload["nome"] is None


# ─── Pendente, mensagem comum (não é botão) ────────────────────────────────────


async def test_pendente_mensagem_de_texto_comum_bloqueia_de_novo() -> None:
    core = _mock_core(select_data=[{"lgpd_aceito": False, "lgpd_revogado": False}])
    vertical = _mock_vertical(
        termo={"id": "t1", "titulo": "Termo de Consentimento", "versao": "1.0"}
    )

    with (
        patch(_PATCH_CORE, return_value=core),
        patch(_PATCH_VERTICAL, return_value=vertical),
        patch("gateway.consent_handler.settings") as cfg,
    ):
        cfg.crm_url = "https://crm.exemplo.com"
        resultado = await handle_consentimento(_mensagem("oi"), _TENANT_A, "contato-existente")

    assert isinstance(resultado, ResultadoConsentimento)
    assert resultado.botoes is not None
    assert "Termo de Consentimento" in resultado.mensagem
    core.table.return_value.update.assert_not_called()


def test_link_de_consentimento_usa_codigo_aleatorio_e_guarda_so_o_hash() -> None:
    """O link nunca leva o id do contato; o banco guarda só o SHA-256 do código."""
    import hashlib

    vertical = MagicMock()
    with patch("gateway.consent_handler.settings") as cfg:
        cfg.crm_url = "https://crm.exemplo.com/"
        link = _link_consentimento(vertical, "tenant-a", "contato-123")

    assert link is not None and link.startswith("https://crm.exemplo.com/lgpd/consentimento/")
    codigo = link.rsplit("/", 1)[1]
    assert "contato-123" not in link and len(codigo) == 43
    linha = vertical.table.return_value.insert.call_args.args[0]
    assert linha["tenant_id"] == "tenant-a" and linha["contato_id"] == "contato-123"
    assert linha["codigo_hash"] == hashlib.sha256(codigo.encode()).hexdigest()
    assert codigo not in str(linha)
    outro = None
    with patch("gateway.consent_handler.settings") as cfg:
        cfg.crm_url = "https://crm.exemplo.com"
        outro = _link_consentimento(vertical, "tenant-a", "contato-123")
    assert outro != link


def test_sem_gravar_o_codigo_o_pedido_segue_sem_link() -> None:
    vertical = MagicMock()
    vertical.table.return_value.insert.return_value.execute.side_effect = RuntimeError("banco")
    with patch("gateway.consent_handler.settings") as cfg:
        cfg.crm_url = "https://crm.exemplo.com"
        assert _link_consentimento(vertical, "tenant-a", "contato-123") is None


# ─── Aceitar via botão ──────────────────────────────────────────────────────────


async def test_clicar_aceitar_marca_lgpd_aceito_e_insere_audit_log() -> None:
    core = _mock_core()
    vertical = _mock_vertical(
        {"id": "termo-1", "titulo": "Termo", "versao": "1", "conteudo_hash": "hash-1"}
    )

    with patch(_PATCH_CORE, return_value=core), patch(_PATCH_VERTICAL, return_value=vertical):
        resultado = await handle_consentimento(
            _mensagem(button_id=_BOTAO_ACEITAR), _TENANT_A, "contato-existente"
        )

    assert isinstance(resultado, ResultadoConsentimento)
    assert resultado.botoes is None
    assert "registrado" in resultado.mensagem.lower()

    update_payload = core.table.return_value.update.call_args.args[0]
    assert update_payload["lgpd_aceito"] is True
    assert update_payload["lgpd_revogado"] is False

    audit_payload = vertical.table.return_value.insert.call_args.args[0]
    assert audit_payload["acao"] == "consentimento_dado"
    assert audit_payload["entidade_id"] == "contato-existente"
    assert audit_payload["realizado_por"] is None


# ─── Recusar via botão ──────────────────────────────────────────────────────────


async def test_clicar_recusar_marca_lgpd_revogado_e_insere_audit_log() -> None:
    core = _mock_core()
    vertical = _mock_vertical()

    with patch(_PATCH_CORE, return_value=core), patch(_PATCH_VERTICAL, return_value=vertical):
        resultado = await handle_consentimento(
            _mensagem(button_id=_BOTAO_NEGAR), _TENANT_A, "contato-existente"
        )

    assert isinstance(resultado, ResultadoConsentimento)
    update_payload = core.table.return_value.update.call_args.args[0]
    assert update_payload["lgpd_aceito"] is False
    assert update_payload["lgpd_revogado"] is True

    audit_payload = vertical.table.return_value.insert.call_args.args[0]
    assert audit_payload["acao"] == "consentimento_negado"


# ─── Já aceito → segue fluxo normal ─────────────────────────────────────────────


async def test_lgpd_aceito_true_e_nao_revogado_retorna_none() -> None:
    core = _mock_core(select_data=[{"lgpd_aceito": True, "lgpd_revogado": False}])
    vertical = _mock_vertical()

    with (
        patch(_PATCH_CORE, return_value=core),
        patch(_PATCH_VERTICAL, return_value=vertical),
        patch("gateway.consent_handler.communication_allowed", return_value=True),
    ):
        resultado = await handle_consentimento(
            _mensagem("Quero agendar"), _TENANT_A, "contato-existente"
        )

    assert resultado is None


# ─── Falha no UPDATE de aceite ──────────────────────────────────────────────────


async def test_falha_no_update_de_aceite_nao_insere_audit_log() -> None:
    core = _mock_core(update_side_effect=Exception("conexão perdida"))
    vertical = _mock_vertical()

    with patch(_PATCH_CORE, return_value=core), patch(_PATCH_VERTICAL, return_value=vertical):
        resultado = await handle_consentimento(
            _mensagem(button_id=_BOTAO_ACEITAR), _TENANT_A, "contato-existente"
        )

    assert isinstance(resultado, ResultadoConsentimento)
    assert resultado.mensagem != ""
    vertical.table.return_value.insert.assert_not_called()


# ─── Isolamento por tenant ───────────────────────────────────────────────────────


async def test_isolamento_por_tenant_mesmo_telefone_estados_diferentes() -> None:
    """contato_id A (tenant A, já aceito) não interfere no contato_id B (tenant B, pendente)."""
    core_a = _mock_core(select_data=[{"lgpd_aceito": True, "lgpd_revogado": False}])
    vertical_a = _mock_vertical()

    with (
        patch(_PATCH_CORE, return_value=core_a),
        patch(_PATCH_VERTICAL, return_value=vertical_a),
        patch("gateway.consent_handler.communication_allowed", return_value=True),
    ):
        resultado_a = await handle_consentimento(_mensagem("oi"), _TENANT_A, "contato-a")

    assert resultado_a is None
    core_b = _mock_core(select_data=[{"lgpd_aceito": False, "lgpd_revogado": False}])
    vertical_b = _mock_vertical()

    with patch(_PATCH_CORE, return_value=core_b), patch(_PATCH_VERTICAL, return_value=vertical_b):
        resultado_b = await handle_consentimento(_mensagem("oi"), _TENANT_B, "contato-b")

    assert isinstance(resultado_b, ResultadoConsentimento)


# ─── Consentimento só por botão (API oficial da Meta) ─────────────────────────


async def test_resposta_texto_1_nao_e_tratada_como_consentimento() -> None:
    """Consentimento só vale pelo botão (button_id). Texto solto "1"/"2" não
    pode ser tomado como aceite — segue para o pedido pendente de novo."""
    core = _mock_core(select_data=[{"lgpd_aceito": False, "lgpd_revogado": False}])
    vertical = _mock_vertical()

    with patch(_PATCH_CORE, return_value=core), patch(_PATCH_VERTICAL, return_value=vertical):
        resultado = await handle_consentimento(_mensagem("1"), _TENANT_A, "contato-existente")

    core.table.return_value.update.assert_not_called()
    assert isinstance(resultado, ResultadoConsentimento)
    assert resultado.botoes is not None
