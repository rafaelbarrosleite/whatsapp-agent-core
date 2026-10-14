import logging
from datetime import datetime, timezone

from core.telefone import normalizar_telefone
from supabase import Client

logger = logging.getLogger(__name__)

_PAPEL: dict[str, str] = {"paciente": "usuario", "agente": "agente", "atendente": "agente"}
# "atendente" mapeia para o mesmo papel='agente' do CHECK constraint — a
# distinção "agente vs atendente humano" é feita pelo prefixo no texto e por
# perfil_id (NULL = agente; preenchido = atendente), não por um novo valor de
# papel (coluna mensagens.perfil_id em supabase/schema.sql).


def resolver_contato_id(client: Client, tenant_id: str, telefone: str) -> str | None:
    """Busca o UUID do contato por (tenant_id, telefone). Retorna None se não existe.

    Normaliza o telefone antes do match — defesa em profundidade caso algum call
    site passe um número não-canônico.
    """
    result = (
        client.table("contatos")
        .select("id")
        .eq("tenant_id", tenant_id)
        .eq("telefone", normalizar_telefone(telefone))
        .limit(1)
        .execute()
    )
    return result.data[0]["id"] if result.data else None


def obter_ou_criar_conversa(
    client: Client,
    tenant_id: str,
    contato_id: str,
    canal_id: str | None = None,
) -> str:
    """Retorna o conversa_id de uma conversa aberta existente ou cria uma nova.

    Delega para a função PL/pgSQL homônima (migration 20240620000000), que usa
    INSERT ... ON CONFLICT DO UPDATE — operação atômica que elimina a janela de
    corrida do SELECT-then-INSERT original.
    """
    result = client.rpc(
        "obter_ou_criar_conversa",
        {
            "p_tenant_id": tenant_id,
            "p_contato_id": contato_id,
            "p_canal_id": canal_id,
        },
    ).execute()
    return str(result.data)


def salvar_mensagem(
    client: Client,
    tenant_id: str,
    conversa_id: str,
    origem: str,
    conteudo: str,
    metadata: dict | None = None,
    perfil_id: str | None = None,
) -> None:
    """Persiste uma mensagem. origem: 'paciente' | 'agente' | 'atendente'.

    perfil_id identifica QUAL atendente enviou quando origem='atendente';
    fica None para mensagens do agente ou do paciente.
    """
    papel = _PAPEL.get(origem, origem)
    client.table("mensagens").insert(
        {
            "tenant_id": tenant_id,
            "conversa_id": conversa_id,
            "papel": papel,
            "conteudo": conteudo,
            "metadata": metadata or {},
            "perfil_id": perfil_id,
        }
    ).execute()


def agente_ativo(client: Client, tenant_id: str, telefone: str) -> bool:
    """Retorna True se o agente (agente) deve responder, False se um humano assumiu.

    Considera as DUAS conversas ativas possíveis — 'aberta' e 'aguardando_humano'
    — pegando a mais recente por atividade (updated_at). 'aguardando_humano'
    significa que um humano assumiu o atendimento: o agente NÃO responde, mesmo que
    agente_ativo esteja dessincronizado nessa linha. Só 'aberta' com
    agente_ativo=True libera o agente.

    True por padrão quando não há contato ou conversa ativa — novo contato
    sempre é atendido pelo agente.
    """
    contato_id = resolver_contato_id(client, tenant_id, telefone)
    if not contato_id:
        return True

    result = (
        client.table("conversas")
        .select("estado, agente_ativo")
        .eq("tenant_id", tenant_id)
        .eq("contato_id", contato_id)
        .in_("estado", ["aberta", "aguardando_humano"])
        .order("updated_at", desc=True)
        .limit(1)
        .execute()
    )
    if not result.data:
        return True

    conversa = result.data[0]
    return conversa["estado"] == "aberta" and bool(conversa["agente_ativo"])


def _buscar_conversa_ativa_row(client: Client, tenant_id: str, contato_id: str) -> dict | None:
    """Linha da conversa ATIVA mais recente do contato ('aberta' ou
    'aguardando_humano'), ou None. Mesmo critério de agente_ativo(); usada por
    resolver_conversa_para_entrada para aplicar a regra 24h/2h."""
    result = (
        client.table("conversas")
        .select("id, estado, agente_ativo, updated_at")
        .eq("tenant_id", tenant_id)
        .eq("contato_id", contato_id)
        .in_("estado", ["aberta", "aguardando_humano"])
        .order("updated_at", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


# ---------------------------------------------------------------------------
# Resolução de conversa para mensagem de entrada
# ---------------------------------------------------------------------------


def _agora() -> datetime:
    """now() em UTC. Isolado para os testes congelarem o tempo via patch."""
    return datetime.now(timezone.utc)


def _marcar_atividade(client: Client, tenant_id: str, conversa_id: str) -> None:
    # Toca updated_at sem mudar estado (mantém aguardando_humano). O trigger do
    # banco já atualiza updated_at; setamos explicitamente para clareza.
    client.table("conversas").update({"updated_at": _agora().isoformat()}).eq(
        "tenant_id", tenant_id
    ).eq("id", conversa_id).execute()


def resolver_conversa_para_entrada(
    client: Client,
    tenant_id: str,
    contato_id: str,
    canal_id: str | None = None,
) -> tuple[str, bool, str]:
    """Resolve a conversa de destino para uma mensagem de ENTRADA e decide o controle.

    Retorna (conversa_id, agente_ativo, acao). `acao` é só para log/teste:
      'nova' | 'reuso_aberta' | 'reuso_mantem_humano'

    GARANTIA CENTRAL (correção do bug 2026-07-12): a entrada de mensagem NUNCA
    altera `estado`/`agente_ativo` de uma conversa em 'aguardando_humano' nem
    reativa o agente. O estado só muda por ação explícita do atendente
    (assumir/devolver em app/atendente_router). Antes havia auto-devolução
    (sem resposta humana ou >2h) e auto-encerramento (>24h) a partir da entrada
    de mensagem — isso fazia o agente voltar a responder logo após o atendente
    assumir e ainda não ter respondido, colidindo com o humano e tirando a
    conversa da fila dele. Uma mensagem nova em 'aguardando_humano' é apenas
    salva (core/pipeline) e mostrada ao atendente, sem resposta automática.
    """
    conv = _buscar_conversa_ativa_row(client, tenant_id, contato_id)

    # Sem conversa ATIVA (nenhuma, ou a última já encerrada) → cria nova (atômico).
    if conv is None:
        return obter_ou_criar_conversa(client, tenant_id, contato_id, canal_id), True, "nova"

    if conv["estado"] == "aberta":
        return conv["id"], bool(conv["agente_ativo"]), "reuso_aberta"

    # estado == 'aguardando_humano' — humano no controle. Mantém sempre com o
    # humano; só registra atividade (updated_at), sem tocar estado/agente_ativo.
    _marcar_atividade(client, tenant_id, conv["id"])
    return conv["id"], False, "reuso_mantem_humano"


# ---------------------------------------------------------------------------
# Coexistência: a clínica respondeu pelo app WhatsApp Business no celular
# ---------------------------------------------------------------------------

ROTULO_CELULAR_CLINICA = "Clínica (celular)"


def registrar_mensagem_celular_clinica(
    client: Client,
    tenant_id: str,
    telefone_paciente: str,
    texto: str,
    message_id: str,
    canal_id: str | None = None,
) -> tuple[str, str]:
    """Registra no histórico a mensagem que a clínica mandou pelo celular e
    PAUSA o agente nessa conversa (estado 'aguardando_humano', o mesmo de quando
    a atendente assume pelo CRM). O agente só volta quando alguém devolver a
    conversa no CRM. Retorna (contato_id, conversa_id).

    Contato que ainda não existe (a clínica puxou conversa pelo celular) é
    criado sem consentimento LGPD: o agente não fala com ele enquanto a conversa
    estiver com a equipe, e o gate de consentimento continua valendo depois.
    """
    telefone = normalizar_telefone(telefone_paciente)
    contato_id = resolver_contato_id(client, tenant_id, telefone)
    if not contato_id:
        inserido = (
            client.table("contatos")
            .insert({"tenant_id": tenant_id, "telefone": telefone, "lgpd_aceito": False})
            .execute()
        )
        contato_id = inserido.data[0]["id"]

    conv = _buscar_conversa_ativa_row(client, tenant_id, contato_id)
    conversa_id = (
        conv["id"] if conv else obter_ou_criar_conversa(client, tenant_id, contato_id, canal_id)
    )
    if not conv or conv["estado"] != "aguardando_humano" or conv["agente_ativo"]:
        client.table("conversas").update({"estado": "aguardando_humano", "agente_ativo": False}).eq(
            "tenant_id", tenant_id
        ).eq("id", conversa_id).execute()

    salvar_mensagem(
        client,
        tenant_id,
        conversa_id,
        "atendente",
        f"{ROTULO_CELULAR_CLINICA}: {texto}",
        metadata={"origem": "celular_clinica", "message_id": message_id},
    )
    return contato_id, conversa_id
