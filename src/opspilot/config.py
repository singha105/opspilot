"""Runtime settings, loaded from environment variables prefixed with ``OPSPILOT_``."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    """All OpsPilot configuration. Every field is documented in ``.env.example``."""

    model_config = SettingsConfigDict(
        env_prefix="OPSPILOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    log_level: LogLevel = "INFO"
    log_json: bool = True

    llm_model: str = "qwen3:4b"
    ollama_base_url: str = "http://localhost:11434"

    weaviate_url: str = "http://localhost:8090"

    admin_context: str = Field(
        default="k3d-opspilot",
        description="kubectl context used by fault injection only, never by the agent.",
    )
    demo_namespace: str = "shop"
    secrets_dir: Path = Path(".secrets")
    scenarios_dir: Path = Path("faults/scenarios")
    demo_base_dir: Path = Path("demo/k8s/base")

    @property
    def reader_kubeconfig(self) -> Path:
        """Kubeconfig of the read-only ``opspilot-reader`` ServiceAccount."""
        return self.secrets_dir / "opspilot-reader.kubeconfig"

    @property
    def operator_kubeconfig(self) -> Path:
        """Kubeconfig of the narrowly scoped ``opspilot-operator`` ServiceAccount."""
        return self.secrets_dir / "opspilot-operator.kubeconfig"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
