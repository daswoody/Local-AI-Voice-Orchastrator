"""Heim-AI TTS-Engine-Kit: gemeinsamer Engine-Vertrag v1 (v1.20).

Eine neue Engine = Backend-Klasse (siehe backend.py) + create_app("paket.modul:Klasse")."""

from .app import CONTRACT_VERSION, create_app
from .backend import Backend, EngineInfo, Voice
from .text import split_sentences

__all__ = ["CONTRACT_VERSION", "Backend", "EngineInfo", "Voice", "create_app", "split_sentences"]
