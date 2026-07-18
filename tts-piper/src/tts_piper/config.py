from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Deutsches Standard-Voice-Modell (rhasspy/piper-voices auf HuggingFace).
    # "thorsten-medium" ist die gaengige deutsche Stimme mit gutem
    # Qualitaet/Latenz-Verhaeltnis fuer die Filler-Rolle (4.3).
    piper_voice: str = "de_DE-thorsten-medium"
    models_dir: str = "/models"
    piper_bin: str = "piper"
    # Timeout pro Synthese - Filler-Phrasen sind kurz, Piper ist
    # Sub-Sekunden-schnell (4.3); alles ueber 30s ist ein Haenger.
    synthesis_timeout_s: int = 30


settings = Settings()
