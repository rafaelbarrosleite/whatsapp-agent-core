# ROADMAP.md — Fases de Construção do Core

> Plano original de construção, mantido como registro. Todas as fases foram entregues; o motor está em produção atendendo uma clínica.

> Construir em ordem. Cada fase entrega algo funcional e testável. Não avançar sem a fase anterior rodando. Ao fim de cada fase, documentar o que foi feito e por quê.

---

## FASE 0 — Fundação do projeto
**Objetivo:** esqueleto do projeto rodando, sem lógica de negócio ainda.

- [x] Inicializar projeto Python com `pyproject.toml` (gerenciador: `uv` ou `poetry`)
- [x] Dependências base: `fastapi`, `uvicorn`, `langgraph`, `langchain-anthropic`, `pydantic`, `pydantic-settings`, `supabase`, `langfuse`, `pytest`, `ruff`
- [x] `.gitignore` (incluir `.env`, `__pycache__`, `.venv`)
- [x] `.env.example` com as chaves necessárias (sem valores reais)
- [x] `config.py` carregando settings via `pydantic-settings`
- [x] `app/main.py` com FastAPI e uma rota `/health` retornando `{"status": "ok"}`
- [x] Rodar localmente: `uvicorn app.main:app --reload` e acessar `/health`

**Critério de pronto:** `/health` responde no navegador.

---

## FASE 1 — Schema multi-tenant no Supabase
**Objetivo:** a fundação de dados com isolamento por tenant.

- [x] Tabela `tenants` (id, nome, vertical, system_prompt, config, ativo)
- [x] Tabela `canais_whatsapp` (id, tenant_id, numero, provider, credenciais_ref, rotulo, ativo)
- [x] Tabela `contatos` (id, tenant_id, telefone, nome, ...) — única por `(tenant_id, telefone)`
- [x] Tabela `conversas` (id, tenant_id, contato_id, estado, updated_at)
- [x] Tabela `mensagens` (id, conversa_id, papel, conteudo, created_at)
- [x] Tabela `audit_log` (id, tenant_id, ator, acao, detalhe, created_at) — imutável
- [x] Habilitar **RLS** em todas as tabelas com `tenant_id`
- [x] Esquema versionado (hoje consolidado em `supabase/schema.sql`)

**Critério de pronto:** schema aplicado no Supabase, RLS ativo, inserção de teste isolada por tenant.

---

## FASE 2 — State + Tool Registry (núcleo do core)
**Objetivo:** a espinha dorsal de que tudo depende.

- [x] `core/state.py` — TypedDict do State (messages com add_messages, tenant_id, contato_id, intencao, etc)
- [x] `core/registry.py` — Tool Registry: registrar tools genéricas e por vertical; resolver o conjunto de tools de um tenant
- [x] `tools/crm/` — primeiras tools genéricas: `buscar_contato`, `criar_contato` (com filtro de tenant_id forçado)
- [x] Testes unitários do registry e das tools

**Critério de pronto:** registry devolve o conjunto certo de tools para um tenant fictício; tools testadas isoladamente.

---

## FASE 3 — Grafo do agente (LangGraph)
**Objetivo:** o agente raciocina e usa tools.

- [x] `core/nodes.py` — nós: identificar_contato, entender_intencao, agente (ReAct com tools), escalar_humano
- [x] `core/graph.py` — montagem do grafo com arestas condicionais
- [x] Checkpointer (MemorySaver para dev; PostgresSaver depois) usando telefone como thread_id
- [x] Roteamento de modelo (Haiku x Sonnet)
- [x] Teste: simular uma conversa de ponta a ponta sem WhatsApp real

**Critério de pronto:** uma mensagem de texto entra e o agente responde usando ao menos uma tool, com memória entre turnos.

---

## FASE 4 — Adapter de canal + Gateway
**Objetivo:** receber WhatsApp de verdade.

- [x] `channels/base.py` — modelo `MensagemRecebida` e interface do adapter
- [x] `channels/cloud_api.py` — normalização do webhook da API oficial da Meta (Evolution removida em 26/09/2026)
- [x] `gateway/webhook.py` — endpoint que recebe, valida assinatura, normaliza
- [x] `gateway/tenant_resolver.py` — número recebido → tenant_id
- [x] `security/` — validação de assinatura HMAC, rate limiting
- [x] Conectar o gateway ao grafo da Fase 3

**Critério de pronto:** mensagem real (ou simulada no formato do provider) percorre adapter → resolver → core → resposta.

---

## FASE 5 — Observabilidade + primeiro vertical
**Objetivo:** ver custos e provar a reutilização.

- [x] Integrar Langfuse (callback no grafo)
- [x] Implementar UM vertical completo (ex: clínica) como conjunto de tools plugável
- [x] Validar que trocar o tenant troca tools + prompt sem tocar no core

**Critério de pronto:** dois tenants de verticais diferentes atendidos pela mesma instância; custos visíveis no Langfuse.

---

## FASE 6 — Deploy + CRM (Next.js)
**Objetivo:** produção e painel.

- [x] Deploy em VPS com Docker (app + variáveis de ambiente)
- [x] Webhook público configurado no provider
- [x] Início do painel Next.js multi-tenant (dashboard, contatos, conversas)

**Critério de pronto:** core em produção recebendo mensagens; painel lendo dados por tenant.

---

## Ordem de construção
Fase 0 → 1 → 2 → 3 são o núcleo (FastAPI, dados, LangGraph). Fases 4-6 conectam ao mundo real.
