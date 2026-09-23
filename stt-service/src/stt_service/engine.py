import gc
import logging
import threading
import time

import numpy as np

from .config import settings

logger = logging.getLogger(__name__)

_CUDA_ERROR_MARKERS = ("cuda", "cublas", "cudnn", "out of memory", "device")


def _looks_like_cuda_error(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in _CUDA_ERROR_MARKERS)


def parse_device(device: str) -> tuple[str, int]:
    """"cuda:1" -> ("cuda", 1); "cuda"/"cpu" -> Index 0. faster-whisper
    nimmt Device und Index getrennt entgegen (device_index)."""
    device = (device or "cpu").strip().lower()
    if ":" in device:
        name, _, index = device.partition(":")
        try:
            return name, int(index)
        except ValueError:
            return name, 0
    return device, 0


class WhisperEngine:
    """Kapselt faster-whisper hinter einer schmalen Schnittstelle.

    Lazy geladen (Tests laufen ohne faster-whisper, Container startet
    schnell). Zwei Besonderheiten:

    - **Automatischer CPU-Fallback:** Teilen sich LLM (LM Studio), XTTS und
      Whisper eine Karte, schlaegt Whispers CUDA-Init mit Out-of-Memory
      fehl; statt die Spracherkennung sterben zu lassen, laeuft sie dann
      (langsamer) auf der CPU weiter.
    - **Umschaltbares Device (v1.14):** Das Admin-Panel weist zur Laufzeit
      eine Karte zu ("cuda:0", "cuda:1", "cpu"); das Modell wird dann dort
      neu geladen. Deshalb wird zwischen `assigned` (gewuenscht) und
      `effective` (tatsaechlich geladen) unterschieden - nur so ist im
      Panel sichtbar, wenn der CPU-Fallback gegriffen hat."""

    def __init__(self) -> None:
        self._model = None
        self._effective: str | None = None
        self._assigned: str = settings.whisper_device
        self._compute_type: str = settings.whisper_compute_type
        # Gesetzt, wenn die Inferenz auf CUDA gekippt ist: dann bleibt die
        # Engine auf CPU, bis das Panel explizit ein Device zuweist.
        self._forced_cpu = False
        self._lock = threading.Lock()

    # ---- Laden / Device-Wechsel -------------------------------------------

    def _create_model(self, device: str, compute_type: str):
        from faster_whisper import WhisperModel

        name, index = parse_device(device)
        kwargs = {"compute_type": compute_type, "download_root": settings.models_dir}
        if name == "cuda":
            kwargs["device_index"] = index
        return WhisperModel(settings.whisper_model, device=name, **kwargs)

    def load(self, force_cpu: bool = False) -> None:
        with self._lock:
            self._load_locked(force_cpu)

    def _load_locked(self, force_cpu: bool = False) -> None:
        target = "cpu" if (force_cpu or self._forced_cpu) else self._assigned
        if self._model is not None and self._effective == target:
            return

        compute_type = "int8" if parse_device(target)[0] == "cpu" else self._compute_type
        try:
            self._model = self._create_model(target, compute_type)
            self._effective = target
        except Exception as exc:
            if parse_device(target)[0] == "cpu":
                raise
            logger.warning(
                "Whisper-Laden auf %s fehlgeschlagen (VRAM durch LLM/XTTS belegt?) - "
                "CPU-Fallback (int8, langsamer): %s",
                target, str(exc)[:200],
            )
            self._model = self._create_model("cpu", "int8")
            self._effective = "cpu"

    def set_device(self, device: str, compute_type: str | None = None) -> dict:
        """Weist zur Laufzeit ein Device zu und laedt das Modell dort neu
        (Admin-Panel, v1.14). Liefert den Status inklusive tatsaechlichem
        Device - bei VRAM-Mangel kann das trotz Zuweisung "cpu" sein."""
        with self._lock:
            self._assigned = device
            self._forced_cpu = False
            if compute_type:
                self._compute_type = compute_type
            # Altes Modell wegwerfen, damit das VRAM der alten Karte frei
            # wird, BEVOR auf der neuen geladen wird.
            self._model = None
            self._effective = None
            gc.collect()
            self._load_locked()
            return self._status_locked()

    # ---- Status ------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            return self._status_locked()

    def _status_locked(self) -> dict:
        return {
            "assigned": self._assigned,
            "effective": self._effective,
            "compute_type": self._compute_type,
            "loaded": self._model is not None,
            "model": settings.whisper_model,
        }

    # ---- Inferenz ----------------------------------------------------------

    def transcribe(self, pcm16: bytes) -> dict:
        """Erwartet PCM16 mono mit 16 kHz (Whisper-Eingabeformat laut 4.1)."""
        self.load()
        try:
            return self._run(pcm16)
        except Exception as exc:
            # CUDA kann auch erst bei der Inferenz kippen (z. B. wenn das
            # LLM NACH dem Whisper-Load ins VRAM gewachsen ist).
            if parse_device(self._effective or "cpu")[0] != "cpu" and _looks_like_cuda_error(exc):
                logger.warning(
                    "CUDA-Fehler bei der Inferenz - wechsle auf CPU: %s", str(exc)[:200]
                )
                with self._lock:
                    self._model = None
                    self._forced_cpu = True
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
            "device": self._effective,
        }


engine = WhisperEngine()
