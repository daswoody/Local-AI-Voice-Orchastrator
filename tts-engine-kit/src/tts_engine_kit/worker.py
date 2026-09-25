"""Modell-Unterprozess und seine Steuerung (Engine-Vertrag v1).

Warum ein Unterprozess: Ein Python-Prozess, der einmal CUDA benutzt hat,
behaelt seinen CUDA-Kontext (einige hundert MB VRAM) bis zu seinem Ende -
Modell loeschen und empty_cache() helfen dagegen nicht (so bei XTTS, v1.19).
Laeuft das Modell in einem eigenen Prozess, heisst "entladen" einfach
"Prozess beenden", und die Karte ist danach wirklich leer. Der HTTP-Teil
(Hauptprozess) importiert nie torch und bleibt dabei erreichbar.

Zustaende: preparing (Gewichte laden o. ae., Backend.prepare) -> idle
(bereit, Modell nicht geladen) -> loading -> ready; off = ausgeschaltet;
error = Laden/Prozess gescheitert (detail sagt warum). Immer nur EINE
Synthese zur Zeit - weitere warten bis busy_wait_s, dann EngineBusy."""

import importlib
import itertools
import logging
import multiprocessing
import os
import queue
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass

from .backend import Voice

logger = logging.getLogger(__name__)

OFF = "off"
DEVICE_PATTERN = r"^(off|cpu|cuda(:\d+)?)$"
# spawn statt fork: CUDA vertraegt kein fork, und der Unterprozess soll
# nichts vom Hauptprozess erben ausser dem, was er braucht.
_CTX = multiprocessing.get_context("spawn")


class EngineOff(RuntimeError):
    """Im Admin-Panel ausgeschaltet."""


class EngineBusy(RuntimeError):
    """Eine andere Synthese laeuft laenger als erlaubt."""


class EngineNotReady(RuntimeError):
    """Modell (noch) nicht geladen: Download, Laden oder Ladefehler."""


class EngineBadRequest(ValueError):
    """Die Engine lehnt die Anfrage ab (z. B. Sprache, fehlendes Sample)."""


class EngineFailed(RuntimeError):
    """Synthese im Modell-Prozess gescheitert oder Prozess abgestuerzt."""


@dataclass
class WorkerSettings:
    # Start-Device; das Admin-Panel weist zur Laufzeit ein anderes zu.
    device: str = "cuda:0"
    # Modell schon beim Start laden? Default nein: Es laedt beim Aktivieren
    # im Panel bzw. beim ersten Probehoeren - so belegt ein frisch
    # deployter Container nicht ungefragt VRAM neben XTTS.
    preload: bool = False
    busy_wait_s: float = 60.0
    load_wait_s: float = 150.0
    chunk_timeout_s: float = 300.0
    voice_cache: int = 8
    # Nach einem Ladefehler (z. B. VRAM voll) nicht bei jedem Request neu
    # versuchen - ein Geraetewechsel im Panel versucht es sofort.
    load_retry_s: float = 60.0

    @classmethod
    def from_env(cls) -> "WorkerSettings":
        env = os.environ
        device = env.get("ENGINE_DEVICE", "cuda:0").strip() or "cuda:0"
        if not re.match(DEVICE_PATTERN, device):
            raise ValueError(f"ENGINE_DEVICE={device!r}: erlaubt sind off, cpu, cuda, cuda:N")
        return cls(
            device=device,
            preload=env.get("ENGINE_PRELOAD", "false").strip().lower() in ("1", "true", "yes", "on"),
            busy_wait_s=float(env.get("ENGINE_BUSY_WAIT_S", "60")),
            load_wait_s=float(env.get("ENGINE_LOAD_WAIT_S", "150")),
            chunk_timeout_s=float(env.get("ENGINE_CHUNK_TIMEOUT_S", "300")),
            voice_cache=int(env.get("ENGINE_VOICE_CACHE", "8")),
            load_retry_s=float(env.get("ENGINE_LOAD_RETRY_S", "60")),
        )


def load_backend_class(path: str):
    """"paket.modul:Klasse" -> Klasse."""
    module_name, _, attribute = path.partition(":")
    return getattr(importlib.import_module(module_name), attribute)


def _describe(exc: BaseException) -> str:
    text = str(exc).strip()
    return (f"{type(exc).__name__}: {text}" if text else type(exc).__name__)[:500]


# ---- Unterprozess ------------------------------------------------------------------


def _child_main(conn, backend_path: str, kwargs: dict, device: str, cache_size: int) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [modell] %(name)s: %(message)s")
    try:
        backend = load_backend_class(backend_path)(**kwargs)
        effective = backend.load(device) or device
    except BaseException as exc:
        logging.exception("Laden auf %s fehlgeschlagen", device)
        conn.send(("load_error", _describe(exc)))
        return
    logging.info("Modell geladen (%s)", effective)
    conn.send(("ready", effective))

    prepared: OrderedDict = OrderedDict()
    pending: list = []
    while True:
        if pending:
            message = pending.pop(0)
        else:
            try:
                message = conn.recv()
            except (EOFError, OSError):
                return
        if message[0] == "stop":
            return
        if message[0] != "synth":
            continue  # z. B. ein "cancel" fuer eine laengst fertige Anfrage
        _, request_id, job = message
        try:
            voice = _prepared_voice(backend, prepared, job.get("voice"), cache_size)
            chunks = backend.synthesize(job["text"], job.get("language"), voice, job.get("instruction"))
            for rate, pcm in chunks:
                # Zwischen zwei Chunks: Client weg (cancel) oder Stopp?
                cancelled = False
                while conn.poll():
                    control = conn.recv()
                    if control[0] == "stop":
                        return
                    if control[0] == "cancel" and control[1] == request_id:
                        cancelled = True
                    elif control[0] == "synth":
                        pending.append(control)
                if cancelled:
                    break
                conn.send(("chunk", request_id, int(rate), bytes(pcm)))
            conn.send(("done", request_id))
        except Exception as exc:
            logging.exception("Synthese fehlgeschlagen")
            conn.send(("error", request_id, _describe(exc), isinstance(exc, ValueError)))


def _terminate(proc, conn) -> None:
    """Modell-Prozess beenden - erst hoeflich, dann hart."""
    if proc is None:
        return
    try:
        conn.send(("stop",))
    except OSError:
        pass
    proc.join(timeout=10)
    if proc.is_alive():  # z. B. mitten im Laden: liest keine Nachrichten
        proc.terminate()
        proc.join(timeout=5)
    if proc.is_alive():
        proc.kill()
        proc.join(timeout=5)
    try:
        conn.close()
    except OSError:
        pass


def _prepared_voice(backend, prepared: OrderedDict, voice: dict | None, cache_size: int):
    if not voice:
        return None
    key = voice["key"]
    if key in prepared:
        prepared.move_to_end(key)
        return prepared[key]
    result = backend.prepare_voice(Voice(key=key, wav=voice["wav"], transcript=voice.get("transcript")))
    prepared[key] = result
    while len(prepared) > max(cache_size, 1):
        prepared.popitem(last=False)
    return result


# ---- Hauptprozess --------------------------------------------------------------------


class EngineWorker:
    def __init__(self, backend_path: str, backend_cls, settings: WorkerSettings) -> None:
        self._backend_path = backend_path
        self._backend_cls = backend_cls
        self.settings = settings
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        # Genau eine Synthese bzw. ein Geraetewechsel zur Zeit.
        self._exclusive = threading.Lock()
        self._ids = itertools.count(1)
        self.assigned = settings.device
        self.state = "preparing"
        self.effective: str | None = None
        self.detail = ""
        self._kwargs: dict | None = None
        self._want_loaded = settings.preload
        self._retry_after = 0.0
        self._proc = None
        self._conn = None
        self._active: tuple[int, queue.Queue] | None = None

    # ---- Lebenszyklus

    def start(self) -> None:
        threading.Thread(target=self._prepare, name="tts-prepare", daemon=True).start()

    def shutdown(self) -> None:
        with self._lock:
            detached = self._detach_locked()
        _terminate(*detached)

    def _prepare(self) -> None:
        try:
            kwargs = self._backend_cls.prepare() or {}
        except Exception as exc:
            logger.exception("Vorbereitung der Engine fehlgeschlagen")
            with self._lock:
                self.state = "error"
                self.detail = f"Vorbereitung fehlgeschlagen: {_describe(exc)}"
                self._retry_after = time.monotonic() + self.settings.load_retry_s
                self._changed.notify_all()
            return
        with self._lock:
            self._kwargs = kwargs
            self.state = OFF if self.assigned == OFF else "idle"
            if self.assigned != OFF and self._want_loaded:
                self._spawn_locked()
            self._changed.notify_all()

    def _retry_prepare_locked(self, force: bool = False) -> None:
        """Gescheiterte Vorbereitung (z. B. Download) erneut versuchen - aus
        einem Request erst nach load_retry_s, per Geraetewechsel sofort."""
        if self._kwargs is None and self.state == "error" and (
                force or time.monotonic() >= self._retry_after):
            self.state = "preparing"
            self.start()

    def _spawn_locked(self) -> None:
        parent_conn, child_conn = _CTX.Pipe()
        proc = _CTX.Process(
            target=_child_main,
            args=(child_conn, self._backend_path, self._kwargs, self.assigned, self.settings.voice_cache),
            name="tts-modell",
        )
        proc.start()
        child_conn.close()
        self._proc, self._conn = proc, parent_conn
        self.state, self.effective, self.detail = "loading", None, ""
        threading.Thread(target=self._read, args=(proc, parent_conn), name="tts-modell-reader",
                         daemon=True).start()

    def _detach_locked(self) -> tuple:
        """Prozess abkoppeln (der Reader haelt ihn danach nicht mehr fuer
        aktuell); beenden dann AUSSERHALB der Sperre - das kann dauern, und
        Status-Abfragen sollen derweil antworten."""
        proc, conn = self._proc, self._conn
        self._proc = self._conn = None
        self.effective = None
        if proc is not None and self.state in ("loading", "ready"):
            self.state = "idle"
        return proc, conn

    def _read(self, proc, conn) -> None:
        """Nachrichten des Modell-Prozesses verteilen, bis er endet."""
        while True:
            try:
                message = conn.recv()
            except (EOFError, OSError):
                break
            kind = message[0]
            if kind in ("ready", "load_error"):
                with self._lock:
                    if proc is self._proc:
                        if kind == "ready":
                            self.state, self.effective, self.detail = "ready", message[1], ""
                        else:
                            self.state = "error"
                            self.detail = f"Laden fehlgeschlagen: {message[1]}"
                            self._retry_after = time.monotonic() + self.settings.load_retry_s
                        self._changed.notify_all()
                continue
            active = self._active
            if active is not None and message[1] == active[0]:
                active[1].put(message)

        proc.join(timeout=5)
        with self._lock:
            if proc is not self._proc:
                return  # absichtlich gestoppt
            self._proc = self._conn = None
            self.effective = None
            if self.state != "error":
                self.state = "error"
                self.detail = f"Modell-Prozess unerwartet beendet (Exit-Code {proc.exitcode})"
            detail = self.detail
            active = self._active
            self._changed.notify_all()
        if active is not None:
            active[1].put(("error", active[0], detail, False))

    # ---- Steuerung

    def status(self) -> dict:
        with self._lock:
            return self._status_locked()

    def _status_locked(self) -> dict:
        return {
            "assigned": self.assigned,
            "effective": self.effective,
            "loaded": self.state == "ready",
            "state": self.state,
            "detail": self.detail,
            "model": self._backend_cls.info().name,
        }

    def set_device(self, device: str) -> dict:
        """Neues Device: Modell-Prozess beenden und - ausser bei "off" - dort
        neu laden. Wartet bis load_wait_s aufs Laden (Status sagt, ob fertig)."""
        if not self._exclusive.acquire(timeout=self.settings.busy_wait_s):
            raise EngineBusy("Eine Synthese laeuft noch - Geraetewechsel gleich erneut versuchen")
        try:
            with self._lock:
                detached = self._detach_locked()
            _terminate(*detached)
            with self._lock:
                self.assigned = device
                self._retry_after = 0.0
                self.detail = ""
                self._want_loaded = device != OFF
                if self._kwargs is None:
                    # Noch beim Vorbereiten: Laedt danach von selbst.
                    self._retry_prepare_locked(force=True)
                    return self._status_locked()
                if device == OFF:
                    self.state = OFF
                    self._changed.notify_all()
                    return self._status_locked()
                self._spawn_locked()
                deadline = time.monotonic() + self.settings.load_wait_s
                while self.state == "loading" and (remaining := deadline - time.monotonic()) > 0:
                    self._changed.wait(timeout=remaining)
                return self._status_locked()
        finally:
            self._exclusive.release()

    def _not_ready_text_locked(self) -> str:
        if self.state == "preparing":
            return "Engine bereitet sich noch vor (Download der Gewichte) - bitte warten"
        if self.state == "loading":
            return "Modell wird geladen - gleich noch einmal versuchen"
        if self.state == "error":
            return self.detail or "Modell nicht geladen"
        return "Modell nicht geladen"

    def synthesize(self, job: dict, load_timeout_s: float = 0.0) -> Iterator[tuple[int, bytes]]:
        """Generator: (Samplerate, PCM16)-Chunks. Ist das Modell nicht geladen,
        wird es geladen; gewartet wird bis load_timeout_s (0 = gar nicht,
        dann EngineNotReady - der Orchestrator nimmt so lange seine
        Rueckfallebene). Wer den Generator vorzeitig schliesst, bricht die
        Synthese nach dem laufenden Chunk ab."""
        if not self._exclusive.acquire(timeout=self.settings.busy_wait_s):
            raise EngineBusy(f"Engine ist belegt - eine andere Synthese laeuft seit ueber "
                             f"{self.settings.busy_wait_s:.0f} s")
        request = None
        try:
            with self._lock:
                if self.assigned == OFF:
                    raise EngineOff("Engine ist im Admin-Panel ausgeschaltet (GPUs)")
                self._want_loaded = True
                if self._kwargs is None:
                    self._retry_prepare_locked()
                elif self._proc is None and time.monotonic() >= self._retry_after:
                    self._spawn_locked()
                deadline = time.monotonic() + max(load_timeout_s, 0.0)
                while self.state != "ready" and self.state != "error":
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._changed.wait(timeout=remaining)
                if self.state != "ready":
                    raise EngineNotReady(self._not_ready_text_locked())
                request = (next(self._ids), queue.Queue())
                self._active = request
                conn = self._conn
            conn.send(("synth", request[0], job))
            finished = False
            try:
                while True:
                    try:
                        message = request[1].get(timeout=self.settings.chunk_timeout_s)
                    except queue.Empty:
                        finished = True
                        detail = (f"Modell antwortet seit {self.settings.chunk_timeout_s:.0f} s "
                                  "nicht - Prozess beendet, der naechste Aufruf laedt neu")
                        with self._lock:
                            detached = self._detach_locked()
                            self.state, self.detail = "error", detail
                        _terminate(*detached)
                        raise EngineFailed(detail)
                    if message[0] == "chunk":
                        yield message[2], message[3]
                    elif message[0] == "done":
                        finished = True
                        return
                    else:
                        finished = True
                        raise (EngineBadRequest if message[3] else EngineFailed)(message[2])
            finally:
                if not finished:
                    try:
                        conn.send(("cancel", request[0]))
                    except OSError:
                        pass
        finally:
            with self._lock:
                if request is not None and self._active is request:
                    self._active = None
            self._exclusive.release()
