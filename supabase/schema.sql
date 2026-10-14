-- supabase/schema.sql — esquema do banco do motor (Supabase / Postgres 15+).
--
-- Gerado com pg_dump a partir das migrations do sistema em produção, só com as
-- tabelas que o motor usa. Sem dados. Rode uma vez num projeto Supabase novo
-- (SQL Editor) ou num Postgres local com supabase/tests/supabase_stub.sql antes.
--
-- Decisões que este arquivo mostra:
--   - Multi-tenant: toda tabela de negócio tem tenant_id; o contato é único por
--     (tenant_id, telefone), nunca só pelo telefone.
--   - RLS ligado em TODAS as tabelas e nenhuma policy para o navegador (anon):
--     só o backend (service_role) lê e escreve. O painel só lê o próprio perfil.
--   - canais_whatsapp só aceita provider = 'cloud_api' (API oficial da Meta) e
--     token cifrado no formato 'v1:...' (AES-256-GCM, chave fora do banco).
--   - audit_log e lgpd_audit_log são somente-inserção (trigger bloqueia
--     update/delete).
--   - Idempotência de webhook (claim/finish_idempotency_event) e criação de
--     conversa atômica (obter_ou_criar_conversa) ficam no banco.

-- Funções vêm antes das tabelas que elas usam (ordem do pg_dump).
SET check_function_bodies = false;


CREATE FUNCTION public.atualizar_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE FUNCTION public.audit_log_imutavel() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    RAISE EXCEPTION 'audit_log é imutável: operação % não é permitida', TG_OP;
END;
$$;

CREATE FUNCTION public.claim_idempotency_event(p_provider text, p_external_event_id text, p_tenant_id uuid, p_payload_hash text, p_stale_after_seconds integer DEFAULT 300) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
declare
    claimed boolean := false;
begin
    if nullif(trim(p_external_event_id), '') is null then
        raise exception 'external_event_id is required';
    end if;

    insert into public.idempotency_events (
        provider, external_event_id, tenant_id, payload_hash
    ) values (
        p_provider, p_external_event_id, p_tenant_id, p_payload_hash
    )
    on conflict (provider, external_event_id) do update
       set status = 'processing',
           attempts = public.idempotency_events.attempts + 1,
           locked_at = now(),
           last_error = null
     where public.idempotency_events.tenant_id = excluded.tenant_id
       and public.idempotency_events.payload_hash = excluded.payload_hash
       and (
         public.idempotency_events.status = 'failed'
         or (
           public.idempotency_events.status = 'processing'
           and public.idempotency_events.locked_at
               < now() - make_interval(secs => greatest(p_stale_after_seconds, 30))
         )
       );

    get diagnostics claimed = row_count;
    return claimed;
end;
$$;

CREATE FUNCTION public.finish_idempotency_event(p_provider text, p_external_event_id text, p_success boolean, p_result jsonb DEFAULT '{}'::jsonb, p_error text DEFAULT NULL::text) RETURNS void
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    update public.idempotency_events
       set status = case when p_success then 'completed' else 'failed' end,
           completed_at = case when p_success then now() else null end,
           result = coalesce(p_result, '{}'::jsonb),
           last_error = case when p_success then null else left(p_error, 2000) end
     where provider = p_provider
       and external_event_id = p_external_event_id
       and status = 'processing'
$$;

CREATE FUNCTION public.notify_lead_transferido() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
begin
  if new.atribuido_a is not null
     and new.atribuido_a is distinct from old.atribuido_a then
    insert into public.crm_notificacoes (tenant_id, perfil_id, tipo, titulo, contato_id)
    values (new.tenant_id, new.atribuido_a, 'lead_transferido',
            'Lead transferido: ' || coalesce(new.nome, new.telefone, 'sem nome'),
            new.contato_id);
  end if;
  return new;
end;
$$;

CREATE FUNCTION public.obter_ou_criar_conversa(p_tenant_id uuid, p_contato_id uuid, p_canal_id uuid DEFAULT NULL::uuid) RETURNS uuid
    LANGUAGE plpgsql
    AS $$
DECLARE
  v_id UUID;
BEGIN
  INSERT INTO public.conversas (tenant_id, contato_id, estado, agente_ativo, canal_id)
  VALUES (p_tenant_id, p_contato_id, 'aberta', TRUE, p_canal_id)
  ON CONFLICT (tenant_id, contato_id) WHERE estado IN ('aberta','aguardando_humano')
  DO UPDATE SET updated_at = NOW()
  RETURNING id INTO v_id;

  RETURN v_id;
END;
$$;

CREATE FUNCTION public.podar_memoria_agente(manter integer DEFAULT 2) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
declare
  n_checkpoints integer;
  n_escritas integer;
  n_blobs integer;
begin
  if manter < 1 then
    raise exception 'manter precisa ser pelo menos 1';
  end if;

  -- Pode rodar mais de uma vez na mesma transação.
  drop table if exists pg_temp._mantidos, pg_temp._limite, pg_temp._versoes;

  create temporary table _mantidos on commit drop as
  select thread_id, checkpoint_ns, checkpoint_id, checkpoint
  from (
    select c.thread_id, c.checkpoint_ns, c.checkpoint_id, c.checkpoint,
           row_number() over (partition by c.thread_id, c.checkpoint_ns order by c.checkpoint_id desc) as posicao
    from public.checkpoints c
  ) x
  where posicao <= manter;

  create temporary table _limite on commit drop as
  select thread_id, checkpoint_ns, min(checkpoint_id) as menor_mantido
  from pg_temp._mantidos
  group by thread_id, checkpoint_ns;

  create temporary table _versoes on commit drop as
  select m.thread_id, m.checkpoint_ns, v.key as channel, v.value as version
  from pg_temp._mantidos m, jsonb_each_text(m.checkpoint -> 'channel_versions') v;

  delete from public.checkpoint_writes w
  using pg_temp._limite l
  where w.thread_id = l.thread_id and w.checkpoint_ns = l.checkpoint_ns
    and w.checkpoint_id < l.menor_mantido;
  get diagnostics n_escritas = row_count;

  delete from public.checkpoints c
  using pg_temp._limite l
  where c.thread_id = l.thread_id and c.checkpoint_ns = l.checkpoint_ns
    and c.checkpoint_id < l.menor_mantido;
  get diagnostics n_checkpoints = row_count;

  delete from public.checkpoint_blobs b
  where exists (
          select 1 from pg_temp._versoes v
          where v.thread_id = b.thread_id and v.checkpoint_ns = b.checkpoint_ns
            and v.channel = b.channel and v.version > b.version)
    and not exists (
          select 1 from pg_temp._versoes v
          where v.thread_id = b.thread_id and v.checkpoint_ns = b.checkpoint_ns
            and v.channel = b.channel and v.version = b.version);
  get diagnostics n_blobs = row_count;

  return jsonb_build_object('checkpoints', n_checkpoints, 'escritas', n_escritas, 'blobs', n_blobs);
end;
$$;

CREATE FUNCTION public.registrar_historico_estagio_lead() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO public.leads_historico_estagio
            (lead_id, tenant_id, estagio_anterior, estagio_novo)
        VALUES (NEW.id, NEW.tenant_id, NULL, NEW.estagio);
    ELSIF NEW.estagio IS DISTINCT FROM OLD.estagio THEN
        INSERT INTO public.leads_historico_estagio
            (lead_id, tenant_id, estagio_anterior, estagio_novo)
        VALUES (NEW.id, NEW.tenant_id, OLD.estagio, NEW.estagio);
    END IF;
    RETURN NULL;  -- AFTER trigger: retorno é ignorado
END;
$$;

CREATE FUNCTION public.registrar_preferencia_lgpd(p_tenant_id uuid, p_contato_id uuid, p_canal text, p_finalidade text, p_decisao text, p_termo_id uuid, p_termo_versao text, p_termo_hash text, p_origem text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
begin
  if p_decisao not in ('aceito', 'recusado', 'revogado') then
    raise exception 'invalid consent decision';
  end if;

  if p_termo_id is not null and not exists (
    select 1
    from public.lgpd_termos t
    where t.id = p_termo_id
      and t.tenant_id = p_tenant_id
      and t.versao is not distinct from p_termo_versao
      and t.conteudo_hash is not distinct from p_termo_hash
  ) then
    raise exception 'consent term does not match tenant, version and hash';
  end if;

  insert into public.lgpd_decisoes_historico (
    tenant_id, contato_id, canal, finalidade, decisao,
    termo_id, termo_versao, termo_hash, origem
  ) values (
    p_tenant_id, p_contato_id, p_canal, p_finalidade, p_decisao,
    p_termo_id, p_termo_versao, p_termo_hash, p_origem
  );

  insert into public.lgpd_preferencias (
    tenant_id, contato_id, canal, finalidade, decisao,
    termo_id, termo_versao, termo_hash, origem, decidido_em, revogado_em
  ) values (
    p_tenant_id, p_contato_id, p_canal, p_finalidade, p_decisao,
    p_termo_id, p_termo_versao, p_termo_hash, p_origem, now(),
    case when p_decisao = 'revogado' then now() else null end
  )
  on conflict (tenant_id, contato_id, canal, finalidade) do update
    set decisao = excluded.decisao,
        termo_id = excluded.termo_id,
        termo_versao = excluded.termo_versao,
        termo_hash = excluded.termo_hash,
        origem = excluded.origem,
        decidido_em = excluded.decidido_em,
        revogado_em = excluded.revogado_em,
        updated_at = now();
end;
$$;

CREATE FUNCTION public.set_lgpd_termo_hash() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_temp'
    AS $$
begin
  new.conteudo_hash := encode(
    extensions.digest(coalesce(new.conteudo, ''), 'sha256'),
    'hex'
  );
  return new;
end;
$$;

CREATE TABLE public.agendamentos (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    contato_id uuid NOT NULL,
    perfil_id uuid,
    data_hora timestamp with time zone NOT NULL,
    tipo text DEFAULT 'Consulta'::text NOT NULL,
    status text DEFAULT 'agendado'::text NOT NULL,
    convenio text,
    observacao text,
    google_event_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    atendente_id uuid,
    lembrete_enviado boolean DEFAULT false NOT NULL,
    lembrete_enviado_em timestamp with time zone,
    status_presenca text DEFAULT 'agendado'::text,
    aguardando_em timestamp with time zone,
    em_atendimento_em timestamp with time zone,
    atendido_em timestamp with time zone,
    nao_compareceu_em timestamp with time zone,
    duracao_minutos integer DEFAULT 30 NOT NULL,
    CONSTRAINT agendamentos_duracao_minutos_check CHECK (((duracao_minutos >= 5) AND (duracao_minutos <= 480))),
    CONSTRAINT agendamentos_status_check CHECK ((status = ANY (ARRAY['agendado'::text, 'confirmado'::text, 'confirmado_paciente'::text, 'realizado'::text, 'cancelado'::text, 'cancelado_paciente'::text, 'remarcado'::text, 'nao_compareceu'::text])))
);

CREATE TABLE public.agent_knowledge_blocks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    title text NOT NULL,
    content text NOT NULL,
    category text,
    keywords text[],
    trigger_intents text[],
    priority integer DEFAULT 0 NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.agent_prompts_historico (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    prompt_texto text NOT NULL,
    versao integer NOT NULL,
    atualizado_por uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.audit_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    ator text NOT NULL,
    acao text NOT NULL,
    detalhe jsonb DEFAULT '{}'::jsonb NOT NULL,
    ip_origem inet,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.canais_whatsapp (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    numero text NOT NULL,
    provider text NOT NULL,
    credenciais_ref text,
    rotulo text,
    ativo boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    principal boolean DEFAULT false NOT NULL,
    phone_number_id text,
    waba_id text,
    token_cifrado text,
    CONSTRAINT canais_whatsapp_somente_cloud_api CHECK ((provider = 'cloud_api'::text)),
    CONSTRAINT canais_whatsapp_tem_credencial CHECK ((((phone_number_id IS NOT NULL) AND (token_cifrado IS NOT NULL)) OR (NULLIF(btrim(credenciais_ref), ''::text) IS NOT NULL))),
    CONSTRAINT canais_whatsapp_token_cifrado CHECK (((token_cifrado IS NULL) OR (token_cifrado ~~ 'v1:%'::text)))
);

CREATE TABLE public.checkpoint_blobs (
    thread_id text NOT NULL,
    checkpoint_ns text DEFAULT ''::text NOT NULL,
    channel text NOT NULL,
    version text NOT NULL,
    type text NOT NULL,
    blob bytea
);

CREATE TABLE public.checkpoint_migrations (
    v integer NOT NULL
);

CREATE TABLE public.checkpoint_writes (
    thread_id text NOT NULL,
    checkpoint_ns text DEFAULT ''::text NOT NULL,
    checkpoint_id text NOT NULL,
    task_id text NOT NULL,
    idx integer NOT NULL,
    channel text NOT NULL,
    type text,
    blob bytea NOT NULL,
    task_path text DEFAULT ''::text NOT NULL
);

CREATE TABLE public.checkpoints (
    thread_id text NOT NULL,
    checkpoint_ns text DEFAULT ''::text NOT NULL,
    checkpoint_id text NOT NULL,
    parent_checkpoint_id text,
    type text,
    checkpoint jsonb NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE TABLE public.contatos (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    telefone text NOT NULL,
    nome text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    email text,
    cpf text,
    data_nascimento date,
    convenio text,
    etapa text DEFAULT 'novo'::text NOT NULL,
    score integer DEFAULT 50 NOT NULL,
    origem text,
    interesse text,
    observacoes text,
    ultima_mensagem_em timestamp with time zone,
    proximo_followup_em timestamp with time zone,
    lgpd_aceito boolean DEFAULT false NOT NULL,
    lgpd_aceito_at timestamp with time zone,
    lgpd_revogado boolean DEFAULT false NOT NULL,
    lgpd_revogado_at timestamp with time zone,
    lgpd_ip text,
    lgpd_canal text,
    anonimizado boolean DEFAULT false NOT NULL,
    anonimizado_at timestamp with time zone,
    tipo text DEFAULT 'lead'::text NOT NULL,
    endereco text,
    cidade text,
    cep text,
    CONSTRAINT contatos_tipo_check CHECK ((tipo = ANY (ARRAY['lead'::text, 'paciente'::text])))
);

CREATE TABLE public.conversas (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    contato_id uuid NOT NULL,
    canal_id uuid,
    estado text DEFAULT 'aberta'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    agente_ativo boolean DEFAULT true NOT NULL,
    status text DEFAULT 'aberta'::text NOT NULL,
    atribuido_para uuid,
    started_at timestamp with time zone DEFAULT now(),
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    atribuido_nome text,
    CONSTRAINT conversas_estado_check CHECK ((estado = ANY (ARRAY['aberta'::text, 'aguardando_humano'::text, 'encerrada'::text])))
);

CREATE TABLE public.crm_notificacoes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    perfil_id uuid,
    tipo text NOT NULL,
    titulo text NOT NULL,
    mensagem text,
    lida boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    conversation_id uuid,
    conversa_id uuid,
    corpo text,
    contato_id uuid,
    lida_at timestamp with time zone,
    tarefa_id uuid
);

CREATE TABLE public.documentos_paciente (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    contato_id uuid,
    agendamento_id uuid,
    tipo text NOT NULL,
    mime_type text NOT NULL,
    storage_path text NOT NULL,
    nome_original text,
    descricao_ia text,
    tamanho_bytes integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    origem text DEFAULT 'paciente'::text NOT NULL,
    enviado_por uuid,
    CONSTRAINT documentos_paciente_tipo_check CHECK ((tipo = ANY (ARRAY['imagem'::text, 'documento'::text])))
);

CREATE TABLE public.erros_motor (
    id bigint NOT NULL,
    tenant_id uuid NOT NULL,
    criado_em timestamp with time zone DEFAULT now() NOT NULL,
    tipo text NOT NULL,
    detalhe text NOT NULL,
    CONSTRAINT erros_motor_detalhe_check CHECK ((length(detalhe) <= 300)),
    CONSTRAINT erros_motor_tipo_check CHECK ((tipo = ANY (ARRAY['envio'::text, 'canal'::text, 'agente'::text])))
);

ALTER TABLE public.erros_motor ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.erros_motor_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.followup_queue (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    contato_id uuid NOT NULL,
    perfil_id uuid,
    tipo text DEFAULT 'followup'::text NOT NULL,
    mensagem text,
    agendado_para timestamp with time zone,
    status text DEFAULT 'pendente'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT followup_queue_status_check CHECK ((status = ANY (ARRAY['pendente'::text, 'enviado'::text, 'cancelado'::text])))
);

CREATE TABLE public.handoff_fila (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    conversa_id uuid NOT NULL,
    contato_id uuid NOT NULL,
    perfil_id uuid NOT NULL,
    perfil_nome text NOT NULL,
    perfil_role text NOT NULL,
    perfil_telefone text,
    status text DEFAULT 'aguardando'::text NOT NULL,
    motivo text,
    resumo_conversa text,
    ultimo_handoff_em timestamp with time zone,
    criado_em timestamp with time zone DEFAULT now() NOT NULL,
    assumido_em timestamp with time zone,
    encerrado_em timestamp with time zone,
    CONSTRAINT handoff_fila_status_check CHECK ((status = ANY (ARRAY['aguardando'::text, 'em_atendimento'::text, 'devolvido'::text, 'encerrado'::text])))
);

CREATE TABLE public.idempotency_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    provider text NOT NULL,
    external_event_id text NOT NULL,
    tenant_id uuid NOT NULL,
    payload_hash text NOT NULL,
    status text DEFAULT 'processing'::text NOT NULL,
    attempts integer DEFAULT 1 NOT NULL,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    locked_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    last_error text,
    result jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT idempotency_events_attempts_check CHECK ((attempts > 0)),
    CONSTRAINT idempotency_events_status_check CHECK ((status = ANY (ARRAY['processing'::text, 'completed'::text, 'failed'::text])))
);

CREATE TABLE public.integracoes_oauth (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    provedor text NOT NULL,
    access_token text,
    refresh_token text NOT NULL,
    token_expira_em timestamp with time zone,
    email_conectado text,
    calendar_id text DEFAULT 'primary'::text,
    ativo boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE public.integration_logs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    integracao text NOT NULL,
    operacao text NOT NULL,
    request_payload jsonb,
    response_status integer,
    response_body text,
    duracao_ms integer,
    sucesso boolean DEFAULT false NOT NULL,
    erro text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.leads (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    contato_id uuid,
    nome text NOT NULL,
    telefone text,
    origem text NOT NULL,
    indicado_por text,
    tipo_servico text,
    estagio text DEFAULT 'em_conversa'::text NOT NULL,
    valor numeric(10,2),
    proximo_passo text,
    data_proximo_contato date,
    motivo_perda text,
    observacoes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    source text,
    source_spreadsheet_id text,
    source_sheet_gid text,
    source_row_id text,
    source_hash text,
    last_synced_at timestamp with time zone,
    atribuido_a uuid,
    atribuido_em timestamp with time zone,
    fila_espera_em timestamp with time zone,
    CONSTRAINT leads_estagio_check CHECK ((estagio = ANY (ARRAY['em_conversa'::text, 'consulta_agendada'::text, 'compareceu_consulta'::text, 'proposta_enviada'::text, 'em_followup'::text, 'fechou_tratamento'::text, 'perdido'::text, 'outro'::text]))),
    CONSTRAINT leads_motivo_perda_check CHECK (((motivo_perda IS NULL) OR (motivo_perda = ANY (ARRAY['preco_valor'::text, 'sumiu_nao_respondeu'::text, 'foi_com_outro_medico'::text, 'sem_fit_perfil_diferente'::text, 'sem_orcamento_agora'::text, 'localizacao_distancia'::text, 'outro'::text])))),
    CONSTRAINT leads_motivo_perda_so_se_perdido CHECK (((motivo_perda IS NULL) OR (estagio = 'perdido'::text))),
    CONSTRAINT leads_origem_check CHECK (((origem IS NULL) OR (origem = ANY (ARRAY['whatsapp'::text, 'instagram'::text, 'indicacao'::text, 'google'::text, 'site'::text, 'outro'::text, 'ligacao'::text, 'convenio_parceria'::text, 'encaminhamento_medico'::text])))),
    CONSTRAINT leads_tipo_servico_check CHECK (((tipo_servico IS NULL) OR (tipo_servico = ANY (ARRAY['unico'::text, 'recorrente'::text]))))
);

CREATE TABLE public.leads_historico_estagio (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    lead_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    estagio_anterior text,
    estagio_novo text NOT NULL,
    changed_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.lgpd_audit_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    entidade text NOT NULL,
    entidade_id uuid NOT NULL,
    acao text NOT NULL,
    realizado_por uuid,
    canal text,
    detalhes jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.lgpd_decisoes_historico (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    contato_id uuid NOT NULL,
    canal text NOT NULL,
    finalidade text NOT NULL,
    decisao text NOT NULL,
    termo_id uuid,
    termo_versao text,
    termo_hash text,
    origem text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT lgpd_decisoes_historico_decisao_check CHECK ((decisao = ANY (ARRAY['apresentado'::text, 'aceito'::text, 'recusado'::text, 'revogado'::text])))
);

CREATE TABLE public.lgpd_preferencias (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    contato_id uuid NOT NULL,
    canal text NOT NULL,
    finalidade text NOT NULL,
    decisao text NOT NULL,
    termo_id uuid,
    termo_versao text,
    termo_hash text,
    origem text NOT NULL,
    decidido_em timestamp with time zone DEFAULT now() NOT NULL,
    revogado_em timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT lgpd_preferencias_decisao_check CHECK ((decisao = ANY (ARRAY['aceito'::text, 'recusado'::text, 'revogado'::text])))
);

CREATE TABLE public.lgpd_termos (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    tipo text NOT NULL,
    versao text NOT NULL,
    titulo text NOT NULL,
    conteudo text NOT NULL,
    ativo boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    conteudo_hash text
);

CREATE TABLE public.links_consentimento (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    contato_id uuid NOT NULL,
    codigo_hash text NOT NULL,
    criado_em timestamp with time zone DEFAULT now() NOT NULL,
    expira_em timestamp with time zone NOT NULL,
    CONSTRAINT link_consentimento_ate_30_dias CHECK (((expira_em > criado_em) AND (expira_em <= (criado_em + '30 days'::interval)))),
    CONSTRAINT links_consentimento_codigo_hash_check CHECK ((length(codigo_hash) = 64))
);

CREATE TABLE public.mensagens (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    conversa_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    papel text NOT NULL,
    conteudo text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    perfil_id uuid,
    CONSTRAINT mensagens_papel_check CHECK ((papel = ANY (ARRAY['usuario'::text, 'agente'::text, 'sistema'::text])))
);

CREATE TABLE public.nps_respostas (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    agendamento_id uuid,
    contato_id uuid NOT NULL,
    score integer,
    comentario text,
    canal text DEFAULT 'whatsapp'::text,
    respondido_em timestamp with time zone,
    criado_em timestamp with time zone DEFAULT now(),
    token text NOT NULL,
    canal_envio text DEFAULT 'pos_consulta'::text NOT NULL,
    canal_resposta text,
    status text DEFAULT 'pendente'::text NOT NULL,
    nota integer,
    link_url text,
    enviado_em timestamp with time zone,
    reforco_agendado_em timestamp with time zone,
    reforco_enviado_em timestamp with time zone,
    google_review_requested_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    lead_id uuid,
    CONSTRAINT nps_respostas_nota_check CHECK (((nota IS NULL) OR ((nota >= 0) AND (nota <= 10)))),
    CONSTRAINT nps_respostas_score_check CHECK (((score >= 0) AND (score <= 10)))
);

CREATE TABLE public.pedidos_agendamento (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    contato_id uuid NOT NULL,
    nome_paciente text NOT NULL,
    data_nascimento text,
    convenio text,
    tipo_consulta text DEFAULT 'outro'::text NOT NULL,
    preferencia_horario text,
    observacao text,
    status text DEFAULT 'pendente'::text NOT NULL,
    agendamento_id uuid,
    resolvido_por uuid,
    resolvido_em timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT pedidos_agendamento_status_check CHECK ((status = ANY (ARRAY['pendente'::text, 'agendado'::text, 'descartado'::text]))),
    CONSTRAINT pedidos_agendamento_tipo_check CHECK ((tipo_consulta = ANY (ARRAY['primeira_consulta'::text, 'retorno'::text, 'outro'::text])))
);

CREATE TABLE public.perfis (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    user_id uuid,
    nome text NOT NULL,
    email text,
    telefone text,
    role text DEFAULT 'atendente'::text NOT NULL,
    ativo boolean DEFAULT true NOT NULL,
    avatar_url text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    termos_aceitos boolean DEFAULT false,
    termos_aceitos_at timestamp with time zone,
    termos_versao text,
    ultimo_handoff_em timestamp with time zone,
    registro_profissional text,
    especialidade text,
    cpf text,
    crm text,
    crm_uf text,
    rqe text,
    cabecalho_extra text
);

CREATE TABLE public.tenant_config (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    chave text NOT NULL,
    valor jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.tenants (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    nome text NOT NULL,
    vertical text NOT NULL,
    system_prompt text,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    ativo boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    telefone_atendente text
);

CREATE TABLE public.uso_ia (
    id bigint NOT NULL,
    tenant_id uuid NOT NULL,
    criado_em timestamp with time zone DEFAULT now() NOT NULL,
    uso text NOT NULL,
    modelo text NOT NULL,
    tokens_entrada integer DEFAULT 0 NOT NULL,
    tokens_saida integer DEFAULT 0 NOT NULL,
    cache_leitura integer DEFAULT 0 NOT NULL,
    cache_escrita integer DEFAULT 0 NOT NULL,
    CONSTRAINT uso_ia_cache_escrita_check CHECK ((cache_escrita >= 0)),
    CONSTRAINT uso_ia_cache_leitura_check CHECK ((cache_leitura >= 0)),
    CONSTRAINT uso_ia_tokens_entrada_check CHECK ((tokens_entrada >= 0)),
    CONSTRAINT uso_ia_tokens_saida_check CHECK ((tokens_saida >= 0)),
    CONSTRAINT uso_ia_uso_check CHECK ((uso = ANY (ARRAY['atendimento'::text, 'interno'::text, 'briefing'::text, 'analise'::text, 'copiloto'::text])))
);

ALTER TABLE public.uso_ia ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.uso_ia_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

ALTER TABLE ONLY public.agendamentos
    ADD CONSTRAINT agendamentos_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.agent_knowledge_blocks
    ADD CONSTRAINT agent_knowledge_blocks_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.agent_knowledge_blocks
    ADD CONSTRAINT agent_knowledge_blocks_tenant_title_unique UNIQUE (tenant_id, title);

ALTER TABLE ONLY public.agent_prompts_historico
    ADD CONSTRAINT agent_prompts_historico_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.agent_prompts_historico
    ADD CONSTRAINT agent_prompts_historico_tenant_versao_key UNIQUE (tenant_id, versao);

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.canais_whatsapp
    ADD CONSTRAINT canais_whatsapp_numero_key UNIQUE (numero);

ALTER TABLE ONLY public.canais_whatsapp
    ADD CONSTRAINT canais_whatsapp_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.checkpoint_blobs
    ADD CONSTRAINT checkpoint_blobs_pkey PRIMARY KEY (thread_id, checkpoint_ns, channel, version);

ALTER TABLE ONLY public.checkpoint_migrations
    ADD CONSTRAINT checkpoint_migrations_pkey PRIMARY KEY (v);

ALTER TABLE ONLY public.checkpoint_writes
    ADD CONSTRAINT checkpoint_writes_pkey PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx);

ALTER TABLE ONLY public.checkpoints
    ADD CONSTRAINT checkpoints_pkey PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id);

ALTER TABLE ONLY public.contatos
    ADD CONSTRAINT contatos_id_tenant_unique UNIQUE (id, tenant_id);

ALTER TABLE ONLY public.contatos
    ADD CONSTRAINT contatos_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.contatos
    ADD CONSTRAINT contatos_tenant_telefone_unique UNIQUE (tenant_id, telefone);

ALTER TABLE ONLY public.conversas
    ADD CONSTRAINT conversas_id_tenant_unique UNIQUE (id, tenant_id);

ALTER TABLE ONLY public.conversas
    ADD CONSTRAINT conversas_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.crm_notificacoes
    ADD CONSTRAINT crm_notificacoes_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.documentos_paciente
    ADD CONSTRAINT documentos_paciente_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.documentos_paciente
    ADD CONSTRAINT documentos_paciente_storage_path_key UNIQUE (storage_path);

ALTER TABLE ONLY public.erros_motor
    ADD CONSTRAINT erros_motor_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.followup_queue
    ADD CONSTRAINT followup_queue_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.handoff_fila
    ADD CONSTRAINT handoff_fila_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.idempotency_events
    ADD CONSTRAINT idempotency_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.idempotency_events
    ADD CONSTRAINT idempotency_events_provider_external_key UNIQUE (provider, external_event_id);

ALTER TABLE ONLY public.integracoes_oauth
    ADD CONSTRAINT integracoes_oauth_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.integracoes_oauth
    ADD CONSTRAINT integracoes_oauth_tenant_id_provedor_key UNIQUE (tenant_id, provedor);

ALTER TABLE ONLY public.integration_logs
    ADD CONSTRAINT integration_logs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.leads_historico_estagio
    ADD CONSTRAINT leads_historico_estagio_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.leads
    ADD CONSTRAINT leads_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.lgpd_audit_log
    ADD CONSTRAINT lgpd_audit_log_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.lgpd_decisoes_historico
    ADD CONSTRAINT lgpd_decisoes_historico_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.lgpd_preferencias
    ADD CONSTRAINT lgpd_preferencias_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.lgpd_preferencias
    ADD CONSTRAINT lgpd_preferencias_tenant_id_contato_id_canal_finalidade_key UNIQUE (tenant_id, contato_id, canal, finalidade);

ALTER TABLE ONLY public.lgpd_termos
    ADD CONSTRAINT lgpd_termos_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.links_consentimento
    ADD CONSTRAINT links_consentimento_codigo_hash_key UNIQUE (codigo_hash);

ALTER TABLE ONLY public.links_consentimento
    ADD CONSTRAINT links_consentimento_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.mensagens
    ADD CONSTRAINT mensagens_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.nps_respostas
    ADD CONSTRAINT nps_respostas_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.pedidos_agendamento
    ADD CONSTRAINT pedidos_agendamento_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.perfis
    ADD CONSTRAINT perfis_pkey PRIMARY KEY (id);

ALTER TABLE public.perfis
    ADD CONSTRAINT perfis_role_check CHECK ((role = ANY (ARRAY['admin'::text, 'medico'::text, 'gestor'::text, 'atendente'::text, 'concierge'::text, 'marketing'::text]))) NOT VALID;

ALTER TABLE ONLY public.perfis
    ADD CONSTRAINT perfis_tenant_id_email_key UNIQUE (tenant_id, email);

ALTER TABLE ONLY public.tenant_config
    ADD CONSTRAINT tenant_config_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.tenant_config
    ADD CONSTRAINT tenant_config_tenant_id_chave_key UNIQUE (tenant_id, chave);

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.uso_ia
    ADD CONSTRAINT uso_ia_pkey PRIMARY KEY (id);

CREATE INDEX agendamentos_contato_id_idx ON public.agendamentos USING btree (contato_id);

CREATE INDEX agendamentos_tenant_perfil_data_idx ON public.agendamentos USING btree (tenant_id, perfil_id, data_hora);

CREATE INDEX agent_prompts_historico_tenant_idx ON public.agent_prompts_historico USING btree (tenant_id, versao DESC);

CREATE UNIQUE INDEX canais_whatsapp_phone_number_id_key ON public.canais_whatsapp USING btree (phone_number_id) WHERE (phone_number_id IS NOT NULL);

CREATE UNIQUE INDEX canais_whatsapp_um_principal_por_tenant ON public.canais_whatsapp USING btree (tenant_id) WHERE principal;

CREATE INDEX checkpoint_blobs_thread_id_idx ON public.checkpoint_blobs USING btree (thread_id);

CREATE INDEX checkpoint_writes_thread_id_idx ON public.checkpoint_writes USING btree (thread_id);

CREATE INDEX checkpoints_thread_id_idx ON public.checkpoints USING btree (thread_id);

CREATE INDEX contatos_tenant_tipo_updated_idx ON public.contatos USING btree (tenant_id, tipo, updated_at DESC);

CREATE INDEX conversas_tenant_estado_updated_idx ON public.conversas USING btree (tenant_id, estado, updated_at DESC);

CREATE INDEX crm_notificacoes_contato_id_idx ON public.crm_notificacoes USING btree (contato_id);

CREATE INDEX crm_notificacoes_tenant_perfil_lida_idx ON public.crm_notificacoes USING btree (tenant_id, perfil_id, lida);

CREATE INDEX documentos_paciente_contato_id_idx ON public.documentos_paciente USING btree (contato_id);

CREATE INDEX erros_motor_tenant_idx ON public.erros_motor USING btree (tenant_id, criado_em DESC);

CREATE INDEX followup_queue_contato_id_idx ON public.followup_queue USING btree (contato_id);

CREATE INDEX handoff_fila_contato_id_idx ON public.handoff_fila USING btree (contato_id);

CREATE INDEX handoff_fila_tenant_conversa_status_idx ON public.handoff_fila USING btree (tenant_id, conversa_id, status, criado_em DESC);

CREATE INDEX idempotency_events_tenant_status_idx ON public.idempotency_events USING btree (tenant_id, status, first_seen_at DESC);

CREATE INDEX idx_agendamentos_lembrete ON public.agendamentos USING btree (lembrete_enviado, data_hora) WHERE (lembrete_enviado = false);

CREATE INDEX idx_agendamentos_tenant_contato ON public.agendamentos USING btree (tenant_id, contato_id);

CREATE INDEX idx_agendamentos_tenant_data ON public.agendamentos USING btree (tenant_id, data_hora);

CREATE INDEX idx_agendamentos_tenant_status ON public.agendamentos USING btree (tenant_id, status);

CREATE INDEX idx_agent_knowledge_blocks_tenant_active_priority ON public.agent_knowledge_blocks USING btree (tenant_id, active, priority);

CREATE INDEX idx_audit_log_created_at ON public.audit_log USING btree (created_at);

CREATE INDEX idx_audit_log_tenant_id ON public.audit_log USING btree (tenant_id);

CREATE INDEX idx_canais_tenant_id ON public.canais_whatsapp USING btree (tenant_id);

CREATE INDEX idx_contatos_tenant_id ON public.contatos USING btree (tenant_id);

CREATE INDEX idx_conversas_atribuido_para ON public.conversas USING btree (atribuido_para) WHERE (atribuido_para IS NOT NULL);

CREATE INDEX idx_conversas_contato_id ON public.conversas USING btree (contato_id);

CREATE INDEX idx_conversas_tenant_estado ON public.conversas USING btree (tenant_id, estado);

CREATE INDEX idx_conversas_tenant_id ON public.conversas USING btree (tenant_id);

CREATE INDEX idx_conversas_updated_at ON public.conversas USING btree (updated_at DESC);

CREATE INDEX idx_crm_notificacoes_tenant_id ON public.crm_notificacoes USING btree (tenant_id);

CREATE INDEX idx_documentos_tenant_contato ON public.documentos_paciente USING btree (tenant_id, contato_id);

CREATE INDEX idx_documentos_tenant_created ON public.documentos_paciente USING btree (tenant_id, created_at DESC);

CREATE INDEX idx_followup_queue_tenant_status_agendado ON public.followup_queue USING btree (tenant_id, status, agendado_para);

CREATE INDEX idx_handoff_fila_conversa ON public.handoff_fila USING btree (conversa_id);

CREATE INDEX idx_handoff_fila_perfil ON public.handoff_fila USING btree (perfil_id);

CREATE INDEX idx_handoff_fila_status ON public.handoff_fila USING btree (status);

CREATE INDEX idx_handoff_fila_tenant ON public.handoff_fila USING btree (tenant_id);

CREATE INDEX idx_integration_logs_integracao ON public.integration_logs USING btree (integracao, created_at DESC);

CREATE INDEX idx_integration_logs_tenant ON public.integration_logs USING btree (tenant_id, created_at DESC);

CREATE INDEX idx_leads_historico_tenant_estagio_novo ON public.leads_historico_estagio USING btree (tenant_id, estagio_novo);

CREATE INDEX idx_leads_historico_tenant_lead ON public.leads_historico_estagio USING btree (tenant_id, lead_id);

CREATE INDEX idx_leads_tenant_contato ON public.leads USING btree (tenant_id, contato_id);

CREATE INDEX idx_leads_tenant_estagio ON public.leads USING btree (tenant_id, estagio);

CREATE INDEX idx_leads_tenant_proximo_contato ON public.leads USING btree (tenant_id, data_proximo_contato);

CREATE INDEX idx_lgpd_audit_log_entidade ON public.lgpd_audit_log USING btree (entidade_id);

CREATE INDEX idx_lgpd_audit_log_tenant ON public.lgpd_audit_log USING btree (tenant_id);

CREATE INDEX idx_lgpd_termos_tenant ON public.lgpd_termos USING btree (tenant_id);

CREATE INDEX idx_mensagens_conversa_created ON public.mensagens USING btree (conversa_id, created_at DESC);

CREATE INDEX idx_mensagens_conversa_id ON public.mensagens USING btree (conversa_id);

CREATE INDEX idx_mensagens_created_at ON public.mensagens USING btree (created_at);

CREATE INDEX idx_mensagens_perfil_id ON public.mensagens USING btree (perfil_id);

CREATE INDEX idx_mensagens_tenant_id ON public.mensagens USING btree (tenant_id);

CREATE INDEX idx_nps_agendamento ON public.nps_respostas USING btree (agendamento_id);

CREATE INDEX idx_nps_contato ON public.nps_respostas USING btree (contato_id);

CREATE INDEX idx_nps_tenant ON public.nps_respostas USING btree (tenant_id);

CREATE INDEX idx_perfis_tenant_id ON public.perfis USING btree (tenant_id);

CREATE INDEX idx_tenant_config_tenant ON public.tenant_config USING btree (tenant_id);

CREATE INDEX leads_atribuido_a_idx ON public.leads USING btree (tenant_id, atribuido_a) WHERE (atribuido_a IS NOT NULL);

CREATE INDEX leads_contato_id_idx ON public.leads USING btree (contato_id);

CREATE INDEX leads_estagio_idx ON public.leads USING btree (estagio);

CREATE INDEX leads_fila_espera_idx ON public.leads USING btree (fila_espera_em) WHERE (fila_espera_em IS NOT NULL);

CREATE INDEX leads_source_idx ON public.leads USING btree (source, source_spreadsheet_id, source_sheet_gid);

CREATE UNIQUE INDEX leads_source_row_unique ON public.leads USING btree (tenant_id, source, source_spreadsheet_id, source_sheet_gid, source_row_id) WHERE (source IS NOT NULL);

CREATE INDEX leads_tenant_fila_idx ON public.leads USING btree (tenant_id, contato_id) WHERE (fila_espera_em IS NOT NULL);

CREATE INDEX leads_tenant_idx ON public.leads USING btree (tenant_id);

CREATE INDEX lgpd_decisoes_historico_contato_id_idx ON public.lgpd_decisoes_historico USING btree (contato_id);

CREATE INDEX lgpd_decisoes_historico_contato_idx ON public.lgpd_decisoes_historico USING btree (tenant_id, contato_id, created_at DESC);

CREATE INDEX lgpd_preferencias_contato_id_idx ON public.lgpd_preferencias USING btree (contato_id);

CREATE INDEX lgpd_preferencias_contato_idx ON public.lgpd_preferencias USING btree (tenant_id, contato_id, canal, finalidade);

CREATE UNIQUE INDEX lgpd_termos_um_ativo_por_tipo ON public.lgpd_termos USING btree (tenant_id, tipo) WHERE ativo;

CREATE INDEX links_consentimento_contato_idx ON public.links_consentimento USING btree (tenant_id, contato_id);

CREATE INDEX mensagens_tenant_papel_idx ON public.mensagens USING btree (tenant_id, papel, created_at DESC);

CREATE INDEX nps_respostas_contato_id_idx ON public.nps_respostas USING btree (contato_id);

CREATE INDEX nps_respostas_reforco_idx ON public.nps_respostas USING btree (reforco_agendado_em) WHERE ((status = 'pendente'::text) AND (respondido_em IS NULL));

CREATE INDEX nps_respostas_tenant_criado_idx ON public.nps_respostas USING btree (tenant_id, criado_em DESC);

CREATE INDEX nps_respostas_tenant_status_idx ON public.nps_respostas USING btree (tenant_id, status);

CREATE UNIQUE INDEX nps_respostas_token_key ON public.nps_respostas USING btree (token);

CREATE INDEX pedidos_agendamento_contato_id_idx ON public.pedidos_agendamento USING btree (contato_id);

CREATE INDEX pedidos_agendamento_tenant_status_idx ON public.pedidos_agendamento USING btree (tenant_id, status, created_at DESC);

CREATE UNIQUE INDEX pedidos_agendamento_um_pendente_idx ON public.pedidos_agendamento USING btree (tenant_id, contato_id) WHERE (status = 'pendente'::text);

CREATE UNIQUE INDEX uq_conversas_tenant_contato_ativa ON public.conversas USING btree (tenant_id, contato_id) WHERE (estado = ANY (ARRAY['aberta'::text, 'aguardando_humano'::text]));

CREATE INDEX uso_ia_tenant_idx ON public.uso_ia USING btree (tenant_id, criado_em DESC);

CREATE TRIGGER agendamentos_updated_at BEFORE UPDATE ON public.agendamentos FOR EACH ROW EXECUTE FUNCTION public.atualizar_updated_at();

CREATE TRIGGER agent_knowledge_blocks_updated_at BEFORE UPDATE ON public.agent_knowledge_blocks FOR EACH ROW EXECUTE FUNCTION public.atualizar_updated_at();

CREATE TRIGGER audit_log_bloquear_alteracao BEFORE DELETE OR UPDATE ON public.audit_log FOR EACH ROW EXECUTE FUNCTION public.audit_log_imutavel();

CREATE TRIGGER canais_whatsapp_updated_at BEFORE UPDATE ON public.canais_whatsapp FOR EACH ROW EXECUTE FUNCTION public.atualizar_updated_at();

CREATE TRIGGER contatos_updated_at BEFORE UPDATE ON public.contatos FOR EACH ROW EXECUTE FUNCTION public.atualizar_updated_at();

CREATE TRIGGER conversas_updated_at BEFORE UPDATE ON public.conversas FOR EACH ROW EXECUTE FUNCTION public.atualizar_updated_at();

CREATE TRIGGER leads_registrar_historico_estagio AFTER INSERT OR UPDATE OF estagio ON public.leads FOR EACH ROW EXECUTE FUNCTION public.registrar_historico_estagio_lead();

CREATE TRIGGER leads_updated_at BEFORE UPDATE ON public.leads FOR EACH ROW EXECUTE FUNCTION public.atualizar_updated_at();

CREATE TRIGGER lgpd_termos_hash_trigger BEFORE INSERT OR UPDATE OF conteudo ON public.lgpd_termos FOR EACH ROW EXECUTE FUNCTION public.set_lgpd_termo_hash();

CREATE TRIGGER perfis_updated_at BEFORE UPDATE ON public.perfis FOR EACH ROW EXECUTE FUNCTION public.atualizar_updated_at();

CREATE TRIGGER tenants_updated_at BEFORE UPDATE ON public.tenants FOR EACH ROW EXECUTE FUNCTION public.atualizar_updated_at();

CREATE TRIGGER trg_lead_transferido AFTER UPDATE OF atribuido_a ON public.leads FOR EACH ROW EXECUTE FUNCTION public.notify_lead_transferido();

ALTER TABLE ONLY public.agendamentos
    ADD CONSTRAINT agendamentos_atendente_id_fkey FOREIGN KEY (atendente_id) REFERENCES public.perfis(id);

ALTER TABLE ONLY public.agendamentos
    ADD CONSTRAINT agendamentos_contato_fk FOREIGN KEY (contato_id, tenant_id) REFERENCES public.contatos(id, tenant_id);

ALTER TABLE ONLY public.agendamentos
    ADD CONSTRAINT agendamentos_perfil_id_fkey FOREIGN KEY (perfil_id) REFERENCES public.perfis(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.agent_prompts_historico
    ADD CONSTRAINT agent_prompts_historico_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.canais_whatsapp
    ADD CONSTRAINT canais_whatsapp_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.contatos
    ADD CONSTRAINT contatos_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.conversas
    ADD CONSTRAINT conversas_canal_id_fkey FOREIGN KEY (canal_id) REFERENCES public.canais_whatsapp(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.conversas
    ADD CONSTRAINT conversas_contato_tenant_fk FOREIGN KEY (contato_id, tenant_id) REFERENCES public.contatos(id, tenant_id);

ALTER TABLE ONLY public.conversas
    ADD CONSTRAINT conversas_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.crm_notificacoes
    ADD CONSTRAINT crm_notificacoes_contato_fk FOREIGN KEY (contato_id, tenant_id) REFERENCES public.contatos(id, tenant_id);

ALTER TABLE ONLY public.crm_notificacoes
    ADD CONSTRAINT crm_notificacoes_perfil_id_fkey FOREIGN KEY (perfil_id) REFERENCES public.perfis(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.documentos_paciente
    ADD CONSTRAINT documentos_paciente_agendamento_id_fkey FOREIGN KEY (agendamento_id) REFERENCES public.agendamentos(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.documentos_paciente
    ADD CONSTRAINT documentos_paciente_contato_fk FOREIGN KEY (contato_id, tenant_id) REFERENCES public.contatos(id, tenant_id);

ALTER TABLE ONLY public.documentos_paciente
    ADD CONSTRAINT documentos_paciente_enviado_por_fkey FOREIGN KEY (enviado_por) REFERENCES public.perfis(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.followup_queue
    ADD CONSTRAINT followup_queue_contato_fk FOREIGN KEY (contato_id, tenant_id) REFERENCES public.contatos(id, tenant_id);

ALTER TABLE ONLY public.followup_queue
    ADD CONSTRAINT followup_queue_perfil_id_fkey FOREIGN KEY (perfil_id) REFERENCES public.perfis(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.handoff_fila
    ADD CONSTRAINT handoff_fila_contato_fk FOREIGN KEY (contato_id, tenant_id) REFERENCES public.contatos(id, tenant_id);

ALTER TABLE ONLY public.handoff_fila
    ADD CONSTRAINT handoff_fila_perfil_id_fkey FOREIGN KEY (perfil_id) REFERENCES public.perfis(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.idempotency_events
    ADD CONSTRAINT idempotency_events_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.leads
    ADD CONSTRAINT leads_atribuido_a_fkey FOREIGN KEY (atribuido_a) REFERENCES public.perfis(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.leads
    ADD CONSTRAINT leads_contato_fk FOREIGN KEY (contato_id, tenant_id) REFERENCES public.contatos(id, tenant_id);

ALTER TABLE ONLY public.leads_historico_estagio
    ADD CONSTRAINT leads_historico_estagio_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.lgpd_decisoes_historico
    ADD CONSTRAINT lgpd_decisoes_historico_contato_fk FOREIGN KEY (contato_id, tenant_id) REFERENCES public.contatos(id, tenant_id);

ALTER TABLE ONLY public.lgpd_decisoes_historico
    ADD CONSTRAINT lgpd_decisoes_historico_termo_id_fkey FOREIGN KEY (termo_id) REFERENCES public.lgpd_termos(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.lgpd_preferencias
    ADD CONSTRAINT lgpd_preferencias_contato_fk FOREIGN KEY (contato_id, tenant_id) REFERENCES public.contatos(id, tenant_id);

ALTER TABLE ONLY public.lgpd_preferencias
    ADD CONSTRAINT lgpd_preferencias_termo_id_fkey FOREIGN KEY (termo_id) REFERENCES public.lgpd_termos(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.mensagens
    ADD CONSTRAINT mensagens_conversa_tenant_fk FOREIGN KEY (conversa_id, tenant_id) REFERENCES public.conversas(id, tenant_id);

ALTER TABLE ONLY public.nps_respostas
    ADD CONSTRAINT nps_respostas_agendamento_id_fkey FOREIGN KEY (agendamento_id) REFERENCES public.agendamentos(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.nps_respostas
    ADD CONSTRAINT nps_respostas_contato_fk FOREIGN KEY (contato_id, tenant_id) REFERENCES public.contatos(id, tenant_id);

ALTER TABLE ONLY public.pedidos_agendamento
    ADD CONSTRAINT pedidos_agendamento_agendamento_id_fkey FOREIGN KEY (agendamento_id) REFERENCES public.agendamentos(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.pedidos_agendamento
    ADD CONSTRAINT pedidos_agendamento_contato_fk FOREIGN KEY (contato_id, tenant_id) REFERENCES public.contatos(id, tenant_id);

ALTER TABLE ONLY public.pedidos_agendamento
    ADD CONSTRAINT pedidos_agendamento_resolvido_por_fkey FOREIGN KEY (resolvido_por) REFERENCES public.perfis(id) ON DELETE SET NULL;

ALTER TABLE public.agendamentos ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.agent_knowledge_blocks ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.agent_prompts_historico ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.audit_log ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.canais_whatsapp ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.checkpoint_blobs ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.checkpoint_migrations ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.checkpoint_writes ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.checkpoints ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.contatos ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.conversas ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.crm_notificacoes ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.documentos_paciente ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.erros_motor ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.followup_queue ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.handoff_fila ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.idempotency_events ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.integracoes_oauth ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.integration_logs ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.leads ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.leads_historico_estagio ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.lgpd_audit_log ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.lgpd_decisoes_historico ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.lgpd_preferencias ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.lgpd_termos ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.links_consentimento ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.mensagens ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.nps_respostas ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.pedidos_agendamento ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.perfis ENABLE ROW LEVEL SECURITY;

CREATE POLICY perfis_select ON public.perfis FOR SELECT USING (((auth.uid() = id) OR (auth.uid() = user_id)));

CREATE POLICY perfis_select_own ON public.perfis FOR SELECT USING ((auth.uid() = user_id));

ALTER TABLE public.tenant_config ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.tenants ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.uso_ia ENABLE ROW LEVEL SECURITY;

GRANT USAGE ON SCHEMA public TO anon;
GRANT USAGE ON SCHEMA public TO authenticated;
GRANT USAGE ON SCHEMA public TO service_role;

REVOKE ALL ON FUNCTION public.podar_memoria_agente(manter integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.podar_memoria_agente(manter integer) TO service_role;

GRANT ALL ON TABLE public.agendamentos TO service_role;

GRANT ALL ON TABLE public.agent_knowledge_blocks TO service_role;

GRANT ALL ON TABLE public.agent_prompts_historico TO service_role;

GRANT ALL ON TABLE public.audit_log TO service_role;

GRANT ALL ON TABLE public.canais_whatsapp TO service_role;

GRANT ALL ON TABLE public.checkpoint_blobs TO service_role;

GRANT ALL ON TABLE public.checkpoint_migrations TO service_role;

GRANT ALL ON TABLE public.checkpoint_writes TO service_role;

GRANT ALL ON TABLE public.checkpoints TO service_role;

GRANT ALL ON TABLE public.contatos TO service_role;

GRANT ALL ON TABLE public.conversas TO service_role;

GRANT ALL ON TABLE public.crm_notificacoes TO service_role;

GRANT ALL ON TABLE public.documentos_paciente TO service_role;

GRANT SELECT,INSERT,REFERENCES,TRIGGER ON TABLE public.erros_motor TO service_role;

GRANT ALL ON TABLE public.followup_queue TO service_role;

GRANT ALL ON TABLE public.handoff_fila TO service_role;

GRANT ALL ON TABLE public.idempotency_events TO service_role;

GRANT ALL ON TABLE public.integracoes_oauth TO service_role;

GRANT ALL ON TABLE public.integration_logs TO service_role;

GRANT ALL ON TABLE public.leads TO service_role;

GRANT ALL ON TABLE public.leads_historico_estagio TO service_role;

GRANT ALL ON TABLE public.lgpd_audit_log TO service_role;

GRANT ALL ON TABLE public.lgpd_decisoes_historico TO service_role;

GRANT ALL ON TABLE public.lgpd_preferencias TO service_role;

GRANT ALL ON TABLE public.lgpd_termos TO service_role;

GRANT SELECT,INSERT,REFERENCES,DELETE,TRIGGER ON TABLE public.links_consentimento TO service_role;

GRANT ALL ON TABLE public.mensagens TO service_role;

GRANT ALL ON TABLE public.nps_respostas TO service_role;

GRANT ALL ON TABLE public.pedidos_agendamento TO service_role;

GRANT ALL ON TABLE public.perfis TO service_role;
GRANT SELECT ON TABLE public.perfis TO authenticated;

GRANT ALL ON TABLE public.tenant_config TO service_role;

GRANT ALL ON TABLE public.tenants TO service_role;

GRANT SELECT,INSERT,REFERENCES,TRIGGER ON TABLE public.uso_ia TO service_role;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO service_role;


-- Funções do backend: SECURITY DEFINER passa por cima do RLS, então só o
-- service_role executa. Sem isto o Postgres deixa PUBLIC (e o anon da API)
-- chamá-las.
REVOKE ALL ON FUNCTION public.claim_idempotency_event(text, text, uuid, text, integer) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.finish_idempotency_event(text, text, boolean, jsonb, text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.registrar_preferencia_lgpd(uuid, uuid, text, text, text, uuid, text, text, text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.obter_ou_criar_conversa FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_idempotency_event(text, text, uuid, text, integer) TO service_role;
GRANT EXECUTE ON FUNCTION public.finish_idempotency_event(text, text, boolean, jsonb, text) TO service_role;
GRANT EXECUTE ON FUNCTION public.registrar_preferencia_lgpd(uuid, uuid, text, text, text, uuid, text, text, text) TO service_role;
GRANT EXECUTE ON FUNCTION public.obter_ou_criar_conversa TO service_role;

-- Tabelas: o Supabase dá permissão ao anon/authenticated em toda tabela nova.
-- Aqui ela é retirada (o RLS sem policy já bloqueia as linhas; isto fecha
-- também a permissão). Única exceção: o painel lê o próprio perfil.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
GRANT SELECT ON public.perfis TO authenticated;
