from db.client import get_supabase
from supabase import Client


def get_vertical_supabase() -> Client:
    """Retorna o cliente Supabase das tabelas do vertical (painel CRM).

    Banco único desde 03/10/2026 :
    as tabelas do CORE e as do vertical vivem no mesmo banco, então este é o
    mesmo cliente de get_supabase(). O nome ficou para não mexer nos ~37
    módulos que o usam. Mesma regra de segurança: service_role ignora RLS,
    então todo acesso DEVE filtrar explicitamente por tenant_id no código.
    """
    return get_supabase()
