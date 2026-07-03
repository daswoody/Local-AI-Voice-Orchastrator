from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    litellm_base_url: str = "http://litellm:4000"
    litellm_api_key: str = "sk-changeme"
    litellm_model: str = "gemma-4-e4b"

    # MCP-Gateway von LiteLLM (4.6): aggregiert alle registrierten
    # MCP-Server (z. B. mcp-time) unter einem Endpoint.
    litellm_mcp_url: str = "http://litellm:4000/mcp"
    # Tools-Liste nicht bei jedem Turn neu vom Gateway holen.
    mcp_tools_cache_seconds: int = 300
    # Obergrenze fuer LLM->Tool->LLM-Runden pro Turn (Schutz vor
    # Endlosschleifen); danach wird ohne Tools final geantwortet.
    tool_max_iterations: int = 5
    # Geraete-Tools koennen eine App-seitige Bestaetigung erfordern (4.4) -
    # der Nutzer braucht Zeit zum Tippen.
    device_tool_timeout_s: int = 60

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

    # SQLite (1.7b) + Ablagen fuer Voice-Samples und vorgenerierte Filler
    # (1.7d). /data und /voices sind Volumes im Compose; /voices ist
    # dasselbe Volume, das XTTS liest - so landet ein Sample-Upload aus dem
    # Admin-Panel direkt dort, wo XTTS es erwartet.
    database_path: str = "/data/heimai.db"
    voices_dir: str = "/voices"
    filler_cache_dir: str = "/data/filler-cache"

    # LM Studio native REST-API (ab 0.4.0) fuer das Modell-Panel (4.14).
    # Laeuft auf dem VM-Host, nicht im Docker-Netz - daher IP statt
    # Service-Name.
    lmstudio_base_url: str = "http://192.168.2.105:1234"

    # Seed-Admin fuer die Erst-Einrichtung (wird nur in eine leere DB
    # geschrieben; danach verwaltet das Admin-Panel die User).
    admin_username: str = "admin"
    admin_password: str = "changeme"

    jwt_secret: str = "changeme-dev-secret"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7


settings = Settings()
