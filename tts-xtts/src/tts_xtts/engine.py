import threading
from collections.abc import Iterator
from pathlib import Path

from .config import settings

SAMPLE_RATE = 24000  # XTTS-v2 gibt fest 24 kHz aus (passt zum Protokoll, 4.13)


class XttsEngine:
    """Kapselt XTTS-v2 (coqui-tts-Fork) mit Streaming-Inferenz.

    Lazy geladen wie die Whisper-Engine; zusaetzlich werden die
    Conditioning-Latents pro voice_id gecacht, damit das Voice-Cloning
    (~1-2s Latent-Berechnung) nur einmal pro Stimme anfaellt."""

    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()
        self._latents_cache: dict[str, tuple] = {}

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            import torch  # noqa: F401 - stellt sicher, dass torch da ist, bevor TTS importiert
            from TTS.api import TTS

            api = TTS(settings.model_name).to(settings.device)
            # Fuer inference_stream brauchen wir das rohe Xtts-Modell hinter
            # der High-Level-API (die API selbst kann kein Streaming).
            self._model = api.synthesizer.tts_model

    def _voice_path(self, voice_id: str) -> Path:
        return Path(settings.voices_dir) / f"{voice_id}.wav"

    def has_voice(self, voice_id: str) -> bool:
        return self._voice_path(voice_id).exists()

    def list_voices(self) -> list[str]:
        voices_dir = Path(settings.voices_dir)
        if not voices_dir.exists():
            return []
        return sorted(p.stem for p in voices_dir.glob("*.wav"))

    def _latents(self, voice_id: str) -> tuple:
        if voice_id not in self._latents_cache:
            gpt_cond_latent, speaker_embedding = self._model.get_conditioning_latents(
                audio_path=[str(self._voice_path(voice_id))]
            )
            self._latents_cache[voice_id] = (gpt_cond_latent, speaker_embedding)
        return self._latents_cache[voice_id]

    def stream(self, text: str, voice_id: str, language: str | None = None) -> Iterator[bytes]:
        """Text -> Iterator von PCM16-Chunks (mono, 24 kHz)."""
        self.load()
        import torch

        gpt_cond_latent, speaker_embedding = self._latents(voice_id)
        chunks = self._model.inference_stream(
            text,
            language or settings.language,
            gpt_cond_latent,
            speaker_embedding,
        )
        for chunk in chunks:
            pcm = (chunk.clamp(-1.0, 1.0) * 32767).to(torch.int16).cpu().numpy().tobytes()
            yield pcm


engine = XttsEngine()
