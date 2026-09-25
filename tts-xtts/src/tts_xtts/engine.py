import gc
import logging
import queue
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000  # XTTS-v2 gibt fest 24 kHz aus (passt zum Protokoll, 4.13)

_DONE = object()  # Ende-Markierung in der Chunk-Queue von stream()

# Device-Wert fuer "ausgeschaltet" (v1.19): kein Modell, kein VRAM.
OFF = "off"


class EngineBusy(RuntimeError):
    """Eine andere Synthese belegt das Modell laenger als erlaubt."""


class EngineDisabled(RuntimeError):
    """Im Admin-Panel ausgeschaltet (GPUs -> Aus): Das Modell bleibt
    entladen, bis wieder eine Karte (oder die CPU) zugewiesen wird."""


class XttsEngine:
    """Kapselt XTTS-v2 (coqui-tts-Fork) mit Streaming-Inferenz.

    Lazy geladen wie die Whisper-Engine; zusaetzlich werden die
    Conditioning-Latents pro voice_id gecacht, damit das Voice-Cloning
    (~1-2s Latent-Berechnung) nur einmal pro Stimme anfaellt.

    Umschaltbares Device (v1.14): Das Admin-Panel weist zur Laufzeit eine
    Karte zu ("cuda:0", "cuda:1", "cpu") oder schaltet XTTS aus ("off",
    v1.19: Modell entladen, Synthesen bekommen 503). `assigned` ist die Zuweisung,
    `effective` das tatsaechlich genutzte Device - weichen sie ab, hat der
    Fallback gegriffen (Karte voll). ACHTUNG: Die gecachten Latents sind
    Tensoren auf der alten Karte und muessen beim Wechsel mit weg.

    Immer nur EINE Synthese (v1.16): XTTS-v2 ist nicht reentrant. coqui-tts
    legt pro Synthese die Text-Konditionierung am Modell selbst ab
    (gpt.gpt_inference.cached_prefix_emb) und liest sie bei JEDEM
    generierten Audio-Token wieder aus - unter anderem fuer dessen
    Positions-Index. Startet eine zweite Synthese, waehrend die erste noch
    streamt (z. B. mehrere "Audio generieren"-Klicks im Panel), rechnet die
    erste mit Text und Laenge der zweiten weiter: verschobenes Timing
    (Zeitlupe, Fetzen am Ende) oder ein negativer Positions-Index
    (IndexError, auf CUDA ein Device-Assert). Weitere Synthesen warten
    deshalb, bis die laufende fertig ist."""

    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()
        # Exklusiv fuer eine Synthese (siehe oben). Reihenfolge beim Sperren
        # immer _synth_lock -> _lock, nie umgekehrt.
        self._synth_lock = threading.Lock()
        self._latents_cache: dict[str, tuple] = {}
        self._assigned: str = settings.device
        self._effective: str | None = None

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        timeout = settings.synth_lock_timeout_s
        if not self._synth_lock.acquire(timeout=timeout):
            raise EngineBusy(f"XTTS ist belegt - eine andere Synthese laeuft seit ueber {timeout:.0f}s")
        try:
            yield
        finally:
            self._synth_lock.release()

    # ---- Laden / Device-Wechsel -------------------------------------------

    def load(self) -> None:
        with self._lock:
            if self._assigned == OFF:
                raise EngineDisabled(
                    "XTTS ist im Admin-Panel ausgeschaltet (GPUs -> XTTS) - "
                    "erst wieder eine Karte zuweisen"
                )
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
        """Weist zur Laufzeit ein Device zu und laedt das Modell dort neu -
        bei "off" bleibt es entladen (VRAM frei bis auf den CUDA-Kontext des
        Prozesses, den erst ein Container-Neustart freigibt).

        Wartet auf eine laufende Synthese: Modell und Latents mitten in
        einer Generierung auszutauschen, waere derselbe Fehler in Gruen."""
        with self._exclusive(), self._lock:
            self._assigned = device
            self._model = None
            self._effective = None
            # Latents zeigen auf die alte Karte -> mit verwerfen, sonst
            # knallt die naechste Synthese mit einem Device-Mismatch.
            self._latents_cache.clear()
            self._free_memory()
            if device != OFF:
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
        """Text -> Iterator von PCM16-Chunks (mono, 24 kHz).

        Die Generierung laeuft in einem eigenen Thread, der die Engine
        exklusiv haelt und die Chunks in eine Queue legt. Warum nicht direkt
        im Generator? Dann haelt der Generator die Sperre ueber seine yields
        hinweg - und bricht der HTTP-Client ab (Barge-in), schliesst
        Starlette ihn nicht: Die Sperre hing bis zum naechsten Garbage-
        Collector-Lauf, im Test lief der naechste Request nach 120 s in den
        503. So ist sie spaetestens mit dem Ende der Generierung frei; wird
        der Generator geschlossen, stoppt die Generierung schon beim
        naechsten Chunk."""
        chunks: queue.Queue = queue.Queue()
        stop = threading.Event()
        threading.Thread(
            target=self._produce, args=(text, voice_id, language, chunks, stop),
            name="xtts-stream", daemon=True,
        ).start()
        try:
            while (item := chunks.get()) is not _DONE:
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            stop.set()

    def _produce(
        self, text: str, voice_id: str, language: str | None,
        chunks: queue.Queue, stop: threading.Event,
    ) -> None:
        try:
            with self._exclusive():
                self.load()
                gpt_cond_latent, speaker_embedding = self._latents(voice_id)
                generator = self._model.inference_stream(
                    text,
                    language or settings.language,
                    gpt_cond_latent,
                    speaker_embedding,
                )
                for chunk in generator:
                    if stop.is_set():
                        break
                    chunks.put(_tensor_to_pcm16(chunk))
        except Exception as exc:
            chunks.put(exc)
        finally:
            chunks.put(_DONE)

    def synthesize(
        self, text: str, voice_id: str, language: str | None = None,
        temperature: float | None = None,
    ) -> bytes:
        """Text -> komplettes PCM16 (mono, 24 kHz), nicht streamend (v1.16).

        Fuer die Filler-Vorgenerierung, wo Qualitaet statt Latenz zaehlt:
        inference() berechnet die GPT-Latents in einem vollstaendigen
        Durchlauf und dekodiert die ganze Aeusserung am Stueck - ohne die
        Chunk-Naehte (Crossfades) und das gekappte Ende des Streaming-Pfads.
        `temperature` erlaubt dem Orchestrator, bei einem Neuversuch
        vorsichtiger zu sampeln."""
        options = {} if temperature is None else {"temperature": temperature}
        with self._exclusive():
            self.load()
            gpt_cond_latent, speaker_embedding = self._latents(voice_id)
            result = self._model.inference(
                text,
                language or settings.language,
                gpt_cond_latent,
                speaker_embedding,
                **options,
            )
        return _float_to_pcm16(result["wav"])


def _tensor_to_pcm16(samples) -> bytes:
    """Float-Tensor im Bereich [-1, 1] -> PCM16-Bytes (Streaming-Chunks)."""
    import torch

    return (samples.clamp(-1.0, 1.0) * 32767).to(torch.int16).cpu().numpy().tobytes()


def _float_to_pcm16(samples) -> bytes:
    """numpy-Float-Array [-1, 1] (so liefert inference() das WAV) -> PCM16."""
    import numpy as np

    return (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


engine = XttsEngine()
