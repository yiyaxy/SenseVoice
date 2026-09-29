"""SenseVoice 的 FunASR 适配层。

路由层只依赖这里公开的少量接口，避免把 FunASR 的返回结构扩散到业务代码。
"""

from .engine import FunASREngine, EngineConfig, EngineError
from .runtime import SpeechRuntime, get_speech_runtime
from .speaker import VoiceprintStore, SpeakerMatch, VoiceprintError
from .timeline import TimelineSegment, SessionTimeline
from .session import AudioGap, InferenceGate, OnlineVADSegmenter, OnlineVADState
from .metrics import (
    character_error_counts,
    character_error_rate,
    edit_counts,
    edit_distance,
    latency_summary,
    normalize_transcript,
    strict_character_error_counts,
    strict_character_error_rate,
    timeline_boundary_summary,
    tokenize_transcript,
    voiceprint_error_summary,
    word_error_counts,
    word_error_rate,
)

__all__ = [
    "EngineConfig",
    "EngineError",
    "FunASREngine",
    "AudioGap",
    "InferenceGate",
    "OnlineVADSegmenter",
    "OnlineVADState",
    "SessionTimeline",
    "SpeakerMatch",
    "SpeechRuntime",
    "TimelineSegment",
    "VoiceprintError",
    "VoiceprintStore",
    "character_error_counts",
    "character_error_rate",
    "edit_counts",
    "edit_distance",
    "get_speech_runtime",
    "latency_summary",
    "normalize_transcript",
    "strict_character_error_counts",
    "strict_character_error_rate",
    "timeline_boundary_summary",
    "tokenize_transcript",
    "voiceprint_error_summary",
    "word_error_counts",
    "word_error_rate",
]
