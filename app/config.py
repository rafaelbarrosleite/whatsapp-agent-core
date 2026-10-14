from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Fase 3 — LLM
    anthropic_api_key: str | None = None

    # Fase 1 — banco de dados
    supabase_url: str | None = None
    supabase_service_role_key: str | None = None

    # Connection string direta do Postgres (para PostgresSaver do LangGraph)
    # Usar Transaction Pooler (porta 6543) com ?prepare_threshold=0
    supabase_db_url: str | None = None

    # Banco único desde 03/10/2026: as tabelas do vertical (painel CRM) estão
    # no mesmo banco. VERTICAL_SUPABASE_URL e VERTICAL_SERVICE_ROLE_KEY não são
    # mais lidas (db/vertical_client.py usa o cliente acima).

    # Fase 5 — observabilidade
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # Fase 4 — validação de webhook
    webhook_secret: str | None = None

    # Canal WhatsApp: SOMENTE a API oficial da Meta (Cloud API). A Evolution
    # API foi removida em 26/09/2026 por decisão do fundador — não reintroduzir.
    # Fase 4 — Cloud API (Meta)
    whatsapp_verify_token: str | None = None
    # Credencial da PLATAFORMA, só para os alertas internos de erro
    # (core/error_notifier.py → MONITOR_PHONE). Nunca usada para falar com
    # paciente: cada clínica envia pelo próprio canal (canais_whatsapp.
    # credenciais_ref → <REF>_PHONE_NUMBER_ID e <REF>_TOKEN), sem fallback (1.8).
    whatsapp_cloud_token: str | None = None
    whatsapp_phone_number_id: str | None = None
    # App Secret da Meta App — usado para validar HMAC X-Hub-Signature-256 nos webhooks.
    # Credencial de nível da App (não por tenant): todos os webhooks Cloud API chegam
    # com o mesmo App Secret independentemente do número de destino.
    whatsapp_app_secret: str | None = None
    # App Secret do segundo app da Meta (Tech Provider). Na
    # migração os dois apps mandam webhooks para a mesma URL; aceita os dois.
    whatsapp_app_secret_secundario: str | None = None

    @property
    def whatsapp_app_secrets(self) -> list[str]:
        """App Secrets aceitos na validação HMAC dos webhooks da Meta."""
        return [s for s in (self.whatsapp_app_secret, self.whatsapp_app_secret_secundario) if s]

    # Monitoramento — número que recebe alertas de erro via WhatsApp (lê MONITOR_PHONE)
    monitor_phone: str | None = None

    # Google Calendar
    google_client_id: str = ""
    google_client_secret: str = ""
    # Chave AES-256 (32 bytes em base64) que cifra os tokens OAuth do Google em
    # integracoes_oauth (lê TOKENS_CRYPTO_KEY). Mesmo valor no painel CRM.
    # Formato compartilhado: security/cifra_tokens.py ↔ lib/cifra-tokens.ts.
    tokens_crypto_key: str | None = None

    # URL do CRM (painel CRM em Next.js) — usado para callbacks internos como NPS
    crm_url: str | None = None

    # Chave de autenticação para chamadas internas ao CRM
    # (mesmo valor configurado no painel CRM)
    internal_api_key: str | None = None

    # Transcrição de áudio (Groq Whisper)
    groq_api_key: str | None = None

    # Rajada de mensagens (gateway/rajada.py): o agente espera o paciente ficar
    # este tempo sem escrever antes de responder, e junta o que chegou numa
    # resposta só. O teto garante resposta mesmo se ele não parar de digitar.
    debounce_mensagens_segundos: float = 3.0
    debounce_maximo_segundos: float = 12.0

    # Nome do agente quando o tenant não define tenants.config.nome_agente
    # (prefixo "Nome: " das mensagens e saudação inicial). Fica na config, não
    # no core, para o motor não carregar a persona de um cliente (lê
    # NOME_AGENTE_PADRAO).
    nome_agente_padrao: str = "Assistente"

    # App
    app_env: str = "development"
    log_level: str = "INFO"


settings = Settings()
