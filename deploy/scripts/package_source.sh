#!/usr/bin/env bash

# 生成可复制到 Windows/WSL 验证目录的源码包；不读取真实配置，也不打包模型和缓存。
set -euo pipefail

SOURCE_ROOT="$(cd "${1:-$(dirname "${BASH_SOURCE[0]}")/../..}" && pwd)"
OUTPUT_PATH="${2:-${TMPDIR:-/tmp}/sensevoice-next-source.tar.gz}"
OUTPUT_DIR="$(dirname "$OUTPUT_PATH")"
mkdir -p "$OUTPUT_DIR"

STAGING_DIR="$(mktemp -d "${TMPDIR:-/tmp}/sensevoice-package.XXXXXX")"
ARCHIVE_TMP="$STAGING_DIR/source.tar.gz"
MANIFEST_TMP="$STAGING_DIR/SOURCE_MANIFEST.sha256"
trap 'rm -f "$MANIFEST_TMP" 2>/dev/null || true; rmdir "$STAGING_DIR" 2>/dev/null || true' EXIT

if command -v sha256sum >/dev/null 2>&1; then
    hash_file() { sha256sum "$1"; }
else
    hash_file() { shasum -a 256 "$1"; }
fi

# 仅复制运行时、适配层、部署模板和文档，避免把个人文件带到公共电脑。
COPY_ITEMS=(
    api.py
    realtime_api.py
    model.py
    requirements.txt
    speech
    utils
    README_realtime_zh.md
    docs
    deploy/config/sensevoice.env.example
    deploy/config/tts-voices.example.json
    deploy/scripts/diagnose.sh
    deploy/scripts/preflight.sh
    deploy/scripts/package_source.sh
    deploy/systemd/sensevoice-next.service
    LICENSE
)

for item in "${COPY_ITEMS[@]}"; do
    if [[ ! -e "$SOURCE_ROOT/$item" ]]; then
        printf '[FAIL] 缺少打包项: %s\n' "$item" >&2
        exit 1
    fi
done

# 生成归档内的逐文件清单；清单本身只包含哈希和相对路径，不含配置值或模型内容。
(
    cd "$SOURCE_ROOT"
    for item in "${COPY_ITEMS[@]}"; do
        if [[ -d "$item" ]]; then
            find "$item" -type f \
                ! -path '*/__pycache__/*' \
                ! -name '*.pyc' \
                ! -name '._*' -print
        else
            printf '%s\n' "$item"
        fi
    done | LC_ALL=C sort -u | while IFS= read -r file; do
        hash_file "$file"
    done
) > "$MANIFEST_TMP"

COPYFILE_DISABLE=1 tar \
    --no-xattrs \
    --no-mac-metadata \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='._*' \
    -czf "$ARCHIVE_TMP" \
    -C "$SOURCE_ROOT" \
    "${COPY_ITEMS[@]}" \
    -C "$STAGING_DIR" \
    SOURCE_MANIFEST.sha256

# 原子替换目标文件，避免传输端看到半成品；目标路径由调用方明确指定。
mv -f "$ARCHIVE_TMP" "$OUTPUT_PATH"

CHECKSUM="$(hash_file "$OUTPUT_PATH" | awk '{print $1}')"

printf 'archive=%s\nsha256=%s\nmanifest=SOURCE_MANIFEST.sha256\n' "$OUTPUT_PATH" "$CHECKSUM"
