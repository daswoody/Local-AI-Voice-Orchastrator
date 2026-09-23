"""GPU-Verteilung der Dienste (4.2, NEU in v1.14).

Zwei Aufgaben:

1. **Karten anzeigen** - per NVML (nvidia-ml-py) liest der Orchestrator
   Name, VRAM-Belegung und Compute-Capability jeder Karte. Das umfasst
   AUCH den Verbrauch von LM Studio auf dem Host, weil NVML den echten
   Treiberzustand liest und nicht nur den eigenen Prozess. Genau das ist
   die Basis, um spontan zu entscheiden, welcher Dienst wohin soll.
2. **Dienste zuweisen** - STT und XTTS koennen ihr Modell zur Laufzeit auf
   einer anderen Karte (oder der CPU) neu laden. Der Orchestrator merkt
   sich die Zuweisung in app_settings und stellt sie nach einem
   Container-Neustart wieder her.

Bewusst NICHT ueber Docker: Die GPU-Zuweisung eines Containers ist beim
Start eingefroren: sie zu aendern hiesse Container neu erstellen, den
Docker-Socket in den Orchestrator zu mounten (faktisch Root auf dem Host)
und sich mit Coolify zu ueberwerfen, das die Compose beim naechsten Deploy
zurueckschreibt. Stattdessen sehen die Container ALLE Karten (compose:
count all) und die Anwendung waehlt ihr Device selbst."""

import asyncio
import logging

import httpx

from .. import repos
from ..config import settings

logger = logging.getLogger(__name__)

_SETTING_PREFIX = "gpu_device_"

# Ab Turing (Compute Capability 7.0) rechnen die Tensor Cores float16
# schnell; Pascal und aelter (z. B. GTX 1080) emulieren es und sind damit
# langsamer als int8. Der Orchestrator gibt dem STT-Dienst deshalb beim
# Kartenwechsel den passenden Compute-Type mit.
_FLOAT16_MIN_CAPABILITY = 7.0

# Dienste-Registry: Was laeuft wo, und was davon koennen wir steuern?
SERVICES: dict[str, dict] = {
    "stt": {
        "label": "Spracherkennung (Whisper)",
        "base_url": lambda: settings.stt_base_url,
        "controllable": True,
        "note": "~1,5-3 GB VRAM je nach Modellgroesse; faellt bei VRAM-Mangel auf CPU zurueck.",
    },
    "tts-xtts": {
        "label": "Sprachausgabe (XTTS-v2)",
        "base_url": lambda: settings.xtts_base_url,
        "controllable": True,
        "note": "~3 GB VRAM. Auf CPU technisch moeglich, aber sehr langsam (Sekunden pro Satz).",
    },
    "tts-piper": {
        "label": "Filler-Stimme (Piper)",
        "base_url": lambda: settings.piper_base_url,
        "controllable": False,
        "fixed_device": "cpu",
        "note": "Bewusst CPU: Piper ist auch ohne GPU sub-sekundenschnell (4.3).",
    },
    "llm": {
        "label": "Sprachmodell (LM Studio)",
        "base_url": None,
        "controllable": False,
        "external": True,
        "note": (
            "Laeuft auf dem Host, nicht als Container - der Orchestrator kann die "
            "Karte nicht umstellen. Auswahl direkt in LM Studio (GPU-Einstellungen "
            "bzw. CUDA_VISIBLE_DEVICES des LM-Studio-Prozesses)."
        ),
    },
}

CONTROLLABLE = [name for name, spec in SERVICES.items() if spec["controllable"]]


def _base_url(name: str) -> str:
    return str(SERVICES[name]["base_url"]()).rstrip("/")


# ---- Karten (NVML) ----------------------------------------------------------


def list_gpus() -> dict:
    """Karten inkl. VRAM-Belegung. Ohne NVML-Zugriff (Orchestrator-Container
    ohne GPU-Freigabe) wird das sauber gemeldet statt zu krachen - das Panel
    bleibt dann bedienbar, zeigt nur keine Zahlen."""
    try:
        import pynvml
    except ImportError:
        return {
            "available": False,
            "reason": "nvidia-ml-py nicht installiert (Image neu bauen).",
            "gpus": [],
        }

    try:
        pynvml.nvmlInit()
    except Exception as exc:
        return {
            "available": False,
            "reason": (
                "Kein GPU-Zugriff im Orchestrator-Container. In der Compose den "
                "devices-Block mit capabilities [utility] ergaenzen und neu "
                f"deployen. ({str(exc)[:120]})"
            ),
            "gpus": [],
        }

    gpus = []
    try:
        for index in range(pynvml.nvmlDeviceGetCount()):
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):  # nvidia-ml-py < 12 liefert bytes
                name = name.decode("utf-8", "replace")
            try:
                major, minor = pynvml.nvmlDeviceGetCudaComputeCapability(handle)
                capability = float(f"{major}.{minor}")
            except Exception:
                capability = None
            gpus.append(
                {
                    "index": index,
                    "device": f"cuda:{index}",
                    "name": name,
                    "memory_total_mb": memory.total // 1024 // 1024,
                    "memory_used_mb": memory.used // 1024 // 1024,
                    "memory_free_mb": memory.free // 1024 // 1024,
                    "compute_capability": capability,
                    "compute_type": recommended_compute_type(capability),
                }
            )
    except Exception as exc:
        logger.exception("NVML-Abfrage fehlgeschlagen")
        return {"available": False, "reason": f"NVML-Fehler: {str(exc)[:200]}", "gpus": []}
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass

    return {"available": True, "reason": "", "gpus": gpus}


def recommended_compute_type(capability: float | None) -> str:
    """float16 nur auf Karten, die es nativ koennen (Turing+)."""
    if capability is not None and capability < _FLOAT16_MIN_CAPABILITY:
        return "int8_float16"
    return "float16"


def _compute_type_for(device: str, gpus: list[dict]) -> str | None:
    if not device.startswith("cuda"):
        return None
    index = int(device.partition(":")[2] or 0)
    for gpu in gpus:
        if gpu["index"] == index:
            return gpu["compute_type"]
    return None


# ---- Zuweisungen ------------------------------------------------------------


def assigned_device(name: str) -> str | None:
    """Vom Admin gewaehlte Karte (None = noch nie gesetzt, dann gilt der
    Container-Default aus der Compose)."""
    return repos.get_setting(_SETTING_PREFIX + name)


def store_assignment(name: str, device: str) -> None:
    repos.set_setting(_SETTING_PREFIX + name, device)


async def service_status(name: str) -> dict:
    """Was meldet der Dienst selbst? assigned = ihm zugewiesen, effective =
    wo das Modell wirklich liegt (kann per CPU-Fallback abweichen)."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{_base_url(name)}/v1/device")
            response.raise_for_status()
            return {"reachable": True, **response.json()}
    except Exception as exc:
        return {"reachable": False, "error": str(exc)[:200]}


async def set_service_device(name: str, device: str, compute_type: str | None = None) -> dict:
    """Weist einem Dienst eine Karte zu und laedt sein Modell dort neu.
    Grosszuegiger Timeout: XTTS braucht fuer den Reload einige Sekunden."""
    payload: dict = {"device": device}
    if compute_type and name == "stt":
        payload["compute_type"] = compute_type

    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(f"{_base_url(name)}/v1/device", json=payload)
        if response.status_code >= 400:
            raise RuntimeError(
                f"{name} hat den Wechsel auf {device} abgelehnt "
                f"({response.status_code}): {response.text[:300]}"
            )
        return response.json()


async def apply_device(name: str, device: str) -> dict:
    """Zuweisung setzen UND merken (ueberlebt Container-Neustarts)."""
    gpus = list_gpus().get("gpus", [])
    status = await set_service_device(name, device, _compute_type_for(device, gpus))
    store_assignment(name, device)
    return status


async def overview() -> dict:
    """Alles, was das Panel braucht: Karten + Dienste nebeneinander."""
    gpu_info = list_gpus()
    statuses = await asyncio.gather(
        *(service_status(name) for name in CONTROLLABLE), return_exceptions=True
    )
    live = dict(zip(CONTROLLABLE, statuses))

    services = []
    for name, spec in SERVICES.items():
        entry = {
            "name": name,
            "label": spec["label"],
            "controllable": spec["controllable"],
            "external": spec.get("external", False),
            "note": spec.get("note", ""),
            "assigned": assigned_device(name),
            "effective": spec.get("fixed_device"),
            "reachable": None,
        }
        status = live.get(name)
        if isinstance(status, dict):
            entry["reachable"] = status.get("reachable", False)
            if status.get("reachable"):
                entry["assigned"] = status.get("assigned") or entry["assigned"]
                entry["effective"] = status.get("effective")
                entry["loaded"] = status.get("loaded", False)
                entry["compute_type"] = status.get("compute_type")
            else:
                entry["error"] = status.get("error")
        services.append(entry)

    return {"nvml": {k: v for k, v in gpu_info.items() if k != "gpus"},
            "gpus": gpu_info["gpus"], "services": services}


async def restore_assignments() -> None:
    """Gespeicherte Zuweisungen wiederherstellen (Start des Orchestrators und
    nach jedem Panel-Aufruf). Wichtig: NUR wenn der Dienst etwas anderes
    zugewiesen hat als gespeichert - ist er per CPU-Fallback ausgewichen,
    stimmt `assigned` weiterhin und wir fassen ihn nicht an (sonst wuerden
    wir den Fallback in einer Endlosschleife zurueckdrehen)."""
    for name in CONTROLLABLE:
        stored = assigned_device(name)
        if not stored:
            continue
        status = await service_status(name)
        if not status.get("reachable") or status.get("assigned") == stored:
            continue
        try:
            logger.info(
                "Stelle GPU-Zuweisung fuer %s wieder her: %s (Dienst meldete %s)",
                name, stored, status.get("assigned"),
            )
            gpus = list_gpus().get("gpus", [])
            await set_service_device(name, stored, _compute_type_for(stored, gpus))
        except Exception:
            # Komfort, kein Muss: Der Dienst laeuft auf seinem Default weiter.
            logger.warning("Konnte GPU-Zuweisung fuer %s nicht wiederherstellen", name)
