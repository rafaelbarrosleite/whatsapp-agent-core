\set ON_ERROR_STOP 1
-- O esquema sobe do zero num Postgres vazio e nasce fechado:
--   - toda tabela tem RLS;
--   - o navegador (anon) não tem policy, permissão de tabela nem executa função
--     (o painel logado só lê perfis);
--   - canal só aceita a API oficial da Meta;
--   - o mesmo telefone pode ser contato de clínicas diferentes, não da mesma.

\i /supabase/tests/supabase_stub.sql
\i /supabase/schema.sql

do $$
declare
  sem_rls text;
  t1 uuid := gen_random_uuid();
  t2 uuid := gen_random_uuid();
begin
  select string_agg(tablename, ', ') into sem_rls
  from pg_tables where schemaname = 'public' and not rowsecurity;
  if sem_rls is not null then raise exception 'tabelas sem RLS: %', sem_rls; end if;

  if exists (select 1 from pg_policies where schemaname = 'public' and 'anon' = any(roles)) then
    raise exception 'existe policy para anon';
  end if;

  if exists (
    select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and (has_function_privilege('anon', p.oid, 'execute')
           or has_function_privilege('authenticated', p.oid, 'execute'))
      and p.prorettype <> 'trigger'::regtype
  ) then
    raise exception 'função executável pelo navegador';
  end if;

  if exists (
    select 1 from information_schema.role_table_grants
    where table_schema = 'public'
      and (grantee = 'anon' or (grantee = 'authenticated' and table_name <> 'perfis'))
  ) then
    raise exception 'tabela com permissão para o navegador';
  end if;

  insert into tenants (id, nome, vertical) values (t1, 'Clínica A', 'clinica'), (t2, 'Clínica B', 'clinica');

  begin
    insert into canais_whatsapp (tenant_id, numero, provider) values (t1, '+5511999990000', 'evolution');
    raise exception 'aceitou provider não oficial';
  exception when check_violation then null;
  end;

  insert into contatos (tenant_id, telefone) values (t1, '+5511999990000'), (t2, '+5511999990000');
  begin
    insert into contatos (tenant_id, telefone) values (t1, '+5511999990000');
    raise exception 'aceitou contato duplicado na mesma clínica';
  exception when unique_violation then null;
  end;
end $$;

\echo 'schema.sql: ok'
