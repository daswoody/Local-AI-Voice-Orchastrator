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

    # Voice-Pipeline-Services (1.8-1.10); Defaults = Compose-Service-Namen
    # im ai-lab-Netzwerk (Hostname-Konvention 4.6).
    stt_base_url: str = "http://stt:8000"
    piper_base_url: str = "http://tts-piper:8000"
    xtts_base_url: str = "http://tts-xtts:8000"

    # Ausgabe-Stream laeuft einheitlich auf XTTS-Rate (Protokoll 4.13:
    # "typisch 24 kHz"); Piper-Filler wird darauf resampled, damit Filler
    # und Hauptantwort als EIN audio_chunk-Stream funktionieren (4.3).
    target_sample_rate: int = 24000
    # Eingabe laut Protokoll fix PCM16/16k (Whisper-Format).
    input_sample_rate: int = 16000

    # Filler erst ausspielen, wenn die Antwort laenger braucht als der
    # Delay - so nerven schnelle Antworten nicht mit unnoetigem Filler.
    filler_enabled: bool = True
    filler_delay_ms: int = 1200

    # ~5 Minuten PCM16/16k; schuetzt vor unbegrenzt wachsendem Puffer.
    max_audio_buffer_bytes: int = 10 * 1024 * 1024

    default_voice_id: str = "default-de-female"

    admin_username: str = "admin"
    admin_password: str = "changeme"

    jwt_secret: str = "changeme-dev-secret"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7


settings = Settings()
