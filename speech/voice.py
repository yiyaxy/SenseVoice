"""TTS 音色配置；客户端只能选择服务端登记的 voice_id。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional


class VoiceProfileError(ValueError):
    """音色不存在、被禁用或配置不安全。"""


_VOICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_VOICE_LANGUAGE_ALIASES = {
    "zh": "all_zh",
    "all_zh": "zh",
    "yue": "all_yue",
    "all_yue": "yue",
    "ja": "all_ja",
    "all_ja": "ja",
    "ko": "all_ko",
    "all_ko": "ko",
}


@dataclass(frozen=True)
class VoiceProfile:
    """一个经过服务端授权的 GPT-SoVITS 参考音色。"""

    voice_id: str
    display_name: str
    ref_audio_path: str
    prompt_text: str
    prompt_lang: str
    languages: tuple[str, ...] = ()
    version: str = "1"
    enabled: bool = True

    @property
    def reference_ready(self) -> bool:
        """确认服务端参考音频可读取，避免把空路径配置报成可用音色。"""
        try:
            path = Path(self.ref_audio_path)
            return path.is_file() and os.access(path, os.R_OK)
        except OSError:
            return False

    def supports_language(self, text_language: str) -> bool:
        if not self.languages:
            return True
        requested = str(text_language or "").strip().lower()
        accepted = {str(value).strip().lower() for value in self.languages}
        if requested in accepted or "auto" in accepted:
            return True
        # 配置文件可以使用业务语言名，也可以直接使用 GPT-SoVITS 语言码。
        return _VOICE_LANGUAGE_ALIASES.get(requested) in accepted

    def public_dict(self) -> Dict[str, Any]:
        """只暴露可供客户端选择的元数据，不泄露路径和参考稿。"""
        return {
            "voice_id": self.voice_id,
            "name": self.display_name,
            "languages": list(self.languages),
            "version": self.version,
            "status": "ready" if self.enabled and self.reference_ready else "missing_reference",
        }


class VoiceProfileStore:
    """从固定配置加载 TTS 音色；请求体不能覆盖参考音频路径。"""

    def __init__(self, profiles: Iterable[VoiceProfile] = ()) -> None:
        self._profiles: Dict[str, VoiceProfile] = {}
        for profile in profiles:
            self._validate(profile)
            self._profiles[profile.voice_id] = profile

    @classmethod
    def from_environment(
        cls,
        *,
        default_ref_audio_path: str,
        default_prompt_text: str,
        default_prompt_lang: str,
        default_text_lang: str = "auto",
    ) -> "VoiceProfileStore":
        profiles: List[VoiceProfile] = []
        if default_ref_audio_path:
            default_profile = VoiceProfile(
                voice_id=os.getenv("GPT_SOVITS_DEFAULT_VOICE_ID", "assistant_default"),
                display_name=os.getenv("GPT_SOVITS_DEFAULT_VOICE_NAME", "默认助手音色"),
                ref_audio_path=default_ref_audio_path,
                prompt_text=default_prompt_text,
                prompt_lang=default_prompt_lang,
                languages=(default_text_lang,) if default_text_lang != "auto" else (),
            )
            try:
                cls._validate(default_profile)
            except VoiceProfileError:
                # 错误的默认配置不应阻塞 ASR；健康接口会显示音色未配置。
                pass
            else:
                profiles.append(default_profile)

        config_path = os.getenv("SENSEVOICE_VOICE_PROFILE_FILE", "").strip()
        if config_path:
            profiles.extend(
                cls._read_file(
                    Path(config_path),
                    default_ref_audio_path=default_ref_audio_path,
                    default_prompt_text=default_prompt_text,
                    default_prompt_lang=default_prompt_lang,
                )
            )
        return cls(profiles)

    @staticmethod
    def _validate(profile: VoiceProfile) -> None:
        if not _VOICE_ID_PATTERN.fullmatch(profile.voice_id or ""):
            raise VoiceProfileError("音色 ID 格式无效")
        if not profile.ref_audio_path:
            raise VoiceProfileError(f"音色 {profile.voice_id} 缺少参考音频")
        if not profile.prompt_lang:
            raise VoiceProfileError(f"音色 {profile.voice_id} 缺少参考语言")

    @classmethod
    def _read_file(
        cls,
        path: Path,
        *,
        default_ref_audio_path: str,
        default_prompt_text: str,
        default_prompt_lang: str,
    ) -> List[VoiceProfile]:
        """读取管理员维护的 JSON；错误只让健康检查显示缺项，不启动任意路径音色。"""
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return []
        rows = document.get("profiles", document) if isinstance(document, Mapping) else document
        if not isinstance(rows, list):
            return []
        profiles: List[VoiceProfile] = []
        for item in rows:
            if not isinstance(item, Mapping):
                continue
            raw_voice_id = item.get("voice_id", "")
            raw_ref_audio_path = item.get("ref_audio_path", "")
            if not isinstance(raw_voice_id, str) or not isinstance(raw_ref_audio_path, str):
                continue
            voice_id = raw_voice_id.strip()
            ref_audio_path = raw_ref_audio_path.strip()
            if not voice_id or not ref_audio_path:
                continue
            languages = item.get("languages", [])
            if isinstance(languages, str):
                languages = [languages]
            if not isinstance(languages, list):
                languages = []
            raw_enabled = item.get("enabled", True)
            if not isinstance(raw_enabled, bool):
                continue
            profile = VoiceProfile(
                voice_id=voice_id,
                display_name=str(item.get("name", voice_id)),
                ref_audio_path=ref_audio_path,
                prompt_text=str(item.get("prompt_text", default_prompt_text)),
                prompt_lang=str(item.get("prompt_lang", default_prompt_lang)),
                languages=tuple(value for value in languages if isinstance(value, str) and value),
                version=str(item.get("version", "1")),
                enabled=raw_enabled,
            )
            try:
                cls._validate(profile)
            except VoiceProfileError:
                continue
            profiles.append(profile)
        return profiles

    @property
    def default_voice_id(self) -> Optional[str]:
        # 停用音色不应成为默认值，否则健康检查通过后首轮请求仍会失败。
        return next(
            (profile.voice_id for profile in self._profiles.values() if profile.enabled),
            None,
        )

    def resolve(self, voice_id: Optional[str] = None) -> VoiceProfile:
        if voice_id is None:
            selected = (self.default_voice_id or "").strip()
        elif isinstance(voice_id, str):
            selected = voice_id.strip()
        else:
            raise VoiceProfileError("音色 ID 格式无效")
        profile = self._profiles.get(selected)
        if profile is None:
            raise VoiceProfileError("音色不存在或未授权")
        if not profile.enabled:
            raise VoiceProfileError("音色已停用")
        if not profile.reference_ready:
            raise VoiceProfileError("音色参考音频不存在或不可读取")
        return profile

    def public_profiles(self) -> List[Dict[str, Any]]:
        return [profile.public_dict() for profile in self._profiles.values() if profile.enabled]

    def status(self) -> Dict[str, Any]:
        enabled_profiles = [profile for profile in self._profiles.values() if profile.enabled]
        ready_profiles = [profile for profile in enabled_profiles if profile.reference_ready]
        return {
            "configured": bool(enabled_profiles),
            "ready": bool(enabled_profiles) and len(ready_profiles) == len(enabled_profiles),
            "count": len(enabled_profiles),
            "ready_count": len(ready_profiles),
            "voice_ids": [profile.voice_id for profile in enabled_profiles],
        }
