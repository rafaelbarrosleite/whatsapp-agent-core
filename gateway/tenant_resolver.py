from dataclasses import dataclass, field

from db.client import get_supabase
from supabase import Client


@dataclass(frozen=True)
class TenantInfo:
    tenant_id: str
    vertical: str
    canal_id: str
    provider: str = "cloud_api"
    credenciais_ref: str | None = None
    phone_number_id: str | None = None
    token_cifrado: str | None = field(default=None, repr=False)


class TenantNaoEncontradoError(Exception):
    """Levantada quando nenhum canal ativo está cadastrado para o número recebido."""

    def __init__(self, numero: str) -> None:
        super().__init__(f"Nenhum canal ativo cadastrado para o número {numero!r}")
        self.numero = numero


class TenantResolver:
    """Resolve tenant_id e vertical a partir do número de destino do webhook.

    Recebe o número que RECEBEU a mensagem (numero_destino) e consulta
    canais_whatsapp para identificar o tenant dono daquele número.
    """

    def __init__(self, client: Client | None = None) -> None:
        # Aceita client externo para facilitar testes sem conexão real ao Supabase.
        self._client = client or get_supabase()

    def resolver(self, numero_destino: str) -> TenantInfo:
        """Retorna TenantInfo para o número recebido.

        Lança TenantNaoEncontradoError se o número não estiver cadastrado
        ou o canal estiver inativo (ativo=False).
        """
        result = (
            self._client.table("canais_whatsapp")
            .select(
                "id, tenant_id, provider, credenciais_ref, phone_number_id, token_cifrado, "
                "tenants(vertical)"
            )
            .eq("numero", numero_destino)
            .eq("ativo", True)
            .limit(1)
            .execute()
        )
        if not result.data:
            raise TenantNaoEncontradoError(numero_destino)

        row = result.data[0]
        return TenantInfo(
            tenant_id=row["tenant_id"],
            vertical=row["tenants"]["vertical"],
            canal_id=row["id"],
            provider=row["provider"],
            credenciais_ref=row.get("credenciais_ref") or None,
            phone_number_id=row.get("phone_number_id") or None,
            token_cifrado=row.get("token_cifrado") or None,
        )
