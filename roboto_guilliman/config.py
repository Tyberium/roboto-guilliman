"""Shared configuration loaded from environment."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gcp_project_id: str = "roboto-guilliman"
    gcp_location: str = "europe-west1"

    firestore_collection: str = "warhammer_rules_11th"
    firestore_database: str = "(default)"
    chat_history_collection: str = "chat_history"

    embedding_model: str = "text-embedding-004"
    llm_model: str = "gemini-2.5-flash-lite"
    llm_temperature: float = 0.3
    llm_max_output_tokens: int = 2048

    top_k: int = 8
    chunk_size: int = 1200
    chunk_overlap: int = 200

    port: int = 8080
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
