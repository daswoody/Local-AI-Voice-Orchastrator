"""Abgleich mit dem ECHTEN qwen-tts-Paket (nur wenn installiert, z. B. im
Image bzw. mit `uv sync --extra engine`): Die Aufrufe des Backends und der
Nachbau in tests/fakes muessen zu dessen API passen. Gewichte braucht das
nicht - die gibt es nur im Container."""

from types import SimpleNamespace

import numpy as np
import pytest
from conftest import wav_bytes

from tts_engine_kit.audio import wav_to_float
from tts_qwen3.backend import LANGUAGES

pytestmark = pytest.mark.real_qwen
qwen_tts = pytest.importorskip("qwen_tts", reason="echtes qwen-tts nicht installiert")
if not hasattr(qwen_tts, "__file__") or "fakes" in str(qwen_tts.__file__):
    pytest.skip("nur der Nachbau gefunden", allow_module_level=True)


def _bare_model(languages):
    model = qwen_tts.Qwen3TTSModel.__new__(qwen_tts.Qwen3TTSModel)
    model.model = SimpleNamespace(get_supported_languages=lambda: languages)
    return model


def test_backend_calls_use_real_parameter_names():
    import inspect

    create = inspect.signature(qwen_tts.Qwen3TTSModel.create_voice_clone_prompt).parameters
    generate = inspect.signature(qwen_tts.Qwen3TTSModel.generate_voice_clone).parameters
    assert {"ref_audio", "ref_text", "x_vector_only_mode"} <= set(create)
    assert {"text", "language", "voice_clone_prompt"} <= set(generate)


def test_our_language_names_pass_the_real_validation():
    model = _bare_model(["auto", *LANGUAGES.values()])
    model._validate_languages(["german", "auto", "German"])
    with pytest.raises(ValueError):
        model._validate_languages(["klingonisch"])


def test_reference_tuple_is_accepted_by_the_real_audio_normalizer():
    samples, rate = wav_to_float(wav_bytes(width=3, channels=2))
    [(audio, audio_rate)] = _bare_model([])._normalize_audio_inputs([(samples, rate)])
    assert audio_rate == 24000 and audio.dtype == np.float32 and audio.ndim == 1
