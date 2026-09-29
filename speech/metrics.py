"""ASR 质量指标的统一文本口径。

该模块只用于离线评测和验收记录，不改变对外返回的原始转写文本。
默认 CER 会忽略 Unicode 标点、符号和空白，避免仅因标点恢复差异增加错误数；同时提供
标点敏感的严格 CER，供需要把标点差异计入质量数据的验收使用。
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Iterable, Mapping


_RICH_TOKEN_PATTERN = re.compile(r"<\|[^|]*\|>")
_CJK_CHAR_PATTERN = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]"
)
# ``\w`` 在 Unicode 模式下会把 CJK 字符也视为单词的一部分。例如
# ``hello中文`` 会被误切为一个 token，而 ``hello 中文`` 会被切成三个，导致
# 仅空白差异也被统计为 WER。当前服务的混合中英粤语口径按英文/数字连续片段和
# CJK 单字切分，因此这里明确限定 ASCII 英文、数字与下划线。
_WORD_PATTERN = re.compile(r"[A-Za-z0-9_]+(?:['’][A-Za-z0-9_]+)*", re.ASCII)


def normalize_transcript(
    text: str,
    *,
    strip_punctuation: bool = True,
    casefold: bool = True,
    unicode_nfkc: bool = True,
) -> str:
    """按验收口径规范化文本，同时不做未经确认的数字改写。

    ``strip_punctuation=True`` 会移除标点、符号和空白，但保留字母、数字及组合音标；
    这对中文、英语和粤语都使用同一字符级 CER 口径。SenseVoice 的控制标签始终移除。
    """
    value = "" if text is None else str(text)
    value = _RICH_TOKEN_PATTERN.sub("", value)
    if unicode_nfkc:
        value = unicodedata.normalize("NFKC", value)
    if casefold:
        value = value.casefold()
    if not strip_punctuation:
        return value
    return "".join(
        character
        for character in value
        if unicodedata.category(character)[0] in {"L", "M", "N"}
    )


def edit_distance(reference: Iterable[str], hypothesis: Iterable[str]) -> int:
    """计算两个序列的 Levenshtein 编辑距离，使用 O(min(n, m)) 内存。"""
    left = list(reference)
    right = list(hypothesis)
    if len(left) < len(right):
        left, right = right, left
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for left_index, left_value in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_value in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_value != right_value),
                )
            )
        previous = current
    return previous[-1]


def edit_counts(reference: Iterable[str], hypothesis: Iterable[str]) -> dict[str, int]:
    """返回编辑错误分解：替换、漏字（删除）和插字（插入）。

    评测文本通常很短，使用完整 DP 表保留一条可复核的最短路径；``errors``
    等于三类错误之和，与 :func:`edit_distance` 的结果一致。
    """
    left = list(reference)
    right = list(hypothesis)
    # 每个单元为 (总错误, 替换, 删除, 插入)。删除是参考文本多出的 token，
    # 插入是识别文本多出的 token。相同 token 优先沿对角线匹配。
    matrix: list[list[tuple[int, int, int, int]]] = [
        [(0, 0, 0, 0) for _ in range(len(right) + 1)]
        for _ in range(len(left) + 1)
    ]
    for row in range(1, len(left) + 1):
        matrix[row][0] = (row, 0, row, 0)
    for column in range(1, len(right) + 1):
        matrix[0][column] = (column, 0, 0, column)

    def choose(*candidates: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        # 先选最短编辑距离；同距离下优先较少替换，再较少删除，保证输出稳定。
        return min(candidates, key=lambda item: (item[0], item[1], item[2], item[3]))

    for row in range(1, len(left) + 1):
        for column in range(1, len(right) + 1):
            if left[row - 1] == right[column - 1]:
                diagonal = matrix[row - 1][column - 1]
            else:
                previous = matrix[row - 1][column - 1]
                diagonal = (
                    previous[0] + 1,
                    previous[1] + 1,
                    previous[2],
                    previous[3],
                )
            deletion = matrix[row - 1][column]
            deletion = (deletion[0] + 1, deletion[1], deletion[2] + 1, deletion[3])
            insertion = matrix[row][column - 1]
            insertion = (insertion[0] + 1, insertion[1], insertion[2], insertion[3] + 1)
            matrix[row][column] = choose(diagonal, deletion, insertion)

    errors, substitutions, deletions, insertions = matrix[-1][-1]
    return {
        "errors": errors,
        "substitutions": substitutions,
        "deletions": deletions,
        "insertions": insertions,
        "reference_length": len(left),
    }


def tokenize_transcript(text: str) -> list[str]:
    """按混合中英粤语验收口径分词：CJK 按字，英文/数字连续片段按词。"""
    value = "" if text is None else str(text)
    value = _RICH_TOKEN_PATTERN.sub("", value)
    value = unicodedata.normalize("NFKC", value).casefold()
    # 标点和符号替换为空格，避免 ``hello,world`` 被粘成一个词；保留撇号以
    # 让英文缩写/所有格保持一个词。数字不做改写，逗号分隔的数字按两个 token 统计。
    value = "".join(
        character
        if (
            character.isspace()
            or unicodedata.category(character)[0] in {"L", "M", "N"}
            or character in {"'", "’"}
        )
        else " "
        for character in value
    )
    tokens: list[str] = []
    index = 0
    while index < len(value):
        cjk_match = _CJK_CHAR_PATTERN.match(value, index)
        if cjk_match:
            tokens.append(cjk_match.group(0))
            index = cjk_match.end()
            continue
        word_match = _WORD_PATTERN.match(value, index)
        if word_match:
            tokens.append(word_match.group(0))
            index = word_match.end()
            continue
        index += 1
    return tokens


def character_error_rate(reference: str, hypothesis: str) -> float:
    """返回归一化后的字符错误率；空参考文本按 0 或 1 处理。"""
    reference_text = normalize_transcript(reference)
    hypothesis_text = normalize_transcript(hypothesis)
    if not reference_text:
        return 0.0 if not hypothesis_text else 1.0
    return edit_distance(reference_text, hypothesis_text) / len(reference_text)


def character_error_counts(reference: str, hypothesis: str) -> dict[str, int]:
    """返回按统一 CER 归一化口径分解的字符错误数量。"""
    return edit_counts(normalize_transcript(reference), normalize_transcript(hypothesis))


def _punctuation_sensitive_text(text: str) -> str:
    """保留标点和符号、移除空白，作为严格 CER 的字符序列。"""
    value = normalize_transcript(text, strip_punctuation=False)
    return "".join(character for character in value if not character.isspace())


def strict_character_error_rate(reference: str, hypothesis: str) -> float:
    """返回把标点/符号差异计入、但不让空白格式主导结果的字符错误率。"""
    reference_text = _punctuation_sensitive_text(reference)
    hypothesis_text = _punctuation_sensitive_text(hypothesis)
    if not reference_text:
        return 0.0 if not hypothesis_text else 1.0
    return edit_distance(reference_text, hypothesis_text) / len(reference_text)


def strict_character_error_counts(reference: str, hypothesis: str) -> dict[str, int]:
    """返回标点敏感严格 CER 的替换、漏字和插字分解。"""
    return edit_counts(
        _punctuation_sensitive_text(reference),
        _punctuation_sensitive_text(hypothesis),
    )


def word_error_rate(reference: str, hypothesis: str) -> float:
    """返回混合中英粤语分词后的词错误率；空参考文本按 0 或 1 处理。"""
    reference_tokens = tokenize_transcript(reference)
    hypothesis_tokens = tokenize_transcript(hypothesis)
    if not reference_tokens:
        return 0.0 if not hypothesis_tokens else 1.0
    return edit_distance(reference_tokens, hypothesis_tokens) / len(reference_tokens)


def word_error_counts(reference: str, hypothesis: str) -> dict[str, int]:
    """返回按混合中英粤语分词口径分解的词错误数量。"""
    return edit_counts(tokenize_transcript(reference), tokenize_transcript(hypothesis))


def latency_summary(samples: Iterable[float]) -> dict[str, float | int | None]:
    """汇总延迟样本，返回 count、min、P50、P95 和 max（单位由调用方约定）。

    使用线性插值计算百分位；空样本返回 ``None`` 百分位而不是伪造零值。
    负数、NaN、无穷和不可转换值都会显式报错，避免把错误的时钟读数混入验收数据。
    """
    values: list[float] = []
    for sample in samples:
        if isinstance(sample, bool):
            raise ValueError("延迟样本必须是有限非负数")
        try:
            value = float(sample)
        except (TypeError, ValueError) as error:
            raise ValueError("延迟样本必须是有限非负数") from error
        if value < 0 or not math.isfinite(value):
            raise ValueError("延迟样本必须是有限非负数")
        values.append(value)
    if not values:
        return {"count": 0, "min": None, "p50": None, "p95": None, "max": None}

    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        return ordered[lower] + (ordered[upper] - ordered[lower]) * weight

    return {
        "count": len(ordered),
        "min": round(ordered[0], 3),
        "p50": round(percentile(0.50), 3),
        "p95": round(percentile(0.95), 3),
        "max": round(ordered[-1], 3),
    }


def _timeline_bounds(segments: Iterable[Mapping[str, Any]]) -> list[tuple[float, float]]:
    """读取并校验句段边界；评测不接受负数、NaN 或倒置区间。"""
    result: list[tuple[float, float]] = []
    for segment in segments:
        if not isinstance(segment, Mapping):
            raise ValueError("时间线句段必须是对象")
        if isinstance(segment.get("start_ms"), bool) or isinstance(
            segment.get("end_ms"), bool
        ):
            raise ValueError("时间线句段必须包含有效 start_ms/end_ms")
        try:
            start = float(segment.get("start_ms"))
            end = float(segment.get("end_ms"))
        except (TypeError, ValueError) as error:
            raise ValueError("时间线句段必须包含有效 start_ms/end_ms") from error
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError("时间线句段范围无效")
        result.append((start, end))
    return sorted(result)


def timeline_boundary_summary(
    reference_segments: Iterable[Mapping[str, Any]],
    hypothesis_segments: Iterable[Mapping[str, Any]],
) -> dict[str, float | int | None]:
    """按时间顺序比较句段边界，返回 MAE/P95 和缺失/多余段数（单位：毫秒）。

    该函数只评估同一录音的有序句段边界，不把不同说话人的句段强行配对；
    `missing_segments`/`extra_segments` 非零时，调用方应另行记录分段错误。
    """
    reference = _timeline_bounds(reference_segments)
    hypothesis = _timeline_bounds(hypothesis_segments)
    paired = min(len(reference), len(hypothesis))
    start_errors = [abs(hypothesis[index][0] - reference[index][0]) for index in range(paired)]
    end_errors = [abs(hypothesis[index][1] - reference[index][1]) for index in range(paired)]

    def error_summary(values: list[float]) -> tuple[float | None, float | None]:
        if not values:
            return None, None
        summary = latency_summary(values)
        return summary["p50"], summary["p95"]

    start_p50, start_p95 = error_summary(start_errors)
    end_p50, end_p95 = error_summary(end_errors)
    all_errors = start_errors + end_errors
    _, boundary_p95 = error_summary(all_errors)
    return {
        "reference_segments": len(reference),
        "hypothesis_segments": len(hypothesis),
        "paired_segments": paired,
        "missing_segments": max(0, len(reference) - paired),
        "extra_segments": max(0, len(hypothesis) - paired),
        "start_mae_ms": round(sum(start_errors) / paired, 3) if paired else None,
        "end_mae_ms": round(sum(end_errors) / paired, 3) if paired else None,
        "start_p50_ms": start_p50,
        "start_p95_ms": start_p95,
        "end_p50_ms": end_p50,
        "end_p95_ms": end_p95,
        "boundary_mae_ms": round(sum(all_errors) / len(all_errors), 3) if all_errors else None,
        "boundary_p95_ms": boundary_p95,
    }


def voiceprint_error_summary(
    genuine_scores: Iterable[float],
    impostor_scores: Iterable[float],
    *,
    threshold: float,
) -> dict[str, float | int | None]:
    """按相似度阈值计算声纹 FRR/FAR；空测试集的比率返回 ``None``。

    ``genuine_scores`` 是同一登记人员的验证分数，``impostor_scores`` 是非同一
    人员（含开放集未知人员）的分数。分数必须位于余弦相似度的 ``[-1, 1]``；
    匹配规则与在线库一致，分数大于等于阈值视为接受。
    """
    if isinstance(threshold, bool):
        raise ValueError("声纹阈值必须是有限数值")
    try:
        threshold = float(threshold)
    except (TypeError, ValueError) as error:
        raise ValueError("声纹阈值必须是有限数值") from error
    if not math.isfinite(threshold) or threshold < -1 or threshold > 1:
        raise ValueError("声纹阈值必须位于 [-1, 1]")

    def scores(values: Iterable[float]) -> list[float]:
        output: list[float] = []
        for value in values:
            if isinstance(value, bool):
                raise ValueError("声纹相似度必须是有限数值")
            try:
                score = float(value)
            except (TypeError, ValueError) as error:
                raise ValueError("声纹相似度必须是有限数值") from error
            if not math.isfinite(score) or score < -1 or score > 1:
                raise ValueError("声纹相似度必须位于 [-1, 1]")
            output.append(score)
        return output

    genuine = scores(genuine_scores)
    impostor = scores(impostor_scores)
    false_rejects = sum(score < threshold for score in genuine)
    false_accepts = sum(score >= threshold for score in impostor)
    return {
        "threshold": round(threshold, 6),
        "genuine_count": len(genuine),
        "impostor_count": len(impostor),
        "false_rejects": false_rejects,
        "false_accepts": false_accepts,
        "frr": round(false_rejects / len(genuine), 6) if genuine else None,
        "far": round(false_accepts / len(impostor), 6) if impostor else None,
        "open_set_false_accept_rate": (
            round(false_accepts / len(impostor), 6) if impostor else None
        ),
    }


__all__ = [
    "character_error_counts",
    "character_error_rate",
    "edit_counts",
    "edit_distance",
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
