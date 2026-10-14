-- Imita o mínimo do Supabase num Postgres puro (papéis da API e auth.uid()),
-- para supabase/schema.sql subir em teste local e no CI.
-- Papéis são do cluster, não do banco: só cria se ainda não existem.
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role nologin bypassrls;
  end if;
end $$;
create schema if not exists auth;
create schema if not exists extensions;
grant usage on schema public, auth, extensions to anon, authenticated, service_role;
create function auth.uid() returns uuid language sql stable as $$
  select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
$$;
-- Igual ao Supabase: grants amplos por padrão para os papéis da API. O
-- schema.sql precisa fechar o acesso mesmo assim.
alter default privileges for role postgres in schema public
  grant all on tables to anon, authenticated, service_role;
