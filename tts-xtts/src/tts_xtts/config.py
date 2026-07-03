from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    model_name: str = "tts_models/multilingual/multi-dataset/xtts_v2"
    # XTTS-v2 auf CPU ist unbrauchbar langsam (zweistellige Sekunden pro
    # Satz) - GPU ist hier Pflicht, ~3 GB VRAM laut Budget (4.2).
    device: str = "cuda"
    # Sample-WAVs pro voice_id ({voice_id}.wav) fuer das Voice-Cloning.
    # Deutsche Samples erstellen ist ein offener Punkt der Spezifikation.
    voices_dir: str = "/voices"
    language: str = "de"
    preload: bool = False


settings = Settings()
