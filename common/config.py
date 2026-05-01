"""Platform configuration from environment variables."""

from pydantic_settings import BaseSettings


class PlatformConfig(BaseSettings):
    """Global platform settings. Read from env vars (case-insensitive)."""

    kb_dsn: str = "postgresql://agent_kb@agent-kb.amer.dev:5433/agent_kb"
    llm_manager_url: str = "http://llm-manager-backend.llm-manager.svc:8081"
    llm_manager_api_key: str = ""
    llm_registration_secret: str = ""
    global_max_iterations: int = 30
    agent_image_tag: str = "latest"
    agent_image_repo: str = "amerenda/mycroft"
    argo_namespace: str = "mycroft"
    argo_ui_url: str = "https://argo.amer.dev"
    github_token: str = ""
    sazed_url: str = ""  # e.g. http://sazed.sazed.svc:8000 — empty = reports disabled
    # When True, runtime writes failure / iteration-limit notices to KB under
    # /notifications/alex/{task_id}. Env: KB_NOTIFICATIONS_ENABLED=true
    kb_notifications_enabled: bool = False

    model_config = {"env_prefix": "", "case_sensitive": False}
