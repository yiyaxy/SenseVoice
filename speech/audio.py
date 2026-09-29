"""音频解码和采样时钟工具。

所有时间均以输入音频起点为零点，静音和网络分片不会改变采样偏移。
"""

from __future__ import annotations

from io import BytesIO
from typing import Any, Tuple

TARGET_FS = 16_000


class AudioDecodeError(ValueError):
    """表示输入音频无法按服务约定解码。"""


def _resample(waveform: Any, source_fs: int, target_fs: int) -> Any:
    if source_fs == target_fs:
        return waveform
    try:
        import torchaudio

        return torchaudio.transforms.Resample(
            orig_freq=source_fs, new_freq=target_fs
        )(waveform)
    except ImportError as error:
        raise AudioDecodeError("重采样需要 torchaudio") from error


def decode_audio(data: bytes, target_fs: int = TARGET_FS) -> Tuple[Any, float]:
    """用 SoundFile 解码、单声道化并重采样，返回波形和秒数。"""
    if not data:
        raise AudioDecodeError("未收到音频数据")
    try:
        import soundfile as sf
        import torch

        samples, sample_rate = sf.read(
            BytesIO(data), dtype="float32", always_2d=True
        )
        waveform = torch.from_numpy(samples).transpose(0, 1)
        waveform = _resample(waveform, int(sample_rate), target_fs).mean(dim=0)
        if not bool(torch.isfinite(waveform).all().item()):
            raise AudioDecodeError("音频包含非有限采样值")
    except AudioDecodeError:
        raise
    except Exception as error:  # noqa: BLE001
        raise AudioDecodeError(f"音频格式不支持或解码失败: {type(error).__name__}") from error
    return waveform, float(waveform.shape[-1]) / target_fs


def decode_pcm_s16le(
    data: bytes, target_fs: int = TARGET_FS
) -> Tuple[Any, float]:
    """解码实时接口的 16 kHz、16 bit、小端、单声道 PCM。"""
    if not data:
        raise AudioDecodeError("未收到音频数据")
    if len(data) % 2:
        raise AudioDecodeError("PCM 音频长度必须按 16 bit 样本对齐")
    try:
        import torch

        samples = torch.frombuffer(memoryview(data), dtype=torch.int16).to(torch.float32)
        waveform = samples.div(32768.0)
    except Exception as error:  # noqa: BLE001
        raise AudioDecodeError("PCM 音频解码失败") from error
    return waveform, float(waveform.shape[-1]) / target_fs


def validate_voiceprint_audio(
    waveform: Any,
    duration_seconds: float,
    *,
    min_duration_seconds: float = 1.0,
    max_duration_seconds: float = 30.0,
) -> None:
    """拒绝过短、近似静音或严重削波的声纹登记样本。"""
    if duration_seconds < min_duration_seconds:
        raise AudioDecodeError("登记语音过短，至少需要 1 秒有效语音")
    if duration_seconds > max_duration_seconds:
        raise AudioDecodeError("登记语音过长，单段不得超过 30 秒")
    try:
        import torch

        values = torch.as_tensor(waveform, dtype=torch.float32).flatten()
        if values.numel() == 0:
            raise AudioDecodeError("登记语音为空")
        if not bool(torch.isfinite(values).all().item()):
            raise AudioDecodeError("登记语音包含非有限采样值")
        rms = float(torch.sqrt(torch.mean(values.square())).item())
        clipped_ratio = float((values.abs() >= 0.999).float().mean().item())
        # 仅用整段 RMS 会让“长静音 + 很短语音”样本通过；按 20 ms 帧估算有效语音，
        # 让登记库至少包含足够的非静音内容，同时保留自然停顿。
        frame_length = max(1, round(TARGET_FS * 0.02))
        frame_count = values.numel() // frame_length
        if frame_count:
            frames = values[: frame_count * frame_length].reshape(frame_count, frame_length)
            frame_rms = torch.sqrt(torch.mean(frames.square(), dim=1))
            active_frames = frame_rms >= 0.003
            active_duration = float(active_frames.sum().item() * frame_length / TARGET_FS)
            silence_ratio = 1.0 - float(active_frames.float().mean().item())
        else:
            active_duration = 0.0
            silence_ratio = 1.0
    except AudioDecodeError:
        raise
    except Exception as error:  # noqa: BLE001
        raise AudioDecodeError("登记语音质量无法检测") from error
    if rms < 0.005:
        raise AudioDecodeError("登记语音有效音量过低或接近静音")
    if active_duration < min_duration_seconds:
        raise AudioDecodeError("登记语音有效内容不足，请减少静音并重新录制")
    if silence_ratio > 0.8:
        raise AudioDecodeError("登记语音静音比例过高，请重新录制")
    if clipped_ratio > 0.02:
        raise AudioDecodeError("登记语音削波过重，请重新录制")


def validate_pcm_stream_prefix(data: bytes) -> None:
    """拒绝把常见压缩音频文件误当成实时 PCM 音频。"""
    if not data:
        return
    encoded_signatures = (
        b"RIFF",  # WAV 文件
        b"RF64",  # 大文件 WAV
        b"ID3",  # 带 ID3 头的 MP3
        b"OggS",  # Ogg/Opus 文件
        b"fLaC",  # FLAC 文件
        b"\x1a\x45\xdf\xa3",  # WebM/Matroska 文件
    )
    if any(data.startswith(signature) for signature in encoded_signatures):
        raise AudioDecodeError(
            "实时 ASR 仅接受 16 kHz、单声道、16 bit PCM；请先解码 WAV/MP3"
        )
    # 无 ID3 头的 MP3 和 AAC 常以同步字节开头，避免被静默当成 PCM。
    if len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0:
        raise AudioDecodeError(
            "检测到压缩音频帧；实时 ASR 仅接受 16 kHz、单声道、16 bit PCM"
        )


def waveform_to_numpy(waveform: Any) -> Any:
    """把 Torch 波形转成 FunASR 接受的 CPU numpy；测试替身可直接原样返回。"""
    if hasattr(waveform, "detach"):
        return waveform.detach().cpu().numpy()
    return waveform
