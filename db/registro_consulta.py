"""db/registro_consulta.py — ações do analisador de conversas (Fase 4 / B2).

Reúne as escritas que a IA dispara ao interpretar a conversa atendente↔lead:
criar o paciente terceiro (quando o agendamento é para outra pessoa), registrar
a consulta no paciente correto (promovendo a paciente e avançando o funil) e
mover o lead para "em espera" (encaixe). Espelha a lógica do CRM
(lib/pacientes/criar-paciente-terceiro.ts + /api/agendamentos), do lado do agente.

contatos vive no CORE; leads/agendamentos no VERTICAL. Data e hora no fuso da
clínica (core/fuso.py).
"""

import logging
import uuid
from datetime import datetime, timezone

from core.telefone import normalizar_telefone
from db.contatos import promover_para_paciente
from supabase import Client

log = logging.getLogger(__name__)

# Progressão do funil (espelha lib/leads/advance-stage.ts). em_espera fica de
# fora de propósito (estado paralelo de encaixe).
_ORDEM_ESTAGIO = {
    "em_conversa": 1,
    "com_atendente": 2,
    "consulta_agendada": 3,
    "compareceu_consulta": 4,
    "proposta_enviada": 5,
    "em_followup": 6,
    "fechou_tratamento": 7,
}


def _telefone_sintetico() -> str:
    return f"sem-telefone:{uuid.uuid4()}"


def criar_paciente_terceiro(
    core: Client,
    vertical: Client,
    *,
    tenant_id: str,
    nome: str,
    telefone: str | None = None,
    convenio: str | None = None,
    nascimento: str | None = None,
    indicado_por: str | None = None,
    responsavel_contato_id: str | None = None,
    responsavel_telefone: str | None = None,
    responsavel_nome: str | None = None,
) -> str:
    """Cria contato (CORE) + lead (VERTICAL) do paciente real (terceiro) e
    retorna o contato_id. Telefone sintético quando ausente/igual ao do
    responsável (contatos.telefone é NOT NULL + UNIQUE); o número real de
    notificação vai em contatos.metadata.responsavel_telefone."""
    proprio = normalizar_telefone(telefone) if telefone else None
    resp = normalizar_telefone(responsavel_telefone) if responsavel_telefone else None

    if proprio and proprio != resp:
        existe = (
            core.table("contatos")
            .select("id")
            .eq("tenant_id", tenant_id)
            .eq("telefone", proprio)
            .limit(1)
            .execute()
        )
        telefone_contato = _telefone_sintetico() if existe.data else proprio
    else:
        telefone_contato = _telefone_sintetico()

    contato = (
        core.table("contatos")
        .insert(
            {
                "tenant_id": tenant_id,
                "nome": nome,
                "telefone": telefone_contato,
                "tipo": "lead",
                "data_nascimento": nascimento or None,
                "convenio": convenio or None,
                "metadata": {
                    "responsavel_telefone": resp,
                    "responsavel_contato_id": responsavel_contato_id,
                },
            }
        )
        .execute()
    )
    contato_id = contato.data[0]["id"]

    tel_lead = resp if telefone_contato.startswith("sem-telefone:") else telefone_contato
    solicitante = responsavel_nome or responsavel_telefone
    observacoes = f"Consulta solicitada por {solicitante}." if solicitante else None

    vertical.table("leads").insert(
        {
            "tenant_id": tenant_id,
            "contato_id": contato_id,
            "nome": nome,
            "telefone": tel_lead,
            "indicado_por": indicado_por or None,
            "observacoes": observacoes,
            "origem": "whatsapp",
            "estagio": "em_conversa",
        }
    ).execute()

    log.info(
        "registro_consulta: paciente terceiro criado | contato=%s | tenant=%s",
        contato_id,
        tenant_id,
    )
    return contato_id


def _mover_estagio(vertical: Client, tenant_id: str, contato_id: str, alvo: str) -> None:
    """Move os leads do contato para `alvo` apenas se for progresso (nunca
    regride). Best-effort."""
    alvo_ordem = _ORDEM_ESTAGIO.get(alvo)
    try:
        leads = (
            vertical.table("leads")
            .select("id, estagio")
            .eq("tenant_id", tenant_id)
            .eq("contato_id", contato_id)
            .execute()
        ).data or []
        for lead in leads:
            atual = _ORDEM_ESTAGIO.get(lead.get("estagio"))
            if alvo_ordem and atual and atual < alvo_ordem:
                vertical.table("leads").update({"estagio": alvo}).eq("id", lead["id"]).eq(
                    "tenant_id", tenant_id
                ).execute()
    except Exception:
        log.warning(
            "registro_consulta: falha ao mover estágio p/ %s | contato=%s",
            alvo,
            contato_id,
            exc_info=True,
        )


def registrar_consulta(
    core: Client,
    vertical: Client,
    *,
    tenant_id: str,
    contato_id: str,
    data: str,  # YYYY-MM-DD
    hora: str,  # HH:MM
    fuso: timezone,
    convenio: str | None = None,
    observacao: str | None = None,
    atendente_id: str | None = None,
) -> str | None:
    """Insere a consulta (status=confirmado, no fuso da clínica), promove o
    contato a paciente e avança o lead para consulta_agendada. Retorna o
    agendamento_id."""
    data_hora = (
        datetime.strptime(f"{data}T{hora}", "%Y-%m-%dT%H:%M").replace(tzinfo=fuso).isoformat()
    )
    inserido = (
        vertical.table("agendamentos")
        .insert(
            {
                "tenant_id": tenant_id,
                "contato_id": contato_id,
                "atendente_id": atendente_id,
                "data_hora": data_hora,
                "tipo": "Consulta",
                "status": "confirmado",
                "convenio": convenio,
                "observacao": observacao
                or "Registrada automaticamente pela IA a partir da conversa.",
            }
        )
        .execute()
    )
    agendamento_id = inserido.data[0]["id"] if inserido.data else None

    promover_para_paciente(core, contato_id, tenant_id)
    _mover_estagio(vertical, tenant_id, contato_id, "consulta_agendada")
    log.info(
        "registro_consulta: consulta criada | agendamento=%s | contato=%s",
        agendamento_id,
        contato_id,
    )
    return agendamento_id


def marcar_fila_espera(vertical: Client, tenant_id: str, contato_id: str) -> None:
    """Coloca o lead/paciente na fila de espera (encaixe): grava fila_espera_em.
    Não mexe no estágio — a fila é uma TAG paralela (FIFO por fila_espera_em).
    Só marca se ainda não estiver na fila (preserva a ordem de chegada)."""
    agora = datetime.now(timezone.utc).isoformat()
    try:
        leads = (
            vertical.table("leads")
            .select("id, fila_espera_em")
            .eq("tenant_id", tenant_id)
            .eq("contato_id", contato_id)
            .execute()
        ).data or []
        for lead in leads:
            if not lead.get("fila_espera_em"):
                vertical.table("leads").update({"fila_espera_em": agora}).eq("id", lead["id"]).eq(
                    "tenant_id", tenant_id
                ).execute()
    except Exception:
        log.warning(
            "registro_consulta: falha ao marcar fila de espera | contato=%s",
            contato_id,
            exc_info=True,
        )
