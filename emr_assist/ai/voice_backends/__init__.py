"""Voice backend abstraction layer.

Provides a base class for TTS engines and a simple registry
so new backends can be added by dropping a module into this package.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Generator, List, Optional, Type

import numpy as np


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class VoiceInfo:
    """Metadata for a single voice offered by a backend."""
    id: str
    name: str
    language: str = "en"
    gender: str = ""
    description: str = ""


@dataclass
class GenerationResult:
    """Audio data returned by a TTS backend."""
    audio: np.ndarray          # 1-D float32 waveform, values in [-1, 1]
    sample_rate: int           # e.g. 24000
    duration: float = 0.0     # seconds (computed if not set)

    def __post_init__(self) -> None:
        if self.duration <= 0 and self.audio.size > 0:
            self.duration = self.audio.size / self.sample_rate


@dataclass
class AudioChunk:
    """A single chunk from streaming generation."""
    audio: np.ndarray
    sample_rate: int
    is_final: bool = False


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class VoiceBackend(ABC):
    """Interface every TTS backend must implement."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable backend name (e.g. 'Kokoro TTS')."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the engine is installed and ready."""

    @abstractmethod
    def list_voices(self) -> List[VoiceInfo]:
        """Return available voices for this backend."""

    @abstractmethod
    def generate(
        self,
        text: str,
        voice: str,
        *,
        speed: float = 1.0,
        **kwargs,
    ) -> GenerationResult:
        """Blocking: generate audio for *text* with the given *voice* id."""

    def generate_stream(
        self,
        text: str,
        voice: str,
        *,
        speed: float = 1.0,
        **kwargs,
    ) -> Generator[AudioChunk, None, None]:
        """Streaming: yield audio chunks as they are produced.

        Default implementation falls back to a single-chunk generate().
        Backends that support incremental output should override this.
        """
        result = self.generate(text, voice, speed=speed, **kwargs)
        yield AudioChunk(audio=result.audio, sample_rate=result.sample_rate, is_final=True)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_registry: Dict[str, Type[VoiceBackend]] = {}


def register_backend(cls: Type[VoiceBackend]) -> Type[VoiceBackend]:
    """Class decorator — registers a VoiceBackend subclass."""
    _registry[cls.__name__] = cls
    return cls


def get_available_backends() -> List[VoiceBackend]:
    """Instantiate and return all backends that report is_available()."""
    available: List[VoiceBackend] = []
    for cls in _registry.values():
        try:
            inst = cls()
            if inst.is_available():
                available.append(inst)
        except Exception:
            pass
    return available


def get_all_backends() -> List[VoiceBackend]:
    """Instantiate and return all registered backends regardless of availability."""
    backends: List[VoiceBackend] = []
    for cls in _registry.values():
        try:
            backends.append(cls())
        except Exception:
            pass
    return backends


# Auto-import backends in this package so @register_backend fires
try:
    from . import kokoro_backend as _kokoro  # noqa: E402, F401
except Exception:
    pass
