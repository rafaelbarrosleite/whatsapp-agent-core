"""db/historico.py — coexistência: importa o histórico e a agenda do app
WhatsApp Business quando a clínica conecta o número à API oficial.

A Meta manda, uma única vez (até 180 dias para trás), as conversas que a clínica
já tinha no celular (webhook `history`), e a agenda de contatos do app
(webhook `smb_app_state_sync`). Regras:

- O histórico entra numa conversa ENCERRADA própria por paciente
  (metadata.origem = 'historico_whatsapp'). Não aparece no inbox, não acorda a
  agente e não mexe na conversa ativa; fica na ficha do paciente ("Atendimentos").
- Paciente que ainda não existe é criado SEM consentimento LGPD (mesma regra
  do eco do celular): o gate de consentimento continua valendo quando ele
  escrever de novo.
- Paciente que revogou o consentimento não tem o histórico importado. Limite:
  contato anonimizado perde o telefone, então não é reconhecido; por isso a
  sincronização deve ser pedida logo ao conectar o número (a Meta só aceita
  nas primeiras 24 h).
- Reimportar o mesmo trecho não duplica: mensagens já gravadas (mesmo
  whatsapp_message_id na conversa de histórico) são puladas.
- Agenda: só preenche o nome de contato que JÁ existe e está sem nome. Nunca
  cria contato a partir da agenda (ela tem família, fornecedores...) e nunca
  apaga nada quando a clínica remove um contato do celular.
"""

import logging

from channels.base import ContatoAgenda, ConversaHistorico
from core.telefone import normalizar_telefone
from db.conversas import ROTULO_CELULAR_CLINICA
from supabase import Client

logger = logging.getLogger(__name__)

ORIGEM_HISTORICO = "historico_whatsapp"
_LOTE_INSERT = 200


def _obter_ou_criar_contato(client: Client, tenant_id: str, telefone: str) -> str | None:
    """contato_id do paciente, criando-o se preciso. None = não importar
    (o paciente revogou o consentimento LGPD: o histórico dele não entra)."""
    result = (
        client.table("contatos")
        .select("id, lgpd_revogado, anonimizado")
        .eq("tenant_id", tenant_id)
        .eq("telefone", telefone)
        .limit(1)
        .execute()
    )
    if result.data:
        row = result.data[0]
        if row.get("lgpd_revogado") or row.get("anonimizado"):
            return None
        return row["id"]
    inserido = (
        client.table("contatos")
        .insert(
            {
                "tenant_id": tenant_id,
                "telefone": telefone,
                "lgpd_aceito": False,
                "metadata": {"origem": ORIGEM_HISTORICO},
            }
        )
        .execute()
    )
    return inserido.data[0]["id"]


def _obter_ou_criar_conversa_historico(
    client: Client,
    tenant_id: str,
    contato_id: str,
    canal_id: str | None,
    inicio_iso: str,
    fim_iso: str,
) -> str:
    result = (
        client.table("conversas")
        .select("id")
        .eq("tenant_id", tenant_id)
        .eq("contato_id", contato_id)
        .eq("estado", "encerrada")
        .eq("metadata->>origem", ORIGEM_HISTORICO)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]["id"]
    inserido = (
        client.table("conversas")
        .insert(
            {
                "tenant_id": tenant_id,
                "contato_id": contato_id,
                "canal_id": canal_id,
                "estado": "encerrada",
                "status": "encerrada",
                "agente_ativo": False,
                "metadata": {"origem": ORIGEM_HISTORICO},
                "started_at": inicio_iso,
                "created_at": inicio_iso,
                "updated_at": fim_iso,
            }
        )
        .execute()
    )
    return inserido.data[0]["id"]


def _ids_ja_importados(
    client: Client, tenant_id: str, conversa_id: str, ids: list[str]
) -> set[str]:
    existentes: set[str] = set()
    for i in range(0, len(ids), _LOTE_INSERT):
        trecho = ids[i : i + _LOTE_INSERT]
        result = (
            client.table("mensagens")
            .select("metadata->>whatsapp_message_id")
            .eq("tenant_id", tenant_id)
            .eq("conversa_id", conversa_id)
            .in_("metadata->>whatsapp_message_id", trecho)
            .execute()
        )
        for row in result.data or []:
            valor = row.get("whatsapp_message_id")
            if valor:
                existentes.add(valor)
    return existentes


def importar_conversa_historico(
    client: Client,
    tenant_id: str,
    canal_id: str | None,
    conversa: ConversaHistorico,
) -> int:
    """Grava as mensagens antigas de UM paciente. Retorna quantas entraram."""
    mensagens = sorted(conversa.mensagens, key=lambda m: m.timestamp)
    if not mensagens:
        return 0
    telefone = normalizar_telefone(conversa.numero_cliente)
    contato_id = _obter_ou_criar_contato(client, tenant_id, telefone)
    if contato_id is None:
        return 0
    conversa_id = _obter_ou_criar_conversa_historico(
        client,
        tenant_id,
        contato_id,
        canal_id,
        mensagens[0].timestamp.isoformat(),
        mensagens[-1].timestamp.isoformat(),
    )

    ja = _ids_ja_importados(client, tenant_id, conversa_id, [m.message_id for m in mensagens])
    linhas: list[dict] = []
    vistos: set[str] = set()
    for m in mensagens:
        if m.message_id in ja or m.message_id in vistos:
            continue
        vistos.add(m.message_id)
        linhas.append(
            {
                "tenant_id": tenant_id,
                "conversa_id": conversa_id,
                # Mesmos papéis do pipeline: paciente → 'usuario'; clínica pelo
                # celular → 'agente' com o rótulo de eco_celular.
                "papel": "usuario" if m.do_paciente else "agente",
                "conteudo": m.texto if m.do_paciente else f"{ROTULO_CELULAR_CLINICA}: {m.texto}",
                "metadata": {
                    "origem": ORIGEM_HISTORICO,
                    "whatsapp_message_id": m.message_id,
                    "tipo": m.tipo,
                },
                "created_at": m.timestamp.isoformat(),
            }
        )
    for i in range(0, len(linhas), _LOTE_INSERT):
        client.table("mensagens").insert(linhas[i : i + _LOTE_INSERT]).execute()
    return len(linhas)


def aplicar_contato_agenda(client: Client, tenant_id: str, contato: ContatoAgenda) -> bool:
    """Preenche o nome de um contato existente e sem nome. Retorna True se gravou."""
    if contato.acao != "add" or not contato.nome:
        return False
    telefone = normalizar_telefone(contato.numero_cliente)
    result = (
        client.table("contatos")
        .select("id, nome")
        .eq("tenant_id", tenant_id)
        .eq("telefone", telefone)
        .limit(1)
        .execute()
    )
    if not result.data or (result.data[0].get("nome") or "").strip():
        return False
    (
        client.table("contatos")
        .update({"nome": contato.nome})
        .eq("tenant_id", tenant_id)
        .eq("id", result.data[0]["id"])
        .execute()
    )
    return True
