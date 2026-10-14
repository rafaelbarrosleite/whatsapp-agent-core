"""app/tools_setup.py — registra no Tool Registry as tools que o motor oferece.

Um lugar só para o servidor (app/main.py) e o chat de terminal
(scripts/chat_cli.py) carregarem exatamente o mesmo conjunto: tools genéricas
de CRM + tools do vertical clínica. Um vertical novo entra aqui.
"""

from core.registry import ToolRegistry
from db.client import get_supabase
from db.vertical_client import get_vertical_supabase


def registrar_tools(registry: ToolRegistry) -> None:
    from tools.crm.contatos import registrar as registrar_crm
    from tools.verticals.clinica.agenda_demo import registrar as registrar_agenda
    from tools.verticals.clinica.conhecimento import registrar as registrar_conhecimento
    from tools.verticals.clinica.handoff import registrar as registrar_handoff
    from tools.verticals.clinica.leads import registrar as registrar_leads
    from tools.verticals.clinica.localizacao import registrar as registrar_localizacao
    from tools.verticals.clinica.pedido_agendamento import registrar as registrar_pedido

    core, vertical = get_supabase(), get_vertical_supabase()
    registrar_crm(registry, client=core)
    registrar_conhecimento(registry, vertical)
    registrar_agenda(registry, vertical, core)
    registrar_handoff(registry, vertical)
    registrar_leads(registry, vertical)
    registrar_localizacao(registry, vertical)
    registrar_pedido(registry, vertical)
