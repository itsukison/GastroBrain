from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str = ""

    claude_api_key: str = ""
    cohere_api: str = Field(default="", alias="COHERE_API")
    anthropic_model: str = "claude-sonnet-4-6"
    anthropic_haiku_model: str = "claude-haiku-4-5-20251001"
    embedding_model: str = "embed-multilingual-v3.0"
    rerank_model: str = "rerank-multilingual-v3.0"

    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_base_url: str = "https://jp.cloud.langfuse.com"

    slack_bot_token: str = ""
    slack_signing_secret: str = Field(default="", alias="SLACK_SIGNING_SECRET")

    supabase_jwt_secret: str = ""
    supabase_project_url: str = ""

    notepm_api_token: str = Field(default="", alias="NOTEPM_API_TOKEN")
    notepm_team_subdomain: str = "gastroduce-jp"
    notepm_cutoff_date: str = "2024-05-26"
    notepm_managers_file: Path = PROJECT_ROOT / "config" / "notepm_managers.yaml"
    notepm_excluded_notes_file: Path = PROJECT_ROOT / "config" / "notepm_excluded_notes.yaml"

    # Google Drive — meeting-transcript ingestion (company-wide "全社権限" folder).
    gdrive_folder_id: str = "16E6m0EBnir5jo9jrHZUQGmL7hHq9z8QE"
    # If set, impersonate this service account (with drive.readonly) instead of
    # using direct user ADC. Needed when the Workspace blocks user-OAuth Drive.
    gdrive_impersonate_sa: str = Field(default="", alias="GDRIVE_IMPERSONATE_SA")

    web_history_window: int = 10
    web_allowed_origins: str = ""

    # MCP — comma-separated "label:token" pairs. Empty disables the /mcp route.
    gastrobrain_mcp_tokens: str = ""
    gastrobrain_mcp_enabled: bool = True

    # OAuth 2.1 authorization server for /mcp/ — see oauth.py.
    # Empty client_id disables the OAuth surface (static tokens still work).
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    # HMAC keys. Mint with: python -c "import secrets; print(secrets.token_urlsafe(64))"
    # Separate keys so a compromise of one doesn't forge the other.
    gastrobrain_oauth_jwt_key: str = ""
    gastrobrain_oauth_state_key: str = ""
    # The public-facing base URL of this Cloud Run service. Used as the OAuth
    # issuer and as the audience for access tokens. No trailing slash.
    gastrobrain_oauth_issuer: str = ""

    chunk_target_chars: int = 500
    chunk_overlap_chars: int = 80
    retrieve_top_k_dense: int = 50
    retrieve_top_k_lexical: int = 50
    retrieve_top_k_fused: int = 25
    retrieve_top_k_final: int = 8
    rerank_score_floor: float = 0.20

    env: str = "dev"
    log_level: str = "INFO"

    def require(self, *fields: str) -> None:
        missing = [f for f in fields if not getattr(self, f, None)]
        if missing:
            raise RuntimeError(
                f"Missing required env vars: {', '.join(m.upper() for m in missing)}. "
                f"Add them to {PROJECT_ROOT / '.env'}"
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def __getattr__(name: str) -> Any:
    if name == "settings":
        return get_settings()
    raise AttributeError(name)
