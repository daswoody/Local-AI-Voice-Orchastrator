import json
import subprocess
from pathlib import Path

from .config import settings


class PiperEngine:
    """Ruft das piper-CLI per subprocess auf, statt die Python-API zu nutzen:
    Das CLI-Interface (--output-raw) ist ueber piper-tts-Versionen stabil,
    die Python-API hat sich mehrfach geaendert."""

    def __init__(self) -> None:
        self._sample_rate: int | None = None

    @property
    def model_path(self) -> Path:
        return Path(settings.models_dir) / f"{settings.piper_voice}.onnx"

    @property
    def config_path(self) -> Path:
        return Path(settings.models_dir) / f"{settings.piper_voice}.onnx.json"

    def sample_rate(self) -> int:
        # Die Samplerate steht in der Voice-Config (typisch 22050 bei
        # medium-Stimmen), nicht im CLI-Output.
        if self._sample_rate is None:
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
            self._sample_rate = int(config["audio"]["sample_rate"])
        return self._sample_rate

    def synthesize(self, text: str) -> tuple[bytes, int]:
        """Text -> (PCM16 mono, Samplerate)."""
        result = subprocess.run(
            [
                settings.piper_bin,
                "--model", str(self.model_path),
                "--config", str(self.config_path),
                "--output-raw",
            ],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=settings.synthesis_timeout_s,
        )
        if result.returncode != 0:
            raise RuntimeError(f"piper fehlgeschlagen: {result.stderr.decode('utf-8', 'replace')[:500]}")
        return result.stdout, self.sample_rate()


engine = PiperEngine()
