"""面向外部客户端的实时语音对话服务。"""

import asyncio
import array
import json
import logging
import math
import os
import queue
import re
import secrets
import threading
import time
import urllib.error
from urllib.parse import urlparse
import urllib.request
import wave
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, AsyncIterator, Callable, Dict, Iterator, List, Mapping, Optional, Union

import soundfile as sf
from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi import File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from api import TARGET_FS, app
from speech.audio import (
    AudioDecodeError,
    decode_audio,
    decode_pcm_s16le,
    validate_pcm_stream_prefix,
    validate_voiceprint_audio,
)
from speech.auth import constant_time_equal
from speech.engine import EngineError, SUPPORTED_LANGUAGES
from speech.runtime import get_speech_runtime
from speech.speaker import (
    SessionSpeakerMapper,
    VoiceprintError,
    resolve_trusted_tenant,
)
from speech.session import (
    ASR_INFERENCE_GATE,
    BoundedSpeechQueue,
    InferenceGate,
    OnlineVADSegmenter,
)
from speech.timeline import SessionTimeline
from speech.tts import StreamingWavDecoder, TTSStreamError
from speech.voice import VoiceProfile, VoiceProfileError, VoiceProfileStore


LOGGER = logging.getLogger(__name__)
RICH_TEXT_TOKEN_PATTERN = re.compile(r"<\|.*?\|>")
SENTENCE_END_PATTERN = re.compile(r"[。！？!?；;\n]")
SOFT_SPLIT_PATTERN = re.compile(r"[\s,，、:：/]")
SUPPORTED_ASR_LANGUAGES = set(SUPPORTED_LANGUAGES)
TTS_AUTO_LANGUAGE_MAP = {
    # 独立 TTS 没有 ASR 语言上下文时以普通话为默认；完整对话会在解析 ASR 后覆盖该值。
    "auto": "all_zh",
    "zh": "all_zh",
    "en": "en",
    "yue": "all_yue",
    "ja": "all_ja",
    "ko": "all_ko",
}
SUPPORTED_TTS_LANGUAGES = frozenset(TTS_AUTO_LANGUAGE_MAP)
SUPPORTED_TTS_ENGINE_LANGUAGES = frozenset(TTS_AUTO_LANGUAGE_MAP.values())
TTS_OUTPUT_FORMATS = {"wav", "pcm_s16le"}
STREAM_END = object()
ASR_RUNTIME = get_speech_runtime()
ASR_GATE = ASR_INFERENCE_GATE
TTS_GATE = InferenceGate(limit=1)


@app.on_event("startup")
async def warmup_speech_runtime() -> None:
    """启动时预热一次统一引擎；失败保留服务进程并由健康接口报告未就绪。"""
    if os.getenv("SENSEVOICE_SKIP_STARTUP_WARMUP", "0") == "1":
        return
    ready = await asyncio.to_thread(ASR_RUNTIME.warmup)
    if not ready:
        LOGGER.warning("SenseVoice/FSMN-VAD/CAM++ 尚未预热完成")


class ClientInputError(ValueError):
    """表示可以安全返回给客户端的请求错误。"""


class RetryableTurnError(RuntimeError):
    """表示当前话轮未进入 ASR，可安全把原始音频放回缓冲区重试。"""


def _normalise_asr_language(value: Any) -> str:
    """统一 HTTP 与 WebSocket 的 ASR 语言输入，空值按自动检测处理。"""
    if value is None:
        language = "auto"
    elif not isinstance(value, str):
        raise ClientInputError("ASR 语言必须是字符串")
    else:
        language = value.strip().lower() or "auto"
    if language not in SUPPORTED_ASR_LANGUAGES:
        raise ClientInputError("不支持的识别语言")
    return language


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _env_value(name: str, default: str = "") -> str:
    """读取运行时配置；模板占位符一律按未配置处理。"""
    value = os.getenv(name, default).strip()
    return "" if value.startswith("请替换") else value


def _valid_http_url(value: str) -> bool:
    """只接受上游 HTTP(S) 地址，避免配置看似存在但首请求才失败。"""
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _pcm_rms(data: bytes) -> float:
    """计算 PCM 能量，仅用于显式开启的自动收口兜底。"""
    if len(data) < 2:
        return 0.0
    samples = array.array("h")
    samples.frombytes(data[: len(data) - len(data) % 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768.0


def _result_duration_ms(result: Mapping[str, Any]) -> float:
    """读取 ASR 结果时长；异常值按零处理，避免污染后续时间轴。"""
    try:
        duration_seconds = float(result.get("duration_seconds", 0))
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        return 0.0
    return duration_seconds * 1000


def _pack_pcm_wav(
    pcm_audio: bytes, *, sample_rate: int, channels: int, sample_width: int
) -> bytes:
    """用真实 PCM 长度重新封装 WAV，避免沿用上游流式占位头。"""
    if sample_rate <= 0 or channels != 1 or sample_width != 2:
        raise TTSStreamError("TTS 音频格式无效")
    output = BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_audio)
    return output.getvalue()


def _pcm_duration_ms(
    pcm_audio_bytes: int,
    *,
    sample_rate: int | None,
    channels: int | None,
    sample_width: int | None,
) -> int:
    """按实际 PCM 样本计算音频时长；无效格式返回零而不伪造统计。"""
    if (
        pcm_audio_bytes <= 0
        or sample_rate is None
        or channels is None
        or sample_width is None
        or sample_rate <= 0
        or channels <= 0
        or sample_width <= 0
    ):
        return 0
    bytes_per_second = sample_rate * channels * sample_width
    return round(pcm_audio_bytes * 1000 / bytes_per_second)


@dataclass(frozen=True)
class ServiceSettings:
    """所有部署参数均从环境变量读取，避免在代码中保存凭据。"""

    service_api_key: str
    allowed_origins: List[str]
    max_audio_bytes: int
    max_utterance_seconds: float
    max_history_turns: int
    llm_base_url: str
    llm_chat_completions_url: str
    llm_api_key: str
    llm_model: str
    llm_system_prompt: str
    llm_timeout_seconds: int
    max_llm_output_chars: int
    tts_base_url: str
    tts_ref_audio_path: str
    tts_prompt_text: str
    tts_prompt_lang: str
    tts_text_lang: str
    tts_streaming_mode: int
    tts_speed_factor: float
    tts_timeout_seconds: int
    voiceprint_admin_key: str = ""
    voiceprint_session_key: str = ""

    @classmethod
    def from_environment(cls) -> "ServiceSettings":
        raw_origins = _env_value(
            "SENSEVOICE_ALLOWED_ORIGINS", "http://localhost,http://127.0.0.1"
        )
        allowed_origins = [item.strip() for item in raw_origins.split(",") if item.strip()]
        voiceprint_admin_key = _env_value("SENSEVOICE_VOICEPRINT_ADMIN_KEY")
        voiceprint_session_key = _env_value("SENSEVOICE_VOICEPRINT_SESSION_KEY")
        return cls(
            service_api_key=_env_value("SENSEVOICE_API_KEY"),
            allowed_origins=allowed_origins,
            max_audio_bytes=max(
                1, _get_int("SENSEVOICE_MAX_AUDIO_BYTES", 20 * 1024 * 1024)
            ),
            max_utterance_seconds=max(
                0.1, _get_float("SENSEVOICE_MAX_UTTERANCE_SECONDS", 30.0)
            ),
            max_history_turns=max(1, _get_int("SENSEVOICE_MAX_HISTORY_TURNS", 10)),
            llm_base_url=_env_value("LLM_BASE_URL").rstrip("/"),
            llm_chat_completions_url=_env_value("LLM_CHAT_COMPLETIONS_URL").rstrip("/"),
            llm_api_key=_env_value("LLM_API_KEY"),
            llm_model=_env_value("LLM_MODEL"),
            llm_system_prompt=_env_value(
                "LLM_SYSTEM_PROMPT", "你是实时语音对话助手，请使用简洁、自然的口语回答。"
            ),
            llm_timeout_seconds=max(1, _get_int("LLM_TIMEOUT_SECONDS", 90)),
            max_llm_output_chars=max(
                256, min(_get_int("SENSEVOICE_MAX_LLM_OUTPUT_CHARS", 12_000), 100_000)
            ),
            tts_base_url=_env_value("GPT_SOVITS_BASE_URL").rstrip("/"),
            tts_ref_audio_path=_env_value("GPT_SOVITS_REF_AUDIO_PATH"),
            tts_prompt_text=_env_value("GPT_SOVITS_PROMPT_TEXT"),
            tts_prompt_lang=_env_value("GPT_SOVITS_PROMPT_LANG"),
            tts_text_lang=_env_value("GPT_SOVITS_TEXT_LANG", "auto"),
            tts_streaming_mode=max(
                1, min(3, _get_int("GPT_SOVITS_STREAMING_MODE", 3))
            ),
            tts_speed_factor=max(0.1, _get_float("GPT_SOVITS_SPEED_FACTOR", 1.0)),
            tts_timeout_seconds=max(1, _get_int("GPT_SOVITS_TIMEOUT_SECONDS", 90)),
            voiceprint_admin_key=voiceprint_admin_key,
            voiceprint_session_key=voiceprint_session_key,
        )

    def missing_dialogue_settings(self) -> List[str]:
        missing: List[str] = []
        if self.llm_chat_completions_url:
            if not _valid_http_url(self.llm_chat_completions_url):
                missing.append("LLM_CHAT_COMPLETIONS_URL")
        elif self.llm_base_url:
            if not _valid_http_url(self.llm_base_url):
                missing.append("LLM_BASE_URL")
        else:
            missing.extend(["LLM_BASE_URL", "LLM_CHAT_COMPLETIONS_URL"])
        missing.extend(
            name
            for name, value in {
                "LLM_MODEL": self.llm_model,
            }.items()
            if not value
        )
        return missing


SETTINGS = ServiceSettings.from_environment()
VOICE_PROFILES = VoiceProfileStore.from_environment(
    default_ref_audio_path=SETTINGS.tts_ref_audio_path,
    default_prompt_text=SETTINGS.tts_prompt_text,
    default_prompt_lang=SETTINGS.tts_prompt_lang,
    default_text_lang=SETTINGS.tts_text_lang,
)

# 浏览器接入只允许明确配置的来源，默认仅放行本机开发地址。
app.add_middleware(
    CORSMiddleware,
    allow_origins=SETTINGS.allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "X-API-Key",
        "X-Voiceprint-Admin-Key",
        "X-Voiceprint-Tenant",
        "X-Voiceprint-Session-Key",
    ],
)


@app.get("/api/v1/health")
async def health_check():
    """返回不含密钥、内部地址或声纹向量的能力状态。"""
    missing = SETTINGS.missing_dialogue_settings()
    if not SETTINGS.service_api_key:
        missing.insert(0, "SENSEVOICE_API_KEY")
    tts_missing = _tts_missing_settings()
    for name in tts_missing:
        if name not in missing:
            missing.append(name)
    capabilities = ASR_RUNTIME.capability_status()
    voiceprint_status = capabilities["voiceprint_store"]
    voice_status = VOICE_PROFILES.status()
    configured = not missing
    tts_configured = bool(SETTINGS.service_api_key) and not tts_missing
    voiceprint_management_ready = (
        bool(SETTINGS.voiceprint_admin_key)
        and voiceprint_status["encryption_configured"]
        and voiceprint_status.get("storage_ready", False)
    )
    voiceprint_session_ready = bool(SETTINGS.voiceprint_session_key)
    tts_ready = tts_configured and voice_status["ready"]
    all_ready = configured and capabilities["asr_ready"] and tts_ready
    voiceprint_missing = []
    if not SETTINGS.voiceprint_admin_key:
        voiceprint_missing.append("SENSEVOICE_VOICEPRINT_ADMIN_KEY")
    if not SETTINGS.voiceprint_session_key:
        voiceprint_missing.append("SENSEVOICE_VOICEPRINT_SESSION_KEY")
    if not voiceprint_status["encryption_configured"]:
        voiceprint_missing.append("SENSEVOICE_VOICEPRINT_KEY")
    if not voiceprint_status.get("storage_ready", False):
        voiceprint_missing.append("SENSEVOICE_VOICEPRINT_PATH")
    return {
        "status": (
            "ready"
            if all_ready
            else ("configuration_required" if missing or tts_missing else "warming_up")
        ),
        "build_version": capabilities["build_version"],
        "model_version": capabilities["model_version"],
        "vad_model_version": capabilities["vad_model_version"],
        "punc_model_version": capabilities["punc_model_version"],
        "spk_model_id": capabilities["spk_model_id"],
        "spk_model_version": capabilities["spk_model_version"],
        "asr_languages": ["zh", "en", "yue", "ja", "ko"],
        "asr_ready": bool(SETTINGS.service_api_key) and capabilities["asr_ready"],
        "tts_ready": tts_ready,
        "tts_stream_ready": tts_ready,
        "tts_missing_settings": tts_missing,
        "tts_voice_profiles": VOICE_PROFILES.public_profiles(),
        "dialogue_ready": all_ready,
        "timestamps_ready": capabilities["timestamps_ready"],
        "streaming_vad_ready": capabilities["streaming_vad_ready"],
        "streaming_vad_error": capabilities["streaming_vad_error"],
        "diarization_ready": capabilities["diarization_ready"],
        "speaker_identification_ready": capabilities["speaker_identification_ready"],
        "speaker_identification_mode": (
            "persistent"
            if (
                voiceprint_management_ready
                and voiceprint_session_ready
                and capabilities["speaker_identification_ready"]
            )
            else "anonymous"
        ),
        "voiceprint_management_ready": voiceprint_management_ready,
        "voiceprint_session_ready": voiceprint_session_ready,
        "voiceprint_encryption_ready": voiceprint_status["encryption_configured"],
        "voiceprint_storage_ready": voiceprint_status.get("storage_ready", False),
        "voiceprint_missing_settings": voiceprint_missing,
        "authentication_ready": bool(SETTINGS.service_api_key),
        "missing_settings": missing,
    }


class DevTTSRequest(BaseModel):
    """开发回归台的 TTS 请求；参考音频始终由服务端 voice_id 映射。"""

    text: str = Field(min_length=1, max_length=2_000)
    text_lang: str = Field(default="auto", min_length=1, max_length=32)
    streaming_mode: int | None = Field(default=None, ge=1, le=3)
    voice_id: str | None = Field(default=None, min_length=1, max_length=64)


def _tts_missing_settings() -> List[str]:
    """返回 TTS 必需配置名，不泄露参考音频路径或上游地址。"""
    missing = [
        name
        for name, value in {
            "GPT_SOVITS_BASE_URL": SETTINGS.tts_base_url,
        }.items()
        if not value or not _valid_http_url(value)
    ]
    try:
        _normalise_tts_preference(SETTINGS.tts_text_lang)
    except ValueError:
        missing.append("GPT_SOVITS_TEXT_LANG")

    voice_status = VOICE_PROFILES.status()
    if not voice_status["configured"]:
        # JSON 音色可以完全自带参考音频、逐字稿和参考语言；只有在没有任何
        # 有效 profile 时，才要求默认环境变量或提示管理员维护 profile 文件。
        profile_file = _env_value("SENSEVOICE_VOICE_PROFILE_FILE")
        if profile_file:
            missing.append("SENSEVOICE_VOICE_PROFILE_FILE")
        else:
            if not SETTINGS.tts_ref_audio_path:
                missing.append("GPT_SOVITS_REF_AUDIO_PATH")
            if not SETTINGS.tts_prompt_lang:
                missing.append("GPT_SOVITS_PROMPT_LANG")
        missing.append("GPT_SOVITS_VOICE_PROFILE")
    elif not voice_status["ready"]:
        missing.append("GPT_SOVITS_REF_AUDIO_PATH_FILE")
    return missing


@app.post("/api/v1/dev/tts")
async def dev_tts(payload: DevTTSRequest) -> Response:
    """独立合成一段 WAV，供管理端验证文本准确率、音色和延迟。"""
    text = payload.text.strip()
    if not text:
        return JSONResponse(status_code=422, content={"detail": "TTS 文本不能为空", "code": "invalid_text"})
    missing = _tts_missing_settings()
    if missing:
        return JSONResponse(
            status_code=503,
            content={"detail": "GPT-SoVITS 尚未配置完整", "missing_settings": missing},
        )

    try:
        voice = VOICE_PROFILES.resolve(payload.voice_id)
    except VoiceProfileError as error:
        return JSONResponse(status_code=422, content={"detail": str(error)})

    try:
        text_language = _normalise_tts_language(payload.text_lang)
    except ValueError as error:
        return JSONResponse(status_code=422, content={"detail": str(error)})
    if not voice.supports_language(text_language):
        return JSONResponse(
            status_code=422,
            content={"detail": "该音色不支持请求的合成语言", "voice_id": voice.voice_id},
        )
    started = time.monotonic()
    first_audio_ms: float | None = None
    pcm_audio = bytearray()
    decoder = StreamingWavDecoder()
    has_pcm_audio = False
    try:
        async with TTS_GATE.slot():
            async with _managed_blocking_stream(
                lambda: _iter_gpt_sovits_stream(
                    SETTINGS,
                    text,
                    text_language,
                    # 完整 WAV 接口必须使用可回读的非流式响应。
                    0,
                    voice,
                )
            ) as audio_stream:
                async for audio_chunk in audio_stream:
                    pcm_chunk = decoder.feed(audio_chunk)
                    if pcm_chunk and first_audio_ms is None:
                        first_audio_ms = (time.monotonic() - started) * 1000
                        has_pcm_audio = True
                    elif pcm_chunk:
                        has_pcm_audio = True
                    pcm_audio.extend(pcm_chunk)
        decoder.finish()
    except Exception as error:  # noqa: BLE001
        LOGGER.exception("开发 TTS 合成失败")
        return JSONResponse(
            status_code=502,
            content={"detail": "GPT-SoVITS 合成失败", "error_type": type(error).__name__},
        )
    if not has_pcm_audio:
        return JSONResponse(
            status_code=502,
            content={"detail": "GPT-SoVITS 未返回可播放音频", "code": "tts_empty"},
        )

    try:
        audio = _pack_pcm_wav(
            bytes(pcm_audio),
            sample_rate=int(decoder.sample_rate or 0),
            channels=int(decoder.channels or 0),
            sample_width=int(decoder.sample_width or 0),
        )
    except (TTSStreamError, ValueError, TypeError):
        return JSONResponse(
            status_code=502,
            content={"detail": "GPT-SoVITS 音频格式无效", "code": "tts_invalid_audio"},
        )

    duration_ms = 0
    if audio:
        try:
            duration_ms = round(sf.info(BytesIO(audio)).duration * 1000)
        except Exception:  # noqa: BLE001
            duration_ms = 0
    return Response(
        content=bytes(audio),
        media_type="audio/wav",
        headers={
            "X-TTS-First-Audio-Ms": str(round(first_audio_ms or 0)),
            "X-TTS-Total-Ms": str(round((time.monotonic() - started) * 1000)),
            "X-TTS-Duration-Ms": str(duration_ms),
            "X-TTS-Text-Language": text_language,
            "X-TTS-Voice-Id": voice.voice_id,
            "X-TTS-Voice-Version": voice.version,
        },
    )


@app.get("/api/v1/tts/voices")
async def list_tts_voices() -> Dict[str, Any]:
    """返回可选择的音色元数据，不暴露参考音频路径和参考稿。"""
    return {"voices": VOICE_PROFILES.public_profiles()}


def _voiceprint_tenant(request: Request) -> str:
    """声纹库只接受可信 BFF 注入的租户标识，不接受请求体任意指定路径。"""
    if not SETTINGS.voiceprint_admin_key:
        raise HTTPException(status_code=503, detail="声纹管理权限尚未配置")
    provided = request.headers.get("x-voiceprint-admin-key", "")
    if not constant_time_equal(provided, SETTINGS.voiceprint_admin_key):
        raise HTTPException(status_code=403, detail="无声纹管理权限")
    tenant = request.headers.get("x-voiceprint-tenant", "")
    if not tenant:
        raise HTTPException(status_code=400, detail="缺少 X-Voiceprint-Tenant")
    return tenant


@app.get("/api/v1/voiceprints")
async def list_voiceprints(request: Request):
    """仅返回人员元数据，不返回原始声纹向量。"""
    try:
        tenant = _voiceprint_tenant(request)
        return {"people": ASR_RUNTIME.voiceprints.list_people(tenant)}
    except HTTPException:
        raise
    except VoiceprintError as error:
        return JSONResponse(status_code=422, content={"detail": str(error)})


@app.post("/api/v1/voiceprints")
async def register_voiceprint(
    request: Request,
    files: List[UploadFile] = File(description="1 至 5 段单人干净语音，建议 3 至 5 段"),
    person_id: str = Form(...),
    authorized: bool = Form(...),
):
    """登记授权人员的多个 CAM++ 向量；登记录音只在请求内存中处理。"""
    try:
        tenant = _voiceprint_tenant(request)
        if not files:
            raise VoiceprintError("至少上传一段登记语音")
        if len(files) > 5:
            raise VoiceprintError("一次最多登记 5 段语音")
        max_sample_bytes = max(
            1, _get_int("SENSEVOICE_MAX_VOICEPRINT_AUDIO_BYTES", 20 * 1024 * 1024)
        )
        vectors = []
        for file in files:
            # 先限制读取大小，再解码，避免管理员接口因异常上传占满内存。
            data = await file.read(max_sample_bytes + 1)
            if len(data) > max_sample_bytes:
                raise VoiceprintError("登记语音超过单段大小限制")
            waveform, duration = decode_audio(data, TARGET_FS)
            validate_voiceprint_audio(waveform, duration)
            vector = await ASR_GATE.run(ASR_RUNTIME.extract_embedding, waveform)
            if vector is None:
                raise VoiceprintError("未能从登记语音提取 CAM++ 声纹")
            vectors.append(vector)
        result = ASR_RUNTIME.voiceprints.register(
            tenant,
            person_id,
            vectors,
            authorized=authorized,
            model_id=ASR_RUNTIME.engine.config.spk_model,
            model_revision=ASR_RUNTIME.engine.config.spk_model_revision,
            preprocess_version="pcm16k-mono-f32-v1",
        )
        LOGGER.info("声纹登记完成 tenant=%s person_id=%s samples=%s", tenant, person_id, result["sample_count"])
        return {"status": "registered", **result}
    except HTTPException:
        raise
    except (AudioDecodeError, VoiceprintError, EngineError) as error:
        return JSONResponse(status_code=422, content={"detail": str(error)})
    except RuntimeError as error:
        if str(error) == "server_busy":
            return JSONResponse(status_code=503, content={"detail": "语音引擎繁忙"})
        raise


@app.delete("/api/v1/voiceprints/{person_id}")
async def delete_voiceprint(person_id: str, request: Request):
    """删除持久样本、聚合中心和匹配缓存；不保留登记录音。"""
    try:
        tenant = _voiceprint_tenant(request)
        deleted = ASR_RUNTIME.voiceprints.delete(tenant, person_id)
        if not deleted:
            return JSONResponse(status_code=404, content={"detail": "人员不存在"})
        LOGGER.info("声纹删除完成 tenant=%s person_id=%s", tenant, person_id)
        return {"status": "deleted", "person_id": person_id}
    except HTTPException:
        raise
    except VoiceprintError as error:
        return JSONResponse(status_code=422, content={"detail": str(error)})


@app.get("/api/v1/speakers")
async def list_speakers(request: Request):
    """提供规划契约的说话人查询别名；底层仍复用加密声纹库。"""
    result = await list_voiceprints(request)
    if isinstance(result, dict):
        people = []
        for person in result.get("people", []):
            item = dict(person)
            if "person_id" in item:
                item["speaker_profile_id"] = item["person_id"]
            people.append(item)
        return {"speakers": people}
    return result


@app.post("/api/v1/speakers/enroll")
async def enroll_speaker(
    request: Request,
    files: List[UploadFile] = File(description="1 至 5 段单人干净语音，建议 3 至 5 段"),
    speaker_profile_id: str = Form(...),
    authorized: bool = Form(...),
):
    """提供规划契约的声纹登记别名，不改变既有 voiceprints 接口。"""
    result = await register_voiceprint(request, files, speaker_profile_id, authorized)
    if isinstance(result, dict):
        result = dict(result)
        result["speaker_profile_id"] = result.get("person_id", speaker_profile_id)
    return result


@app.delete("/api/v1/speakers/{speaker_profile_id}")
async def delete_speaker(speaker_profile_id: str, request: Request):
    """提供规划契约的声纹删除别名；删除语义与旧接口一致。"""
    result = await delete_voiceprint(speaker_profile_id, request)
    if isinstance(result, dict):
        result = dict(result)
        result["speaker_profile_id"] = result.get("person_id", speaker_profile_id)
    return result


def _chat_completions_url(settings: ServiceSettings) -> str:
    if settings.llm_chat_completions_url:
        return settings.llm_chat_completions_url
    if settings.llm_base_url.endswith("/chat/completions"):
        return settings.llm_base_url
    if settings.llm_base_url.endswith("/v1"):
        return f"{settings.llm_base_url}/chat/completions"
    return f"{settings.llm_base_url}/v1/chat/completions"


def _tts_url(settings: ServiceSettings) -> str:
    if settings.tts_base_url.endswith("/tts"):
        return settings.tts_base_url
    return f"{settings.tts_base_url}/tts"


def _infer_waveform(waveform: Any, language: str, duration_seconds: float) -> Dict[str, Any]:
    """对已经归一化到 16 kHz 单声道的波形执行一次识别。"""
    language = _normalise_asr_language(language)
    if duration_seconds > SETTINGS.max_utterance_seconds:
        raise ClientInputError(
            f"单轮音频最长 {SETTINGS.max_utterance_seconds:g} 秒，请在客户端分段提交"
        )

    try:
        # 实时链路交给 punc_model 统一恢复标点；SenseVoice 的 ITN 标点若同时开启会重复输出。
        result = ASR_RUNTIME.transcribe(waveform, language, False, "realtime")
    except EngineError as error:
        raise ClientInputError(str(error)) from error
    result["duration_seconds"] = round(duration_seconds, 3)
    return result


def _transcribe_audio(audio_bytes: bytes, language: str) -> Dict[str, Any]:
    """在单独线程中执行阻塞的音频解码与 ASR 推理。"""
    if not audio_bytes:
        raise ClientInputError("未收到音频数据")
    if len(audio_bytes) > SETTINGS.max_audio_bytes:
        raise ClientInputError("音频超过单次请求大小限制")
    try:
        waveform, duration = decode_audio(audio_bytes, TARGET_FS)
    except AudioDecodeError as error:
        raise ClientInputError(str(error)) from error
    return _infer_waveform(waveform, language, duration)


def _transcribe_pcm(audio_bytes: bytes, language: str) -> Dict[str, Any]:
    """识别实时接口接收的 16 kHz、16 bit、小端单声道 PCM。"""
    if not audio_bytes:
        raise ClientInputError("未收到音频数据")
    if len(audio_bytes) > SETTINGS.max_audio_bytes:
        raise ClientInputError("音频超过单次请求大小限制")
    try:
        waveform, duration = decode_pcm_s16le(audio_bytes, TARGET_FS)
    except AudioDecodeError as error:
        raise ClientInputError(str(error)) from error
    return _infer_waveform(waveform, language, duration)


async def _process_stream_commit(
    websocket: WebSocket,
    *,
    audio_bytes: bytes,
    language: str,
    timeline: SessionTimeline,
    speaker_mapper: SessionSpeakerMapper,
    voiceprint_tenant: Optional[str],
    turn_id: str,
    offset_ms: float,
    started: float,
    first_partial_ms: int | None,
    emit_segment_events: bool = False,
) -> bool:
    """完成一段实时 ASR；失败时保留输入，让客户端或自动 VAD 稍后重试。"""
    try:
        final = await ASR_GATE.run(_transcribe_pcm, audio_bytes, language)
    except RuntimeError as error:
        await websocket.send_json(
            {
                "type": "error",
                "code": "server_busy" if str(error) == "server_busy" else "asr_failed",
            }
        )
        return False
    except ClientInputError as error:
        await websocket.send_json(
            {"type": "error", "code": "invalid_audio", "message": str(error)}
        )
        return False
    except Exception:  # noqa: BLE001
        LOGGER.exception("实时 ASR final 推理失败")
        await websocket.send_json({"type": "error", "code": "asr_failed"})
        return False

    if not isinstance(final.get("text"), str) or not RICH_TEXT_TOKEN_PATTERN.sub(
        "", str(final.get("text", ""))
    ).strip():
        # 无语音也视为本段已消费，客户端可以继续发送下一段而无需重连。
        await websocket.send_json(
            {
                "type": "asr.no_speech",
                "turn_id": turn_id,
                "revision": timeline.next_revision(),
            }
        )
        await websocket.send_json(
            {
                "type": "asr.completed",
                "turn_id": turn_id,
                "revision": timeline.next_revision(),
                "final_ms": round((time.monotonic() - started) * 1000),
            }
        )
        return True

    final_ms = round((time.monotonic() - started) * 1000)
    try:
        final["segments"] = _attach_anonymous_speakers(
            final, speaker_mapper, voiceprint_tenant
        )
        final["segments"] = [
            item.to_dict()
            for item in timeline.build_segments(
                final["segments"],
                turn_id=turn_id,
                offset_ms=offset_ms,
                language=final.get("language", language),
            )
        ]
    except (TypeError, ValueError, KeyError):
        # 模型结果不符合时间线契约时返回可重试错误，不让 WebSocket 直接断开。
        LOGGER.exception("实时 ASR 结果整理失败")
        await websocket.send_json({"type": "error", "code": "asr_failed"})
        return False
    await websocket.send_json(
        {
            "type": "asr.final",
            **_public_asr_result(final),
            "turn_id": turn_id,
            "revision": timeline.next_revision(),
            "first_partial_ms": first_partial_ms,
            "final_ms": final_ms,
        }
    )
    if emit_segment_events:
        for segment in final["segments"]:
            await websocket.send_json(
                _segment_event(
                    "final",
                    segment,
                    turn_id=turn_id,
                    revision=timeline.next_revision(),
                )
            )
    for segment in final["segments"]:
        if segment.get("speaker_status") in {"identified", "unknown", "ambiguous"}:
            await websocket.send_json(
                {
                    "type": "speaker.update",
                    "segment_id": segment.get("segment_id"),
                    "turn_id": turn_id,
                    "revision": timeline.next_revision(),
                    "speaker": segment.get("speaker"),
                    "speaker_status": segment.get("speaker_status"),
                    "match_score": segment.get("match_score"),
                }
            )
    await websocket.send_json(
        {
            "type": "asr.completed",
            "turn_id": turn_id,
            "revision": timeline.next_revision(),
            "final_ms": final_ms,
        }
    )
    return True


@app.websocket("/api/v1/asr/stream")
async def stream_asr(websocket: WebSocket) -> None:
    """接收 PCM 分帧并周期性返回 partial，commit 后返回 final。"""
    if not SETTINGS.service_api_key:
        await websocket.close(code=1011, reason="服务未配置 API 密钥")
        return
    provided_key, accepted_subprotocol = _websocket_api_key(websocket)
    if not constant_time_equal(provided_key, SETTINGS.service_api_key):
        await websocket.close(code=4401, reason="API 密钥无效")
        return
    await websocket.accept(subprotocol=accepted_subprotocol)
    await websocket.send_json(
        {
            "type": "session.ready",
            "audio_format": "pcm_s16le",
            "sample_rate": TARGET_FS,
            "channels": 1,
            "sample_width": 2,
            "asr_languages": ["auto", "zh", "en", "yue", "ja", "ko"],
            "capabilities": [
                "segments",
                "anonymous_speakers",
                "revisioned_events",
                "segment_events",
            ],
            "segment_events": False,
            "auto_vad": "opt_in",
        }
    )

    language = "auto"
    audio_buffer = bytearray()
    session_offset_ms = 0.0
    stream_offset_ms = 0.0
    timeline = SessionTimeline()
    speaker_mapper = SessionSpeakerMapper()
    voiceprint_tenant = resolve_trusted_tenant(websocket.headers)
    current_turn_id: str | None = None
    last_partial_bytes = 0
    first_partial_ms: int | None = None
    utterance_started: float | None = None
    auto_vad = False
    segment_events = False
    vad_segmenter: OnlineVADSegmenter | None = None
    vad_base_offset_ms = 0.0
    # VAD 可能在一帧很短或缓冲尚未达到最小提交长度时先报告结束；保留该
    # 会话 epoch 内的结束点，待后续音频到达后再尝试收口，避免丢失短话轮。
    vad_pending_end_ms: float | None = None
    vad_error_sent = False
    pcm_probe = bytearray()
    pcm_format_checked = False
    max_pcm_utterance_bytes = max(
        2,
        round(max(0.001, SETTINGS.max_utterance_seconds) * TARGET_FS * 2),
    )

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            audio_chunk = message.get("bytes")
            if audio_chunk is not None:
                if not pcm_format_checked:
                    pcm_probe.extend(audio_chunk[:16])
                    try:
                        validate_pcm_stream_prefix(bytes(pcm_probe))
                    except AudioDecodeError as error:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "code": "invalid_audio_format",
                                "message": str(error),
                            }
                        )
                        return
                    if len(pcm_probe) >= 4:
                        pcm_format_checked = True
                if len(audio_chunk) % 2:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": "invalid_audio_alignment",
                            "message": "实时 PCM 二进制帧必须按 16 bit 样本对齐",
                        }
                    )
                    return
                if utterance_started is None:
                    utterance_started = time.monotonic()
                    current_turn_id = timeline.new_turn()
                    await websocket.send_json({"type": "asr.processing"})
                chunk_duration_ms = len(audio_chunk) / 2 / TARGET_FS * 1000
                stream_offset_ms += chunk_duration_ms
                # 先检查剩余容量，避免把超大单帧复制进缓冲后才丢弃；丢弃的时长仍
                # 推进会话时钟，保证后续句段不会回到已丢弃音频的时间范围。
                if len(audio_chunk) > max(0, SETTINGS.max_audio_bytes - len(audio_buffer)):
                    dropped_duration_ms = (
                        len(audio_buffer) + len(audio_chunk)
                    ) / 2 / TARGET_FS * 1000
                    audio_buffer.clear()
                    session_offset_ms += dropped_duration_ms
                    utterance_started = None
                    current_turn_id = None
                    last_partial_bytes = 0
                    first_partial_ms = None
                    if vad_segmenter is not None:
                        vad_segmenter.reset()
                        vad_base_offset_ms = session_offset_ms
                    vad_pending_end_ms = None
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": "audio_too_large",
                            "dropped_duration_ms": round(dropped_duration_ms, 3),
                        }
                    )
                    continue
                audio_buffer.extend(audio_chunk)
                if len(audio_buffer) > max_pcm_utterance_bytes:
                    # PCM 时长可由采样数直接确定，超过单轮上限时立即结束当前话轮。
                    dropped_duration_ms = len(audio_buffer) / 2 / TARGET_FS * 1000
                    audio_buffer.clear()
                    session_offset_ms += dropped_duration_ms
                    utterance_started = None
                    current_turn_id = None
                    last_partial_bytes = 0
                    first_partial_ms = None
                    if vad_segmenter is not None:
                        vad_segmenter.reset()
                        vad_base_offset_ms = session_offset_ms
                    vad_pending_end_ms = None
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": "utterance_too_long",
                            "max_seconds": SETTINGS.max_utterance_seconds,
                            "dropped_duration_ms": round(dropped_duration_ms, 3),
                        }
                    )
                    continue
                vad_closed_ends: List[float] = []
                if auto_vad and vad_segmenter is not None:
                    try:
                        vad_waveform, _ = decode_pcm_s16le(audio_chunk, TARGET_FS)
                        vad_signals = await ASR_GATE.run(
                            vad_segmenter.process, vad_waveform, is_final=False
                        )
                        vad_closed_ends = [
                            signal[1]
                            for signal in vad_signals
                            if len(signal) >= 2 and signal[1] >= 0
                        ]
                        if vad_closed_ends:
                            # 结束点以当前 VAD cache epoch 为基准，取最新值；成功消费后
                            # 再按 session_offset_ms 清理，不能因当前帧过短而直接丢掉。
                            latest_vad_end_ms = max(vad_closed_ends)
                            vad_pending_end_ms = max(
                                latest_vad_end_ms,
                                vad_pending_end_ms
                                if vad_pending_end_ms is not None
                                else latest_vad_end_ms,
                            )
                    except RuntimeError as error:
                        if str(error) == "server_busy":
                            await websocket.send_json({"type": "error", "code": "server_busy"})
                        elif not vad_error_sent:
                            vad_error_sent = True
                            auto_vad = False
                            vad_pending_end_ms = None
                            await websocket.send_json(
                                {"type": "error", "code": "vad_unavailable"}
                            )
                    except AudioDecodeError:
                        if not vad_error_sent:
                            vad_error_sent = True
                            auto_vad = False
                            vad_pending_end_ms = None
                            await websocket.send_json(
                                {"type": "error", "code": "vad_invalid_audio"}
                            )
                # SenseVoice 本身不是增量模型，用约 800ms 滑窗推理提供稳定的 partial。
                if len(audio_buffer) - last_partial_bytes >= TARGET_FS * 2 * 0.8:
                    try:
                        partial = await ASR_GATE.run(
                            _transcribe_pcm, bytes(audio_buffer), language
                        )
                    except RuntimeError as error:
                        if str(error) == "server_busy":
                            await websocket.send_json({"type": "error", "code": "server_busy"})
                        partial = None
                    except ClientInputError:
                        partial = None
                    except Exception:  # noqa: BLE001
                        LOGGER.exception("实时 ASR partial 推理失败")
                        partial = None
                    last_partial_bytes = len(audio_buffer)
                    if partial and partial.get("text"):
                        if first_partial_ms is None and utterance_started is not None:
                            first_partial_ms = round((time.monotonic() - utterance_started) * 1000)
                        try:
                            partial_segments = _attach_anonymous_speakers(
                                partial, speaker_mapper, voiceprint_tenant
                            )
                            # partial 与 segment 事件必须共享会话绝对时间基准；当前
                            # audio_buffer 仅覆盖本话轮，因此统一加上 session_offset_ms。
                            partial_segments = [
                                _offset_partial_segment(segment, session_offset_ms)
                                for segment in partial_segments
                            ]
                        except (TypeError, ValueError, KeyError):
                            # partial 只用于提示，结果不合法时跳过本次，不中断实时会话。
                            LOGGER.exception("实时 ASR partial 结果整理失败")
                            partial_segments = []
                        await websocket.send_json({
                            "type": "asr.partial",
                            "text": partial["text"],
                            "language": partial["language"],
                            "turn_id": current_turn_id,
                            "revision": (partial_revision := timeline.next_revision()),
                            "segment_id": f"partial_{partial_revision}",
                            "segments": partial_segments,
                            "elapsed_ms": round(
                                (time.monotonic() - (utterance_started or time.monotonic())) * 1000
                            ),
                        })
                        if segment_events:
                            for segment_index, partial_segment in enumerate(partial_segments):
                                await websocket.send_json(
                                    _segment_event(
                                        "partial",
                                        partial_segment,
                                        turn_id=current_turn_id,
                                        revision=timeline.next_revision(),
                                        segment_id=(
                                            f"partial_segment_{current_turn_id or 'unknown'}_"
                                            f"{segment_index}"
                                        ),
                                    )
                                )
                if auto_vad and vad_pending_end_ms is not None and audio_buffer:
                    cut_ms = vad_pending_end_ms
                    committed_in_vad_epoch_ms = max(
                        0.0, session_offset_ms - vad_base_offset_ms
                    )
                    cut_bytes = round(
                        max(0.0, cut_ms - committed_in_vad_epoch_ms)
                        * TARGET_FS
                        * 2
                        / 1000
                    )
                    cut_bytes = min(len(audio_buffer), cut_bytes - cut_bytes % 2)
                    if cut_bytes >= 3200:
                        utterance = bytes(audio_buffer[:cut_bytes])
                        utterance_offset_ms = session_offset_ms
                        started = utterance_started or time.monotonic()
                        turn_id = current_turn_id or timeline.new_turn()
                        if await _process_stream_commit(
                            websocket,
                            audio_bytes=utterance,
                            language=language,
                            timeline=timeline,
                            speaker_mapper=speaker_mapper,
                            voiceprint_tenant=voiceprint_tenant,
                            turn_id=turn_id,
                            offset_ms=utterance_offset_ms,
                            started=started,
                            first_partial_ms=first_partial_ms,
                            emit_segment_events=segment_events,
                        ):
                            del audio_buffer[:cut_bytes]
                            session_offset_ms += cut_bytes / 2 / TARGET_FS * 1000
                            last_partial_bytes = 0
                            first_partial_ms = None
                            consumed_vad_epoch_ms = max(
                                0.0, session_offset_ms - vad_base_offset_ms
                            )
                            if (
                                vad_pending_end_ms is not None
                                and vad_pending_end_ms <= consumed_vad_epoch_ms + 1e-3
                            ):
                                vad_pending_end_ms = None
                            # 自动收口已消费一个完整 VAD 片段。没有剩余缓冲时，下一轮必须从新
                            # cache 和新时间基准开始；若同一帧还含下一段语音，则保留已处理的 cache，
                            # 避免重置后丢掉这段尚未提交的 VAD 上下文。
                            if vad_segmenter is not None and not audio_buffer:
                                vad_segmenter.reset()
                                vad_base_offset_ms = session_offset_ms
                                vad_pending_end_ms = None
                            if audio_buffer:
                                utterance_started = time.monotonic()
                                current_turn_id = timeline.new_turn()
                                await websocket.send_json({"type": "asr.processing"})
                            else:
                                utterance_started = None
                                current_turn_id = None
                        # 失败时保留原始 buffer，避免自动收口丢失 final。
                continue

            raw_event = message.get("text")
            if raw_event is None:
                continue
            try:
                event = json.loads(raw_event)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "code": "invalid_json"})
                continue
            if not isinstance(event, dict):
                await websocket.send_json({"type": "error", "code": "invalid_event"})
                continue
            event_type = event.get("type")
            if event_type == "configure":
                if audio_buffer:
                    await websocket.send_json(
                        {"type": "error", "code": "configure_requires_empty_buffer"}
                    )
                    continue
                try:
                    requested = _normalise_asr_language(event.get("language", language))
                except ClientInputError:
                    await websocket.send_json({"type": "error", "code": "unsupported_asr_language"})
                    continue
                requested_segment_events = event.get("segment_events", segment_events)
                if not isinstance(requested_segment_events, bool):
                    await websocket.send_json(
                        {"type": "error", "code": "invalid_segment_events"}
                    )
                    continue
                raw_auto_vad = event.get("auto_vad", auto_vad)
                if not isinstance(raw_auto_vad, bool):
                    await websocket.send_json({"type": "error", "code": "invalid_auto_vad"})
                    continue
                requested_auto_vad = raw_auto_vad
                if requested_auto_vad and audio_buffer:
                    await websocket.send_json(
                        {"type": "error", "code": "auto_vad_requires_empty_buffer"}
                    )
                    continue
                if requested_auto_vad:
                    if vad_segmenter is None:
                        vad_segmenter = OnlineVADSegmenter(ASR_RUNTIME.engine)
                    else:
                        vad_segmenter.reset()
                    vad_base_offset_ms = session_offset_ms
                    vad_pending_end_ms = None
                    vad_error_sent = False
                elif not requested_auto_vad and vad_segmenter is not None:
                    vad_segmenter.reset()
                    vad_base_offset_ms = session_offset_ms
                    vad_pending_end_ms = None
                # 所有字段校验通过后再一次性提交，错误事件不会污染当前会话状态。
                language = requested
                segment_events = requested_segment_events
                auto_vad = requested_auto_vad
                await websocket.send_json(
                    {
                        "type": "session.configured",
                        "language": language,
                        "segment_events": segment_events,
                        "auto_vad": auto_vad,
                        "vad_engine": "fsmn-vad" if auto_vad else None,
                    }
                )
            elif event_type == "audio.gap":
                raw_gap_ms = event.get("duration_ms", 0)
                try:
                    if isinstance(raw_gap_ms, bool):
                        raise ValueError
                    gap_ms = float(raw_gap_ms)
                except (TypeError, ValueError):
                    gap_ms = 0
                if not math.isfinite(gap_ms) or gap_ms <= 0 or gap_ms > 60_000:
                    await websocket.send_json({"type": "error", "code": "invalid_audio_gap"})
                    continue
                if audio_buffer:
                    await websocket.send_json(
                        {"type": "error", "code": "audio_gap_requires_commit"}
                    )
                    continue
                session_offset_ms += gap_ms
                stream_offset_ms += gap_ms
                if vad_segmenter is not None:
                    vad_segmenter.reset()
                    vad_base_offset_ms = session_offset_ms
                vad_pending_end_ms = None
                await websocket.send_json(
                    {"type": "audio.gap", "start_ms": round(session_offset_ms - gap_ms, 3), "end_ms": round(session_offset_ms, 3)}
                )
            elif event_type == "commit":
                if not audio_buffer:
                    await websocket.send_json({"type": "error", "code": "invalid_audio"})
                    continue
                started = utterance_started or time.monotonic()
                turn_id = current_turn_id or timeline.new_turn()
                utterance_offset_ms = session_offset_ms
                if not await _process_stream_commit(
                    websocket,
                    audio_bytes=bytes(audio_buffer),
                    language=language,
                    timeline=timeline,
                    speaker_mapper=speaker_mapper,
                    voiceprint_tenant=voiceprint_tenant,
                    turn_id=turn_id,
                    offset_ms=utterance_offset_ms,
                    started=started,
                    first_partial_ms=first_partial_ms,
                    emit_segment_events=segment_events,
                ):
                    # server_busy 或推理失败时保留已接收音频，客户端可重试 commit。
                    continue
                session_offset_ms += len(audio_buffer) / 2 / TARGET_FS * 1000
                audio_buffer.clear()
                last_partial_bytes = 0
                first_partial_ms = None
                utterance_started = None
                current_turn_id = None
                if vad_segmenter is not None:
                    vad_segmenter.reset()
                vad_pending_end_ms = None
                session_offset_ms = stream_offset_ms
                vad_base_offset_ms = session_offset_ms
            elif event_type == "ping":
                await websocket.send_json({"type": "pong"})
            else:
                await websocket.send_json({"type": "error", "code": "unknown_event"})
    except WebSocketDisconnect:
        return


def _iter_openai_stream(
    settings: ServiceSettings, messages: List[Dict[str, str]]
) -> Iterator[str]:
    """读取 OpenAI 兼容 Chat Completions 的 SSE 文本增量。"""
    payload = json.dumps(
        {"model": settings.llm_model, "messages": messages, "stream": True},
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if settings.llm_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_api_key}"

    request = urllib.request.Request(
        _chat_completions_url(settings), data=payload, headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=settings.llm_timeout_seconds) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            event = json.loads(data)
            choices = event.get("choices", [])
            if not choices:
                continue
            content = choices[0].get("delta", {}).get("content")
            if isinstance(content, str) and content:
                yield content


def _iter_gpt_sovits_stream(
    settings: ServiceSettings,
    text: str,
    text_language: str,
    streaming_mode: int | None = None,
    voice: VoiceProfile | None = None,
) -> Iterator[bytes]:
    """调用 GPT-SoVITS v2 的流式 /tts 接口。"""
    selected_voice = voice or VOICE_PROFILES.resolve()
    payload = {
        "text": text,
        "text_lang": text_language,
        "ref_audio_path": selected_voice.ref_audio_path,
        "prompt_text": selected_voice.prompt_text,
        "prompt_lang": selected_voice.prompt_lang,
        "text_split_method": "cut5",
        "speed_factor": settings.tts_speed_factor,
        "media_type": "wav",
        "streaming_mode": (
            settings.tts_streaming_mode if streaming_mode is None else streaming_mode
        ),
        "parallel_infer": True,
    }
    request = urllib.request.Request(
        _tts_url(settings),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "audio/wav"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=settings.tts_timeout_seconds) as response:
        while chunk := response.read(8192):
            yield chunk


async def _stream_from_blocking_iterator(
    iterator_factory: Callable[[], Iterator[Union[str, bytes]]]
) -> AsyncIterator[Union[str, bytes]]:
    """把阻塞上游桥接到异步请求，并用有界队列提供背压。"""
    stream_queue: "queue.Queue[object]" = queue.Queue(
        maxsize=max(1, _get_int("SENSEVOICE_STREAM_QUEUE_SIZE", 8))
    )
    stop_event = threading.Event()

    def run_iterator() -> None:
        try:
            for item in iterator_factory():
                while not stop_event.is_set():
                    try:
                        stream_queue.put(item, timeout=0.2)
                        break
                    except queue.Full:
                        continue
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as error:
            while not stop_event.is_set():
                try:
                    stream_queue.put(error, timeout=0.2)
                    break
                except queue.Full:
                    continue
        except Exception as error:  # noqa: BLE001
            while not stop_event.is_set():
                try:
                    stream_queue.put(error, timeout=0.2)
                    break
                except queue.Full:
                    continue
        finally:
            while not stop_event.is_set():
                try:
                    stream_queue.put(STREAM_END, timeout=0.2)
                    break
                except queue.Full:
                    continue

    worker = threading.Thread(target=run_iterator, daemon=True)
    worker.start()
    try:
        while True:
            try:
                # 使用有界等待；否则消费者取消时，底层 executor 线程可能永久卡在 queue.get。
                item = await asyncio.to_thread(stream_queue.get, True, 0.2)
            except queue.Empty:
                if stop_event.is_set():
                    return
                continue
            if item is STREAM_END:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        stop_event.set()
        # 阻塞上游未必能立即感知 stop_event；等待桥接线程结束后再释放 TTS 门禁，避免共享实例重入。
        join_task = asyncio.create_task(asyncio.to_thread(worker.join))
        try:
            await asyncio.shield(join_task)
        except asyncio.CancelledError:
            # 取消不能中断已在运行的 urllib 线程，先等其按请求超时自然退出，再传播取消。
            await join_task
            raise


@asynccontextmanager
async def _managed_blocking_stream(
    iterator_factory: Callable[[], Iterator[Union[str, bytes]]]
) -> AsyncIterator[AsyncIterator[Union[str, bytes]]]:
    """保证调用方退出流式循环时，桥接线程已停止后再释放外层资源。"""
    stream = _stream_from_blocking_iterator(iterator_factory)
    try:
        yield stream
    finally:
        # async for 被取消时不一定会立即回收异步生成器，显式 aclose 才能及时 join 上游线程。
        await stream.aclose()


def _safe_speech_boundary(text: str, maximum: int = 80) -> int:
    """寻找不会优先截断英文单词或数字的短句边界。"""
    if len(text) <= maximum:
        return len(text)
    soft_boundaries = [match.end() for match in SOFT_SPLIT_PATTERN.finditer(text[:maximum])]
    if soft_boundaries:
        boundary = soft_boundaries[-1]
        if boundary >= maximum // 2:
            return boundary

    def is_ascii_token_char(value: str) -> bool:
        return bool(value) and value.isascii() and value.isalnum()

    # 只在切点两侧都是 ASCII 单词/数字字符时回退，中文连续字符不受影响。
    if maximum < len(text) and is_ascii_token_char(text[maximum - 1]) and is_ascii_token_char(text[maximum]):
        boundary = maximum - 1
        while boundary > 0 and is_ascii_token_char(text[boundary - 1]):
            boundary -= 1
        if boundary >= maximum // 2:
            return boundary
    return maximum


def _take_ready_speech_segments(buffer: str, final: bool = False) -> tuple[List[str], str]:
    """优先按句子发起 TTS；无标点的长回复也会及时切分。"""
    segments: List[str] = []
    cursor = 0
    for match in SENTENCE_END_PATTERN.finditer(buffer):
        candidate = buffer[cursor : match.end()].strip()
        if candidate:
            segments.append(candidate)
        cursor = match.end()
    remainder = buffer[cursor:]

    while len(remainder) >= 80:
        boundary = _safe_speech_boundary(remainder, 80)
        segments.append(remainder[:boundary].strip())
        remainder = remainder[boundary:]
    if final and remainder.strip():
        segments.append(remainder.strip())
        remainder = ""
    return segments, remainder


def _normalise_tts_preference(value: Any) -> str:
    """校验业务侧 TTS 语言，并保留 auto 的按识别语言选择语义。"""
    if value is None:
        language = "auto"
    elif not isinstance(value, str):
        raise ValueError("TTS 语言必须是字符串")
    else:
        language = value.strip().lower()
    if language in SUPPORTED_TTS_LANGUAGES:
        return language
    # 兼容已直接使用 GPT-SoVITS 语言码的调用方，并统一回业务语言名。
    reverse = {
        "all_zh": "zh",
        "all_yue": "yue",
        "all_ja": "ja",
        "all_ko": "ko",
        "en": "en",
    }
    if language in SUPPORTED_TTS_ENGINE_LANGUAGES:
        return reverse[language]
    raise ValueError("不支持的 TTS 语言")


def _normalise_tts_language(value: Any) -> str:
    """将业务语言名转换成 GPT-SoVITS 的 text_lang。"""
    preference = _normalise_tts_preference(value)
    return TTS_AUTO_LANGUAGE_MAP[preference]


def _resolve_tts_language(preferred_language: Any, detected_language: str) -> str:
    """按显式配置或 ASR 检测结果选择 GPT-SoVITS 语言码。"""
    preference = _normalise_tts_preference(preferred_language)
    if preference != "auto":
        return TTS_AUTO_LANGUAGE_MAP[preference]
    return TTS_AUTO_LANGUAGE_MAP.get(detected_language, "all_zh")


def _websocket_api_key(websocket: WebSocket) -> tuple[str, Optional[str]]:
    """兼容服务端 Header 鉴权与浏览器子协议鉴权两种接入方式。"""
    header_key = websocket.headers.get("x-api-key", "")
    if header_key:
        return header_key, None

    subprotocols = websocket.scope.get("subprotocols", [])
    if len(subprotocols) >= 2 and subprotocols[0] == "sensevoice":
        return subprotocols[1], "sensevoice"
    return "", None


def _normalise_request_id(value: Any) -> str:
    """限制客户端请求 ID，避免把任意大字符串回显到事件流。"""
    if value is None or value == "":
        return secrets.token_urlsafe(10)
    if not isinstance(value, str):
        raise ValueError("request_id 必须是字符串")
    request_id = value.strip()
    if not request_id or len(request_id) > 128:
        raise ValueError("request_id 长度无效")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in request_id):
        raise ValueError("request_id 包含控制字符")
    return request_id


def _public_asr_result(result: Mapping[str, Any]) -> Dict[str, Any]:
    """将统一引擎结果转成 WebSocket 可公开的字段。"""
    return {
        "text": result.get("text", ""),
        "raw_text": result.get("raw_text", ""),
        "language": result.get("language", "auto"),
        "duration_seconds": result.get("duration_seconds", 0),
        "segments": result.get("segments", []),
    }


def _segment_event(
    event_name: str,
    segment: Mapping[str, Any],
    *,
    turn_id: str | None,
    revision: int,
    segment_id: str | None = None,
) -> Dict[str, Any]:
    """把内部句段转换为管理端规划的独立事件，不暴露声纹向量。"""
    raw_status = str(segment.get("speaker_status", "unknown"))
    speaker_id = segment.get("speaker")
    public_status = {
        "identified": "matched",
        "anonymous": "unknown",
    }.get(raw_status, raw_status)
    return {
        "type": f"asr.segment.{event_name}",
        "segment_id": segment_id or segment.get("segment_id"),
        "turn_id": turn_id or segment.get("turn_id"),
        "revision": revision,
        "start_ms": segment.get("start_ms"),
        "end_ms": segment.get("end_ms"),
        "text": segment.get("text", ""),
        "language": segment.get("language", "auto"),
        "speaker": {
            "session_speaker_id": speaker_id,
            "speaker_profile_id": speaker_id if raw_status == "identified" else None,
            "status": public_status,
            "confidence": segment.get("match_score"),
        },
        "words": segment.get("words", []),
    }


def _offset_partial_segment(segment: Mapping[str, Any], offset_ms: float) -> Dict[str, Any]:
    """把当前话轮的局部 partial 时间转换为会话绝对时间。"""
    item = dict(segment)
    for key in ("start_ms", "end_ms"):
        try:
            value = float(item[key])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            item[key] = round(value + offset_ms, 3)
    return item


def _attach_anonymous_speakers(
    result: Dict[str, Any],
    mapper: SessionSpeakerMapper,
    tenant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """用 CAM++ 向量映射会话编号，并在可信租户下尝试已登记人员匹配。"""
    rows = result.get("_speaker_embeddings", [])
    embedding_kind = result.get("_speaker_embedding_kind", "center")
    output: List[Dict[str, Any]] = []
    mapper.begin_inference()
    for segment in result.get("segments", []):
        item = dict(segment)
        has_local_speaker = "spk" in item
        local = item.get("spk", "unknown")
        embedding = None
        if embedding_kind == "center":
            try:
                index = int(local)
                if 0 <= index < len(rows):
                    embedding = rows[index]
            except (TypeError, ValueError):
                pass
        if has_local_speaker or embedding is not None:
            item["speaker"] = mapper.resolve(local, embedding)
            item["speaker_status"] = "anonymous"
            if embedding is not None and tenant_id:
                try:
                    match = ASR_RUNTIME.voiceprints.identify(
                        tenant_id,
                        embedding,
                        model_id=ASR_RUNTIME.engine.config.spk_model,
                        model_revision=ASR_RUNTIME.engine.config.spk_model_revision,
                        preprocess_version="pcm16k-mono-f32-v1",
                    )
                except VoiceprintError:
                    match = None
                if match is not None:
                    item["speaker_status"] = match.status
                    item["match_score"] = match.score
                    if match.person_id:
                        item["speaker"] = match.person_id
        item.pop("spk", None)
        item.pop("timestamp", None)
        output.append(item)
    return output


@dataclass
class RealtimeSession:
    """一个 WebSocket 连接对应一段独立的多轮会话。"""

    asr_language: str = "auto"
    tts_text_lang: str = "auto"
    tts_output_format: str = "wav"
    tts_streaming_mode: int = 3
    voice_id: Optional[str] = None
    segment_events: bool = False
    history: List[Dict[str, str]] = field(default_factory=list)
    timeline: SessionTimeline = field(default_factory=SessionTimeline)
    speaker_mapper: SessionSpeakerMapper = field(default_factory=SessionSpeakerMapper)
    audio_offset_ms: float = 0.0
    voiceprint_tenant: Optional[str] = None

    def append_message(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})
        message_limit = max(2, SETTINGS.max_history_turns * 2)
        if len(self.history) > message_limit:
            self.history = self.history[-message_limit:]

    def llm_messages(self) -> List[Dict[str, str]]:
        messages = []
        if SETTINGS.llm_system_prompt:
            messages.append({"role": "system", "content": SETTINGS.llm_system_prompt})
        return [*messages, *self.history]


async def _send_tts_segment(
    websocket: WebSocket,
    segment_index: int,
    text: str,
    text_language: str,
    voice: VoiceProfile | None = None,
    output_format: str = "wav",
    streaming_mode: int | None = None,
    cancel_event: asyncio.Event | None = None,
) -> bool:
    """先发送元数据，再按协商格式转发 GPT-SoVITS 的音频流。"""
    if output_format not in TTS_OUTPUT_FORMATS:
        await websocket.send_json(
            {"type": "tts.error", "segment_index": segment_index, "code": "invalid_output_format"}
        )
        return False
    if cancel_event is not None and cancel_event.is_set():
        return True

    if output_format == "pcm_s16le":
        started = time.monotonic()
        decoder = StreamingWavDecoder()
        start_sent = False
        first_audio_ms: int | None = None
        chunk_count = 0
        pcm_audio_bytes = 0
        cancelled = False
        try:
            async with TTS_GATE.slot():
                async with _managed_blocking_stream(
                    lambda: _iter_gpt_sovits_stream(
                        SETTINGS,
                        text,
                        text_language,
                        streaming_mode=streaming_mode,
                        voice=voice,
                    )
                ) as audio_stream:
                    async for wav_chunk in audio_stream:
                        if cancel_event is not None and cancel_event.is_set():
                            # GPT-SoVITS 当前没有可靠的逐请求停止接口，取消后继续消费并丢弃上游数据。
                            cancelled = True
                            continue
                        pcm_chunk = decoder.feed(wav_chunk)
                        if decoder.ready and not start_sent:
                            await websocket.send_json(
                                {
                                    "type": "tts.start",
                                    "segment_index": segment_index,
                                    "format": "pcm_s16le",
                                    "sample_rate": decoder.sample_rate,
                                    "channels": decoder.channels,
                                    "sample_width": decoder.sample_width,
                                    "text": text,
                                    "language": text_language,
                                    "voice_id": voice.voice_id if voice else VOICE_PROFILES.default_voice_id,
                                    "voice_version": voice.version if voice else None,
                                }
                            )
                            start_sent = True
                        if pcm_chunk:
                            if first_audio_ms is None:
                                first_audio_ms = round((time.monotonic() - started) * 1000)
                            await websocket.send_bytes(pcm_chunk)
                            chunk_count += 1
                            pcm_audio_bytes += len(pcm_chunk)
                    decoder.finish()
        except RuntimeError as error:
            if cancel_event is not None and cancel_event.is_set():
                return True
            await websocket.send_json(
                {
                    "type": "tts.error",
                    "segment_index": segment_index,
                    "code": "server_busy" if str(error) == "server_busy" else "tts_failed",
                }
            )
            return False
        except (TTSStreamError, urllib.error.URLError, urllib.error.HTTPError):
            LOGGER.exception("GPT-SoVITS PCM 流式合成失败")
            await websocket.send_json(
                {"type": "tts.error", "segment_index": segment_index, "code": "tts_failed"}
            )
            return False
        if cancelled:
            return True
        if not start_sent or chunk_count == 0:
            await websocket.send_json(
                {"type": "tts.error", "segment_index": segment_index, "code": "tts_empty"}
            )
            return False
        await websocket.send_json(
            {
                "type": "tts.end",
                "segment_index": segment_index,
                "audio_chunks": chunk_count,
                "first_audio_ms": first_audio_ms,
                "total_ms": round((time.monotonic() - started) * 1000),
                "audio_duration_ms": _pcm_duration_ms(
                    pcm_audio_bytes,
                    sample_rate=decoder.sample_rate,
                    channels=decoder.channels,
                    sample_width=decoder.sample_width,
                ),
            }
        )
        return True

    started = time.monotonic()
    await websocket.send_json(
        {
            "type": "tts.start",
            "segment_index": segment_index,
            "format": "wav",
            "text": text,
            "language": text_language,
            "voice_id": voice.voice_id if voice else VOICE_PROFILES.default_voice_id,
            "voice_version": voice.version if voice else None,
        }
    )
    decoder = StreamingWavDecoder()
    pcm_audio = bytearray()
    first_audio_ms: int | None = None
    cancelled = False
    try:
        async with TTS_GATE.slot():
            async with _managed_blocking_stream(
                lambda: _iter_gpt_sovits_stream(
                    SETTINGS,
                    text,
                    text_language,
                    streaming_mode=streaming_mode,
                    voice=voice,
                )
            ) as audio_stream:
                async for audio_chunk in audio_stream:
                    if cancel_event is not None and cancel_event.is_set():
                        # 不能中止上游线程时，至少丢弃剩余音频，避免旧话轮串入下一轮。
                        cancelled = True
                        continue
                    pcm_chunk = decoder.feed(audio_chunk)
                    if pcm_chunk and first_audio_ms is None:
                        first_audio_ms = round((time.monotonic() - started) * 1000)
                    pcm_audio.extend(pcm_chunk)
        if cancelled:
            return True
        decoder.finish()
        wav_audio = _pack_pcm_wav(
            bytes(pcm_audio),
            sample_rate=int(decoder.sample_rate or 0),
            channels=int(decoder.channels or 0),
            sample_width=int(decoder.sample_width or 0),
        )
        for start in range(0, len(wav_audio), 8192):
            await websocket.send_bytes(wav_audio[start : start + 8192])
    except RuntimeError as error:
        if cancel_event is not None and cancel_event.is_set():
            return True
        await websocket.send_json(
            {
                "type": "tts.error",
                "segment_index": segment_index,
                "code": "server_busy" if str(error) == "server_busy" else "tts_failed",
            }
        )
        return False
    except TTSStreamError:
        if cancel_event is not None and cancel_event.is_set():
            return True
        LOGGER.exception("GPT-SoVITS WAV 流解析失败")
        await websocket.send_json(
            {"type": "tts.error", "segment_index": segment_index, "code": "tts_failed"}
        )
        return False
    except Exception:  # noqa: BLE001
        if cancel_event is not None and cancel_event.is_set():
            return True
        LOGGER.exception("GPT-SoVITS 流式合成失败")
        await websocket.send_json(
            {"type": "tts.error", "segment_index": segment_index, "code": "tts_failed"}
        )
        return False
    if not pcm_audio:
        await websocket.send_json(
            {"type": "tts.error", "segment_index": segment_index, "code": "tts_empty"}
        )
        return False
    await websocket.send_json(
        {
            "type": "tts.end",
            "segment_index": segment_index,
            "audio_chunks": max(1, math.ceil(len(wav_audio) / 8192)),
            "first_audio_ms": first_audio_ms,
            "total_ms": round((time.monotonic() - started) * 1000),
            "audio_duration_ms": _pcm_duration_ms(
                len(pcm_audio),
                sample_rate=decoder.sample_rate,
                channels=decoder.channels,
                sample_width=decoder.sample_width,
            ),
        }
    )
    return True


async def _run_tts_stream_request(
    websocket: WebSocket,
    *,
    request_id: str,
    text: str,
    text_language: str,
    streaming_mode: int,
    voice: VoiceProfile,
    cancel_event: asyncio.Event | None = None,
) -> None:
    """把 GPT-SoVITS 的 WAV 流拆成可直接交给 AudioWorklet 的 PCM16。"""
    started = time.monotonic()
    decoder = StreamingWavDecoder()
    start_sent = False
    first_audio_ms: int | None = None
    chunk_count = 0
    pcm_audio_bytes = 0
    cancelled = False
    try:
        async with TTS_GATE.slot():
            async with _managed_blocking_stream(
                lambda: _iter_gpt_sovits_stream(
                    SETTINGS, text, text_language, streaming_mode, voice
                )
            ) as audio_stream:
                async for wav_chunk in audio_stream:
                    if cancel_event is not None and cancel_event.is_set():
                        # 继续消费上游直到连接自然结束，避免未可取消的阻塞线程释放门禁后串行失效。
                        cancelled = True
                        continue
                    pcm_chunk = decoder.feed(wav_chunk)
                    if decoder.ready and not start_sent:
                        await websocket.send_json(
                            {
                                "type": "tts.start",
                                "request_id": request_id,
                                "format": "pcm_s16le",
                                "sample_rate": decoder.sample_rate,
                                "channels": decoder.channels,
                                "sample_width": decoder.sample_width,
                                "voice_id": voice.voice_id,
                                "voice_version": voice.version,
                                "text": text,
                                "language": text_language,
                            }
                        )
                        start_sent = True
                    if pcm_chunk:
                        if first_audio_ms is None:
                            first_audio_ms = round((time.monotonic() - started) * 1000)
                        await websocket.send_bytes(pcm_chunk)
                        chunk_count += 1
                        pcm_audio_bytes += len(pcm_chunk)
                if not cancelled:
                    decoder.finish()
    except RuntimeError as error:
        if cancel_event is not None and cancel_event.is_set():
            await websocket.send_json({"type": "tts.cancelled", "request_id": request_id})
            return
        await websocket.send_json(
            {
                "type": "tts.error",
                "request_id": request_id,
                "code": "server_busy" if str(error) == "server_busy" else "tts_failed",
            }
        )
        return
    except (TTSStreamError, urllib.error.URLError, urllib.error.HTTPError) as error:
        if cancel_event is not None and cancel_event.is_set():
            await websocket.send_json({"type": "tts.cancelled", "request_id": request_id})
            return
        LOGGER.warning("TTS 流解析或上游请求失败: %s", type(error).__name__)
        await websocket.send_json(
            {"type": "tts.error", "request_id": request_id, "code": "tts_failed"}
        )
        return
    if cancelled:
        await websocket.send_json({"type": "tts.cancelled", "request_id": request_id})
        return
    if not start_sent:
        await websocket.send_json(
            {"type": "tts.error", "request_id": request_id, "code": "tts_empty"}
        )
        return
    await websocket.send_json(
        {
            "type": "tts.end",
            "request_id": request_id,
            "audio_chunks": chunk_count,
            "first_audio_ms": first_audio_ms,
            "total_ms": round((time.monotonic() - started) * 1000),
            "audio_duration_ms": _pcm_duration_ms(
                pcm_audio_bytes,
                sample_rate=decoder.sample_rate,
                channels=decoder.channels,
                sample_width=decoder.sample_width,
            ),
        }
    )


@app.websocket("/api/v1/tts/stream")
async def stream_tts(websocket: WebSocket) -> None:
    """独立 TTS 流；文本增量按请求合成，音频二进制帧为 PCM16。"""
    if not SETTINGS.service_api_key:
        await websocket.close(code=1011, reason="服务未配置 API 密钥")
        return
    provided_key, accepted_subprotocol = _websocket_api_key(websocket)
    if not constant_time_equal(provided_key, SETTINGS.service_api_key):
        await websocket.close(code=4401, reason="API 密钥无效")
        return
    missing_tts_settings = _tts_missing_settings()
    if missing_tts_settings:
        await websocket.accept(subprotocol=accepted_subprotocol)
        await websocket.send_json(
            {
                "type": "error",
                "code": "configuration_required",
                "missing_settings": missing_tts_settings,
            }
        )
        await websocket.close(code=1011)
        return

    await websocket.accept(subprotocol=accepted_subprotocol)
    default_voice = VOICE_PROFILES.default_voice_id
    await websocket.send_json(
        {
            "type": "session.ready",
            "capabilities": ["tts_stream", "voice_profiles", "cancel"],
            "audio_format": "pcm_s16le",
            "voices": VOICE_PROFILES.public_profiles(),
            "voice_id": default_voice,
            "streaming_modes": [1, 2, 3],
        }
    )

    voice_id = default_voice
    try:
        text_language = _normalise_tts_language(SETTINGS.tts_text_lang)
    except ValueError:
        await websocket.send_json(
            {"type": "error", "code": "unsupported_tts_language"}
        )
        await websocket.close(code=1011)
        return
    streaming_mode = SETTINGS.tts_streaming_mode
    receive_task: asyncio.Task[Any] | None = asyncio.create_task(websocket.receive())
    synthesis_task: asyncio.Task[Any] | None = None
    synthesis_request_id: str | None = None
    synthesis_cancel_event: asyncio.Event | None = None
    try:
        while True:
            tasks = {receive_task}
            if synthesis_task is not None:
                tasks.add(synthesis_task)
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

            if synthesis_task is not None and synthesis_task in done:
                try:
                    synthesis_task.result()
                except asyncio.CancelledError:
                    # 仅断开连接时会走到这里；普通取消由 cancel_event 协作完成。
                    pass
                except Exception:  # noqa: BLE001
                    LOGGER.exception("独立 TTS 任务失败")
                    if synthesis_request_id:
                        await websocket.send_json(
                            {"type": "tts.error", "request_id": synthesis_request_id, "code": "tts_failed"}
                        )
                synthesis_task = None
                synthesis_request_id = None
                synthesis_cancel_event = None

            if receive_task not in done:
                continue
            message = receive_task.result()
            receive_task = asyncio.create_task(websocket.receive())
            if message.get("type") == "websocket.disconnect":
                return
            if message.get("bytes") is not None:
                await websocket.send_json({"type": "error", "code": "tts_text_only"})
                continue
            raw_event = message.get("text")
            if raw_event is None:
                continue
            try:
                event = json.loads(raw_event)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "code": "invalid_json"})
                continue
            if not isinstance(event, dict):
                await websocket.send_json({"type": "error", "code": "invalid_event"})
                continue
            event_type = event.get("type")
            if event_type == "configure":
                if synthesis_task is not None:
                    await websocket.send_json({"type": "error", "code": "server_busy"})
                    continue
                requested_voice = event.get("voice_id", voice_id)
                try:
                    voice = VOICE_PROFILES.resolve(requested_voice)
                except VoiceProfileError as error:
                    await websocket.send_json({"type": "error", "code": "voice_unavailable", "message": str(error)})
                    continue
                try:
                    requested_language = _normalise_tts_language(
                        event.get("text_lang", text_language)
                    )
                except ValueError:
                    await websocket.send_json(
                        {"type": "error", "code": "unsupported_tts_language"}
                    )
                    continue
                if not voice.supports_language(requested_language):
                    await websocket.send_json({"type": "error", "code": "voice_language_unsupported"})
                    continue
                raw_mode = event.get("streaming_mode", streaming_mode)
                try:
                    if isinstance(raw_mode, bool):
                        raise ValueError
                    requested_mode = int(raw_mode)
                except (TypeError, ValueError):
                    await websocket.send_json({"type": "error", "code": "invalid_streaming_mode"})
                    continue
                if requested_mode not in (1, 2, 3):
                    await websocket.send_json({"type": "error", "code": "invalid_streaming_mode"})
                    continue
                voice_id, text_language, streaming_mode = voice.voice_id, requested_language, requested_mode
                await websocket.send_json(
                    {
                        "type": "session.configured",
                        "voice_id": voice_id,
                        "text_lang": text_language,
                        "streaming_mode": streaming_mode,
                    }
                )
            elif event_type in {"synthesize", "text.final"}:
                if synthesis_task is not None:
                    await websocket.send_json({"type": "error", "code": "server_busy"})
                    continue
                raw_text = event.get("text", "")
                if not isinstance(raw_text, str):
                    await websocket.send_json({"type": "error", "code": "invalid_text"})
                    continue
                text = raw_text.strip()
                if not text or len(text) > 2_000:
                    await websocket.send_json({"type": "error", "code": "invalid_text"})
                    continue
                try:
                    voice = VOICE_PROFILES.resolve(voice_id)
                except VoiceProfileError as error:
                    await websocket.send_json({"type": "error", "code": "voice_unavailable", "message": str(error)})
                    continue
                try:
                    synthesis_request_id = _normalise_request_id(event.get("request_id"))
                except ValueError:
                    await websocket.send_json({"type": "error", "code": "invalid_request_id"})
                    continue
                synthesis_cancel_event = asyncio.Event()
                synthesis_task = asyncio.create_task(
                    _run_tts_stream_request(
                        websocket,
                        request_id=synthesis_request_id,
                        text=text,
                        text_language=text_language,
                        streaming_mode=streaming_mode,
                        voice=voice,
                        cancel_event=synthesis_cancel_event,
                    )
                )
            elif event_type == "cancel":
                try:
                    raw_request_id = event.get("request_id")
                    if raw_request_id is None or raw_request_id == "":
                        raise ValueError("request_id 不能为空")
                    requested_id = _normalise_request_id(raw_request_id)
                except ValueError:
                    await websocket.send_json({"type": "error", "code": "invalid_request_id"})
                    continue
                if synthesis_task is not None and requested_id == synthesis_request_id:
                    if synthesis_cancel_event is not None:
                        synthesis_cancel_event.set()
                else:
                    await websocket.send_json({"type": "tts.cancelled", "request_id": requested_id})
            elif event_type == "ping":
                await websocket.send_json({"type": "pong"})
            else:
                await websocket.send_json({"type": "error", "code": "unknown_event"})
    except WebSocketDisconnect:
        return
    finally:
        if receive_task is not None:
            receive_task.cancel()
            try:
                await receive_task
            except (asyncio.CancelledError, WebSocketDisconnect):
                pass
            except Exception:  # noqa: BLE001
                LOGGER.debug("独立 TTS 接收任务清理时结束异常", exc_info=True)
        if synthesis_task is not None:
            synthesis_task.cancel()
            try:
                await synthesis_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                LOGGER.debug("独立 TTS 合成任务清理时结束异常", exc_info=True)


async def _run_dialogue_turn(
    websocket: WebSocket,
    session: RealtimeSession,
    audio_bytes: bytes,
    *,
    turn_id: str | None = None,
    emit_segment_events: bool = False,
    cancel_event: asyncio.Event | None = None,
) -> None:
    """完成单轮 ASR、LLM 与流式 TTS，并把结果按事件顺序回传。"""
    async def send_cancelled() -> None:
        """统一发送当前话轮取消事件，避免客户端误把旧音频当成新回复。"""
        payload: Dict[str, Any] = {"type": "turn.cancelled"}
        if turn_id is not None:
            payload["turn_id"] = turn_id
        payload["revision"] = session.timeline.next_revision()
        await websocket.send_json(payload)

    await websocket.send_json({"type": "asr.processing"})
    try:
        asr_result = await ASR_GATE.run(
            _transcribe_audio, audio_bytes, session.asr_language
        )
    except RuntimeError as error:
        await websocket.send_json(
            {"type": "error", "code": "server_busy" if str(error) == "server_busy" else "asr_failed"}
        )
        if str(error) == "server_busy":
            # ASR 尚未取得门禁槽位；由外层会话恢复原始音频，允许客户端稍后重试。
            raise RetryableTurnError from error
        return
    except ClientInputError as error:
        await websocket.send_json({"type": "error", "code": "invalid_audio", "message": str(error)})
        return
    except Exception:  # noqa: BLE001
        LOGGER.exception("ASR 推理失败")
        await websocket.send_json({"type": "error", "code": "asr_failed"})
        return

    if not isinstance(asr_result, Mapping):
        await websocket.send_json({"type": "error", "code": "asr_failed"})
        return

    if cancel_event is not None and cancel_event.is_set():
        # ASR 已经消费输入；即使取消回复，也必须推进时间轴，避免下一轮重叠。
        session.audio_offset_ms += _result_duration_ms(asr_result)
        await send_cancelled()
        return

    transcript = asr_result.get("text")
    if not isinstance(transcript, str):
        session.audio_offset_ms += _result_duration_ms(asr_result)
        await websocket.send_json({"type": "error", "code": "asr_failed", "turn_id": turn_id})
        return
    if not RICH_TEXT_TOKEN_PATTERN.sub("", transcript).strip():
        session.audio_offset_ms += _result_duration_ms(asr_result)
        await websocket.send_json(
            {
                "type": "asr.no_speech",
                "turn_id": turn_id,
                "revision": session.timeline.next_revision(),
            }
        )
        return

    turn_id = turn_id or session.timeline.new_turn()
    try:
        asr_result["segments"] = _attach_anonymous_speakers(
            asr_result, session.speaker_mapper, session.voiceprint_tenant
        )
        asr_result["segments"] = [
            item.to_dict()
            for item in session.timeline.build_segments(
                asr_result["segments"],
                turn_id=turn_id,
                offset_ms=session.audio_offset_ms,
                language=asr_result.get("language", session.asr_language),
            )
        ]
    except (TypeError, ValueError, KeyError):
        # 模型结果不符合时间线契约时仅失败当前话轮，连接仍可继续使用。
        LOGGER.exception("完整对话 ASR 结果整理失败")
        session.audio_offset_ms += _result_duration_ms(asr_result)
        await websocket.send_json(
            {"type": "error", "code": "asr_failed", "turn_id": turn_id}
        )
        return
    # 输入话轮已经消费，即使随后取消回复，也必须推进时间轴并保留用户消息。
    session.audio_offset_ms += _result_duration_ms(asr_result)
    session.append_message("user", transcript)
    await websocket.send_json(
        {
            "type": "asr.final",
            **_public_asr_result(asr_result),
            "turn_id": turn_id,
            "revision": session.timeline.next_revision(),
        }
    )
    if emit_segment_events:
        for segment in asr_result["segments"]:
            await websocket.send_json(
                _segment_event(
                    "final",
                    segment,
                    turn_id=turn_id,
                    revision=session.timeline.next_revision(),
                )
            )
    if cancel_event is not None and cancel_event.is_set():
        await send_cancelled()
        return
    for segment in asr_result["segments"]:
        if segment.get("speaker_status") in {"identified", "unknown", "ambiguous"}:
            await websocket.send_json(
                {
                    "type": "speaker.update",
                    "segment_id": segment.get("segment_id"),
                    "turn_id": turn_id,
                    "revision": session.timeline.next_revision(),
                    "speaker": segment.get("speaker"),
                    "speaker_status": segment.get("speaker_status"),
                    "match_score": segment.get("match_score"),
                }
            )
    assistant_text = ""
    speech_buffer = ""
    tts_language = _resolve_tts_language(session.tts_text_lang, asr_result["language"])
    try:
        tts_voice = VOICE_PROFILES.resolve(session.voice_id)
    except VoiceProfileError as error:
        await websocket.send_json({"type": "error", "code": "voice_unavailable", "message": str(error)})
        return
    if not tts_voice.supports_language(tts_language):
        await websocket.send_json(
            {"type": "error", "code": "voice_language_unsupported", "voice_id": tts_voice.voice_id}
        )
        return
    # LLM 接收与 TTS 合成解耦，但 GPT-SoVITS 实例仍按队列串行，避免共享状态串音。
    tts_queue = BoundedSpeechQueue(
        max_items=max(1, _get_int("SENSEVOICE_TTS_QUEUE_SIZE", 4)),
        max_seconds=max(1.0, _get_float("SENSEVOICE_TTS_QUEUE_MAX_SECONDS", 30.0)),
        chars_per_second=max(
            1.0, _get_float("SENSEVOICE_TTS_ESTIMATED_CHARS_PER_SECOND", 12.0)
        ),
    )

    tts_failed = False
    tts_queue_limit = False
    llm_output_limit = False
    llm_limit_error_sent = False

    async def enqueue_tts_segment(segment_index: int, segment: str) -> bool:
        """把短句放入有界队列；配置过小导致单句超预算时给出明确错误。"""
        nonlocal tts_queue_limit
        try:
            accepted = await tts_queue.put(
                (segment_index, segment), cancel_event=cancel_event
            )
            if not accepted and cancel_event is not None and cancel_event.is_set():
                # 取消是正常控制流，不应伪装成队列超限错误。
                return True
            return True
        except ValueError:
            tts_queue_limit = True
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "tts_queue_limit",
                    "message": "TTS 队列时长预算小于单个短句，请提高 SENSEVOICE_TTS_QUEUE_MAX_SECONDS",
                    "turn_id": turn_id,
                }
            )
            return False

    async def drain_tts_queue() -> None:
        nonlocal tts_failed
        while True:
            item, estimate = await tts_queue.get()
            try:
                if item is None:
                    return
                queued_index, queued_text = item
                if cancel_event is not None and cancel_event.is_set():
                    continue
                try:
                    succeeded = await _send_tts_segment(
                        websocket,
                        queued_index,
                        queued_text,
                        tts_language,
                        voice=tts_voice,
                        output_format=session.tts_output_format,
                        streaming_mode=session.tts_streaming_mode,
                        cancel_event=cancel_event,
                    )
                    if not succeeded and (cancel_event is None or not cancel_event.is_set()):
                        tts_failed = True
                except Exception:  # noqa: BLE001
                    # 客户端断开时仍需回收队列，不能让后续哨兵永久阻塞。
                    LOGGER.exception("TTS 队列任务失败")
                    if cancel_event is None or not cancel_event.is_set():
                        tts_failed = True
            finally:
                await tts_queue.task_done(estimate)

    tts_task = asyncio.create_task(drain_tts_queue())

    def cancel_tts_task(_done_task: asyncio.Task[Any]) -> None:
        """客户端断开或话轮被取消时，回收尚未结束的 TTS 队列任务。"""
        if not tts_task.done():
            tts_task.cancel()

    current_task = asyncio.current_task()
    if current_task is not None:
        current_task.add_done_callback(cancel_tts_task)
    segment_index = 0
    llm_failed = False
    try:
        async with _managed_blocking_stream(
            lambda: _iter_openai_stream(SETTINGS, session.llm_messages())
        ) as llm_stream:
            async for delta in llm_stream:
                if cancel_event is not None and cancel_event.is_set():
                    break
                remaining_chars = SETTINGS.max_llm_output_chars - len(assistant_text)
                if remaining_chars <= 0:
                    llm_output_limit = True
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": "llm_output_too_large",
                            "max_chars": SETTINGS.max_llm_output_chars,
                            "turn_id": turn_id,
                        }
                    )
                    llm_limit_error_sent = True
                    break
                if len(delta) > remaining_chars:
                    delta = delta[:remaining_chars]
                    llm_output_limit = True
                assistant_text += delta
                speech_buffer += delta
                await websocket.send_json({"type": "llm.delta", "delta": delta})
                segments, speech_buffer = _take_ready_speech_segments(speech_buffer)
                for segment in segments:
                    if not await enqueue_tts_segment(segment_index, segment):
                        break
                    segment_index += 1
                if tts_queue_limit or llm_output_limit:
                    if llm_output_limit and not llm_limit_error_sent:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "code": "llm_output_too_large",
                                "max_chars": SETTINGS.max_llm_output_chars,
                                "turn_id": turn_id,
                            }
                        )
                        llm_limit_error_sent = True
                    break
    except Exception:  # noqa: BLE001
        if cancel_event is not None and cancel_event.is_set():
            pass
        else:
            LOGGER.exception("LLM 流式请求失败")
            await websocket.send_json({"type": "error", "code": "llm_failed"})
            llm_failed = True

    if (
        (cancel_event is None or not cancel_event.is_set())
        and not tts_queue_limit
        and not llm_output_limit
    ):
        segments, _ = _take_ready_speech_segments(speech_buffer, final=True)
        for segment in segments:
            if not await enqueue_tts_segment(segment_index, segment):
                break
            segment_index += 1

    # 发送哨兵后等待队列排空，确保 turn.completed 不早于最后一个音频段。
    await tts_queue.put(None)
    await tts_task
    if cancel_event is not None and cancel_event.is_set():
        await send_cancelled()
        return
    if tts_queue_limit:
        return
    if llm_output_limit:
        return
    if tts_failed:
        await websocket.send_json({"type": "error", "code": "tts_failed", "turn_id": turn_id})
        return
    if llm_failed:
        return
    if not assistant_text.strip():
        await websocket.send_json({"type": "error", "code": "llm_empty", "turn_id": turn_id})
        return

    session.append_message("assistant", assistant_text)
    await websocket.send_json(
        {
            "type": "turn.completed",
            "text": assistant_text,
            "tts_segments": segment_index,
            "turn_id": turn_id,
            "revision": session.timeline.next_revision(),
        }
    )


@app.websocket("/api/v1/realtime")
async def realtime_dialogue(websocket: WebSocket):
    """接收二进制音频并以 JSON 事件与二进制 TTS 帧返回实时对话结果。"""
    if not SETTINGS.service_api_key:
        await websocket.close(code=1011, reason="服务未配置 API 密钥")
        return
    provided_key, accepted_subprotocol = _websocket_api_key(websocket)
    if not constant_time_equal(provided_key, SETTINGS.service_api_key):
        await websocket.close(code=4401, reason="API 密钥无效")
        return

    missing_settings = SETTINGS.missing_dialogue_settings()
    for name in _tts_missing_settings():
        if name not in missing_settings:
            missing_settings.append(name)
    if missing_settings:
        await websocket.accept(subprotocol=accepted_subprotocol)
        await websocket.send_json(
            {"type": "error", "code": "configuration_required", "missing_settings": missing_settings}
        )
        await websocket.close(code=1011)
        return

    await websocket.accept(subprotocol=accepted_subprotocol)
    session = RealtimeSession(
        tts_text_lang=SETTINGS.tts_text_lang,
        tts_streaming_mode=SETTINGS.tts_streaming_mode,
        voice_id=VOICE_PROFILES.default_voice_id,
        voiceprint_tenant=resolve_trusted_tenant(websocket.headers),
    )
    audio_buffer = bytearray()
    turn_task: asyncio.Task[Any] | None = None
    turn_cancel_event: asyncio.Event | None = None
    turn_audio: bytes | None = None
    await websocket.send_json(
        {
            "type": "session.ready",
            "asr_languages": ["auto", "zh", "en", "yue", "ja", "ko"],
            "capabilities": [
                "segments",
                "anonymous_speakers",
                "revisioned_events",
                "segment_events",
                "tts_voice_profiles",
                "tts_streaming_wav",
                "tts_streaming_pcm",
                "cancel",
            ],
            "tts_voice_profiles": VOICE_PROFILES.public_profiles(),
            "tts_voice_id": session.voice_id,
            "segment_events": session.segment_events,
            "audio_input": "发送完整的 WAV、MP3 或其他 SoundFile 可解码音频二进制帧，再发送 commit 事件",
            "audio_output": "按 tts_output_format 返回；pcm_s16le 可直接播放，wav 可拼接为音频段",
            "tts_output_formats": ["wav", "pcm_s16le"],
            "tts_output_format": session.tts_output_format,
            "tts_streaming_mode": session.tts_streaming_mode,
        }
    )

    receive_task: asyncio.Task[Any] | None = asyncio.create_task(websocket.receive())
    try:
        while True:
            # 对话生成和下一条客户端消息必须并发等待。否则 ASR 门禁繁忙等可重试
            # 错误会在客户端保持静默时无人回收，既留下未读取的 Task 异常，也无法恢复原音频。
            pending_tasks = {receive_task}
            if turn_task is not None:
                pending_tasks.add(turn_task)
            done, _ = await asyncio.wait(
                pending_tasks, return_when=asyncio.FIRST_COMPLETED
            )
            if turn_task is not None and turn_task in done:
                try:
                    turn_task.result()
                except asyncio.CancelledError:
                    pass
                except RetryableTurnError:
                    # 仅恢复明确未进入 ASR 的话轮；若期间收到新音频，保持原始顺序并重新限长。
                    pending_audio = (turn_audio or b"") + bytes(audio_buffer)
                    if len(pending_audio) <= SETTINGS.max_audio_bytes:
                        audio_buffer = bytearray(pending_audio)
                    else:
                        audio_buffer = bytearray(turn_audio or b"")
                        await websocket.send_json(
                            {"type": "error", "code": "audio_too_large"}
                        )
                except Exception:  # noqa: BLE001
                    LOGGER.exception("实时对话任务失败")
                    await websocket.send_json({"type": "error", "code": "dialogue_failed"})
                turn_task = None
                turn_cancel_event = None
                turn_audio = None
            if receive_task not in done:
                continue
            message = receive_task.result()
            receive_task = asyncio.create_task(websocket.receive())
            if message["type"] == "websocket.disconnect":
                return

            audio_chunk = message.get("bytes")
            if audio_chunk is not None:
                # 在追加前检查，避免异常大帧先复制进会话缓冲。完整对话没有自动
                # VAD 时钟，超限输入直接清空当前未提交话轮并等待下一轮。
                if len(audio_chunk) > max(0, SETTINGS.max_audio_bytes - len(audio_buffer)):
                    audio_buffer.clear()
                    await websocket.send_json(
                        {"type": "error", "code": "audio_too_large"}
                    )
                    continue
                audio_buffer.extend(audio_chunk)

            raw_event = message.get("text")
            if raw_event is None:
                continue
            try:
                event = json.loads(raw_event)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "code": "invalid_json"})
                continue
            if not isinstance(event, dict):
                await websocket.send_json({"type": "error", "code": "invalid_event"})
                continue

            event_type = event.get("type")
            if event_type == "configure":
                if turn_task is not None and not turn_task.done():
                    await websocket.send_json({"type": "error", "code": "turn_busy"})
                    continue
                if audio_buffer:
                    await websocket.send_json(
                        {"type": "error", "code": "configure_requires_empty_buffer"}
                    )
                    continue
                try:
                    requested_language = _normalise_asr_language(
                        event.get("language", session.asr_language)
                    )
                except ClientInputError:
                    await websocket.send_json(
                        {"type": "error", "code": "unsupported_asr_language"}
                    )
                    continue
                requested_segment_events = event.get(
                    "segment_events", session.segment_events
                )
                if not isinstance(requested_segment_events, bool):
                    await websocket.send_json(
                        {"type": "error", "code": "invalid_segment_events"}
                    )
                    continue
                try:
                    requested_tts_text_lang = _normalise_tts_preference(
                        event.get("tts_text_lang", session.tts_text_lang)
                    )
                except ValueError:
                    await websocket.send_json(
                        {"type": "error", "code": "unsupported_tts_language"}
                    )
                    continue
                raw_streaming_mode = event.get("streaming_mode", session.tts_streaming_mode)
                try:
                    if isinstance(raw_streaming_mode, bool):
                        raise ValueError
                    requested_streaming_mode = int(raw_streaming_mode)
                except (TypeError, ValueError):
                    await websocket.send_json(
                        {"type": "error", "code": "invalid_streaming_mode"}
                    )
                    continue
                if requested_streaming_mode not in (1, 2, 3):
                    await websocket.send_json(
                        {"type": "error", "code": "invalid_streaming_mode"}
                    )
                    continue
                requested_output_format = str(
                    event.get("tts_output_format", session.tts_output_format)
                ).strip().lower()
                if requested_output_format not in TTS_OUTPUT_FORMATS:
                    await websocket.send_json(
                        {"type": "error", "code": "invalid_tts_output_format"}
                    )
                    continue
                try:
                    requested_voice = VOICE_PROFILES.resolve(
                        event.get("voice_id", session.voice_id)
                    )
                except VoiceProfileError as error:
                    await websocket.send_json(
                        {"type": "error", "code": "voice_unavailable", "message": str(error)}
                    )
                    continue
                if requested_tts_text_lang != "auto" and not requested_voice.supports_language(
                    TTS_AUTO_LANGUAGE_MAP[requested_tts_text_lang]
                ):
                    await websocket.send_json(
                        {"type": "error", "code": "voice_language_unsupported"}
                    )
                    continue
                # 所有字段和音色均校验通过后再一次性提交，避免失败配置残留。
                session.asr_language = requested_language
                session.tts_text_lang = requested_tts_text_lang
                session.segment_events = requested_segment_events
                session.tts_output_format = requested_output_format
                session.tts_streaming_mode = requested_streaming_mode
                session.voice_id = requested_voice.voice_id
                await websocket.send_json(
                    {
                        "type": "session.configured",
                        "language": session.asr_language,
                        "tts_text_lang": session.tts_text_lang,
                        "tts_output_format": session.tts_output_format,
                        "streaming_mode": session.tts_streaming_mode,
                        "segment_events": session.segment_events,
                        "voice_id": session.voice_id,
                    }
                )
            elif event_type == "commit":
                if turn_task is not None and not turn_task.done():
                    await websocket.send_json({"type": "error", "code": "turn_busy"})
                    continue
                if not audio_buffer:
                    # 与独立实时 ASR 保持一致：空提交不创建空话轮，也不推进时间轴。
                    await websocket.send_json({"type": "error", "code": "invalid_audio"})
                    continue
                utterance = bytes(audio_buffer)
                audio_buffer.clear()
                turn_audio = utterance
                turn_id = session.timeline.new_turn()
                turn_cancel_event = asyncio.Event()
                turn_task = asyncio.create_task(
                    _run_dialogue_turn(
                        websocket,
                        session,
                        utterance,
                        turn_id=turn_id,
                        emit_segment_events=session.segment_events,
                        cancel_event=turn_cancel_event,
                    )
                )
            elif event_type == "cancel":
                if turn_task is not None and not turn_task.done():
                    if turn_cancel_event is not None:
                        turn_cancel_event.set()
                else:
                    await websocket.send_json({"type": "turn.cancelled"})
            elif event_type == "clear_history":
                if turn_task is not None and not turn_task.done():
                    await websocket.send_json({"type": "error", "code": "turn_busy"})
                    continue
                session.history.clear()
                await websocket.send_json({"type": "history.cleared"})
            elif event_type == "audio.gap":
                raw_gap_ms = event.get("duration_ms", 0)
                try:
                    if isinstance(raw_gap_ms, bool):
                        raise ValueError
                    gap_ms = float(raw_gap_ms)
                except (TypeError, ValueError):
                    gap_ms = 0
                if not math.isfinite(gap_ms) or gap_ms <= 0 or gap_ms > 60_000:
                    await websocket.send_json({"type": "error", "code": "invalid_audio_gap"})
                    continue
                if turn_task is not None and not turn_task.done():
                    await websocket.send_json({"type": "error", "code": "turn_busy"})
                    continue
                if audio_buffer:
                    await websocket.send_json(
                        {"type": "error", "code": "audio_gap_requires_commit"}
                    )
                    continue
                start_ms = session.audio_offset_ms
                session.audio_offset_ms += gap_ms
                await websocket.send_json(
                    {"type": "audio.gap", "start_ms": round(start_ms, 3), "end_ms": round(session.audio_offset_ms, 3)}
                )
            elif event_type == "ping":
                await websocket.send_json({"type": "pong"})
            else:
                await websocket.send_json({"type": "error", "code": "unknown_event"})
    except WebSocketDisconnect:
        return
    finally:
        if receive_task is not None:
            receive_task.cancel()
            try:
                await receive_task
            except (asyncio.CancelledError, WebSocketDisconnect):
                pass
            except Exception:  # noqa: BLE001
                LOGGER.debug("实时对话接收任务清理时结束异常", exc_info=True)
        if turn_task is not None:
            if not turn_task.done():
                turn_task.cancel()
            try:
                await turn_task
            except (asyncio.CancelledError, RetryableTurnError):
                pass
            except Exception:  # noqa: BLE001
                LOGGER.debug("实时对话任务清理时结束异常", exc_info=True)
