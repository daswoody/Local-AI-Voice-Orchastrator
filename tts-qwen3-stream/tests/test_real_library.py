"""Abgleich mit dem ECHTEN faster-qwen3-tts-Paket (nur wenn installiert, z. B.
im Image bzw. mit `uv sync --extra engine`): Die Aufrufe des Backends und der
Nachbau in tests/fakes muessen zu dessen API passen. Gewichte braucht das
nicht - die gibt es nur im Container."""

import inspect
from types import SimpleNamespace

import numpy as np
import pytest
from conftest import wav_bytes

from tts_engine_kit.audio import wav_to_float
from tts_qwen3_stream.backend import LANGUAGES

pytestmark = pytest.mark.real_faster
faster = pytest.importorskip("faster_qwen3_tts", reason="echtes faster-qwen3-tts nicht installiert")
if not hasattr(faster, "__file__") or "fakes" in str(faster.__file__):
    pytest.skip("nur der Nachbau gefunden", allow_module_level=True)


def test_backend_calls_use_real_parameter_names():
    load = inspect.signature(faster.FasterQwen3TTS.from_pretrained).parameters
    stream = inspect.signature(faster.FasterQwen3TTS.generate_voice_clone_streaming).parameters
    assert {"device", "dtype", "attn_implementation", "max_seq_len"} <= set(load)
    assert {"text", "language", "voice_clone_prompt", "chunk_size"} <= set(stream)
    assert callable(faster.FasterQwen3TTS.warmup)


def test_prompt_comes_from_the_wrapped_qwen_tts_model():
    import qwen_tts

    create = inspect.signature(qwen_tts.Qwen3TTSModel.create_voice_clone_prompt).parameters
    assert {"ref_audio", "ref_text", "x_vector_only_mode"} <= set(create)


def test_our_language_names_pass_the_real_validation():
    import qwen_tts

    model = qwen_tts.Qwen3TTSModel.__new__(qwen_tts.Qwen3TTSModel)
    model.model = SimpleNamespace(get_supported_languages=lambda: ["auto", *LANGUAGES.values()])
    model._validate_languages(["german", "auto", "English"])
    with pytest.raises(ValueError):
        model._validate_languages(["klingonisch"])


def test_reference_tuple_is_accepted_by_the_real_audio_normalizer():
    import qwen_tts

    samples, rate = wav_to_float(wav_bytes(width=3, channels=2))
    samples = np.concatenate([samples, np.zeros(12000, dtype=samples.dtype)])
    model = qwen_tts.Qwen3TTSModel.__new__(qwen_tts.Qwen3TTSModel)
    [(audio, audio_rate)] = model._normalize_audio_inputs([(samples, rate)])
    assert audio_rate == 24000 and audio.dtype == np.float32 and audio.ndim == 1


def test_audio_tokenizer_codec_builds_with_the_pinned_transformers():
    """Regression (v1.24.1): Mit transformers >= 5.17 scheiterte das Laden mit
    "'MimiConfig' object has no attribute 'rope_theta'" - qwen_tts' Rope-Eintrag
    ueberschrieb dort Mimis eigene Funktion. Baut denselben Codec (ohne Gewichte)."""
    import qwen_tts  # noqa: F401 - registriert den Rope-Eintrag
    from transformers import MimiConfig, MimiModel

    MimiModel(MimiConfig())
