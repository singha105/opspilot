"""Runtime settings, loaded from environment variables prefixed with ``OPSPILOT_``."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
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
    weaviate_grpc_port: int = 50052

    knowledge_dir: Path = Path("knowledge")
    data_dir: Path = Path("data")
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    reranker_model: str = "ms-marco-MiniLM-L-12-v2"
    default_store: Literal["weaviate", "chroma", "pinecone"] = "weaviate"
    # Retrieval defaults chosen by the evaluation (ADR-0004): best nDCG@5 under 300 ms p95.
    retrieval_mode: Literal["dense", "keyword", "hybrid"] = "hybrid"
    retrieval_alpha: float = Field(default=0.5, ge=0.0, le=1.0)
    retrieval_rerank: bool = False
    retrieval_k: int = Field(default=6, ge=1, le=20)

    # Optional Pinecone (free Starter tier). Read from PINECONE_API_KEY as well.
    pinecone_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("OPSPILOT_PINECONE_API_KEY", "PINECONE_API_KEY"),
    )
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"

    admin_context: str = Field(
        default="k3d-opspilot",
        description="kubectl context used by fault injection only, never by the agent.",
    )
    demo_namespace: str = "shop"
    secrets_dir: Path = Path(".secrets")
    scenarios_dir: Path = Path("faults/scenarios")
    demo_base_dir: Path = Path("demo/k8s/base")

    @property
    def chroma_dir(self) -> Path:
        """Persistent Chroma data and its BM25 indexes."""
        return self.data_dir / "chroma"

    @property
    def model_dir(self) -> Path:
        """Downloaded embedding and reranker models."""
        return self.data_dir / "models"

    @property
    def embedding_cache(self) -> Path:
        """SQLite cache of passage embeddings keyed by text hash."""
        return self.data_dir / "cache" / "embeddings.sqlite"

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
