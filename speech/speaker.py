"""会话匿名说话人和持久声纹库。

声纹文件默认要求 Fernet 加密。为便于离线单元测试，只有调用方显式传入
``allow_unencrypted=True`` 时才允许明文文件；生产配置不应启用该选项。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .auth import constant_time_equal


class VoiceprintError(RuntimeError):
    """声纹库权限、加密或数据错误。"""


def normalise_embedding(values: Sequence[float]) -> List[float]:
    try:
        raw_values = list(values)
        if any(isinstance(value, bool) for value in raw_values):
            raise VoiceprintError("声纹向量元素类型无效")
        vector = [float(value) for value in raw_values]
    except (TypeError, ValueError) as error:
        raise VoiceprintError("声纹向量格式无效") from error
    norm = math.sqrt(sum(value * value for value in vector))
    if not vector or not math.isfinite(norm) or norm <= 1e-8:
        raise VoiceprintError("声纹向量为空或长度无效")
    return [value / norm for value in vector]


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return -1.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm <= 1e-8 or right_norm <= 1e-8:
        return -1.0
    score = sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)
    return max(-1.0, min(1.0, score))


@dataclass(frozen=True)
class SpeakerMatch:
    person_id: Optional[str]
    score: Optional[float]
    status: str
    margin: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "person_id": self.person_id,
            "match_score": self.score,
            "speaker_status": self.status,
            "margin": self.margin,
        }


class SessionSpeakerMapper:
    """将一次或多次推理中的局部 spk 标签映射为会话级匿名编号。"""

    def __init__(self, threshold: float = 0.55) -> None:
        self.threshold = threshold
        self._centers: Dict[str, List[float]] = {}
        self._local: Dict[Any, str] = {}
        self._anonymous_number = 0

    def begin_inference(self) -> None:
        """清除上一次推理的局部标签，避免把新的 spk=0 误当成同一个人。"""
        self._local.clear()

    def resolve(self, local_label: Any, embedding: Optional[Sequence[float]]) -> str:
        normalized = None
        # 不用 ``if embedding``：NumPy 数组的布尔值未定义，会让真实 FunASR
        # 返回的数组在会话映射阶段抛出 ``ValueError``。
        if embedding is not None:
            try:
                normalized = normalise_embedding(embedding)
            except VoiceprintError:
                normalized = None
        if normalized:
            candidates = [
                (cosine_similarity(normalized, center), speaker)
                for speaker, center in self._centers.items()
            ]
            if candidates:
                score, speaker = max(candidates)
                if score >= self.threshold:
                    self._centers[speaker] = normalise_embedding(
                        [a + b for a, b in zip(self._centers[speaker], normalized)]
                    )
                    self._local[local_label] = speaker
                    return speaker
            speaker = f"speaker_{self._anonymous_number}"
            self._anonymous_number += 1
            self._centers[speaker] = normalized
            self._local[local_label] = speaker
            return speaker
        if local_label in self._local:
            return self._local[local_label]
        speaker = f"speaker_{self._anonymous_number}"
        self._anonymous_number += 1
        self._local[local_label] = speaker
        return speaker


_TENANT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_PERSON_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")


def resolve_trusted_tenant(headers: Mapping[str, Any]) -> Optional[str]:
    """仅接受 BFF 共享会话密钥携带的租户，失败时安全降级为匿名。"""
    session_key = os.getenv("SENSEVOICE_VOICEPRINT_SESSION_KEY", "").strip()
    if not session_key or session_key.startswith("请替换"):
        return None
    tenant = str(headers.get("x-voiceprint-tenant", "")).strip()
    provided = str(headers.get("x-voiceprint-session-key", ""))
    if not tenant or not provided or not constant_time_equal(provided, session_key):
        return None
    if not _TENANT_PATTERN.fullmatch(tenant):
        return None
    return tenant


class VoiceprintStore:
    """按可信调用方隔离的持久声纹库。"""

    def __init__(
        self,
        root: Optional[os.PathLike[str] | str] = None,
        *,
        encryption_key: Optional[str | bytes] = None,
        allow_unencrypted: bool = False,
        match_threshold: float = 0.72,
        match_margin: float = 0.04,
    ) -> None:
        self.root = Path(root or os.getenv(
            "SENSEVOICE_VOICEPRINT_PATH", "/data/voice-stack/data/voiceprints"
        ))
        key = encryption_key or os.getenv("SENSEVOICE_VOICEPRINT_KEY")
        if isinstance(key, str) and key.startswith("请替换"):
            key = None
        self._key = key
        self._allow_unencrypted = allow_unencrypted
        if not 0 <= match_threshold <= 1:
            raise ValueError("match_threshold 必须位于 [0, 1]")
        if not 0 <= match_margin <= 1:
            raise ValueError("match_margin 必须位于 [0, 1]")
        self.match_threshold = match_threshold
        self.match_margin = match_margin
        self._lock = threading.RLock()

    @staticmethod
    def _validate(tenant_id: str, person_id: Optional[str] = None) -> None:
        if not _TENANT_PATTERN.fullmatch(tenant_id or ""):
            raise VoiceprintError("声纹库租户标识无效")
        if person_id is not None and not _PERSON_PATTERN.fullmatch(person_id or ""):
            raise VoiceprintError("人员标识无效")

    def _path(self, tenant_id: str) -> Path:
        self._validate(tenant_id)
        return self.root / f"{tenant_id}.voiceprints"

    def _fernet(self) -> Any:
        if not self._key:
            return None
        try:
            from cryptography.fernet import Fernet
        except ImportError as error:
            raise VoiceprintError("声纹加密需要 cryptography 依赖") from error
        key = self._key.encode() if isinstance(self._key, str) else self._key
        try:
            if len(key) != 44:
                key = base64.urlsafe_b64encode(hashlib.sha256(key).digest())
            return Fernet(key)
        except Exception as error:  # noqa: BLE001
            raise VoiceprintError("声纹加密密钥无效") from error

    def _encode(self, document: Mapping[str, Any]) -> bytes:
        raw = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
        fernet = self._fernet()
        if fernet is not None:
            return b"F1\n" + fernet.encrypt(raw)
        if self._allow_unencrypted:
            return b"P1\n" + raw
        raise VoiceprintError("未配置声纹加密密钥，拒绝持久化")

    def _decode(self, data: bytes) -> Dict[str, Any]:
        prefix, _, payload = data.partition(b"\n")
        try:
            if prefix == b"F1":
                fernet = self._fernet()
                if fernet is None:
                    raise VoiceprintError("读取加密声纹需要配置密钥")
                payload = fernet.decrypt(payload)
            elif prefix != b"P1" or not self._allow_unencrypted:
                raise VoiceprintError("声纹文件未加密或格式不受支持")
            document = json.loads(payload.decode("utf-8"))
        except VoiceprintError:
            raise
        except Exception as error:  # noqa: BLE001
            raise VoiceprintError("声纹文件解密或解析失败") from error
        if not isinstance(document, dict):
            raise VoiceprintError("声纹文件结构无效")
        return document

    def _read(self, tenant_id: str) -> Dict[str, Any]:
        path = self._path(tenant_id)
        if not path.exists():
            return {"version": 1, "tenant_id": tenant_id, "persons": {}}
        document = self._decode(path.read_bytes())
        # 文件名不是充分的租户边界；同时校验内容，避免错放文件造成跨租户读取。
        document_tenant = document.get("tenant_id")
        if document_tenant not in (None, tenant_id):
            raise VoiceprintError("声纹文件租户标识不匹配")
        document.setdefault("tenant_id", tenant_id)
        persons = document.get("persons", {})
        if not isinstance(persons, dict):
            raise VoiceprintError("声纹文件人员结构无效")
        for person_id, person in persons.items():
            if not isinstance(person_id, str) or not _PERSON_PATTERN.fullmatch(person_id):
                raise VoiceprintError("声纹文件人员标识无效")
            if not isinstance(person, dict):
                raise VoiceprintError("声纹文件人员记录无效")
            model_id = person.get("model_id")
            if model_id is not None and (
                not isinstance(model_id, str) or not model_id.strip()
            ):
                raise VoiceprintError("声纹文件模型标识无效")
            for field_name in ("model_revision", "preprocess_version"):
                field_value = person.get(field_name)
                if field_value is not None and (
                    not isinstance(field_value, str) or not field_value.strip()
                ):
                    raise VoiceprintError(f"声纹文件{field_name}无效")
            vectors = person.get("vectors", [])
            if not isinstance(vectors, list):
                raise VoiceprintError("声纹文件向量结构无效")
            try:
                # 只校验已保存数据的形状和有限数值，不把归一化结果写回文件。
                for vector in vectors:
                    normalise_embedding(vector)
                center = person.get("center")
                if center is not None:
                    normalise_embedding(center)
            except (TypeError, VoiceprintError) as error:
                raise VoiceprintError("声纹文件向量数据无效") from error
            metadata = person.get("metadata", {})
            if not isinstance(metadata, dict):
                raise VoiceprintError("声纹文件元数据结构无效")
        return document

    def _write(self, tenant_id: str, document: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        target = self._path(tenant_id)
        fd, name = tempfile.mkstemp(prefix=f".{tenant_id}.", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(self._encode(document))
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, target)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def register(
        self,
        tenant_id: str,
        person_id: str,
        embeddings: Iterable[Sequence[float]],
        *,
        authorized: bool,
        model_id: str = "",
        model_revision: str = "unknown",
        preprocess_version: str = "pcm16k-mono-f32-v1",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        self._validate(tenant_id, person_id)
        # 仅接受真正的布尔 True；不能把非空字符串或其他真值误当作用户授权。
        if authorized is not True:
            raise VoiceprintError("登记必须明确确认授权")
        if not isinstance(model_id, str):
            raise VoiceprintError("模型标识必须是字符串")
        if not isinstance(model_revision, str) or not model_revision.strip():
            raise VoiceprintError("模型 revision 必须是非空字符串")
        if not isinstance(preprocess_version, str) or not preprocess_version.strip():
            raise VoiceprintError("预处理版本必须是非空字符串")
        vectors = [normalise_embedding(vector) for vector in embeddings]
        if not vectors:
            raise VoiceprintError("至少需要一个有效声纹向量")
        dimensions = {len(vector) for vector in vectors}
        if len(dimensions) != 1:
            raise VoiceprintError("声纹向量维度不一致")
        center = normalise_embedding(
            [sum(vector[index] for vector in vectors) for index in range(len(vectors[0]))]
        )
        with self._lock:
            document = self._read(tenant_id)
            persons = document.setdefault("persons", {})
            persons[person_id] = {
                # 模型 ID 与 revision 必须同时保存；同一 revision 名称不保证跨模型向量空间一致。
                "model_id": str(model_id).strip() or None,
                "model_revision": model_revision,
                "preprocess_version": preprocess_version,
                "vectors": vectors,
                "center": center,
                "metadata": dict(metadata or {}),
            }
            self._write(tenant_id, document)
        return {
            "person_id": person_id,
            "sample_count": len(vectors),
            "model_id": str(model_id).strip() or None,
            "model_revision": model_revision,
            "preprocess_version": preprocess_version,
        }

    def list_people(self, tenant_id: str) -> List[Dict[str, Any]]:
        self._validate(tenant_id)
        with self._lock:
            document = self._read(tenant_id)
        return [
            {
                "person_id": person_id,
                "model_id": data.get("model_id"),
                "model_revision": data.get("model_revision"),
                "preprocess_version": data.get("preprocess_version"),
                "sample_count": len(data.get("vectors", [])),
                "metadata": data.get("metadata", {}),
            }
            for person_id, data in sorted(document.get("persons", {}).items())
        ]

    def identify(
        self,
        tenant_id: str,
        embedding: Sequence[float],
        *,
        model_id: Optional[str] = None,
        threshold: Optional[float] = None,
        margin: Optional[float] = None,
        model_revision: Optional[str] = None,
        preprocess_version: Optional[str] = None,
    ) -> SpeakerMatch:
        self._validate(tenant_id)
        query = normalise_embedding(embedding)
        threshold = self.match_threshold if threshold is None else threshold
        margin = self.match_margin if margin is None else margin
        if isinstance(threshold, bool) or isinstance(margin, bool):
            raise VoiceprintError("匹配阈值和分差必须是有限数值")
        try:
            threshold = float(threshold)
            margin = float(margin)
        except (TypeError, ValueError) as error:
            raise VoiceprintError("匹配阈值和分差必须是有限数值") from error
        if (
            not math.isfinite(threshold)
            or not math.isfinite(margin)
            or not 0 <= threshold <= 1
            or not 0 <= margin <= 1
        ):
            raise VoiceprintError("匹配阈值和分差必须位于 [0, 1]")
        with self._lock:
            people = self._read(tenant_id).get("persons", {})
        scores = []
        for person_id, data in people.items():
            # 没有 model_id 的历史记录不参与指定模型的匹配，避免跨模型混用。
            if model_id and data.get("model_id") != model_id:
                continue
            if model_revision and data.get("model_revision") != model_revision:
                continue
            if preprocess_version and data.get("preprocess_version") != preprocess_version:
                continue
            center = data.get("center")
            if isinstance(center, list):
                scores.append((cosine_similarity(query, center), person_id))
        scores.sort(reverse=True)
        if not scores:
            return SpeakerMatch(None, None, "unknown", None)
        top_score, top_person = scores[0]
        score_margin = top_score - scores[1][0] if len(scores) > 1 else None
        if top_score < threshold:
            return SpeakerMatch(
                None,
                round(top_score, 6),
                "unknown",
                round(score_margin, 6) if score_margin is not None else None,
            )
        if score_margin is not None and score_margin < margin:
            return SpeakerMatch(
                None,
                round(top_score, 6),
                "ambiguous",
                round(score_margin, 6),
            )
        return SpeakerMatch(
            top_person,
            round(top_score, 6),
            "identified",
            round(score_margin, 6) if score_margin is not None else None,
        )

    def delete(self, tenant_id: str, person_id: str) -> bool:
        self._validate(tenant_id, person_id)
        with self._lock:
            document = self._read(tenant_id)
            persons = document.get("persons", {})
            if person_id not in persons:
                return False
            del persons[person_id]
            self._write(tenant_id, document)
        return True

    def status(self) -> Dict[str, Any]:
        encryption_available = bool(self._key)
        if encryption_available:
            try:
                # 同时校验依赖和密钥格式，避免健康检查把必然失败的登记误报为 ready。
                self._fernet()
            except VoiceprintError:
                encryption_available = False
        try:
            storage_root = self.root
            if storage_root.exists():
                if storage_root.is_dir() and os.access(
                    storage_root, os.W_OK | os.X_OK
                ):
                    # 目录中包含按租户命名的声纹文件；现有目录若对组或其他用户
                    # 开放，不能把它报告为可用于生产登记的安全存储。
                    try:
                        directory_mode = storage_root.stat().st_mode & 0o777
                    except OSError:
                        directory_mode = 0o777
                    storage_available = (directory_mode & 0o077) == 0
                else:
                    storage_available = False
            else:
                # 首次登记会创建目录；检查其现有父目录即可，避免健康检查产生文件。
                parent = storage_root.parent
                storage_available = parent.is_dir() and os.access(
                    parent, os.W_OK | os.X_OK
                )
        except OSError:
            storage_available = False
        return {
            "path_configured": bool(self.root),
            "storage_ready": storage_available,
            "encryption_configured": encryption_available,
            "encrypted_required": not self._allow_unencrypted,
        }
