import hashlib
import logging

from supabase import Client

logger = logging.getLogger(__name__)


def payload_sha256(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


def external_event_id(message_id: str | None, raw_body: bytes) -> str:
    """Use the provider ID when present; fall back to a stable payload digest."""
    normalized = (message_id or "").strip()
    return normalized if normalized else f"sha256:{payload_sha256(raw_body)}"


def claim_event(
    client: Client,
    *,
    provider: str,
    event_id: str,
    tenant_id: str,
    raw_body: bytes,
) -> bool:
    result = client.rpc(
        "claim_idempotency_event",
        {
            "p_provider": provider,
            "p_external_event_id": event_id,
            "p_tenant_id": tenant_id,
            "p_payload_hash": payload_sha256(raw_body),
            "p_stale_after_seconds": 300,
        },
    ).execute()
    return bool(result.data)


def finish_event(
    client: Client,
    *,
    provider: str,
    event_id: str,
    success: bool,
    error: str | None = None,
) -> None:
    client.rpc(
        "finish_idempotency_event",
        {
            "p_provider": provider,
            "p_external_event_id": event_id,
            "p_success": success,
            "p_result": {"consumed": success},
            "p_error": error,
        },
    ).execute()
