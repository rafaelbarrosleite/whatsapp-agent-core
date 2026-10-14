from app.config import settings


async def get_checkpointer():
    """Retorna o checkpointer do LangGraph.

    Com SUPABASE_DB_URL configurado → AsyncPostgresSaver (memória persiste entre restarts).
    Sem ela → MemorySaver (fallback para desenvolvimento local).

    Não usa lru_cache (não compatível com async) — o lifespan chama uma vez e injeta
    o singleton nos grafos dos webhooks.

    ATENÇÃO: AsyncPostgresSaver usa conexão direta ao Postgres — bypassa RLS.
    As tabelas do LangGraph (checkpoints, writes) ficam no banco único (o do CRM,
    desde 03/10/2026; esquema em supabase/schema.sql).
    """
    if not settings.supabase_db_url:
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()

    import psycopg
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    # prepare_threshold=None: DESABILITA prepared statements (psycopg v3).
    #   threshold=0 significa "preparar imediatamente" (errado para pooler),
    #   threshold=None significa "nunca preparar" (correto para Transaction Pooler).
    # autocommit=True: setup() usa CREATE INDEX CONCURRENTLY, que não pode rodar
    #   dentro de bloco de transação implícito do psycopg.
    conn = await psycopg.AsyncConnection.connect(
        settings.supabase_db_url,
        prepare_threshold=None,
        autocommit=True,
    )
    return AsyncPostgresSaver(conn)
