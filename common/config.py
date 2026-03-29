"""Platform configuration from environment variables."""

from pydantic_settings import BaseSettings


class PlatformConfig(BaseSettings):
    """Global platform settings. Read from env vars (case-insensitive)."""

    kb_dsn: str = "postgresql://agent_kb@agent-kb.amer.home:5433/agent_kb"
    llm_manager_url: str = "http://llm-manager-backend.llm-manager.svc:8081"
    llm_manager_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    global_max_iterations: int = 5
    agent_image_tag: str = "latest"
    agent_image_repo: str = "amerenda/mycroft"
    argo_namespace: str = "mycroft"
    github_token: str = ""

    model_config = {"env_prefix": "", "case_sensitive": False}
