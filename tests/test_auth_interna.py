"""Rotas internas do motor: chave em tempo constante e falha fechado."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app

cliente = TestClient(app)


def test_rota_admin_nao_abre_sem_segredo_configurado():
    # Antes: sem WHATSAPP_VERIFY_TOKEN e sem header, None != None era False e passava.
    with patch("app.main.settings") as cfg:
        cfg.internal_api_key = None
        cfg.whatsapp_verify_token = None
        assert cliente.get("/metrics").status_code == 401


def test_rota_admin_exige_a_chave_interna():
    with patch("app.main.settings") as cfg:
        cfg.internal_api_key = "chave-interna-longa"
        cfg.whatsapp_verify_token = "token-da-meta"
        assert cliente.get("/metrics").status_code == 401
        # O token da Meta não serve como senha de admin.
        r = cliente.get("/metrics", headers={"x-admin-token": "token-da-meta"})
        assert r.status_code == 401
        r = cliente.get("/metrics", headers={"Authorization": "Bearer chave-interna-longa"})
        assert r.status_code == 200


def test_config_interna_recusa_chave_errada_e_sem_chave():
    corpo = {"chave": "agenda", "valor": True, "tenant_id": "t1"}
    with patch("app.config_router.settings") as cfg:
        cfg.internal_api_key = "chave-interna-longa"
        assert cliente.patch("/api/tenant/config", json=corpo).status_code == 401
        r = cliente.patch(
            "/api/tenant/config", json=corpo, headers={"Authorization": "Bearer chave-errada"}
        )
        assert r.status_code == 401
        cfg.internal_api_key = None
        r = cliente.patch("/api/tenant/config", json=corpo, headers={"Authorization": "Bearer "})
        assert r.status_code == 401


def test_verificacao_do_webhook_nao_abre_sem_token_configurado():
    with patch("app.webhook_interno.settings") as cfg:
        cfg.whatsapp_verify_token = None
        r = cliente.get("/webhook/interno", params={"hub.mode": "subscribe", "hub.challenge": "x"})
        assert r.status_code == 403
