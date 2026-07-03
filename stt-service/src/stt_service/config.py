from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Whisper Medium laut VRAM-Budget (4.2); auf CPU zum ersten Testen ist
    # "small" oder "base" deutlich fluessiger.
    whisper_model: str = "medium"
    # "cuda" oder "cpu". float16 passt zu CUDA, int8 zu CPU.
    whisper_device: str = "cuda"
    whisper_compute_type: str = "float16"
    # Deutsch fest vorgegeben statt Sprach-Detektion: spart Latenz und
    # verhindert Fehl-Detektionen bei kurzen Kommandos.
    language: str = "de"
    models_dir: str = "/models"
    # Engine direkt beim Start laden statt beim ersten Request (JIT).
    preload: bool = False


settings = Settings()
