"""tools/verticals/clinica/leads.py — tool criar_lead.

Cadastra um lead comercial na tabela `leads` do banco VERTICAL, com as
informações coletadas pelo agente na conversa. Verifica antes se já existe lead
para o contato (todo lead que chega por WhatsApp tem contato_id vinculado) e
NÃO duplica. É a tool que o system prompt referencia para "cadastrar um novo
lead somente após coletar e confirmar todas as informações".
"""

import logging

from langchain_core.tools import BaseTool, tool

from core.registry import ToolFactory, ToolRegistry
from core.telefone import normalizar_telefone
from db.vertical_client import get_vertical_supabase
from supabase import Client

log = logging.getLogger(__name__)

# Espelha o CHECK de leads.origem (migration 20260709000000). Valor fora da
# lista cairia no CHECK do banco — normalizamos para 'outro' por segurança.
_ORIGENS_VALIDAS = {
    "whatsapp",
    "google",
    "instagram",
    "indicacao_paciente",
    "encaminhamento_medico",
    "convenio_parceria",
    "ligacao",
    "outro",
}


def _make_criar_lead(vertical_client: Client) -> ToolFactory:
    """Retorna uma factory que cria a tool criar_lead com o cliente injetado."""

    def factory(tenant_id: str) -> BaseTool:
        # tenant_id fica lacrado na closure — o LLM nunca o vê.
        @tool
        def criar_lead(
            contato_id: str,
            nome: str,
            telefone: str,
            origem: str = "whatsapp",
            indicado_por: str | None = None,
            observacoes: str | None = None,
        ) -> str:
            """Cadastra um novo lead com as informações coletadas na conversa.
            Use SOMENTE depois de coletar e CONFIRMAR todas as informações do
            paciente. A tool verifica sozinha se já existe lead para este
            contato — se existir, NÃO cria outro (não duplica).

            contato_id: id do contato (você já tem em contexto).
            nome: nome completo do paciente que será atendido.
            telefone: telefone do paciente que será atendido (confirme antes —
              pode ser diferente de quem está no WhatsApp).
            origem: como o paciente chegou. Um de: 'whatsapp' (padrão),
              'indicacao_paciente', 'encaminhamento_medico', 'google',
              'instagram', 'convenio_parceria', 'ligacao', 'outro'.
            indicado_por: nome de quem indicou, quando houver indicação.
            observacoes: consolide aqui tudo que não tem campo próprio —
              particular ou convênio (e qual), data de nascimento, se já tem
              exame, se teve queda/torção, e qualquer contexto relevante."""
            origem_norm = origem if origem in _ORIGENS_VALIDAS else "outro"
            telefone_canonico = normalizar_telefone(telefone)

            # Já existe lead? Não duplica. Verifica por contato_id E por telefone
            # canônico: o telefone pega o lead criado no CRM (que pode não ter
            # contato_id), e a normalização garante que "67999991234" e
            # "+5567999991234" (com/sem +55) sejam reconhecidos como o mesmo.
            existente = (
                vertical_client.table("leads")
                .select("id")
                .eq("tenant_id", tenant_id)  # filtro forçado — nunca omitir
                .eq("contato_id", contato_id)
                .limit(1)
                .execute()
            )
            if not existente.data:
                existente = (
                    vertical_client.table("leads")
                    .select("id")
                    .eq("tenant_id", tenant_id)
                    .eq("telefone", telefone_canonico)
                    .limit(1)
                    .execute()
                )
            if existente.data:
                log.info(
                    "criar_lead: lead já existe (por contato ou telefone) — não "
                    "duplica | tenant=%s | contato=%s",
                    tenant_id,
                    contato_id,
                )
                return (
                    "Este paciente já está cadastrado na base de leads — não criei "
                    "outro. Pode seguir normalmente."
                )

            try:
                vertical_client.table("leads").insert(
                    {
                        "tenant_id": tenant_id,
                        "contato_id": contato_id,
                        "nome": nome,
                        "telefone": telefone_canonico,
                        "origem": origem_norm,
                        "indicado_por": indicado_por,
                        "observacoes": observacoes,
                        "estagio": "em_conversa",
                    }
                ).execute()
            except Exception:
                log.error(
                    "criar_lead: falha ao inserir lead | tenant=%s | contato=%s",
                    tenant_id,
                    contato_id,
                    exc_info=True,
                )
                return (
                    "Não consegui cadastrar o lead agora por um problema técnico. "
                    "Continue o atendimento normalmente."
                )

            log.info(
                "criar_lead: lead criado | tenant=%s | contato=%s | origem=%s",
                tenant_id,
                contato_id,
                origem_norm,
            )
            return "Lead cadastrado com sucesso."

        return criar_lead

    return factory


def registrar(registry: ToolRegistry, vertical_client: Client | None = None) -> None:
    """Registra a tool criar_lead como exclusiva do vertical clínica.

    Aceita um client externo para facilitar testes sem conexão real ao Supabase.
    """
    _client = vertical_client or get_vertical_supabase()
    registry.register_vertical("clinica", _make_criar_lead(_client))
