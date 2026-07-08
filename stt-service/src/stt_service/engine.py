import logging
import threading
import time

import numpy as np

from .config import settings

logger = logging.getLogger(__name__)

_CUDA_ERROR_MARKERS = ("cuda", "cublas", "cudnn", "out of memory", "device")


def _looks_like_cuda_error(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in _CUDA_ERROR_MARKERS)


class WhisperEngine:
    """Kapselt faster-whisper hinter einer schmalen Schnittstelle.

    Lazy geladen (Tests laufen ohne faster-whisper, Container startet
    schnell). NEU: automatischer CPU-Fallback - auf der 11-GB-Karte teilen
    sich LLM (LM Studio), XTTS und Whisper das VRAM. Ist ein grosses
    lokales LLM geladen, schlaegt Whispers CUDA-Init mit Out-of-Memory
    fehl; statt die Spracherkennung sterben zu lassen, laeuft sie dann
    (langsamer) auf der CPU weiter."""

    def __init__(self) -> None:
        self._model = None
        self._device: str | None = None
        self._lock = threading.Lock()

    def _create_model(self, device: str, compute_type: str):
        from faster_whisper import WhisperModel

        return WhisperModel(
            settings.whisper_model,
            device=device,
            compute_type=compute_type,
            download_root=settings.models_dir,
        )

    def load(self, force_cpu: bool = False) -> None:
        with self._lock:
            if self._model is not None and not (force_cpu and self._device != "cpu"):
                return

            device = "cpu" if force_cpu else settings.whisper_device
            compute_type = "int8" if device == "cpu" else settings.whisper_compute_type
            try:
                self._model = self._create_model(device, compute_type)
                self._device = device
            except Exception as exc:
                if device == "cpu":
                    raise
                logger.warning(
                    "Whisper-Laden auf %s fehlgeschlagen (VRAM durch LLM/XTTS belegt?) - "
                    "CPU-Fallback (int8, langsamer): %s",
                    device, str(exc)[:200],
                )
                self._model = self._create_model("cpu", "int8")
                self._device = "cpu"

    def transcribe(self, pcm16: bytes) -> dict:
        """Erwartet PCM16 mono mit 16 kHz (Whisper-Eingabeformat laut 4.1)."""
        self.load()
        try:
            return self._run(pcm16)
        except Exception as exc:
            # CUDA kann auch erst bei der Inferenz kippen (z. B. wenn das
            # LLM NACH dem Whisper-Load ins VRAM gewachsen ist).
            if self._device != "cpu" and _looks_like_cuda_error(exc):
                logger.warning(
                    "CUDA-Fehler bei der Inferenz - wechsle dauerhaft auf CPU: %s",
                    str(exc)[:200],
                )
                with self._lock:
                    self._model = None
                self.load(force_cpu=True)
                return self._run(pcm16)
            raise

    def _run(self, pcm16: bytes) -> dict:
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0

        started = time.monotonic()
        segments, info = self._model.transcribe(
            audio,
            language=settings.language,
            beam_size=5,
            vad_filter=True,
        )
        text = "".join(segment.text for segment in segments).strip()
        processing_ms = int((time.monotonic() - started) * 1000)

        return {
            "text": text,
            "language": info.language,
            "audio_duration_ms": int(info.duration * 1000),
            "processing_ms": processing_ms,
            "device": self._device,
        }


engine = WhisperEngine()
