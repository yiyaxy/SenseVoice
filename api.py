"""兼容旧 HTTP ASR 接口，并把推理委托给统一语音运行时。"""

from __future__ import annotations

import os
from enum import Enum
from typing import List

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from typing_extensions import Annotated

from speech.audio import AudioDecodeError, decode_audio
from speech.auth import constant_time_equal
from speech.engine import EngineError
from speech.runtime import get_speech_runtime
from speech.session import ASR_INFERENCE_GATE
from speech.speaker import (
    SessionSpeakerMapper,
    VoiceprintError,
    resolve_trusted_tenant,
)
from speech.timeline import SessionTimeline

TARGET_FS = 16_000
regex = r"<\|.*?\|>"


class Language(str, Enum):
    auto = "auto"
    zh = "zh"
    en = "en"
    yue = "yue"
    ja = "ja"
    ko = "ko"
    nospeech = "nospeech"


app = FastAPI()

# 兼容仍从 api 模块读取这两个名称的离线脚本；新路由不再直接操作模型。
m = None
kwargs = {}
ASR_GATE = ASR_INFERENCE_GATE


def _service_api_key() -> str:
    """读取外部服务密钥；模板占位符不能被当成真实密钥。"""
    value = os.getenv("SENSEVOICE_API_KEY", "").strip()
    return "" if value.startswith("请替换") else value


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """即使直接运行 ``api:app``，兼容 HTTP ASR 也不能无鉴权暴露。"""
    public_paths = {
        "/",
        "/docs",
        "/docs/",
        "/redoc",
        "/redoc/",
        "/openapi.json",
        "/api/v1/health",
        "/api/v1/health/",
    }
    if request.method == "OPTIONS" or request.url.path in public_paths:
        return await call_next(request)
    service_api_key = _service_api_key()
    if not service_api_key:
        return JSONResponse(
            status_code=503,
            content={"detail": "服务未配置 SENSEVOICE_API_KEY，拒绝对外请求"},
        )
    provided_key = request.headers.get("X-API-Key", "")
    if not constant_time_equal(provided_key, service_api_key):
        return JSONResponse(status_code=401, content={"detail": "API 密钥无效"})
    return await call_next(request)


def _max_audio_bytes() -> int:
    """读取与实时接口一致的单请求音频上限，避免 multipart 无限读入内存。"""
    try:
        return max(1, int(os.getenv("SENSEVOICE_MAX_AUDIO_BYTES", str(20 * 1024 * 1024))))
    except ValueError:
        return 20 * 1024 * 1024


def _normalise_language(value: str | None) -> str:
    """兼容旧客户端的空语言字段，并统一大小写。"""
    language = (value or "auto").strip().lower()
    if language not in {item.value for item in Language}:
        raise ValueError("不支持的识别语言")
    return language


def rich_transcription_postprocess(text: str) -> str:
    """兼容旧调用方的文本清理入口。"""
    from speech.engine import _clean_text

    return _clean_text(text)


def _public_result(result: dict, *, include_segments: bool) -> dict:
    """移除声纹向量等内部字段，保持旧 result 结构稳定。"""
    item = {
        key: value
        for key, value in result.items()
        if not key.startswith("_")
        and key
        not in {
            "timestamps_ready",
            "diarization_ready",
            "segments",
            "sentence_info",
            "spk_embedding",
            "spk_embedding_center",
        }
    }
    item.update(
        {
            "key": result.get("key"),
            "text": result.get("text", ""),
            "clean_text": result.get("clean_text", result.get("text", "")),
            "raw_text": result.get("raw_text", result.get("text", "")),
        }
    )
    if include_segments:
        item["segments"] = result.get("segments", [])
    return item


def _decorate_segments(result: dict, *, tenant_id: str | None, timeline: SessionTimeline) -> list:
    """为 HTTP 完整音频附加匿名/已登记身份和稳定句段编号。"""
    mapper = SessionSpeakerMapper()
    mapper.begin_inference()
    rows = result.get("_speaker_embeddings", [])
    embedding_kind = result.get("_speaker_embedding_kind", "center")
    decorated = []
    for raw in result.get("segments", []):
        item = dict(raw)
        local = item.get("spk")
        embedding = None
        if embedding_kind == "center":
            try:
                if local is not None and 0 <= int(local) < len(rows):
                    embedding = rows[int(local)]
            except (TypeError, ValueError):
                pass
        if local is not None or embedding is not None:
            item["speaker"] = mapper.resolve(local, embedding)
            item["speaker_status"] = "anonymous"
            if embedding is not None and tenant_id:
                try:
                    match = get_speech_runtime().voiceprints.identify(
                        tenant_id,
                        embedding,
                        model_id=get_speech_runtime().engine.config.spk_model,
                        model_revision=get_speech_runtime().engine.config.spk_model_revision,
                        preprocess_version="pcm16k-mono-f32-v1",
                    )
                except VoiceprintError:
                    match = None
                if match:
                    item["speaker_status"] = match.status
                    item["match_score"] = match.score
                    if match.person_id:
                        item["speaker"] = match.person_id
        item.pop("spk", None)
        item.pop("timestamp", None)
        decorated.append(item)
    turn_id = timeline.new_turn()
    return [item.to_dict() for item in timeline.build_segments(
        decorated, turn_id=turn_id, language=result.get("language", "auto")
    )]


@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <!DOCTYPE html>
    <html>
        <head><meta charset="utf-8"><title>SenseVoice API</title></head>
        <body><a href='./docs'>Documents of API</a></body>
    </html>
    """


@app.post("/api/v1/asr")
async def turn_audio_to_text(
    request: Request,
    files: Annotated[List[UploadFile], File(description="wav or mp3 audios")],
    keys: Annotated[str | None, Form(description="name of each audio joined with comma")] = None,
    lang: Annotated[str | None, Form(description="language of audio content")] = "auto",
    use_itn: Annotated[bool, Form(description="apply inverse text normalization")] = False,
    segments: Annotated[
        bool, Form(description="return sentence-level timeline segments")
    ] = False,
):
    """保留原有 multipart 字段，并按需追加句段级毫秒时间线。"""
    if not files:
        return JSONResponse(status_code=400, content={"detail": "至少上传一个音频文件"})
    try:
        language = _normalise_language(lang)
    except ValueError as error:
        return JSONResponse(status_code=422, content={"detail": str(error)})
    names = (
        [name.strip() for name in keys.split(",")]
        if keys
        else [file.filename or f"audio-{index}" for index, file in enumerate(files)]
    )
    if len(names) != len(files):
        return JSONResponse(status_code=400, content={"detail": "keys 数量必须与 files 一致"})
    if any(not name for name in names):
        return JSONResponse(status_code=400, content={"detail": "keys 不能包含空名称"})

    runtime = get_speech_runtime()
    output = []
    total_audio_bytes = 0
    max_audio_bytes = _max_audio_bytes()
    for file, key in zip(files, names):
        try:
            remaining = max_audio_bytes - total_audio_bytes
            if remaining <= 0:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "音频超过单次请求大小限制", "code": "audio_too_large"},
                )
            data = await file.read(remaining + 1)
            if len(data) > remaining:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "音频超过单次请求大小限制", "code": "audio_too_large"},
                )
            total_audio_bytes += len(data)
            waveform, _ = decode_audio(data, TARGET_FS)
            result = await ASR_GATE.run(
                runtime.transcribe, waveform, language, use_itn, key
            )
        except (AudioDecodeError, EngineError, ValueError) as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        except RuntimeError as error:
            if str(error) == "server_busy":
                return JSONResponse(status_code=503, content={"detail": "语音引擎繁忙"})
            return JSONResponse(status_code=500, content={"detail": "语音推理失败"})
        if segments:
            try:
                result["segments"] = _decorate_segments(
                    result,
                    tenant_id=resolve_trusted_tenant(request.headers),
                    timeline=SessionTimeline(),
                )
            except (TypeError, ValueError, KeyError):
                # 模型句段不符合公开契约时返回明确错误，不暴露框架堆栈。
                return JSONResponse(
                    status_code=502,
                    content={"detail": "语音结果时间线无效", "code": "asr_result_invalid"},
                )
        output.append(_public_result(result, include_segments=segments))
    return {"result": output}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=50000)
