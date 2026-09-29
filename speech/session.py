"""实时音频会话状态：采样偏移、在线 VAD 缓存和有界推理门禁。"""

from __future__ import annotations

import asyncio
import math
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional, Tuple

from .audio import TARGET_FS


@dataclass(frozen=True)
class AudioGap:
    """网络断片造成的缺口；缺口只推进时钟，不拼接音频。"""

    start_ms: float
    end_ms: float


@dataclass
class OnlineVADState:
    """FunASR 在线 FSMN-VAD 的 cache 和原始采样时钟。"""

    cache: dict = field(default_factory=dict)
    samples_seen: int = 0
    gaps: List[AudioGap] = field(default_factory=list)

    @property
    def offset_ms(self) -> float:
        return self.samples_seen * 1000.0 / TARGET_FS

    def append_samples(self, sample_count: int) -> None:
        if sample_count > 0:
            self.samples_seen += sample_count

    def add_gap(self, duration_ms: float) -> AudioGap:
        if isinstance(duration_ms, bool):
            raise ValueError("duration_ms 必须为正数")
        try:
            duration_ms = float(duration_ms)
        except (TypeError, ValueError) as error:
            raise ValueError("duration_ms 必须为正数") from error
        if not math.isfinite(duration_ms) or duration_ms <= 0:
            raise ValueError("duration_ms 必须为正数")
        # 时间轴以采样点为唯一时钟；极短缺口也至少占用一个采样点，避免记录了
        # 正缺口却不推进 offset，导致后续音频与缺口起点重叠。
        sample_count = max(1, round(duration_ms * TARGET_FS / 1000))
        actual_duration_ms = sample_count * 1000.0 / TARGET_FS
        gap = AudioGap(self.offset_ms, self.offset_ms + actual_duration_ms)
        self.gaps.append(gap)
        self.samples_seen += sample_count
        return gap


class InferenceGate:
    """共享模型使用有界并发门禁，过载时快速返回而非无限排队。"""

    def __init__(self, limit: int = 1) -> None:
        if limit < 1:
            raise ValueError("limit 必须至少为 1")
        self._limit = limit
        self._active = 0
        self._state_lock = asyncio.Lock()

    async def _reserve(self) -> None:
        """在极短的状态锁内抢占槽位；没有槽位时不等待推理完成。"""
        async with self._state_lock:
            if self._active >= self._limit:
                raise RuntimeError("server_busy")
            self._active += 1

    async def _release(self) -> None:
        async with self._state_lock:
            self._active = max(0, self._active - 1)

    async def run(self, operation, *args, **kwargs):
        await self._reserve()
        operation_task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        try:
            # shield 防止调用方取消时连带取消线程任务；线程真正结束后才能释放模型槽位。
            return await asyncio.shield(operation_task)
        except asyncio.CancelledError:
            # asyncio.to_thread 无法中断已开始的工作，取消请求需等待其完成再向上抛出。
            await operation_task
            raise
        finally:
            # 释放槽位必须发生在后台推理完成后，避免模型被并发重入。
            await self._release()

    @asynccontextmanager
    async def slot(self):
        """为无法拆成单次函数调用的流式推理占用一个门禁槽。"""
        await self._reserve()
        try:
            yield
        finally:
            await self._release()


class BoundedSpeechQueue:
    """按短句条数和估算音频时长共同限流的异步 TTS 队列。

    GPT-SoVITS 只有开始合成后才能给出真实音频时长，因此入队前用文本长度作保守预算；
    正在合成的项目在 ``task_done`` 前仍占用预算，避免生产者只看到等待队列为空而继续堆积。
    """

    def __init__(
        self,
        *,
        max_items: int = 4,
        max_seconds: float = 30.0,
        chars_per_second: float = 12.0,
    ) -> None:
        if max_items < 1:
            raise ValueError("max_items 必须至少为 1")
        if max_seconds <= 0:
            raise ValueError("max_seconds 必须为正数")
        if chars_per_second <= 0:
            raise ValueError("chars_per_second 必须为正数")
        self.max_items = max_items
        self.max_seconds = float(max_seconds)
        self.chars_per_second = float(chars_per_second)
        self._queue: asyncio.Queue[Tuple[Optional[Tuple[int, str]], float]] = asyncio.Queue(
            maxsize=max_items
        )
        self._condition = asyncio.Condition()
        self._pending_items = 0
        self._pending_seconds = 0.0

    def estimate_seconds(self, text: str) -> float:
        """返回不小于 250 ms 的文本时长预算，不把标点误当成大量音频。"""
        units = len("".join(str(text).split()))
        return max(0.25, units / self.chars_per_second)

    async def put(
        self,
        item: Optional[Tuple[int, str]],
        *,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> bool:
        """入队；因取消而未入队时返回 False。"""
        estimate = 0.0 if item is None else self.estimate_seconds(item[1])
        if item is not None and estimate > self.max_seconds:
            # 单句本身过长时不无限等待；调用方应先按短句切分。
            raise ValueError("TTS 句段超过时长预算")
        reserved = item is not None
        if reserved:
            async with self._condition:
                while (
                    self._pending_items >= self.max_items
                    or self._pending_seconds + estimate > self.max_seconds
                ):
                    if cancel_event is not None and cancel_event.is_set():
                        return False
                    try:
                        # 队列消费者可能正在等待不可中断的 GPT-SoVITS 请求；
                        # 周期性醒来检查取消，避免生产者永久卡在背压上。
                        await asyncio.wait_for(self._condition.wait(), timeout=0.1)
                    except asyncio.TimeoutError:
                        continue
                self._pending_items += 1
                self._pending_seconds += estimate
        try:
            await self._queue.put((item, estimate))
        except BaseException:
            if reserved:
                async with self._condition:
                    self._pending_items = max(0, self._pending_items - 1)
                    self._pending_seconds = max(0.0, self._pending_seconds - estimate)
                    self._condition.notify_all()
            raise
        return True

    async def get(self) -> Tuple[Optional[Tuple[int, str]], float]:
        return await self._queue.get()

    async def task_done(self, estimate: float) -> None:
        self._queue.task_done()
        if estimate <= 0:
            return
        async with self._condition:
            self._pending_items = max(0, self._pending_items - 1)
            self._pending_seconds = max(0.0, self._pending_seconds - estimate)
            self._condition.notify_all()


# 离线、实时和自动 VAD 共用同一个进程级 ASR 门禁，避免线程池隐性堆积模型任务。
ASR_INFERENCE_GATE = InferenceGate(limit=1)


class OnlineVADSegmenter:
    """调用 FunASR FSMN-VAD 的轻量包装；不存在模型时保持明确的未就绪状态。"""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.state = OnlineVADState()
        # FSMN-VAD 流式接口用 ``[start_ms, -1]`` 表示开始、``[-1, end_ms]``
        # 表示结束；结束事件到达前只保存开始点，不把哨兵值当作公开时间线。
        self._speech_start_ms: Optional[float] = None

    def process(self, waveform: Any, *, is_final: bool = False) -> List[List[float]]:
        streaming_vad = getattr(self.engine, "streaming_vad", None)
        if callable(streaming_vad):
            # 独立 AutoModel 才能在非 final 分帧时返回 FSMN-VAD 的开闭信号。
            result = streaming_vad(waveform, self.state.cache, is_final=is_final)
        else:
            model = getattr(self.engine.model, "vad_model", None)
            if model is None:
                raise RuntimeError("FSMN-VAD 未就绪")
            inference = getattr(self.engine.model, "inference", None)
            if inference is None:
                raise RuntimeError("FunASR VAD 接口不可用")
            try:
                result = inference(
                    [waveform],
                    model=model,
                    kwargs=getattr(self.engine.model, "vad_kwargs", {}),
                    cache=self.state.cache,
                    is_final=is_final,
                    fs=TARGET_FS,
                )
            except TypeError:
                result = inference(
                    [waveform], model=model, kwargs=getattr(self.engine.model, "vad_kwargs", {})
                )
        if isinstance(result, tuple):
            result = result[0]
        self.state.append_samples(int(getattr(waveform, "shape", [0])[-1]))
        segments: List[List[float]] = []
        items = [result] if isinstance(result, Mapping) else result
        if isinstance(items, list) and items and isinstance(items[0], list):
            items = items[0]
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                values = item.get("value")
                if not isinstance(values, list):
                    continue
                for value in values:
                    if (
                        isinstance(value, (list, tuple))
                        and len(value) >= 2
                    ):
                        start, end = value[0], value[1]
                        if isinstance(start, bool) or isinstance(end, bool):
                            continue
                        try:
                            start = float(start)
                            end = float(end)
                        except (TypeError, ValueError):
                            continue
                        if not math.isfinite(start) or not math.isfinite(end):
                            continue
                        if start >= 0 and end == -1:
                            # 开始事件可能重复到达；保留最早的有效起点。
                            if self._speech_start_ms is None:
                                self._speech_start_ms = start
                            else:
                                self._speech_start_ms = min(self._speech_start_ms, start)
                            continue
                        if start == -1 and end >= 0:
                            # 只有同时收到过开始事件才构造区间，避免伪造时间线。
                            if self._speech_start_ms is not None and end > self._speech_start_ms:
                                segments.append([self._speech_start_ms, end])
                                self._speech_start_ms = None
                            continue
                        if start >= 0 and end > start:
                            segments.append([start, end])
                            self._speech_start_ms = None
        return segments

    def reset(self) -> None:
        """清空跨话轮的 VAD cache，下一帧从新的音频时间零点开始。"""
        self.state = OnlineVADState()
        self._speech_start_ms = None
