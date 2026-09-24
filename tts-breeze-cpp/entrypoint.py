"""Container-Entrypoint fuer Breeze-TTS-2.cpp (v1.18).

Laedt beim ersten Start die GGUF-Gewichte von Hugging Face ins Models-Volume
und startet dann breeze-server. Nur Standardbibliothek (urllib), damit das
Laufzeit-Image schlank bleibt.

Welche Datei: BREEZE_GGUF_FILE (Dateiname im Repo oder absoluter Pfad zu einer
eigenen GGUF), sonst die Variante BREEZE_GGUF_QUANT (Default q8_0) aus
BREEZE_GGUF_REPO. Zusaetzliche Argumente (compose `command:`) gehen an den
Server, z. B. --verbose."""

import json
import os
import sys
import urllib.request
from pathlib import Path

HF = "https://huggingface.co"
REPO = os.environ.get("BREEZE_GGUF_REPO", "HoppouAI/Breeze-TTS-2.cpp")
QUANT = os.environ.get("BREEZE_GGUF_QUANT", "q8_0")
FILE = os.environ.get("BREEZE_GGUF_FILE", "").strip()
MODELS_DIR = Path(os.environ.get("BREEZE_MODELS_DIR", "/models"))
PORT = os.environ.get("BREEZE_PORT", "7860")


def _request(url: str, headers: dict | None = None) -> urllib.request.Request:
    headers = dict(headers or {})
    token = os.environ.get("HF_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def pick_file(files: list[str], quant: str) -> str | None:
    """GGUF der gewuenschten Quantisierung - ohne die experimentellen
    "-dd"-Varianten (quantisierter Depth-Decoder). Bei mehreren Treffern
    gewinnt der kuerzeste Name, also die Basisvariante."""
    quant = quant.lower()
    hits = [
        f for f in files
        if f.lower().endswith(".gguf") and quant in Path(f).name.lower()
        and "-dd" not in Path(f).name.lower()
    ]
    return min(hits, key=len) if hits else None


def _repo_files() -> list[str]:
    with urllib.request.urlopen(_request(f"{HF}/api/models/{REPO}"), timeout=60) as response:
        return [entry["rfilename"] for entry in json.load(response).get("siblings", [])]


def _download(remote: str, target: Path) -> None:
    """Mit Fortsetzen: ein abgebrochener Download macht beim naechsten Start
    an der .part-Datei weiter."""
    part = target.with_name(target.name + ".part")
    done = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={done}-"} if done else {}
    url = f"{HF}/{REPO}/resolve/main/{remote}"
    with urllib.request.urlopen(_request(url, headers), timeout=60) as response:
        if done and response.status != 206:
            done = 0  # Server kann nicht fortsetzen -> von vorn
        total = done + int(response.headers.get("Content-Length") or 0)
        step = max(total // 20, 1)
        next_report = done + step
        with open(part, "ab" if done else "wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
                done += len(chunk)
                if done >= next_report:
                    print(f"  {done / 1e9:.2f} / {total / 1e9:.2f} GB", flush=True)
                    next_report += step
    part.rename(target)


def ensure_model() -> Path:
    if FILE and Path(FILE).is_absolute():
        # Eigene GGUF (z. B. selbst konvertiert/quantisiert) im Volume
        if not Path(FILE).exists():
            sys.exit(f"BREEZE_GGUF_FILE={FILE} existiert nicht")
        return Path(FILE)

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    if FILE:
        remote = FILE
    else:
        # Liegt die Variante schon im Volume, gar nicht erst nachfragen.
        local = pick_file([p.name for p in MODELS_DIR.glob("*.gguf")], QUANT)
        if local:
            return MODELS_DIR / local
        try:
            files = _repo_files()
        except Exception as exc:
            sys.exit(f"Dateiliste von {HF}/{REPO} nicht abrufbar ({exc}). Netzwerk bzw. "
                     "HF_TOKEN pruefen - oder BREEZE_GGUF_FILE auf eine eigene GGUF setzen.")
        remote = pick_file(files, QUANT)
        if remote is None:
            ggufs = [f for f in files if f.lower().endswith(".gguf")]
            sys.exit(f"Keine GGUF mit '{QUANT}' in {REPO}. Vorhanden: {ggufs} - "
                     "BREEZE_GGUF_QUANT oder BREEZE_GGUF_FILE anpassen.")

    target = MODELS_DIR / Path(remote).name
    if not target.exists():
        print(f"Lade {REPO}/{remote} nach {target} (einmalig) ...", flush=True)
        try:
            _download(remote, target)
        except Exception:
            print("Download fehlgeschlagen. Falls Hugging Face eine Lizenz-Zustimmung "
                  f"verlangt: auf {HF}/{REPO} zustimmen und HF_TOKEN setzen.", flush=True)
            raise
    return target


if __name__ == "__main__":
    model = ensure_model()
    voices = MODELS_DIR / "voices"
    voices.mkdir(parents=True, exist_ok=True)
    # --ws-port -1: den WebSocket-Kanal braucht der Orchestrator nicht.
    os.execvp("breeze-server", [
        "breeze-server", str(model), "--host", "0.0.0.0", "--port", PORT,
        "--ws-port", "-1", "--voices-dir", str(voices), *sys.argv[1:],
    ])
