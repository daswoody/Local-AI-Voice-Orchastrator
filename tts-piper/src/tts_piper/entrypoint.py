"""Container-Entrypoint: laedt das Voice-Modell beim ersten Start ins
Models-Volume (falls noch nicht vorhanden) und startet dann uvicorn.

Bewusst reines Python (urllib) statt curl/wget - das slim-Image braucht so
keine Extra-Pakete (gleiches Muster wie der Healthcheck-Workaround in 4.8)."""

import os
import sys
import urllib.request
from pathlib import Path

from .config import settings

_HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"


def _voice_url(filename: str) -> str:
    # "de_DE-thorsten-medium" -> de/de_DE/thorsten/medium/<filename>
    locale, name, quality = settings.piper_voice.split("-", 2)
    lang = locale.split("_")[0]
    return f"{_HF_BASE}/{lang}/{locale}/{name}/{quality}/{filename}"


def ensure_voice() -> None:
    models_dir = Path(settings.models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)
    for suffix in (".onnx", ".onnx.json"):
        filename = f"{settings.piper_voice}{suffix}"
        target = models_dir / filename
        if target.exists():
            continue
        url = _voice_url(filename)
        print(f"Lade Voice-Modell: {url}", flush=True)
        tmp = target.with_suffix(target.suffix + ".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.rename(target)


if __name__ == "__main__":
    ensure_voice()
    os.execvp(
        "uvicorn",
        ["uvicorn", "tts_piper.main:app", "--host", "0.0.0.0", "--port", "8000", *sys.argv[1:]],
    )
