"""GPT-SoVITS 流式音频的增量解析工具。"""

from __future__ import annotations

import struct


class TTSStreamError(ValueError):
    """上游流不是可解析的单声道 PCM16 WAV。"""


class StreamingWavDecoder:
    """解析首个 WAV 头，随后只输出完整的 PCM16 采样字节。"""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._header_ready = False
        self._sample_rate: int | None = None
        self._channels: int | None = None
        self._sample_width: int | None = None
        self._odd_byte = b""

    @property
    def ready(self) -> bool:
        return self._header_ready

    @property
    def sample_rate(self) -> int | None:
        return self._sample_rate

    @property
    def channels(self) -> int | None:
        return self._channels

    @property
    def sample_width(self) -> int | None:
        return self._sample_width

    def feed(self, chunk: bytes) -> bytes:
        if not isinstance(chunk, (bytes, bytearray)):
            raise TTSStreamError("TTS 音频块必须是二进制")
        if not self._header_ready:
            self._buffer.extend(chunk)
            self._parse_header()
            if not self._header_ready:
                return b""
            chunk = bytes(self._buffer)
            self._buffer.clear()
        return self._complete_samples(chunk)

    def _parse_header(self) -> None:
        if len(self._buffer) < 12:
            return
        if self._buffer[:4] != b"RIFF" or self._buffer[8:12] != b"WAVE":
            raise TTSStreamError("GPT-SoVITS 未返回 WAV 流")
        cursor = 12
        fmt_found = False
        while cursor + 8 <= len(self._buffer):
            chunk_id = bytes(self._buffer[cursor : cursor + 4])
            size = struct.unpack_from("<I", self._buffer, cursor + 4)[0]
            end = cursor + 8 + size
            if chunk_id == b"data":
                if (
                    not fmt_found
                    or self._sample_rate is None
                    or self._sample_rate <= 0
                ):
                    raise TTSStreamError("WAV 缺少有效 fmt 块")
                if self._channels != 1 or self._sample_width != 2:
                    raise TTSStreamError("TTS 输出必须是单声道 PCM16")
                # data 的声明长度可能是完整文件长度；流式模式无需等它全部到齐。
                del self._buffer[: cursor + 8]
                self._header_ready = True
                return
            if end > len(self._buffer):
                return
            body = self._buffer[cursor + 8 : end]
            if chunk_id == b"fmt ":
                if len(body) < 16:
                    raise TTSStreamError("WAV fmt 块不完整")
                audio_format, channels, sample_rate, _, block_align, bits_per_sample = struct.unpack_from(
                    "<HHIIHH", body, 0
                )
                if audio_format != 1:
                    raise TTSStreamError("TTS 不是 PCM 编码")
                if sample_rate <= 0:
                    raise TTSStreamError("TTS 采样率无效")
                if bits_per_sample != 16 or block_align != channels * 2:
                    raise TTSStreamError("TTS 输出必须是 16 bit PCM")
                self._channels = channels
                self._sample_rate = sample_rate
                self._sample_width = 2
                fmt_found = True
            cursor = end + (size & 1)

    def _complete_samples(self, chunk: bytes) -> bytes:
        data = self._odd_byte + bytes(chunk)
        if len(data) % 2:
            self._odd_byte = data[-1:]
            data = data[:-1]
        else:
            self._odd_byte = b""
        return data

    def finish(self) -> None:
        if not self._header_ready:
            raise TTSStreamError("TTS 流结束前没有收到完整 WAV 头")
        if self._odd_byte:
            raise TTSStreamError("TTS PCM 采样边界不完整")
