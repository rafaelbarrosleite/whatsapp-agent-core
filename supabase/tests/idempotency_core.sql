-- Idempotência do webhook: a Meta reenvia o mesmo evento; só o primeiro é
-- processado. As funções rodam para o backend e ficam fechadas ao navegador.
\set ON_ERROR_STOP 1
\i /supabase/tests/supabase_stub.sql
\i /supabase/schema.sql
insert into public.tenants (id, nome, vertical) values ('00000000-0000-0000-0000-000000000001', 'Clínica A', 'clinica');
set role service_role;
select public.claim_idempotency_event('cloud_api', 'wamid.1', '00000000-0000-0000-0000-000000000001', 'hash', 300) as primeira;
select 1 / (not public.claim_idempotency_event('cloud_api', 'wamid.1', '00000000-0000-0000-0000-000000000001', 'hash', 300))::int as duplicado_recusado;
select public.finish_idempotency_event('cloud_api', 'wamid.1', true, null, null);
reset role;
do $$ begin
  set local role anon;
  begin
    perform public.claim_idempotency_event('cloud_api', 'x', '00000000-0000-0000-0000-000000000001', 'h', 300);
    raise exception 'anon NÃO deveria executar claim';
  exception when insufficient_privilege then null; end;
  begin
    perform 1 from public.idempotency_events;
    raise exception 'anon NÃO deveria ler idempotency_events';
  exception when insufficient_privilege then null; end;
end $$;
do $$ begin
  set local role authenticated;
  begin
    perform 1 from public.idempotency_events;
    raise exception 'authenticated NÃO deveria ler idempotency_events';
  exception when insufficient_privilege then null; end;
end $$;
\echo 'idempotency_core: OK'
