from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    litellm_base_url: str = "http://litellm:4000"
    litellm_api_key: str = "sk-changeme"
    litellm_model: str = "gemma-4-e4b"

    weaviate_url: str = "http://weaviate:8080"
    weaviate_api_key: str | None = None
    weaviate_content_property: str = "content"
    rag_top_k: int = 5
    rag_relative_margin: float = 0.05

    admin_username: str = "admin"
    admin_password: str = "changeme"

    jwt_secret: str = "changeme-dev-secret"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7


settings = Settings()
