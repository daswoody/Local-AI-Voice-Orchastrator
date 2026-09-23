import gc
import logging
import threading
from collections.abc import Iterator
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000  # XTTS-v2 gibt fest 24 kHz aus (passt zum Protokoll, 4.13)


class XttsEngine:
    """Kapselt XTTS-v2 (coqui-tts-Fork) mit Streaming-Inferenz.

    Lazy geladen wie die Whisper-Engine; zusaetzlich werden die
    Conditioning-Latents pro voice_id gecacht, damit das Voice-Cloning
    (~1-2s Latent-Berechnung) nur einmal pro Stimme anfaellt.

    Umschaltbares Device (v1.14): Das Admin-Panel weist zur Laufzeit eine
    Karte zu ("cuda:0", "cuda:1", "cpu"). `assigned` ist die Zuweisung,
    `effective` das tatsaechlich genutzte Device - weichen sie ab, hat der
    Fallback gegriffen (Karte voll). ACHTUNG: Die gecachten Latents sind
    Tensoren auf der alten Karte und muessen beim Wechsel mit weg."""

    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()
        self._latents_cache: dict[str, tuple] = {}
        self._assigned: str = settings.device
        self._effective: str | None = None

    # ---- Laden / Device-Wechsel -------------------------------------------

    def load(self) -> None:
        with self._lock:
            self._load_locked()

    def _load_locked(self) -> None:
        if self._model is not None:
            return
        import torch  # noqa: F401 - stellt sicher, dass torch da ist, bevor TTS importiert
        from TTS.api import TTS

        target = self._assigned
        try:
            api = TTS(settings.model_name).to(target)
        except Exception as exc:
            if target == "cpu":
                raise
            # Gleiche Philosophie wie bei Whisper: lieber langsam als tot.
            # XTTS auf CPU ist allerdings WIRKLICH langsam - das Panel zeigt
            # die Abweichung deshalb als Warnung an.
            logger.warning(
                "XTTS-Laden auf %s fehlgeschlagen (VRAM belegt?) - CPU-Fallback "
                "(sehr langsam): %s", target, str(exc)[:200],
            )
            api = TTS(settings.model_name).to("cpu")
            target = "cpu"
        # Fuer inference_stream brauchen wir das rohe Xtts-Modell hinter
        # der High-Level-API (die API selbst kann kein Streaming).
        self._model = api.synthesizer.tts_model
        self._effective = target

    def set_device(self, device: str) -> dict:
        """Weist zur Laufzeit ein Device zu und laedt das Modell dort neu."""
        with self._lock:
            self._assigned = device
            self._model = None
            self._effective = None
            # Latents zeigen auf die alte Karte -> mit verwerfen, sonst
            # knallt die naechste Synthese mit einem Device-Mismatch.
            self._latents_cache.clear()
            self._free_memory()
            self._load_locked()
            return self._status_locked()

    @staticmethod
    def _free_memory() -> None:
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # pragma: no cover - torch fehlt nur in Tests
            pass

    # ---- Status ------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            return self._status_locked()

    def _status_locked(self) -> dict:
        return {
            "assigned": self._assigned,
            "effective": self._effective,
            "loaded": self._model is not None,
            "model": settings.model_name,
        }

    # ---- Stimmen / Inferenz ------------------------------------------------

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
        # mtime des Samples mitfuehren: Wird es ersetzt (Upload im
        # Admin-Panel), werden die Latents automatisch neu berechnet -
        # sonst wuerde die Stimme bis zum Container-Neustart alt klingen.
        mtime = self._voice_path(voice_id).stat().st_mtime_ns
        cached = self._latents_cache.get(voice_id)
        if cached is None or cached[0] != mtime:
            gpt_cond_latent, speaker_embedding = self._model.get_conditioning_latents(
                audio_path=[str(self._voice_path(voice_id))]
            )
            self._latents_cache[voice_id] = (mtime, (gpt_cond_latent, speaker_embedding))
        return self._latents_cache[voice_id][1]

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
