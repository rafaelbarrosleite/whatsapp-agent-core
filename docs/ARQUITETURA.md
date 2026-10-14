# Arquitetura e decisões

Cada seção diz **o que** foi decidido e **por quê**: o trade-off que levou à escolha.

## 1. Uma instância, vários tenants

**Decisão:** um deploy atende todas as empresas. O número de WhatsApp que recebeu a mensagem diz qual tenant está sendo atendido (`canais_whatsapp.numero → tenant_id`). Um tenant pode ter vários números.

**Por quê:** custo e operação. Um deploy por cliente multiplica servidor, atualização e monitoramento. O preço é o risco de vazamento entre tenants, tratado em três camadas:

1. **Banco:** RLS ligado em todas as tabelas e nenhuma policy para o navegador. Só o backend (`service_role`) lê e escreve. O teste `supabase/tests/schema.sql` falha se alguma tabela nascer sem RLS ou com permissão para `anon`.
2. **Código:** toda tool recebe o `tenant_id` por closure, na hora de ser criada para aquele tenant. O LLM nunca vê nem escolhe o tenant (há teste conferindo que `tenant_id` não aparece no schema das tools).
3. **Identidade:** contato é `(tenant_id, telefone)`. O mesmo telefone em duas clínicas são dois contatos, e a memória da conversa usa essa dupla como `thread_id`.

## 2. O core não conhece o segmento (Tool Registry)

**Decisão:** `core/registry.py` guarda tools genéricas (CRM) e tools por vertical. A cada turno, o agente recebe as genéricas + as do vertical do tenant + o prompt do tenant. O que é de clínica (agenda, base de conhecimento, transferência, pedido de agendamento) vive em `tools/verticals/clinica/`.

**Por quê:** o produto é para vários segmentos. Uma oficina mecânica ou uma farmácia entram registrando tools novas, sem tocar no grafo. Carregar só as tools do vertical também economiza tokens e reduz o que um prompt injection consegue pedir.

Dentro do vertical, tools entram por **recurso ligado**: a agenda só aparece com `agenda.habilitada`, a transferência só com `handoff.habilitado`. Instruir o modelo a usar uma tool que não está no turno faz ele inventar a ação; por isso o prompt muda junto com as tools (`core/nodes.py`).

## 3. Segurança na tool e no código, não no prompt

**Decisão:** toda mensagem do cliente é tratada como não confiável. O que precisa ser garantido está em código determinístico:

- **Guardrail de entrada** (`security/guardrail_entrada.py`): sinais de urgência médica ou de crise geram uma resposta fixa (SAMU 192 / CVV 188) e avisam a equipe, **antes** do LLM. Frases legítimas ("meu pai teve AVC ano passado") não disparam; a bateria de casos está em `tests/test_guardrail_entrada.py`.
- **Aderência na saída** (`security/aderencia.py`): número na resposta (preço, horário, prazo) precisa aparecer em alguma fonte (base de conhecimento, resultado de tool, prompt da clínica ou mensagem do paciente). Sem fonte, a resposta é retida e o paciente recebe uma confirmação segura. Isso ataca a alucinação mais cara em atendimento: o preço inventado.
- **Webhook:** assinatura HMAC `X-Hub-Signature-256` obrigatória; sem assinatura válida, 401.
- **Idempotência:** a Meta reenvia eventos. `claim_idempotency_event` no banco garante que cada mensagem é processada uma vez, mesmo com mais de um worker.
- **Segredos:** o token do WhatsApp de cada clínica fica no banco cifrado com AES-256-GCM (`security/cifra_tokens.py`, formato `v1:`, garantido por constraint). A chave fica só no ambiente. Assim uma clínica nova é cadastrada sem criar variável de ambiente.

**Por quê:** prompt é sugestão; código é garantia. Regra determinística é auditável, testável e não muda quando o prompt muda. Os dois guardrails vieram do Tech Challenge da pós em IA (FIAP), adaptados para conversa de WhatsApp.

## 4. Canal: só a API oficial da Meta, atrás de um adapter

**Decisão:** o webhook da Cloud API é normalizado em `MensagemRecebida` (`channels/base.py`). O core nunca vê o formato da Meta. O banco só aceita `provider = 'cloud_api'`.

**Por quê:** APIs não oficiais de WhatsApp custam menos no começo, mas trazem risco de banimento do número, que é o canal de venda do cliente. O adapter mantém a porta aberta para outro canal (site, Instagram) sem mexer no agente.

## 5. Economia de tokens como requisito

- **Prompt caching:** instruções e tools ficam num bloco estável com `cache_control`; o histórico e os dados que mudam a cada turno ficam fora, para não invalidar o cache (`core/nodes.py`, testado em `tests/test_nodes_cache_historico.py`).
- **Janela de histórico** e **limite de iterações** por turno (`RECURSION_LIMIT`).
- **Modelo por clínica:** Sonnet por padrão para conversa com tools; Haiku configurável e usado nas tarefas de extração (análise de conversas com saída estruturada).
- **Rajada:** quem manda três mensagens seguidas recebe uma resposta só (`gateway/rajada.py`): menos chamadas e uma resposta mais coerente.
- **Medição:** cada chamada grava tokens de entrada, saída e cache em `uso_ia`, por clínica e por uso. É o que permite precificar o plano.

## 6. LGPD

- Consentimento antes da conversa, com termo versionado e hash do texto aceito (`gateway/consent_handler.py`).
- `audit_log` e `lgpd_audit_log` são somente-inserção: trigger bloqueia update e delete.
- Logs em JSON com `correlation_id`; telefone, e-mail e documentos mascarados antes de qualquer log (`security/anonimizacao.py`).
- Erros gravados para o painel levam texto fixo do código, nunca conteúdo da mensagem.

## 7. Humano no circuito

O agente propõe e o humano decide nas ações que importam:
- **Transferência** para atendente com fila round-robin; enquanto um humano atende, o agente fica pausado na conversa.
- **Coexistência:** se a clínica responde pelo próprio celular, o agente percebe e pausa.
- **Pedido de agendamento:** em vez de marcar, o agente coleta os dados e a secretária confirma no painel.

## 8. Fuso por clínica

O horário da consulta é sempre interpretado no fuso da clínica (`tenants.config.fuso_utc_horas`, `core/fuso.py`). Deslocamento fixo em horas basta porque o Brasil não tem horário de verão desde 2019, e não depende da base de fusos do sistema operacional.

## O que eu faria diferente / próximos passos

- **Avaliação de LLM no CI:** bateria de conversas com respostas esperadas, rodando a cada PR que mexe em prompt ou tool.
- **RAG com embeddings:** a base de conhecimento hoje é busca por palavras-chave com prioridade, suficiente para dezenas de blocos por clínica. Com bases maiores, pgvector no mesmo Postgres.
- **Traces no Langfuse:** as chaves já estão nas settings; o custo hoje sai de `uso_ia`.
