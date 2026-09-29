#!/usr/bin/env bash

# Windows/WSL 部署后的只读诊断；不读取配置值，也不修改目标机文件。
set -u

ROOT="${SENSEVOICE_ROOT:-/data/voice-stack}"
APP="${SENSEVOICE_APP:-$ROOT/apps/sensevoice-next}"
ENVIRONMENT="${SENSEVOICE_ENVIRONMENT:-$ROOT/environments/sensevoice-next}"
CONFIG="${SENSEVOICE_CONFIG:-$ROOT/config/sensevoice.env}"
VOICEPRINT_PATH="${SENSEVOICE_VOICEPRINT_PATH:-$ROOT/data/voiceprints}"
errors=0

ok() {
    printf '[OK] %s\n' "$1"
}

warn() {
    printf '[WARN] %s\n' "$1"
}

fail() {
    printf '[FAIL] %s\n' "$1"
    errors=$((errors + 1))
}

http_probe() {
    local name="$1"
    local url="$2"
    local strict="${3:-true}"
    local show_body="${4:-false}"
    local status
    if ! command -v curl >/dev/null 2>&1; then
        warn "未找到 curl，跳过 HTTP 探测: $name"
        return
    fi
    status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null || true)"
    if [[ "$status" =~ ^2[0-9][0-9]$ ]]; then
        ok "$name HTTP $status"
        if [[ "$show_body" == "true" ]]; then
            # /api/v1/health 明确不返回密钥、内部地址或声纹向量；仅打印能力状态便于现场判定。
            curl -fsS --max-time 5 "$url" 2>/dev/null | sed 's/^/  health=/'
        fi
    elif [[ "$status" =~ ^[0-9]{3}$ && "$strict" == "false" ]]; then
        warn "$name HTTP $status（服务可能在线但该探测路径不提供 GET）"
    elif [[ "$status" =~ ^[0-9]{3}$ ]]; then
        fail "$name HTTP $status"
    else
        fail "$name 无法连接"
    fi
}

printf 'SenseVoice Windows/WSL 只读诊断\n'
printf '时间: %s\n' "$(date -Is 2>/dev/null || date)"
if command -v hostname >/dev/null 2>&1; then
    printf '主机: %s\n' "$(hostname)"
fi
printf '根目录: %s\n候选应用: %s\n声纹目录: %s\n' "$ROOT" "$APP" "$VOICEPRINT_PATH"

for path in "$ROOT" "$APP" "$APP/realtime_api.py" "$APP/api.py" "$APP/speech"; do
    if [[ -e "$path" ]]; then
        ok "路径存在: $path"
    else
        fail "路径缺失: $path"
    fi
done

if [[ -f "$APP/SOURCE_MANIFEST.sha256" ]]; then
    if command -v sha256sum >/dev/null 2>&1; then
        if (
            cd "$APP"
            sha256sum -c SOURCE_MANIFEST.sha256 >/dev/null 2>&1
        ); then
            ok "候选源码逐文件 SHA-256 校验通过"
        else
            fail "候选源码逐文件 SHA-256 校验失败；禁止继续流式验收"
        fi
    else
        warn "未找到 sha256sum，无法校验候选源码逐文件清单"
    fi
else
    warn "候选目录没有 SOURCE_MANIFEST.sha256（旧包未提供逐文件清单）"
fi

if [[ -x "$ENVIRONMENT/bin/python" ]]; then
    ok "隔离 Python 存在: $ENVIRONMENT/bin/python"
    "$ENVIRONMENT/bin/python" --version 2>&1 | sed 's/^/  /'
    if "$ENVIRONMENT/bin/python" -c 'import cryptography, fastapi, funasr, soundfile, torch, torchaudio, uvicorn, websockets' >/dev/null 2>&1; then
        ok "ASR/音频/网关核心依赖可导入"
    else
        fail "核心 ASR/音频依赖导入失败（未输出环境详情）"
    fi
else
    fail "隔离 Python 缺失: $ENVIRONMENT/bin/python"
fi

if [[ -f "$CONFIG" ]]; then
    ok "真实配置文件存在（未读取内容）: $CONFIG"
    if command -v stat >/dev/null 2>&1; then
        config_mode="$(stat -c '%a' "$CONFIG" 2>/dev/null || stat -f '%Lp' "$CONFIG" 2>/dev/null || true)"
        config_mode="${config_mode#0}"
        if [[ "$config_mode" =~ ^[4-7][0-7][0-7]$ && "${config_mode:1:2}" == "00" ]]; then
            ok "配置文件权限为 owner-only（模式 $config_mode）"
        else
            fail "配置文件对组或其他用户开放（当前模式 ${config_mode:-unknown}，应为 0600）"
        fi
    fi
else
    warn "真实配置文件不存在: $CONFIG"
fi

if [[ -d "$VOICEPRINT_PATH" ]]; then
    if command -v stat >/dev/null 2>&1; then
        voiceprint_mode="$(stat -c '%a' "$VOICEPRINT_PATH" 2>/dev/null || stat -f '%Lp' "$VOICEPRINT_PATH" 2>/dev/null || true)"
        voiceprint_mode="${voiceprint_mode#0}"
        if [[ "$voiceprint_mode" =~ ^[0-7][0-7][0-7]$ && "${voiceprint_mode:1:2}" == "00" ]]; then
            ok "声纹目录权限为 owner-only（模式 $voiceprint_mode）"
        else
            fail "声纹目录对组或其他用户开放（当前模式 ${voiceprint_mode:-unknown}，应为 0700）"
        fi
    else
        warn "未找到 stat，跳过声纹目录权限检查"
    fi
else
    warn "声纹目录尚未创建: $VOICEPRINT_PATH"
fi

service_state() {
    local unit="$1"
    local state
    state="$(systemctl is-active "$unit" 2>/dev/null || true)"
    if [[ "$state" == "active" ]]; then
        printf 'system %s' "$state"
        return
    fi
    state="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
    if [[ "$state" == "active" ]]; then
        printf 'user %s' "$state"
    else
        printf '%s' "${state:-unknown}"
    fi
}

if command -v systemctl >/dev/null 2>&1; then
    for unit in sensevoice-next.service sensevoice.service gpt-sovits.service; do
        state="$(service_state "$unit")"
        case "$state" in
            system\ active|user\ active) ok "$unit $state" ;;
            inactive|failed|dead) fail "$unit $state" ;;
            *) warn "$unit 状态未知或未安装（$state）" ;;
        esac
    done
else
    warn "未找到 systemctl，跳过服务状态检查"
fi

if command -v ss >/dev/null 2>&1; then
    for port in 50000 50001 9880; do
        if ss -H -ltn | awk '{print $4}' | grep -Eq "(^|:)${port}$"; then
            ok "TCP 端口监听: $port"
        else
            warn "TCP 端口未监听: $port"
        fi
    done
else
    warn "未找到 ss，跳过端口检查"
fi

http_probe "候选 SenseVoice 50001" "http://127.0.0.1:50001/api/v1/health" true true
http_probe "兼容 SenseVoice 50000" "http://127.0.0.1:50000/api/v1/health" true true
http_probe "GPT-SoVITS 9880" "http://127.0.0.1:9880/" false

if command -v sha256sum >/dev/null 2>&1; then
    for file in realtime_api.py api.py speech/engine.py speech/session.py speech/metrics.py speech/speaker.py speech/tts.py; do
        if [[ -f "$APP/$file" ]]; then
            printf 'sha256 %s ' "$file"
            sha256sum "$APP/$file" | awk '{print $1}'
        fi
    done
else
    warn "未找到 sha256sum，跳过源码哈希输出"
fi

if [[ "$errors" -gt 0 ]]; then
    printf '诊断结论: 失败（%s 项）；先修复 FAIL，再进行流式验收。\n' "$errors"
    exit 1
fi

printf '诊断结论: 未发现阻断项；HTTP 能力、模型质量和端到端延迟仍需用授权录音验收。\n'
