"""进程级语音运行时，负责统一引擎与声纹库实例。"""

from __future__ import annotations

import os
import threading
from typing import Any, Dict, Optional

from .engine import EngineConfig, FunASREngine
from .speaker import VoiceprintStore


class SpeechRuntime:
    def __init__(
        self,
        engine: Optional[FunASREngine] = None,
        voiceprints: Optional[VoiceprintStore] = None,
    ) -> None:
        self.engine = engine or FunASREngine(EngineConfig.from_environment())
        if voiceprints is None:
            try:
                threshold = float(os.getenv("SENSEVOICE_VOICEPRINT_THRESHOLD", "0.72"))
            except ValueError:
                threshold = 0.72
            try:
                margin = float(os.getenv("SENSEVOICE_VOICEPRINT_MARGIN", "0.04"))
            except ValueError:
                margin = 0.04
            if not 0 <= threshold <= 1:
                threshold = 0.72
            if not 0 <= margin <= 1:
                margin = 0.04
            voiceprints = VoiceprintStore(
                match_threshold=threshold,
                match_margin=margin,
            )
        self.voiceprints = voiceprints
        self._lock = threading.RLock()

    def warmup(self) -> bool:
        return self.engine.warmup()

    def transcribe(self, waveform: Any, language: str, use_itn: bool, key: str) -> Dict[str, Any]:
        return self.engine.transcribe(waveform, language, use_itn, key)

    def extract_embedding(self, waveform: Any):
        return self.engine.extract_embedding(waveform)

    def capability_status(self) -> Dict[str, Any]:
        status = self.engine.capability_status()
        status["voiceprint_store"] = self.voiceprints.status()
        status["build_version"] = os.getenv("SENSEVOICE_BUILD_VERSION", "development")
        return status


_RUNTIME: Optional[SpeechRuntime] = None
_RUNTIME_LOCK = threading.Lock()


def get_speech_runtime() -> SpeechRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        with _RUNTIME_LOCK:
            if _RUNTIME is None:
                _RUNTIME = SpeechRuntime()
    return _RUNTIME
