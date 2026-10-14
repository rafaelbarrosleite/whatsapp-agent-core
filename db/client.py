from functools import lru_cache

from app.config import settings
from supabase import Client, ClientOptions, create_client

_TIMEOUT = 10  # segundos — evita travamento indefinido na VPS com latência alta


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    """Retorna o cliente Supabase com service_role key (bypassa RLS).

    ATENÇÃO: service_role ignora Row Level Security. Todo acesso via este cliente
    DEVE filtrar explicitamente por tenant_id no código — nunca confie que o banco
    fará o isolamento por conta própria aqui.
    """
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise RuntimeError(
            "SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY são obrigatórios. "
            "Configure o arquivo .env antes de usar o banco de dados."
        )
    return create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
        options=ClientOptions(
            postgrest_client_timeout=_TIMEOUT,
            storage_client_timeout=_TIMEOUT,
        ),
    )
