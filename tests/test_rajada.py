"""Buffer de rajada (gateway/rajada.py) e processamento em lote
(gateway/idempotent_processing.py): o agente espera o paciente parar de digitar,
responde uma vez e nunca roda duas vezes ao mesmo tempo na mesma conversa."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from channels.base import MensagemRecebida, ProviderWhatsApp, TipoMensagem
from core import pipeline
from gateway import rajada
from gateway.idempotent_processing import (
    agrupar_lote,
    combinar_mensagens,
    processar_lote_idempotente,
)
from gateway.tenant_resolver import TenantInfo

_TENANT = TenantInfo("tenant-1", "clinica", "canal-1", "cloud_api", "ref")


def _msg(
    mid: str,
    texto: str | None = "oi",
    tipo: TipoMensagem = TipoMensagem.texto,
    de: str = "+5567988880000",
    **metadata,
) -> MensagemRecebida:
    return MensagemRecebida(
        message_id=mid,
        numero_destino="+5511888880000",
        numero_remetente=de,
        tipo=tipo,
        texto=texto,
        provider=ProviderWhatsApp.cloud_api,
        timestamp=datetime.now(timezone.utc),
        metadata=metadata,
    )


def _item(mid: str, **kw) -> rajada.ItemRajada:
    return rajada.ItemRajada(
        event_id=mid, mensagem=_msg(mid, **kw), tenant=_TENANT, sender=MagicMock(), provider="p"
    )


@pytest.fixture
def janela(monkeypatch):
    monkeypatch.setattr(settings, "debounce_mensagens_segundos", 0.05)
    monkeypatch.setattr(settings, "debounce_maximo_segundos", 0.5)


# ─── Buffer ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rajada_vira_um_lote_so(janela) -> None:
    lotes: list[list[str]] = []

    async def processar(lote):
        lotes.append([i.event_id for i in lote])

    chave = rajada.adicionar(_item("m1"))
    assert chave is not None
    dono = asyncio.create_task(rajada.drenar(chave, processar))
    await asyncio.sleep(0.02)
    assert rajada.adicionar(_item("m2")) is None  # entra no lote do dono
    await asyncio.sleep(0.02)
    assert rajada.adicionar(_item("m3")) is None
    await dono

    assert lotes == [["m1", "m2", "m3"]]
    assert rajada._filas == {}


@pytest.mark.asyncio
async def test_mensagem_durante_a_resposta_vira_proximo_lote_sem_paralelo(janela) -> None:
    lotes: list[list[str]] = []
    rodando = 0
    pico = 0

    async def processar(lote):
        nonlocal rodando, pico
        rodando += 1
        pico = max(pico, rodando)
        lotes.append([i.event_id for i in lote])
        if len(lotes) == 1:
            # Paciente escreve de novo enquanto o agente ainda responde.
            assert rajada.adicionar(_item("m2")) is None
        await asyncio.sleep(0.01)
        rodando -= 1

    chave = rajada.adicionar(_item("m1"))
    await rajada.drenar(chave, processar)

    assert lotes == [["m1"], ["m2"]]
    assert pico == 1


@pytest.mark.asyncio
async def test_teto_responde_mesmo_se_o_paciente_nao_para(monkeypatch) -> None:
    monkeypatch.setattr(settings, "debounce_mensagens_segundos", 0.05)
    monkeypatch.setattr(settings, "debounce_maximo_segundos", 0.12)
    lotes: list[list[str]] = []

    async def processar(lote):
        lotes.append([i.event_id for i in lote])

    donos = [asyncio.create_task(rajada.drenar(rajada.adicionar(_item("m0")), processar))]
    for n in range(1, 8):  # uma mensagem a cada 30 ms, sem nunca parar 50 ms
        await asyncio.sleep(0.03)
        chave = rajada.adicionar(_item(f"m{n}"))
        if chave:  # como o webhook: fila nova → agenda um dono
            donos.append(asyncio.create_task(rajada.drenar(chave, processar)))
    await asyncio.gather(*donos)

    assert len(lotes) >= 2  # o teto cortou a rajada
    assert [e for lote in lotes for e in lote] == [f"m{n}" for n in range(8)]


@pytest.mark.asyncio
async def test_conversas_diferentes_tem_filas_diferentes() -> None:
    assert rajada.adicionar(_item("a", de="+5567911110000")) is not None
    assert rajada.adicionar(_item("b", de="+5567922220000")) is not None
    processar = AsyncMock()
    await rajada.drenar_varias(list(rajada._filas), processar)
    assert processar.await_count == 2


@pytest.mark.asyncio
async def test_erro_no_lote_nao_deixa_fila_orfa() -> None:
    chave = rajada.adicionar(_item("m1"))
    await rajada.drenar(chave, AsyncMock(side_effect=RuntimeError("boom")))
    assert rajada._filas == {}
    # A próxima mensagem da conversa ganha um dono novo.
    assert rajada.adicionar(_item("m2")) is not None


@pytest.mark.asyncio
async def test_fila_sem_dono_ha_muito_tempo_ganha_dono_novo(monkeypatch) -> None:
    chave = rajada.adicionar(_item("m1"))  # a tarefa de fundo nunca rodou
    assert rajada.adicionar(_item("m2")) is None
    monkeypatch.setattr(rajada, "_ORFA_APOS_SEGUNDOS", 0.0)
    await asyncio.sleep(0.001)
    assert rajada.adicionar(_item("m3")) == chave
    processar = AsyncMock()
    await rajada.drenar(chave, processar)
    assert [i.event_id for i in processar.await_args.args[0]] == ["m1", "m2", "m3"]


@pytest.mark.asyncio
async def test_segundo_dono_nao_drena_em_paralelo() -> None:
    chave = rajada.adicionar(_item("m1"))
    liberar = asyncio.Event()
    chamadas = 0

    async def processar(_lote):
        nonlocal chamadas
        chamadas += 1
        await liberar.wait()

    dono = asyncio.create_task(rajada.drenar(chave, processar))
    await asyncio.sleep(0)
    await rajada.drenar(chave, processar)  # retorna na hora: já tem dono
    liberar.set()
    await dono
    assert chamadas == 1


# ─── Agrupamento ─────────────────────────────────────────────────────────────


def test_textos_seguidos_juntam_e_o_resto_fica_sozinho() -> None:
    itens = [
        _item("t1"),
        _item("t2"),
        _item("img", texto=None, tipo=TipoMensagem.imagem, media_id="x"),
        _item("t3"),
        _item("btn", texto="Aceito", tipo=TipoMensagem.botao_resposta, button_id="lgpd_aceitar"),
        _item("aud", texto=None, tipo=TipoMensagem.audio, media_id="y"),
        _item("t4"),
        _item("t5"),
    ]
    grupos = [[i.event_id for i in g] for g in agrupar_lote(itens)]
    assert grupos == [["t1", "t2"], ["img"], ["t3"], ["btn"], ["aud"], ["t4", "t5"]]


def test_combinar_junta_textos_e_guarda_os_originais() -> None:
    grupo = [_item("t1", texto="oi"), _item("t2", texto="quero marcar")]
    m = combinar_mensagens(grupo)
    assert m.texto == "oi\nquero marcar"
    assert m.message_id == "t2"
    assert m.metadata["agrupadas"] == [
        {"message_id": "t1", "texto": "oi"},
        {"message_id": "t2", "texto": "quero marcar"},
    ]


def test_combinar_um_so_devolve_a_propria_mensagem() -> None:
    item = _item("t1")
    assert combinar_mensagens([item]) is item.mensagem


@pytest.mark.asyncio
async def test_lote_roda_o_agente_uma_vez_e_conclui_todos_os_eventos() -> None:
    db = MagicMock()
    with (
        patch("gateway.idempotent_processing.get_supabase", return_value=db),
        patch(
            "gateway.idempotent_processing.processar_mensagem_entrada", new=AsyncMock()
        ) as agente,
        patch("gateway.idempotent_processing.finish_event") as finish,
    ):
        await processar_lote_idempotente([_item("t1"), _item("t2"), _item("t3")])

    agente.assert_awaited_once()
    assert agente.await_args.args[0].texto == "oi\noi\noi"
    assert [c.kwargs["event_id"] for c in finish.call_args_list] == ["t1", "t2", "t3"]
    assert all(c.kwargs["success"] is True for c in finish.call_args_list)


@pytest.mark.asyncio
async def test_falha_num_grupo_nao_impede_o_proximo() -> None:
    with (
        patch("gateway.idempotent_processing.get_supabase", return_value=MagicMock()),
        patch(
            "gateway.idempotent_processing.processar_mensagem_entrada",
            new=AsyncMock(side_effect=[RuntimeError("x"), None]),
        ),
        patch("gateway.idempotent_processing.finish_event") as finish,
    ):
        await processar_lote_idempotente(
            [_item("t1"), _item("img", texto=None, tipo=TipoMensagem.imagem, media_id="m")]
        )

    resultado = {c.kwargs["event_id"]: c.kwargs["success"] for c in finish.call_args_list}
    assert resultado == {"t1": False, "img": True}


# ─── Histórico do CRM continua mensagem a mensagem ───────────────────────────


def test_mensagens_agrupadas_sao_gravadas_uma_a_uma() -> None:
    m = combinar_mensagens([_item("t1", texto="oi"), _item("t2", texto="quero marcar")])
    with patch("core.pipeline.salvar_mensagem_db") as salvar:
        pipeline._salvar_mensagens_paciente(MagicMock(), "tenant-1", "conv-1", m)
    assert [(c.args[4], c.args[5]) for c in salvar.call_args_list] == [
        ("oi", {"whatsapp_message_id": "t1"}),
        ("quero marcar", {"whatsapp_message_id": "t2"}),
    ]


def test_mensagem_avulsa_gravada_como_antes() -> None:
    with patch("core.pipeline.salvar_mensagem_db") as salvar:
        pipeline._salvar_mensagens_paciente(MagicMock(), "tenant-1", "conv-1", _msg("x"))
    salvar.assert_called_once()
    assert salvar.call_args.args[3:] == ("paciente", "oi", {"whatsapp_message_id": "x"})
