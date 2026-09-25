"""Qwen3-TTS-Engine: HTTP-API nach dem Engine-Vertrag v1 (tts-engine-kit)."""

import logging

from tts_engine_kit import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = create_app("tts_qwen3.backend:Qwen3Backend")
