"""Kokoro TTS backend.

Uses the ``kokoro`` Python package (ONNX-based, CPU-friendly).
Install with::

    pip install kokoro soundfile

First run downloads the ONNX model (~300 MB).
"""

from __future__ import annotations

import logging
from typing import Generator, List, Optional

import numpy as np

from . import AudioChunk, GenerationResult, VoiceBackend, VoiceInfo, register_backend

log = logging.getLogger(__name__)

# Built-in Kokoro voice catalogue.
# Format: (voice_id, display_name, language, gender)
_VOICES = [
    ("af_heart",   "Heart (American Female)",    "en-us", "F"),
    ("af_bella",   "Bella (American Female)",    "en-us", "F"),
    ("af_nicole",  "Nicole (American Female)",   "en-us", "F"),
    ("af_sarah",   "Sarah (American Female)",    "en-us", "F"),
    ("af_sky",     "Sky (American Female)",      "en-us", "F"),
    ("am_adam",    "Adam (American Male)",       "en-us", "M"),
    ("am_michael", "Michael (American Male)",    "en-us", "M"),
    ("bf_emma",    "Emma (British Female)",      "en-gb", "F"),
    ("bf_isabella","Isabella (British Female)",  "en-gb", "F"),
    ("bm_george",  "George (British Male)",      "en-gb", "M"),
    ("bm_lewis",   "Lewis (British Male)",       "en-gb", "M"),
]


@register_backend
class KokoroBackend(VoiceBackend):
    """Text-to-speech via the Kokoro ONNX pipeline."""

    def __init__(self) -> None:
        self._pipeline = None  # lazy-loaded

    # -- VoiceBackend interface --

    @property
    def name(self) -> str:
        return "Kokoro TTS"

    def is_available(self) -> bool:
        try:
            import kokoro  # noqa: F401
            return True
        except Exception:
            return False

    def list_voices(self) -> List[VoiceInfo]:
        return [
            VoiceInfo(id=vid, name=vname, language=lang, gender=gen)
            for vid, vname, lang, gen in _VOICES
        ]

    def generate(
        self,
        text: str,
        voice: str,
        *,
        speed: float = 1.0,
        **kwargs,
    ) -> GenerationResult:
        pipeline = self._get_pipeline(voice)
        chunks: list[np.ndarray] = []
        for _gs, _ps, audio_chunk in pipeline(text, voice=voice, speed=speed):
            chunks.append(audio_chunk)

        if not chunks:
            return GenerationResult(audio=np.array([], dtype=np.float32), sample_rate=24000)

        audio = np.concatenate(chunks)
        return GenerationResult(audio=audio, sample_rate=24000)

    def generate_stream(
        self,
        text: str,
        voice: str,
        *,
        speed: float = 1.0,
        **kwargs,
    ) -> Generator[AudioChunk, None, None]:
        pipeline = self._get_pipeline(voice)
        generated_any = False
        for _gs, _ps, audio_chunk in pipeline(text, voice=voice, speed=speed):
            generated_any = True
            yield AudioChunk(audio=audio_chunk, sample_rate=24000, is_final=False)

        if generated_any:
            # Emit a zero-length final marker
            yield AudioChunk(
                audio=np.array([], dtype=np.float32),
                sample_rate=24000,
                is_final=True,
            )

    # -- Internal --

    def _get_pipeline(self, voice: str):
        """Lazy-init the Kokoro pipeline with the right language code."""
        lang_code = self._lang_code_for_voice(voice)
        if self._pipeline is None or getattr(self._pipeline, '_voice_lang', None) != lang_code:
            from kokoro import KPipeline
            self._pipeline = KPipeline(lang_code=lang_code)
            self._pipeline._voice_lang = lang_code  # tag for cache check
        return self._pipeline

    @staticmethod
    def _lang_code_for_voice(voice: str) -> str:
        """Infer language code from voice id prefix."""
        if voice.startswith("b"):
            return "b"  # British English
        return "a"      # American English (default)
