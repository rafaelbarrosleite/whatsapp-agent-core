from datetime import timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from core.fuso import (
    FUSO_PADRAO_HORAS,
    descrever_fuso,
    fuso_da_config,
    fuso_do_tenant,
    offset_horas,
)
from db.registro_consulta import registrar_consulta


@pytest.mark.parametrize(
    ("config", "esperado"),
    [
        ({"fuso_utc_horas": -4}, -4),
        ({"fuso_utc_horas": -5}, -5),
        # Legado: clínica cadastrada antes do campo próprio continua com seu fuso.
        ({"lembrete": {"utc_offset_horas": -4}}, -4),
        # O campo novo vence o legado.
        ({"fuso_utc_horas": -3, "lembrete": {"utc_offset_horas": -4}}, -3),
        # Sem fuso, ou com valor inválido, cai em Brasília.
        ({}, FUSO_PADRAO_HORAS),
        (None, FUSO_PADRAO_HORAS),
        ({"fuso_utc_horas": "-4"}, FUSO_PADRAO_HORAS),
        ({"fuso_utc_horas": True}, FUSO_PADRAO_HORAS),
        ({"fuso_utc_horas": 99}, FUSO_PADRAO_HORAS),
    ],
)
def test_offset_horas(config, esperado):
    assert offset_horas(config) == esperado


def test_fuso_da_config_e_descricao():
    fuso = fuso_da_config({"fuso_utc_horas": -4})
    assert fuso.utcoffset(None) == timedelta(hours=-4)
    assert descrever_fuso(fuso) == "UTC-4"


def test_fuso_do_tenant_le_config_da_clinica():
    core = MagicMock()
    (
        core.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data
    ) = [{"config": {"fuso_utc_horas": -4}}]
    with patch("core.fuso.get_supabase", return_value=core):
        assert fuso_do_tenant("t-1").utcoffset(None) == timedelta(hours=-4)
    core.table.return_value.select.return_value.eq.assert_called_with("id", "t-1")


def test_fuso_do_tenant_falha_de_leitura_usa_padrao():
    with patch("core.fuso.get_supabase", side_effect=RuntimeError("sem banco")):
        assert fuso_do_tenant("t-1").utcoffset(None) == timedelta(hours=FUSO_PADRAO_HORAS)


def test_registrar_consulta_grava_no_fuso_da_clinica():
    vertical = MagicMock()
    vertical.table.return_value.insert.return_value.execute.return_value.data = [{"id": "ag-1"}]
    with (
        patch("db.registro_consulta.promover_para_paciente"),
        patch("db.registro_consulta._mover_estagio"),
    ):
        registrar_consulta(
            MagicMock(),
            vertical,
            tenant_id="t-1",
            contato_id="c-1",
            data="2026-10-30",
            hora="14:00",
            fuso=timezone(timedelta(hours=-3)),
        )
    payload = vertical.table.return_value.insert.call_args.args[0]
    assert payload["data_hora"] == "2026-10-30T14:00:00-03:00"
