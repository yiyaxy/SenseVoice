"""FunASR 的固定配置适配器。

FunASR 只在第一次真正推理时导入和加载，保证健康路由、文档生成以及离线工具不必
在导入阶段占用 GPU。适配器不会把声纹向量放进公开响应。
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

from .audio import TARGET_FS, waveform_to_numpy

LOGGER = logging.getLogger(__name__)
LANGUAGE_TOKEN_PATTERN = re.compile(r"<\|(zh|en|yue|ja|ko|nospeech)\|>")
RICH_TEXT_TOKEN_PATTERN = re.compile(r"<\|.*?\|>")
SUPPORTED_LANGUAGES = ("auto", "zh", "en", "yue", "ja", "ko", "nospeech")


class EngineError(RuntimeError):
    """模型未就绪、模型输出不符合契约或推理失败。"""


@dataclass(frozen=True)
class EngineConfig:
    """FunASR 组件和 revision；revision 可由部署环境显式锁定。"""

    model: str = "iic/SenseVoiceSmall"
    model_revision: str = "master"
    vad_model: str = "fsmn-vad"
    vad_model_revision: str = "master"
    # 句末标点模型为 FunASR 的句段时间线和说话人分段提供必要输入。
    punc_model: Optional[str] = "iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch"
    punc_model_revision: str = "master"
    spk_model: str = "cam++"
    spk_model_revision: str = "master"
    device: str = "auto"
    spk_mode: str = "vad_segment"
    target_fs: int = TARGET_FS
    vad_max_segment_ms: int = 30_000
    vad_chunk_ms: int = 60

    @classmethod
    def from_environment(cls) -> "EngineConfig":
        raw_punc_model = os.getenv("SENSEVOICE_PUNC_MODEL")
        punc_model = (
            cls.punc_model
            if raw_punc_model is None
            else (raw_punc_model.strip() or None)
        )
        try:
            vad_max_segment_ms = int(
                os.getenv("SENSEVOICE_VAD_MAX_SEGMENT_MS", str(cls.vad_max_segment_ms))
            )
        except ValueError:
            vad_max_segment_ms = cls.vad_max_segment_ms
        try:
            vad_chunk_ms = int(os.getenv("SENSEVOICE_VAD_CHUNK_MS", str(cls.vad_chunk_ms)))
        except ValueError:
            vad_chunk_ms = cls.vad_chunk_ms
        return cls(
            model=os.getenv("SENSEVOICE_MODEL", cls.model),
            model_revision=os.getenv("SENSEVOICE_MODEL_REVISION", cls.model_revision),
            vad_model=os.getenv("SENSEVOICE_VAD_MODEL", cls.vad_model),
            vad_model_revision=os.getenv(
                "SENSEVOICE_VAD_MODEL_REVISION", cls.vad_model_revision
            ),
            punc_model=punc_model,
            punc_model_revision=os.getenv(
                "SENSEVOICE_PUNC_MODEL_REVISION", cls.punc_model_revision
            ),
            spk_model=os.getenv("SENSEVOICE_SPK_MODEL", cls.spk_model),
            spk_model_revision=os.getenv(
                "SENSEVOICE_SPK_MODEL_REVISION", cls.spk_model_revision
            ),
            device=os.getenv("SENSEVOICE_DEVICE", cls.device),
            spk_mode=os.getenv("SENSEVOICE_SPK_MODE", cls.spk_mode),
            vad_max_segment_ms=vad_max_segment_ms,
            vad_chunk_ms=max(20, min(vad_chunk_ms, 200)),
        )


def _clean_text(raw_text: str) -> str:
    try:
        from funasr.utils.postprocess_utils import rich_transcription_postprocess

        return rich_transcription_postprocess(raw_text).strip()
    except ImportError:
        return RICH_TEXT_TOKEN_PATTERN.sub("", raw_text).strip()


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _tensor_to_rows(value: Any) -> List[List[float]]:
    """将 CAM++ 输出转成内部列表，失败时返回空列表而不是伪造向量。"""
    if value is None:
        return []
    if hasattr(value, "detach"):
        value = value.detach().cpu().tolist()
    elif hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, list):
        return []
    if value and isinstance(value[0], (int, float)):
        value = [value]
    rows: List[List[float]] = []
    for row in value:
        if isinstance(row, (list, tuple)):
            try:
                rows.append([float(item) for item in row])
            except (TypeError, ValueError):
                continue
    return rows


def _normalise_sentence_info(item: Mapping[str, Any]) -> List[Dict[str, Any]]:
    raw = item.get("sentence_info")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise EngineError("FunASR sentence_info 类型无效")
    output: List[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise EngineError("FunASR sentence_info 条目类型无效")
        start = _as_float(entry.get("start"))
        if start is None:
            start = _as_float(entry.get("start_ms"))
        end = _as_float(entry.get("end"))
        if end is None:
            end = _as_float(entry.get("end_ms"))
        if start is None or end is None or end <= start:
            # 不用文本长度估计时间，避免生成看似精确的伪时间线。
            raise EngineError("FunASR sentence_info 时间范围无效")
        raw_text = entry.get("text", "")
        segment: Dict[str, Any] = {
            "start_ms": round(start, 3),
            "end_ms": round(end, 3),
            # sentence_info 的文本也可能带语言/情绪控制符，公开句段时与顶层文本保持一致。
            "text": _clean_text("" if raw_text is None else str(raw_text)),
        }
        if entry.get("spk") is not None:
            segment["spk"] = entry.get("spk")
        if isinstance(entry.get("timestamp"), list):
            segment["timestamp"] = entry["timestamp"]
        output.append(segment)
    return output


class FunASREngine:
    """统一的 SenseVoice + FSMN-VAD + CAM++ 引擎。"""

    def __init__(
        self,
        config: Optional[EngineConfig] = None,
        model: Any = None,
        model_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.config = config or EngineConfig.from_environment()
        self._model = model
        self._model_factory = model_factory
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self._loaded = model is not None
        self._warmed = False
        self._legacy = False
        self._load_error: Optional[str] = None
        self._streaming_vad_model: Any = None
        self._streaming_vad_load_lock = threading.Lock()
        self._streaming_vad_error: Optional[str] = None
        self._timestamps_ready = False
        self._diarization_ready = False
        self._speaker_identification_ready = False

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def model(self) -> Any:
        self._ensure_loaded()
        return self._model

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            try:
                if self._model_factory is None:
                    from funasr import AutoModel

                    self._model_factory = AutoModel
                options: Dict[str, Any] = {
                    "model": self.config.model,
                    "model_revision": self.config.model_revision,
                    "vad_model": self.config.vad_model,
                    "vad_model_revision": self.config.vad_model_revision,
                    "vad_kwargs": {"max_single_segment_time": self.config.vad_max_segment_ms},
                    "punc_model": self.config.punc_model,
                    "punc_model_revision": self.config.punc_model_revision,
                    "spk_model": self.config.spk_model,
                    "spk_model_revision": self.config.spk_model_revision,
                    "spk_mode": self.config.spk_mode,
                    "device": self._resolved_device(),
                    "disable_update": True,
                    "disable_pbar": True,
                }
                # auto 交给 FunASR 判断 CUDA/CPU，避免在代码中猜测设备编号。
                self._model = self._model_factory(**options)
                if hasattr(self._model, "eval"):
                    self._model.eval()
                self._loaded = True
                self._load_error = None
            except Exception as error:  # noqa: BLE001
                # 保留仓库旧版 SenseVoiceSmall 作为可回退引擎，不覆盖用户的 model.py。
                try:
                    from model import SenseVoiceSmall

                    self._model, _ = SenseVoiceSmall.from_pretrained(
                        model=self.config.model, device=self._resolved_device()
                    )
                    if hasattr(self._model, "eval"):
                        self._model.eval()
                    self._legacy = True
                    self._loaded = True
                    self._load_error = None
                except Exception as fallback_error:  # noqa: BLE001
                    self._load_error = f"{type(fallback_error).__name__}: {fallback_error}"
                    raise EngineError("FunASR 模型加载失败") from error

    def _resolved_device(self) -> str:
        """把部署模板中的 auto 转成 FunASR 可识别的具体设备。"""
        if self.config.device != "auto":
            return self.config.device
        try:
            import torch

            return "cuda:0" if torch.cuda.is_available() else "cpu"
        except ImportError:
            return "cpu"

    def _ensure_streaming_vad_loaded(self) -> Any:
        """按需加载独立 FSMN-VAD，避免启动时重复占用显存。"""
        self._ensure_loaded()
        if self._streaming_vad_model is not None:
            return self._streaming_vad_model
        with self._streaming_vad_load_lock:
            if self._streaming_vad_model is not None:
                return self._streaming_vad_model
            try:
                from funasr import AutoModel

                self._streaming_vad_model = AutoModel(
                    model=self.config.vad_model,
                    model_revision=self.config.vad_model_revision,
                    device=self._resolved_device(),
                    disable_update=True,
                    disable_pbar=True,
                )
                self._streaming_vad_error = None
                return self._streaming_vad_model
            except Exception as error:  # noqa: BLE001
                self._streaming_vad_error = f"{type(error).__name__}: FSMN-VAD 加载失败"
                raise EngineError("FSMN-VAD 流式模型加载失败") from error

    def streaming_vad(self, waveform: Any, cache: Dict[str, Any], is_final: bool = False) -> Any:
        """使用独立 FSMN-VAD 处理一帧音频，并保留跨帧 cache。"""
        model = self._ensure_streaming_vad_loaded()
        audio = waveform_to_numpy(waveform)
        with self._infer_lock:
            try:
                return model.generate(
                    input=[audio],
                    cache=cache,
                    is_final=is_final,
                    chunk_size=self.config.vad_chunk_ms,
                )
            except Exception as error:  # noqa: BLE001
                raise EngineError("FSMN-VAD 流式推理失败") from error

    def warmup(self) -> bool:
        """构建组件并执行一次短静音推理，避免只凭模型对象存在就报告 ready。"""
        try:
            self._ensure_loaded()
            import numpy as np

            # 短静音只用于探测模型调用链，不作为质量样本或业务结果保存。
            self.transcribe(
                np.zeros(self.config.target_fs // 4, dtype="float32"),
                language="auto",
                use_itn=False,
                key="__warmup__",
            )
        except Exception as error:  # noqa: BLE001
            self._load_error = f"{type(error).__name__}: 预热推理失败"
            self._warmed = False
            return False
        self._warmed = True
        self._load_error = None
        return True

    def _call_model(
        self, waveform: Any, language: str, key: str, use_itn: bool
    ) -> Any:
        model = self.model
        data = [waveform if self._legacy else waveform_to_numpy(waveform)]
        options = {
            "language": language,
            "use_itn": use_itn,
            "ban_emo_unk": False,
            "fs": self.config.target_fs,
            "sentence_timestamp": True,
            # 保留 SenseVoice 的语言/情绪标签，避免标点模型覆盖后无法识别语言。
            "return_raw_text": True,
            "return_spk_res": True,
            "return_spk_center": True,
            "output_timestamp": False,
        }
        # AutoModel 的公开入口是 generate；兼容本仓库旧版模型的 inference。
        with self._infer_lock:
            try:
                if hasattr(model, "generate"):
                    return model.generate(input=data, **options)
                return model.inference(data_in=data, **options)
            except TypeError:
                # 旧版模型不认识部分可选开关，仍保留时间/声纹能力的最佳努力路径。
                options.pop("output_timestamp", None)
                options.pop("return_spk_res", None)
                options.pop("return_spk_center", None)
                options.pop("sentence_timestamp", None)
                options.pop("return_raw_text", None)
                if hasattr(model, "generate"):
                    return model.generate(input=data, **options)
                return model.inference(data_in=data, **options)
            except Exception as error:  # noqa: BLE001
                raise EngineError("FunASR 推理失败") from error

    def transcribe(
        self,
        waveform: Any,
        language: str = "auto",
        use_itn: bool = True,
        key: str = "audio",
    ) -> Dict[str, Any]:
        if language not in SUPPORTED_LANGUAGES:
            raise EngineError("不支持的识别语言")
        result = self._call_model(waveform, language, key, use_itn)
        if isinstance(result, Mapping):
            item: Mapping[str, Any] = result
        elif isinstance(result, list) and result and isinstance(result[0], Mapping):
            item = result[0]
        elif (
            isinstance(result, list)
            and result
            and isinstance(result[0], list)
            and result[0]
            and isinstance(result[0][0], Mapping)
        ):
            # 仓库旧版 SenseVoiceSmall.inference 返回按 batch 再包一层的列表。
            item = result[0][0]
        else:
            raise EngineError("FunASR 返回结构无效")

        raw_value = item.get("raw_text", item.get("text", ""))
        raw_text = "" if raw_value is None else str(raw_value)
        language_match = LANGUAGE_TOKEN_PATTERN.search(raw_text)
        language_field = item.get("language", item.get("lang"))
        language_field = str(language_field).strip().lower() if language_field else ""
        if language_field not in SUPPORTED_LANGUAGES:
            language_field = ""
        detected_language = (
            language_match.group(1)
            if language_match
            else (language_field or language)
        )
        segments = _normalise_sentence_info(item)
        center_value = item.get("spk_embedding_center")
        center_rows = _tensor_to_rows(center_value)
        chunk_rows = _tensor_to_rows(item.get("spk_embedding"))
        if center_value is not None:
            # 只有中心向量按修正后的 spk 标签对齐，才能安全用于人员识别。
            rows = center_rows
            embedding_kind = "center" if center_rows else "invalid"
        else:
            # v1.4.13 的 spk_embedding 是 1.5 秒分块向量，不能按 spk 下标直接映射。
            rows = chunk_rows
            embedding_kind = "chunk" if chunk_rows else "none"
        if segments:
            self._timestamps_ready = True
        if any("spk" in segment for segment in segments):
            self._diarization_ready = True
        if embedding_kind == "center" and rows:
            self._speaker_identification_ready = True
        # 真实推理成功后即可确认调用链可用；质量验收仍由独立指标决定。
        self._warmed = True
        return {
            # FunASR 组合 VAD 时可能忽略 key 并生成随机值，必须恢复旧接口的调用方 key。
            "key": key,
            "text": _clean_text(raw_text),
            "raw_text": raw_text,
            "clean_text": RICH_TEXT_TOKEN_PATTERN.sub("", raw_text).strip(),
            "language": detected_language,
            "segments": segments,
            "_speaker_embeddings": rows,
            "_speaker_embedding_chunks": chunk_rows,
            "_speaker_embedding_kind": embedding_kind,
            "timestamps_ready": bool(segments),
            "diarization_ready": any("spk" in segment for segment in segments),
        }

    def extract_embedding(self, waveform: Any) -> Optional[List[float]]:
        """提取单人 CAM++ 向量，仅供登记和内部匹配使用。"""
        # 旧版回退模型没有经过本方案的 CAM++ 中心向量契约；宁可拒绝登记，
        # 也不能把 SenseVoice 的分块向量写入持久库后伪装成可跨会话匹配。
        if self._legacy:
            return None
        model = self.model
        spk_model = getattr(model, "spk_model", None)
        if spk_model is not None and hasattr(model, "inference"):
            try:
                results = model.inference(
                    [waveform_to_numpy(waveform)],
                    model=spk_model,
                    kwargs=getattr(model, "spk_kwargs", {}),
                    fs=self.config.target_fs,
                )
                if isinstance(results, tuple):
                    results = results[0]
                if results and isinstance(results[0], Mapping):
                    rows = _tensor_to_rows(results[0].get("spk_embedding"))
                    if rows:
                        self._speaker_identification_ready = True
                        return rows[0]
            except Exception:  # noqa: BLE001
                LOGGER.exception("CAM++ 声纹向量提取失败")
        result = self.transcribe(waveform, language="auto", use_itn=False, key="voiceprint")
        # 只有中心向量按稳定 spk 标签对齐；分块向量不用于登记或跨会话识别。
        if result.get("_speaker_embedding_kind") != "center":
            return None
        rows = result.get("_speaker_embeddings", [])
        return rows[0] if rows else None

    def capability_status(self) -> Dict[str, Any]:
        """返回可安全公开的模型版本和就绪状态，不包含路径或密钥。"""
        return {
            "model_version": self.config.model_revision,
            "vad_model_version": self.config.vad_model_revision,
            "punc_model_version": (
                self.config.punc_model_revision if self.config.punc_model else None
            ),
            "spk_model_id": self.config.spk_model,
            "spk_model_version": self.config.spk_model_revision,
            "legacy_fallback": self._legacy,
            "asr_ready": self._warmed,
            "timestamps_ready": self._timestamps_ready and not self._legacy,
            "diarization_ready": self._diarization_ready and not self._legacy,
            "speaker_identification_ready": self._speaker_identification_ready
            and not self._legacy,
            "streaming_vad_ready": self._streaming_vad_model is not None,
            "streaming_vad_error": self._streaming_vad_error,
            "load_error": self._load_error,
        }
