"""Container-Entrypoint fuer Breeze TTS 2 (Test-Engine, v1.17).

Laedt beim ersten Start die Modellgewichte von Hugging Face ins
Models-Volume (gleiches Muster wie tts-piper) und startet dann den
offiziellen Streaming-Server (breeze_infer.api). Der Orchestrator spricht
ihn direkt an: POST /v1/audio/speech (Multipart), Antwort PCM16/24 kHz.

Zusaetzliche Argumente (compose `command:`) reicht der Entrypoint an den
Server durch - z. B. --fast-all, das laut Breeze aber ~14,4 GB VRAM braucht."""

import os
import sys
from pathlib import Path

MODEL_REPO = os.environ.get("BREEZE_MODEL_REPO", "BreezeBlue/breeze-tts-2")
MODEL_DIR = Path(os.environ.get("BREEZE_MODEL_DIR", "/models/breeze-tts-2"))
PORT = os.environ.get("BREEZE_PORT", "7860")


def ensure_model() -> None:
    # Marker statt "Verzeichnis existiert": Ein abgebrochener Download
    # hinterlaesst ein halbes Verzeichnis - snapshot_download setzt beim
    # naechsten Start dort fort.
    marker = MODEL_DIR / ".download-complete"
    if marker.exists():
        return
    from huggingface_hub import snapshot_download

    print(f"Lade Breeze-TTS-2-Gewichte ({MODEL_REPO}) nach {MODEL_DIR} - "
          "einmalig, mehrere GB ...", flush=True)
    try:
        snapshot_download(repo_id=MODEL_REPO, local_dir=MODEL_DIR)
    except Exception:
        print("Download fehlgeschlagen. Falls Hugging Face eine Lizenz-Zustimmung "
              f"verlangt: auf https://huggingface.co/{MODEL_REPO} zustimmen und "
              "HF_TOKEN (Lese-Token dieses Kontos) setzen.", flush=True)
        raise
    marker.touch()


if __name__ == "__main__":
    ensure_model()
    os.execvp(sys.executable, [
        sys.executable, "-m", "breeze_infer.api", str(MODEL_DIR),
        "--host", "0.0.0.0", "--port", PORT, *sys.argv[1:],
    ])
