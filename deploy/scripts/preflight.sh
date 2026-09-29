#!/usr/bin/env bash

# Windows/WSL 部署前只读预检；脚本不读取配置内容，也不修改目标机文件。
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

printf 'SenseVoice Windows/WSL 只读预检\n'
printf '根目录: %s\n' "$ROOT"
printf '候选应用: %s\n声纹目录: %s\n' "$APP" "$VOICEPRINT_PATH"

for path in "$ROOT" "$APP" "$APP/realtime_api.py" "$APP/api.py" "$APP/speech"; do
    if [[ -e "$path" ]]; then
        ok "路径存在: $path"
    else
        fail "路径缺失: $path"
    fi
done

if [[ -x "$ENVIRONMENT/bin/python" ]]; then
    ok "隔离 Python 存在: $ENVIRONMENT/bin/python"
    "$ENVIRONMENT/bin/python" --version
    if "$ENVIRONMENT/bin/python" -m pip --version >/dev/null 2>&1; then
        if "$ENVIRONMENT/bin/python" -m pip check >/dev/null 2>&1; then
            ok "pip check 通过"
        else
            fail "pip check 失败（未输出依赖详情，避免泄露本机配置）"
        fi
    else
        # 部分受管虚拟环境只提供运行时，不附带 pip；改用导入检查，不修改环境。
        warn "隔离 Python 未安装 pip，跳过 pip check，改做核心依赖导入检查"
    fi
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
        config_file_mode="$(stat -c '%a' "$CONFIG" 2>/dev/null || stat -f '%Lp' "$CONFIG" 2>/dev/null || true)"
        config_file_mode="${config_file_mode#0}"
        if [[ "$config_file_mode" =~ ^[4-7][0-7][0-7]$ ]]; then
            # owner 至少可读，group/other 不得有任何权限（例如 400、600、700）。
            if [[ "${config_file_mode:1:2}" == "00" ]]; then
                ok "真实配置文件权限收紧（模式 ${config_file_mode}）"
            else
                fail "真实配置文件对组或其他用户可读写；请收紧为 0600: $CONFIG"
            fi
        else
            fail "真实配置文件不可由服务账户安全读取；请收紧为 0600: $CONFIG"
        fi
    else
        warn "未找到 stat，跳过真实配置文件权限检查"
    fi
else
    warn "真实配置文件不存在（部署前需由管理员按模板创建）: $CONFIG"
fi

if [[ -d "$VOICEPRINT_PATH" ]]; then
    if command -v stat >/dev/null 2>&1; then
        voiceprint_dir_mode="$(stat -c '%a' "$VOICEPRINT_PATH" 2>/dev/null || stat -f '%Lp' "$VOICEPRINT_PATH" 2>/dev/null || true)"
        voiceprint_dir_mode="${voiceprint_dir_mode#0}"
        if [[ "$voiceprint_dir_mode" =~ ^[0-7][0-7][0-7]$ && "${voiceprint_dir_mode:1:2}" == "00" ]]; then
            ok "声纹目录权限收紧（模式 ${voiceprint_dir_mode}）"
        else
            fail "声纹目录对组或其他用户开放；请收紧为 0700: $VOICEPRINT_PATH"
        fi
    else
        warn "未找到 stat，跳过声纹目录权限检查"
    fi
else
    warn "声纹目录尚未创建（首次登记时创建）: $VOICEPRINT_PATH"
fi

if [[ -d "$ROOT/models/funasr" ]]; then
    ok "FunASR 模型目录存在: $ROOT/models/funasr"
else
    warn "FunASR 模型目录不存在: $ROOT/models/funasr"
fi

if command -v ss >/dev/null 2>&1; then
    for port in 50000 50001 9880; do
        if ss -H -ltn | awk '{print $4}' | grep -Eq "(^|:)${port}$"; then
            warn "TCP 端口已有监听: ${port}（请按部署记录确认归属）"
        else
            ok "TCP 端口当前无监听: $port"
        fi
    done
else
    warn "未找到 ss，跳过端口检查"
fi

if command -v nvidia-smi >/dev/null 2>&1; then
    if nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader 2>/dev/null; then
        ok "nvidia-smi 可用"
    else
        warn "nvidia-smi 存在但无法查询 GPU"
    fi
else
    warn "未找到 nvidia-smi；CPU 预检仍可继续，GPU 验收需在目标机确认"
fi

if [[ "$errors" -gt 0 ]]; then
    printf '预检结论: 失败（%s 项）\n' "$errors"
    exit 1
fi

printf '预检结论: 基础条件通过；端口占用、配置内容和模型质量仍需人工验收。\n'
