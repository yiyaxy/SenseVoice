"""句段级时间轴和会话内匿名说话人编号。"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional


@dataclass
class TimelineSegment:
    segment_id: str
    turn_id: str
    revision: int
    start_ms: float
    end_ms: float
    text: str
    language: str
    speaker: Optional[str] = None
    speaker_status: str = "pending"
    match_score: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _valid_time(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


class SessionTimeline:
    """为每个 WebSocket 会话生成稳定的 segment/turn/revision。"""

    def __init__(self) -> None:
        self._turn_number = 0
        self._segment_number = 0
        self._revision = 0

    def new_turn(self) -> str:
        self._turn_number += 1
        return f"turn_{self._turn_number}"

    def next_revision(self) -> int:
        self._revision += 1
        return self._revision

    def build_segments(
        self,
        raw_segments: Iterable[Mapping[str, Any]],
        *,
        turn_id: str,
        offset_ms: float = 0,
        language: str = "auto",
    ) -> List[TimelineSegment]:
        offset = _valid_time(offset_ms)
        if offset is None:
            raise ValueError("时间线偏移无效")
        result: List[TimelineSegment] = []
        for raw in raw_segments:
            if not isinstance(raw, Mapping):
                raise ValueError("时间线句段类型无效")
            start = _valid_time(raw.get("start_ms"))
            if start is None:
                start = _valid_time(raw.get("start"))
            end = _valid_time(raw.get("end_ms"))
            if end is None:
                end = _valid_time(raw.get("end"))
            if start is None or end is None or end <= start:
                raise ValueError("时间线句段范围无效")
            absolute_start = start + offset
            absolute_end = end + offset
            if (
                not math.isfinite(absolute_start)
                or not math.isfinite(absolute_end)
                or absolute_end <= absolute_start
            ):
                raise ValueError("时间线绝对范围无效")
            raw_language = raw.get("language", language)
            raw_text = raw.get("text", "")
            self._segment_number += 1
            result.append(
                TimelineSegment(
                    segment_id=f"segment_{self._segment_number}",
                    turn_id=turn_id,
                    revision=self.next_revision(),
                    start_ms=round(absolute_start, 3),
                    end_ms=round(absolute_end, 3),
                    text="" if raw_text is None else str(raw_text),
                    language=language if raw_language is None else str(raw_language),
                    speaker=raw.get("speaker"),
                    speaker_status=str(raw.get("speaker_status", "pending")),
                    match_score=raw.get("match_score"),
                )
            )
        return result
