# CLAUDE.md — Core de Agente de Atendimento WhatsApp (Multi-Tenant)

> Este arquivo é a memória permanente do projeto. Leia-o por completo no início de cada sessão antes de escrever ou alterar qualquer código. NÃO divirja das decisões abaixo sem confirmação explícita do desenvolvedor (Rafael).

---

## 1. O QUE ESTAMOS CONSTRUINDO

Um **core reutilizável** de agente de atendimento por WhatsApp. NÃO é um agente para um negócio específico — é a **fundação** que serve a qualquer vertical (clínica médica, dentista, garagem de carros, farmácia, etc).

Princípio central de design: **separar o genérico do específico.**
- O **core** (genérico) nunca muda.
- Cada **vertical** pluga suas particularidades (estoque, prontuário, agenda) como um conjunto de tools + um system prompt próprio.
- Trocar de vertical = trocar as tools registradas + o prompt. O core permanece intacto.

Objetivo de negócio: produto white-label, multi-tenant, vendável de forma recorrente para múltiplos clientes.

---

## 2. STACK (NÃO SUBSTITUIR SEM CONFIRMAÇÃO)

| Camada | Tecnologia |
|---|---|
| Linguagem | Python 3.11+ |
| Framework de agente | LangGraph |
| LLM | Claude (via langchain-anthropic) |
| Gateway HTTP | FastAPI (async) |
| Banco de dados | Supabase (Postgres + RLS) |
| Canal WhatsApp | **SOMENTE** a API oficial da Meta (WhatsApp Cloud API). Evolution API é proibida (decisão do fundador, 26/09/2026) |
| Observabilidade | Langfuse |
| Deploy | Docker em VPS, deploy **manual** — ver §9 |
| Painel/CRM | Next.js (fase posterior) |

Modelos Claude:
- **Haiku** → classificação de intenção, respostas triviais (economia de tokens)
- **Sonnet** → conversas com tool use, raciocínio, fluxos complexos

---

## 3. DECISÕES DE ARQUITETURA (FECHADAS)

### 3.1 Multi-tenancy
- UMA instância atende vários tenants (negócios). NÃO é um deploy por cliente.
- Isolamento por `tenant_id` via **Row Level Security (RLS)** no Supabase.
- Vazamento de dados entre tenants é catastrófico (LGPD, dados de saúde). RLS é inegociável.

### 3.2 Número de WhatsApp = chave de roteamento de tenant
- O número que RECEBE a mensagem identifica qual tenant está sendo atendido.
- Relação um-para-muitos: um tenant pode ter VÁRIOS números.
- Todo número usa a API oficial da Meta (`provider = 'cloud_api'`, garantido por constraint no banco). Nunca Evolution/Baileys ou APIs não oficiais.

### 3.3 Identidade de contato
- A identidade de um contato é SEMPRE a composição `(tenant_id + telefone)`.
- O telefone sozinho NUNCA é chave única — o mesmo telefone pode ser cliente de tenants diferentes.

### 3.4 Adapter de canal
- O webhook da Cloud API tem formato próprio da Meta.
- Um **adapter** normaliza o payload para um formato interno único (uma classe/modelo `MensagemRecebida`).
- O core nunca sabe qual provider originou a mensagem.

### 3.5 Tool Registry (coração da reutilização)
- O core NÃO conhece nenhum vertical específico.
- Cada vertical REGISTRA suas tools no core.
- Ao iniciar uma conversa, o core carrega: tools genéricas (CRM) + tools do vertical do tenant + system prompt do tenant.
- Carregar só as tools do vertical correto economiza tokens E reduz superfície de ataque.

---

## 4. SEGURANÇA (PRIORIDADE — NÃO É CAMADA "DEPOIS")

1. **Validação de webhook**: validar assinatura HMAC (Cloud API: header `X-Hub-Signature-256`). Rejeitar requisições não assinadas.
2. **Escopo forçado nas tools**: toda tool filtra por `tenant_id` NO CÓDIGO. NUNCA confiar no que o LLM "decidiu". A segurança real está na tool, não no prompt.
3. **Validação de identidade**: confirmar que o telefone do remetente corresponde ao contato/registro sendo consultado, antes de retornar dados sensíveis.
4. **Gestão de segredos**: credenciais de tenant nunca em texto puro no banco nem no código. Credencial do WhatsApp de cada clínica fica no banco **cifrada** (AES-256-GCM, chave `TOKENS_CRYPTO_KEY` só no ambiente; `canais_whatsapp.token_cifrado`, constraint exige `v1:`) — assim uma clínica nova é cadastrada sem criar variável de ambiente. O legado por referência a variável de ambiente (`credenciais_ref`) continua aceito. A tabela é fechada para o navegador.
5. **Dados sensíveis (LGPD)**: campos críticos (ex. diagnóstico, prescrição) cifrados em nível de aplicação.
6. **Audit log imutável**: registrar quem/o quê/quando acessou dados sensíveis e ações do agente.
7. **Rate limiting**: limite de mensagens por contato por minuto (anti-abuso e controle de custo).
8. **Confirmação para ações críticas**: ações irreversíveis (cancelar, apagar) — o agente propõe, confirma com o cliente ou escala para humano. Nunca executa autônomo.
9. **Defesa contra prompt injection**: tratar toda mensagem do cliente como não-confiável. O escopo é garantido pelo código da tool, não pelo texto do prompt.

---

## 5. ECONOMIA DE TOKENS (REQUISITO DE PRODUTO)

1. **Roteamento de modelo**: Haiku para tarefas simples, Sonnet para tool use/raciocínio.
2. **Prompt caching da Anthropic**: cachear a parte estável do prompt (instruções + tools).
3. **Gestão de histórico**: janela deslizante (últimas N mensagens) + sumarização do início quando a conversa cresce.
4. **Tools só do vertical**: nunca carregar tools que não pertencem ao tenant.
5. **System prompt enxuto** e **descrições de tool curtas e claras**.
6. **Limite de iterações** por turno (evitar loops de raciocínio).

Langfuse é a fonte de verdade do custo: monitorar custo por conversa, por tenant, por tipo de mensagem.

---

## 6. CONVENÇÕES DE CÓDIGO

- Python 3.11+, type hints obrigatórios.
- Formatação: `ruff` (lint + format).
- Validação de dados: `pydantic` v2.
- Async em todo I/O (FastAPI, chamadas a banco e APIs externas).
- Variáveis de ambiente via `pydantic-settings` (arquivo `.env`, NUNCA commitado).
- Nomes de domínio em português (tenant, contato, conversa) — consistente com o negócio.
- Cada tool: docstring curta e clara (vai para o contexto do LLM), assinatura tipada, filtro de `tenant_id` interno.
- Testes com `pytest`. Toda tool e todo nó do grafo testável isoladamente.
- Segredos e `.env` no `.gitignore` desde o primeiro commit.

---

## 7. ESTRUTURA DE PASTAS (ALVO)

```
.
├── CLAUDE.md
├── ROADMAP.md
├── .env.example
├── .gitignore
├── pyproject.toml
├── app/
│   ├── main.py                 # FastAPI app
│   ├── config.py               # settings (pydantic-settings)
│   ├── gateway/                # webhooks + resolução de tenant
│   │   ├── webhook.py
│   │   └── tenant_resolver.py
│   ├── channels/               # adapter de canal
│   │   ├── base.py             # interface + modelo MensagemRecebida
│   │   └── cloud_api.py
│   ├── core/                   # o agente (LangGraph)
│   │   ├── state.py            # State do grafo
│   │   ├── graph.py            # construção do grafo
│   │   ├── nodes.py            # nós
│   │   └── registry.py         # Tool Registry
│   ├── tools/
│   │   ├── crm/                # tools genéricas
│   │   └── verticals/          # tools por vertical (clinica, garagem...)
│   ├── db/                     # acesso a Supabase
│   ├── security/               # validação webhook, audit, rate limit
│   └── observability/          # integração Langfuse
├── supabase/schema.sql         # esquema do banco (Supabase)
└── tests/
```

---

## 8. COMO TRABALHAR NESTE PROJETO

- Construir **uma fase por vez** (ver ROADMAP.md). Não pular fases.
- Ao final de cada fase: rodar, testar e documentar o que foi feito e por quê.
- Preferir entregas completas e funcionais a fragmentos.
- Toda decisão de código precisa de justificativa registrada (no PR ou em `docs/`). Mudança sem o "porquê" não entra.
- Antes de qualquer ação destrutiva ou irreversível, pedir confirmação.

---

## 9. DEPLOY (MANUAL)

- O motor roda em Docker numa VPS. O painel CRM (Next.js) é outro repositório e outro deploy.
- **Merge na `main` NÃO publica.** O deploy é manual. Nunca dizer que um merge "já está em produção".
- Mudanças de banco são aplicadas à mão (SQL Editor do Supabase). Quando um PR muda o banco, dizer a ordem entre SQL e deploy, e se a ordem importa.
