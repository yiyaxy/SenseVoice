# Windows / WSL 模型下载与隔离部署

- 更新：2026-09-10。
- 适用范围：本项目的 SenseVoiceSmall + FSMN-VAD + CAM++ 验证版；沿用既有 GPT-SoVITS 与 HKGAI。
- 当前状态：旧版兼容实时 ASR 热修复已切换到现有 `50000` 并完成中文、英语、粤语线上回归；FunASR 新版仍在隔离端口 `50001` 验证，**没有切换到 50000**。新版时间线/声纹能力仍未完成正式质量验收。
- 最近连接探测：`ssh windows-lan` 已恢复可用；最新白名单包经临时 `50002` 校验后，已切换为 `/data/voice-stack/apps/sensevoice-next` 的隔离 `50001`。在线 `50000` 与 GPT‑SoVITS `9880` 未修改。
- 目标环境沿用仓库记录：Windows 主机 `windows-lan`、WSL2 `Ubuntu`、用户 `muushh`、RTX 5090。执行时先核对，不把历史记录视为本次实测。

## 1. 执行边界

**Mac 只开发代码、维护文档、调用远端 API；全部模型下载、Python 环境安装、推理和部署都在 Windows 的 WSL 中执行。** 不在 Mac 运行 `AutoModel`、模型 demo、依赖安装或 Docker 构建，也不先下载到 Mac 再搬运。

| 内容 | 保存位置 | 处理方式 |
|---|---|---|
| 源码、无凭据模板、操作文档 | Mac 本项目，并同步到 WSL | 小文件，可纳入 Git |
| 新版源码 / Python 环境 | WSL 的 `apps/sensevoice-next` / `environments/sensevoice-next` | 与在线版本隔离 |
| 权重、分词器、模型配置、许可证 | WSL 的 `models/` | 不回传 Mac，不提交 Git |
| 下载缓存、安装缓存、临时结果 | WSL 的 `cache/`、`work/` | 不同步到本地项目 |
| API / LLM / 声纹密钥、真实环境文件 | 部署平台 Secret 或目标机已批准的位置 | 不打印、不打包、不提交 |

已有 `apps/sensevoice`、`environments/sensevoice`、`sensevoice.service`、50000 网关和 GPT-SoVITS 9880 保持不变；50000 仅增加了旧版兼容的缓冲式实时 ASR 路由。FunASR 新版正式切换仍是独立步骤。

## 2. 先确认 Windows 真实可用空间

在 **Windows PowerShell** 执行只读检查：

```powershell
wsl.exe --list --verbose
Get-Volume | Select-Object DriveLetter, FileSystemLabel, SizeRemaining, Size
Get-ChildItem HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss |
  Where-Object { $_.GetValue('DistributionName') -eq 'Ubuntu' } |
  ForEach-Object { $_.GetValue('BasePath') }
wsl.exe -d Ubuntu -u muushh
```

最后一条进入 WSL。下文除特别标明外，均在这个 **WSL Bash** 终端执行：

```bash
whoami
findmnt -T /data/voice-stack
df -h /data/voice-stack
df -i /data/voice-stack
du -sh /data/voice-stack/models /data/voice-stack/environments /data/voice-stack/cache
nvidia-smi
ss -ltn '( sport = :50000 or sport = :50001 or sport = :9880 )'
systemctl is-active sensevoice.service gpt-sovits.service
```

某目录不存在时先核对部署记录，不直接创建另一套在线目录。`/data` 若位于 WSL 的 ext4.vhdx，容量最终来自该虚拟磁盘所在的 Windows 盘；若挂载到其他磁盘，以 `findmnt` 为准。**WSL 的 `df` 显示可增长容量，不等于 Windows 物理盘真的有那么多空闲空间。** 这是 [Microsoft 的 WSL 磁盘说明](https://learn.microsoft.com/en-us/windows/wsl/disk-space)中特别指出的限制。

新虚拟环境、CUDA 轮子、安装缓存、四套模型与旧版保留需要一起计算。建议先预留 **30–50 GiB 的工程余量**，不是模型体积报价或保证值；实际以所选 revision 的文件清单、下载后 `du` 和 Windows 剩余空间为准。磁盘不足时暂停：不要清旧模型，不要手工移动 ext4.vhdx，也不要执行会中断在线服务的 WSL 迁移/关闭命令；先确定目标盘和维护窗口。

GPU 驱动沿用 Windows 侧配置；不要为本任务在 WSL 中安装 Linux 显卡驱动。参见 [NVIDIA WSL 指南](https://docs.nvidia.com/cuda/wsl-user-guide/index.html)。

## 3. 目录与缓存

确认现有 `/data/voice-stack` 可由部署账户写入后，在 WSL 执行：

```bash
set -euo pipefail
umask 077
mkdir -p /data/voice-stack/models/funasr \
  /data/voice-stack/cache/uv /data/voice-stack/cache/pip
export MODELSCOPE_CACHE=/data/voice-stack/models/modelscope
export HF_HOME=/data/voice-stack/models/huggingface
export XDG_CACHE_HOME=/data/voice-stack/cache
export UV_CACHE_DIR=/data/voice-stack/cache/uv
export PIP_CACHE_DIR=/data/voice-stack/cache/pip
if ! mkdir -p /data/voice-stack/work 2>/dev/null; then
  export SV_WORK_ROOT=/data/voice-stack/run
else
  export SV_WORK_ROOT=/data/voice-stack/work
fi
export TMPDIR="$SV_WORK_ROOT"
export SV_RUN_DIR="$(mktemp -d "$SV_WORK_ROOT/funasr-next.XXXXXX")"
export SV_BATCH="$(basename "$SV_RUN_DIR")"
export SV_MODEL_ROOT="/data/voice-stack/models/funasr/$SV_BATCH"
mkdir "$SV_MODEL_ROOT"
printf '本批次记录目录：%s\n模型目录：%s\n' "$SV_RUN_DIR" "$SV_MODEL_ROOT"
```

记录两个实际路径，后续步骤在同一终端执行。重连时恢复本批次变量，不反复新建批次重复下载。

新模型使用独立批次目录，避免对在线 ModelScope 缓存执行 `master` 更新。已存在且有完整来源、revision、校验记录的模型可以直接复用其绝对路径；无证据时不把在线缓存当作已冻结模型，也不删除它。

## 4. 只同步代码，不同步模型

当前工作区包含未提交文件，不能仅 `git clone` 后假定已拿到本次改动。可在 **Mac 项目目录** 制作一个仅含运行代码和文档的小包：

```bash
cd /Users/aliez/Desktop/project/SenseVoice
sv_transfer_dir="$(mktemp -d /tmp/sensevoice-source.XXXXXX)"
./deploy/scripts/package_source.sh \
  /Users/aliez/Desktop/project/SenseVoice \
  "$sv_transfer_dir/sensevoice-next-source.tar.gz"
tar -tzf "$sv_transfer_dir/sensevoice-next-source.tar.gz"
```

脚本只打包固定白名单并输出归档 SHA-256；归档根目录还带有 `SOURCE_MANIFEST.sha256` 逐文件清单。传输前检查清单无真实凭据、模型或录音。本包不包含 `.git`、虚拟环境、模型缓存、`data/` 或真实 `deploy/config/*.env`。Mac 只临时占用这个源码包的空间。通过现有 SFTP/文件传输放到 **Windows 的用户临时目录**；校验完成后仅删除本次生成的临时源码包和其临时目录。包内的 `deploy/scripts/diagnose.sh` 是部署后只读诊断脚本。

若 SSH 别名 `windows-lan` 登录的是 Windows，不要把 `windows-lan:/data/...` 当成 WSL 路径。先传到 Windows，再在 PowerShell 用 `wsl.exe -d Ubuntu -- wslpath -u '<源码包的 Windows 绝对路径>'` 获取对应 Linux 路径。

如果当前终端确认 `ssh windows-lan` 可用，可直接把包传到 SSH 用户的 Windows 主目录（不依赖 Windows 盘符写法）：

```bash
scp "$sv_transfer_dir/sensevoice-next-source.tar.gz" \
  windows-lan:./sensevoice-next-source.tar.gz
```

在 Windows PowerShell 先确认文件位置，再把 Windows 路径转换成 WSL 路径；若 SSH 服务的默认目录不是用户主目录，以 `Get-ChildItem` 找到实际文件后替换变量：

```powershell
$svWindowsArchive = Join-Path $env:USERPROFILE 'sensevoice-next-source.tar.gz'
Get-Item $svWindowsArchive
$svWslArchive = (wsl.exe -d Ubuntu -- wslpath -u $svWindowsArchive).Trim()
Write-Output $svWslArchive
```

随后在 WSL 中将 `SV_SOURCE_ARCHIVE` 设置为上一步输出的路径，并继续下面的 SHA-256 校验。传输完成且校验无误后，再删除 Windows 用户目录中的临时源码包。

回到 **WSL**，替换下面唯一的源码包路径后执行：

```bash
export SV_SOURCE_ARCHIVE='/mnt/c/替换为源码包的实际路径/sensevoice-next-source.tar.gz'
sha256sum "$SV_SOURCE_ARCHIVE"
tar -tzf "$SV_SOURCE_ARCHIVE"
test ! -e /data/voice-stack/apps/sensevoice-next
mkdir /data/voice-stack/apps/sensevoice-next
tar -xzf "$SV_SOURCE_ARCHIVE" -C /data/voice-stack/apps/sensevoice-next
(cd /data/voice-stack/apps/sensevoice-next && sha256sum -c SOURCE_MANIFEST.sha256)
sha256sum "$SV_SOURCE_ARCHIVE" > "$SV_RUN_DIR/source.sha256"
```

两端 SHA-256 必须一致。若 `sensevoice-next` 已存在，先确认其中是否有未保存改动；不覆盖、不用 `rsync --delete`。这时改用另一个明确命名的验证目录，并同步调整后文路径。

## 5. 安装隔离环境，不升级在线环境

本节仅为目标机执行步骤，不在 Mac 安装。沿用仓库旧版 RTX 5090 记录中的 `torch==2.11.0+cu128` / `torchaudio==2.11.0+cu128`；FunASR `1.4.13` 是本次候选，**不是已验收组合**。Python 建议采用 3.11，并确认目标机已具备该解释器及 `uv`。

当前 `requirements.txt` 不是完整锁文件，也包含新增的 `cryptography`。实际安装前确认依赖清单；新增生产依赖、系统包或敏感配置需要单独确认。本节不授权升级驱动或修改系统 Python。

```bash
export SV_UV=/data/voice-stack/tools/uv
test -x "$SV_UV"
"$SV_UV" --version
export SV_BASE_PYTHON=/data/voice-stack/environments/sensevoice/bin/python
"$SV_BASE_PYTHON" --version
test ! -e /data/voice-stack/environments/sensevoice-next
"$SV_UV" venv --python "$SV_BASE_PYTHON" /data/voice-stack/environments/sensevoice-next
export SV_PYTHON=/data/voice-stack/environments/sensevoice-next/bin/python
"$SV_UV" pip install --python "$SV_PYTHON" \
  --index-url https://download.pytorch.org/whl/cu128 \
  'torch==2.11.0+cu128' 'torchaudio==2.11.0+cu128'
"$SV_UV" pip install --python "$SV_PYTHON" \
  -r /data/voice-stack/apps/sensevoice-next/requirements.txt \
  'torch==2.11.0+cu128' 'torchaudio==2.11.0+cu128' 'funasr==1.4.13'
"$SV_UV" pip check --python "$SV_PYTHON"
"$SV_UV" pip freeze --python "$SV_PYTHON" > "$SV_RUN_DIR/requirements.freeze.txt"
"$SV_PYTHON" -c 'import fastapi, uvicorn, websockets, soundfile, modelscope, funasr; print("依赖导入成功")'
"$SV_PYTHON" -c 'import torch, torchaudio; print(torch.__version__, torchaudio.__version__, torch.version.cuda, torch.cuda.is_available())'
```

第二次安装仍显式约束 CUDA 轮子，避免被宽泛的 `torch>=...` 升级。若解析冲突、缺少解释器/系统库、`uvicorn`/`websockets` 等导入失败，停止并记录缺项，不去在线环境补装，也不临时放宽版本。`uv` 的约束和冻结命令见[官方命令说明](https://docs.astral.sh/uv/reference/cli/)。冻结文件供复现审查，尚不等于含完整 wheel 校验值的离线安装包。

## 6. 仅在 WSL 下载四个模型

| 用途 | ModelScope ID | 本批次子目录 |
|---|---|---|
| ASR | `iic/SenseVoiceSmall` | `sensevoice` |
| VAD | `iic/speech_fsmn_vad_zh-cn-16k-common-pytorch` | `fsmn-vad` |
| 标点 | `iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch` | `punc` |
| 说话人 / 声纹 | `iic/speech_campplus_sv_zh-cn_16k-common` | `campplus` |

上述模型 ID 来自 [FunASR v1.4.13 别名表](https://github.com/modelscope/FunASR/blob/v1.4.13/funasr/download/name_maps_from_hub.py)。本次不下载额外的 Paraformer、3D-Speaker 或 GPT-SoVITS 权重。

标点模型已纳入候选适配器默认配置：它用于恢复句末标点，并满足 FunASR CAM++ 说话人分段的输入要求；
不因此宣称逐词时间戳或多人质量已验收。若不需要说话人分段，可在受控环境将
`SENSEVOICE_PUNC_MODEL` 置空并重新验证，但实时链路仍会固定关闭 SenseVoice ITN，避免重复标点。

首轮探索可以下载 `master` 到隔离批次目录；它会变化，不能称为固定版本。能确认发布 tag 时先替换为对应 tag。不同 ModelScope SDK 对 commit hash 的支持存在差异，不直接假定任意 Git SHA 都能作为 `revision`；[SDK 下载接口说明](https://github.com/modelscope/modelscope/blob/v1.34.0/modelscope/hub/snapshot_download.py)提供 `revision` 与 `local_dir` 参数。

```bash
export SV_ASR_REVISION=master
export SV_VAD_REVISION=master
export SV_PUNC_REVISION=master
export SV_SPK_REVISION=master
"$SV_PYTHON" - <<'PY'
import os
from pathlib import Path
from modelscope.hub.snapshot_download import snapshot_download

root = Path(os.environ["SV_MODEL_ROOT"])
models = [
    ("iic/SenseVoiceSmall", "sensevoice", "SV_ASR_REVISION"),
    ("iic/speech_fsmn_vad_zh-cn-16k-common-pytorch", "fsmn-vad", "SV_VAD_REVISION"),
    ("iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch", "punc", "SV_PUNC_REVISION"),
    ("iic/speech_campplus_sv_zh-cn_16k-common", "campplus", "SV_SPK_REVISION"),
]
for model_id, folder, revision_var in models:
    revision = os.environ[revision_var]
    downloaded = snapshot_download(
        model_id=model_id,
        revision=revision,
        cache_dir=os.environ["MODELSCOPE_CACHE"],
        local_dir=str(root / folder),
    )
    print(model_id, revision, downloaded)
PY
```

这里只下载仓库快照，不实例化模型、不运行推理、不占用模型显存。完整下载配置、权重、分词文件和许可说明，不仅复制 `model.pt`；不执行仓库远程代码。失败时保留同一批次目录，用同一参数重试，SDK 可复用已校验文件；不要同时启动多个下载任务，不要清空整个缓存。

下载成功后在 WSL 冻结校验清单：

```bash
printf 'iic/SenseVoiceSmall\t%s\niic/speech_fsmn_vad_zh-cn-16k-common-pytorch\t%s\niic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch\t%s\niic/speech_campplus_sv_zh-cn_16k-common\t%s\n' \
  "$SV_ASR_REVISION" "$SV_VAD_REVISION" "$SV_PUNC_REVISION" "$SV_SPK_REVISION" > "$SV_RUN_DIR/model-revisions.tsv"
for sv_component in sensevoice fsmn-vad punc campplus; do
  test -s "$SV_MODEL_ROOT/$sv_component/config.yaml"
done
test -s "$SV_MODEL_ROOT/sensevoice/model.pt"
test -s "$SV_MODEL_ROOT/fsmn-vad/model.pt"
test -s "$SV_MODEL_ROOT/punc/model.pt"
test -s "$SV_MODEL_ROOT/campplus/campplus_cn_common.bin"
for sv_component in sensevoice fsmn-vad punc campplus; do
  (
    cd "$SV_MODEL_ROOT/$sv_component"
    find . -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum
  ) > "$SV_RUN_DIR/$sv_component.sha256"
  (cd "$SV_MODEL_ROOT/$sv_component" && sha256sum -c "$SV_RUN_DIR/$sv_component.sha256")
done
du -sh "$SV_MODEL_ROOT"/*
df -h /data/voice-stack
```

校验清单与 `model-revisions.tsv` 一起标识实际下载产物；哈希只能证明文件未变，不能替代授权审核或推理验证。若文件布局与预期不符，先检查该 revision，不跳过检查。下载完成的批次不再执行更新下载；再次取新版本应新建批次。

正式验收要记录：每个模型 ID、请求的 revision、可获得的上游版本元数据、下载时间、每文件 SHA-256、实际大小、模型许可审核结果。没有可靠固定上游版本时，以保留的完整快照和校验清单复现，不能宣称以后从 `master` 重新下载会得到相同权重。

## 7. 本地路径加载与试运行

加载时使用三个明确的绝对路径，不使用模型别名，避免服务启动时隐式拉取另一份模型。FunASR 的[本地目录加载逻辑](https://github.com/modelscope/FunASR/blob/v1.4.13/funasr/download/download_model_from_hub.py)支持这种方式；不要仅依赖 `disable_update` 把网络下载当成已禁用。

```bash
export SENSEVOICE_MODEL="$SV_MODEL_ROOT/sensevoice"
export SENSEVOICE_VAD_MODEL="$SV_MODEL_ROOT/fsmn-vad"
export SENSEVOICE_PUNC_MODEL="$SV_MODEL_ROOT/punc"
export SENSEVOICE_SPK_MODEL="$SV_MODEL_ROOT/campplus"
export SENSEVOICE_MODEL_REVISION="snapshot-$(sha256sum "$SV_RUN_DIR/sensevoice.sha256" | cut -d ' ' -f 1)"
export SENSEVOICE_VAD_MODEL_REVISION="snapshot-$(sha256sum "$SV_RUN_DIR/fsmn-vad.sha256" | cut -d ' ' -f 1)"
export SENSEVOICE_PUNC_MODEL_REVISION="snapshot-$(sha256sum "$SV_RUN_DIR/punc.sha256" | cut -d ' ' -f 1)"
export SENSEVOICE_SPK_MODEL_REVISION="snapshot-$(sha256sum "$SV_RUN_DIR/campplus.sha256" | cut -d ' ' -f 1)"
export SENSEVOICE_BUILD_VERSION="source-$(sha256sum "$SV_SOURCE_ARCHIVE" | cut -d ' ' -f 1)"
export SENSEVOICE_ALLOWED_ORIGINS=http://localhost:50001
export SENSEVOICE_VOICEPRINT_PATH="$SV_RUN_DIR/voiceprints"
unset SENSEVOICE_VOICEPRINT_ADMIN_KEY SENSEVOICE_VOICEPRINT_KEY SENSEVOICE_VOICEPRINT_SESSION_KEY
read -rsp '输入仅供本次隔离验证的 API Key：' SENSEVOICE_API_KEY
printf '\n'
test -n "$SENSEVOICE_API_KEY"
export SENSEVOICE_API_KEY
```

`snapshot-...` 是本地文件清单的哈希标识，不冒充上游 revision；对应的来源保留在上一步记录中。这些 revision 变量在模型参数为现存本地目录时用于本项目版本标注，不要求模型站点解析该哈希。

首轮不登记真实人员、不设置声纹管理/加密密钥，不连接生产人员库。不要 `source`、打印或覆盖 `/data/voice-stack/config/sensevoice.env`。需要测试完整对话时，由授权操作人把既有 LLM/TTS 参数注入验证进程，不复制凭据进仓库；HKGAI 请求会发送转写内容，须使用获准的联调录音。

确认 CPU 内存余量后可先做 CPU 功能验证；GPU 性能验证必须另外确认旧 ASR 与 GPT-SoVITS 的显存余量/维护窗口。不要并发挤占在线服务，CPU 结果也不能当作 RTX 5090 延迟成绩。

```bash
cd /data/voice-stack/apps/sensevoice-next
export SENSEVOICE_DEVICE=cpu
# 仅在确认 GPU 余量后，将上面的 cpu 改为 cuda:0。
"$SV_PYTHON" -m uvicorn realtime_api:app --host 0.0.0.0 --port 50001 --workers 1
```

前台运行，不用 `--reload` 或多 worker 复制模型。Windows 局域网直连需由管理员配置端口转发和
Private/LocalSubnet 防火墙规则；本次已配置 `192.168.101.75:50001 → 172.31.30.214:50001`。
完整对话配置未注入时，LLM/TTS 缺项属于预期，不把缺项补成虚假值。

Mac 访问默认走现有 SSH 隧道。先在 **Windows PowerShell** 确认回环转发可用：

```powershell
curl.exe --fail http://localhost:50001/api/v1/health
```

确认 `windows-lan` 的 SSH 已可登录且上述地址可达后，在 **Mac** 执行：

```bash
ssh -N -o ExitOnForwardFailure=yes -L localhost:50001:localhost:50001 windows-lan
```

然后从 Mac 直接访问 `http://192.168.101.75:50001`。本次已配置 Windows `portproxy` 和仅限
Private/LocalSubnet 的防火墙规则；若 Windows DHCP 地址变化，需要更新转发和客户端地址。SSH
隧道仍可作为备用入口。

## 8. 验收与当前禁止切换的条件

先在 WSL 用已有且获准的短录音，按[调用文档](./api-call-guide.zh-CN.md)验证 HTTP 与两条 WebSocket。默认手动 `commit`；音频应放在本批次 `work/` 下，不能提交入库。认证 Header 不写入 shell 历史中的明文常量，不开启 `set -x` 或输出全部环境变量。

若绕过 HTTP 直接调用引擎，必须先用项目的 `decode_audio` 归一化为单声道 16 kHz；直接把 32/48 kHz
数组交给 FunASR 会导致句段毫秒边界按错误采样率计算。

| 门禁 | 必须保留的证据 |
|---|---|
| 下载 / 环境 | SenseVoice、FSMN-VAD、标点、CAM++ 四个模型的来源与校验清单、实际占用、依赖冻结、CUDA 实际版本 |
| HTTP | 同一授权录音旧/新结果；中文、英语、粤语分别可识别；旧字段兼容 |
| 时间线 / 匿名说话人 | 有效毫秒边界、句段文本、局部标签与正确中心向量对应；对外无向量 |
| WebSocket | 实际握手、partial/final/completed 顺序、手动 commit、过载时 final 不丢失 |
| 完整对话 | 获准的 ASR → HKGAI → 既有 GPT-SoVITS，单话轮只回复一次 |
| 声纹（另行启用） | 授权与质量门禁、可信租户隔离、加密、跨重启、拒识、删除；不同模型版本不能混比 |

**当前仓库代码还有上线阻断项，下载成功不等于改造已完成：**

- `speech/engine.py` 的 `warmup()` 现在会执行一次短静音推理，但这只是调用链探测，不是质量验收；仍不能仅凭 `ready=true` 验收。
- 本地适配器已修正 CAM++ 中心向量与局部标签的对应：只有 `spk_embedding_center` 参与人员识别，分块 `spk_embedding` 不再按下标冒充人员标签；目标机仍需用真实多人录音核验。
- 已同步版本的 `realtime_api.py` 的 `auto_vad:true` 已改为独立 FSMN-VAD，并在目标机通过连续 PCM 分帧自动返回 `asr.final → asr.completed`；收口后的 cache 重置、统一 CER、声纹目录可用性和 TTS 队列预算修复均已纳入候选源码。默认仍保持手动提交。多人质量、过载 final 保留和生产切换仍未完成验收。
- 2026-09-10 已同步候选版本并加载 `punc_ct-transformer`，完成候选源码的中文、英语、粤语 HTTP/WS 冒烟；它用于减少 SenseVoice ITN 与实时标点重复，并补齐 FunASR 句段分段输入。仍需独立多人录音确认声纹映射和边界质量。
- 声纹登记质量门禁、模型 revision/预处理版本隔离和加密存储已在本地适配器实现，但目标机尚未完成跨重启、拒识、删除和多人质量验收；租户仍依赖 BFF 同时注入 Header 与会话密钥，尚未完成业务身份绑定和密钥轮换。只靠额外管理 Key 不构成生产级租户隔离。

以上以本次文档核对时的代码为准，后续修复应同步更新状态并补实际证据。详见 [P0–P4 实施与验收方案](./funasr-integration-plan.zh-CN.md)。本次不新增测试框架、测试资产或基准工具。

## 9. 正式切换、回退与资料归档

本轮 FunASR 新版已完成“模型下载完成 / 候选源码同步 / 隔离启动 / P1 HTTP、WS、TTS 和 HKGAI 全链路冒烟”阶段；自动 VAD 哨兵事件兼容和实时对话门禁重试修复已切换到隔离 `50001`，并完成手动、自动收口与完整对话验证。上节门禁中的多人质量、声纹、集中延迟和资源证据尚未齐全，不能据此切换在线 `50000`；旧版兼容实时 ASR 热修复继续运行在 `50000`。

切换前需另行确认：维护窗口、现有服务版本与回退入口、GPU 资源、API/BFF 兼容，以及敏感配置的准确路径和变更。若需要 `/data/voice-stack/config/sensevoice-next.env`、声纹密钥或生产配置修改，逐项说明内容与影响，获得确认后再操作；不自动从 `.env.example` 覆盖真实文件。

若后续批准 FunASR 新版切换到在线入口，采用同一组已校验源码、隔离环境和冻结模型路径，经现有 50000 网关接入；GPT-SoVITS 和 HKGAI 配置保持原值。保留当前旧版兼容补丁、旧 venv、旧模型和回退备份；新版声纹数据不覆盖旧数据，避免回退时跨模型版本混用。`50001` 是 `muushh` 的 transient user unit，目标机 `Linger=no`，WSL/用户会话重启后必须手动启动；本次联调不安装或启用开机 unit。

验证新版失败时，先执行 `systemctl --user stop sensevoice-next.service` 停止当前隔离 unit，关闭本次 SSH 隧道，确认当前旧版兼容补丁的 `http://localhost:50000/api/v1/health` 和既有客户端仍正常；不停止旧服务、不卸载依赖、不删除模型。若 FunASR 正式切换后失败，按已确认的回退配置恢复旧版 50000，再核对真实 HTTP/WS 调用。

归档分两层：

- Windows 留存完整快照、依赖清单、校验记录和必要的受控验收产物；临时音频按授权保留期限清理。
- 本地项目只回填不含敏感数据的版本/模型清单摘要、验收结论、实际部署状态与回退说明。不要回传权重、密钥、人员声纹或录音。

完成后更新本文件、[实时服务说明](../README_realtime_zh.md)与改造方案：分别标记“已下载”“已验证”“已切换”，没有对应证据的项目继续标为待完成。

## 10. 本次 Windows/WSL 执行记录

2026-09-04 已在 `windows-lan` 的 WSL2 `DESKTOP-1ENSNDP` 上完成“源码同步、隔离依赖、三模型下载、文件校验和隔离端口冒烟”；2026-09-10 最终白名单源码包 SHA-256 为 `121f197b6c19584578c4329f5a3eb4b99f3089de63eea6ef23bc0e399ab7bc0c`，在临时 `50002` 逐文件校验、门禁繁忙重试与全链路验证通过后，已切换到 `/data/voice-stack/apps/sensevoice-next` 并以 transient user unit 监听 `50001`。此前版本分别保留为 `/data/voice-stack/apps/sensevoice-next-previous-20260910-1410` 与 `/data/voice-stack/apps/sensevoice-next-previous-20260910-1740`；这些是隔离服务部署与 P1 冒烟证据，不是线上 `50000` 切换或完整效果验收：

| 项目 | 实际记录 |
|---|---|
| 源码（隔离已部署） | `/data/voice-stack/apps/sensevoice-next`；包及逐文件清单校验通过，运行最新实时对话任务回收修复。 |
| Python 环境 | `/data/voice-stack/environments/sensevoice-next`；Python 3.11.16 |
| 关键依赖 | `funasr==1.4.13`、`numpy==1.26.4`、`torch/torchaudio==2.11.0+cu128`；隔离 Python 未安装 pip，但核心 ASR/音频/网关依赖导入通过，CUDA 可见 |
| 模型批次 | `/data/voice-stack/models/funasr/funasr-next.r0zdrp`；原三模型请求 revision 均为 `master`，标点模型在候选进程按同 revision 加载，仅作隔离验证 |
| SenseVoiceSmall | 约 897 MiB，权重 `936291369` bytes；清单 `/data/voice-stack/run/funasr-next.r0zdrp/sensevoice.sha256` |
| FSMN-VAD | 约 3.9 MiB，权重 `1721366` bytes；清单 `/data/voice-stack/run/funasr-next.r0zdrp/fsmn-vad.sha256` |
| 标点模型 | 缓存文件于 2026-09-07 生成，本轮 2026-09-10 候选进程加载成功；实际文件清单仍待写入固定批次，不把运行时加载成功当作冻结证据 |
| CAM++ | 约 28 MiB，权重 `campplus_cn_common.bin` 为 `28036335` bytes；清单 `/data/voice-stack/run/funasr-next.r0zdrp/campplus.sha256` |
| 目标机余量 | `/data/voice-stack` 当前约 805 GiB 可用；50000、50001 和 9880 均按各自隔离边界运行 |

P1 冒烟结果（同一隔离进程内的单次热机参考，不是性能承诺）：

| 音频 / 接口 | 结果 |
|---|---|
| `zh.mp3`、`en.mp3`、`yue.mp3`（引擎直调） | 分别检测为 `zh`、`en`、`yue`；均返回非空句段边界，CAM++ 中心向量为 `1×192`；显式预热后 5 次重复约 `84–400 ms / 94–128 ms / 76–97 ms`，样本太少不能作为 P95 性能承诺 |
| `zh.mp3`（已同步候选 HTTP `/api/v1/asr`） | 中文 `zh`，文本“开饭时间早上九点至下午五点”，句段 `420–5600 ms`，匿名模式；无向量泄露 |
| `zh.mp3`、`en.mp3`、`yue.mp3`（已同步候选 HTTP `/api/v1/asr`） | 分别返回 `zh`、`en`、`yue`，均有 1 个有效句段；英语文本为 “the tribal chieftain called for the boy and presented him with fifty pieces of gold”，粤语文本为“呢几个字都表达唔到我想讲嘅意思” |
| `zh.mp3`（已同步候选 WS `/api/v1/asr/stream`） | 握手、配置、`asr.processing → asr.partial → asr.final → asr.segment.final → asr.completed` 顺序通过；手动 `commit` 完成 |
| `zh.mp3`（隔离 `50001` WS `/api/v1/asr/stream`, `auto_vad=true`） | 连续 PCM 分帧并追加静音后自动返回 `asr.final → asr.completed`，未发送 `commit`；协商事件返回 `vad_engine=fsmn-vad`，final 句段为 `420–5440 ms`、匿名 `speaker_0` |
| `assistant_default`（本次 `50002` WS `/api/v1/tts/stream`，模式 3） | 中文/英语/粤语均返回 32 kHz、单声道、16 bit PCM16；首有效音频约 `569/107/239 ms`，总耗时约 `4145/1418/1885 ms`，仅为单请求样本 |
| `zh.mp3`（隔离 `50001` WS `/api/v1/realtime`） | HKGAI SSE → GPT-SoVITS PCM16 全链路通过；本次返回 `asr.final`、1 个 `tts.start/tts.end` 与 `516838` 字节 PCM；门禁繁忙时同一会话可在释放后不重传音频再次 `commit` 完成对话 |
| 旧 50000 与新引擎同录音 | 英语文本一致；粤语有无逗号差异；中文曾出现“开饭/开放”差异；旧 50000 HTTP 约 `52–98 ms`，新引擎首个请求含冷启动约 `5.65 s`，必须分开统计 |
| CAM++ 示例声纹 | 三个官方示例均返回 `192` 维向量；同一说话人两段余弦约 `0.693`，与另一说话人约 `-0.084 / 0.007`；默认阈值 `0.72` 不能直接视为已校准，需独立验证集调参 |
| FSMN-VAD 示例 | 读取前 10 秒、分 1 秒块调用在线接口，最后返回约 `70–9980 ms` 边界；首块约 `5137 ms` 冷启动，说明构件可调用，不等于 WS 自动收口已验收 |

FunASR 仍提示 SenseVoice 没有逐 token timestamp；本批次保留 VAD 句段边界，
不把文本长度换算成伪时间。HTTP 与 WS 的中文清理文本存在一次“开饭/开放”差异，需用授权验收录音
复测，不能把这次冒烟结果当作准确率结论。把两个不同说话人的 CAM++ 示例拼接后仍只得到一个
`[0, 9510] ms` 句段，不能证明多人分段或 `spk` 与中心向量映射；下一步必须使用获准的多人混合录音。
`50001` 由 `muushh` 的 transient systemd user unit `sensevoice-next.service` 手动启动并监听
`0.0.0.0`；它不会在 WSL 重启后恢复，停止该 unit 不影响现有 50000（已含旧版兼容实时 ASR 热修复）与 9880。

当前隔离已部署版哈希为：`realtime_api.py`=`f2ca83792dfdab6625473219683fa5c541f0b5b1d1d840faa8ea1e299decbad2`、
`api.py`=`42715971185b909309dde7adc8c147752ea979e84735ea5054c39cc7f60827c7`、
`speech/engine.py`=`012559b2bed836f21ff28ec6af6cebb3d76d5f0cafd2870b895f9ed34277832d`、
`speech/timeline.py`=`abb135f7f29c4469aed824da6091302e7cea83c14b825a2e9a7e54b56ab67f56`、
`speech/audio.py`=`381ebd2c5638eb202396c972aeceffa898eb9847bdad50faea1dd00bd65a8773`、
`speech/session.py`=`50f71e9409158d02225fbe2c8915ba7ed52e353cb15f1b0a2d6946a00886abd8`、
`speech/metrics.py`=`4de50708b190350fe5988de2097b7d7de40836b0dd053e6b36fd52f1ff51850f`、
`speech/speaker.py`=`2c8c263c6f8a751c2f549af4f8659cabfbea713c81814d47bd1f65baff813e81`、
`speech/voice.py`=`6a53ec677d7df5e719b41ac48ed02a70345ff2f2ba306e360098e7016a440157`、
`speech/auth.py`=`6a6d1a224af328a5158a427af838e5daba903f3fae18aa36df0371150643f230`、
`speech/tts.py`=`3dccd06217306a9acc61f1a12e4a4be1ac6ef29f3a2d15e2f77376c8ac0d9eb3`、
`deploy/scripts/preflight.sh`=`2a86a3aa9fbebc1b26e59b03d757ee7b5cdf6f501399d0f2959960aa50c268e0`、
`deploy/scripts/diagnose.sh`=`6f8839d414bee9f528ac615f64e91067ad5e9f38e751af5607793788aa545f53`。

历史记录（以下字段为此前候选版本，非当前隔离已部署哈希；当前以本段上方为准）：
`api.py`=`da502a9d0bf1a13ee49ca1c1041c58dc704290a913e219c6092eecfddd9fdeea`，
`speech/engine.py`=`aa1c8e8f125b06d8f90670c91057385664a24aaf1da34dda0bf4f2b72666f6af`，
`speech/audio.py`=`85cd03f0697f145a14a6a3e92e541caf946e7d5f6282bce68e14b16d48cc5049`，
`speech/voice.py`=`daee2b550e6e123aa98f679fdb68e10b7e3f632fcbd11a7441c06064d01e1d20`，
`speech/session.py`=`399c8eda85916abccca08b8d645dcbce9fa7dcf51cb2253385e7effb673c612c`，
`speech/metrics.py`=`24a11ccdcb1f553e9eda912557b8bd2e029df8ee70978fdcb8e256ca71ee1dd4`，
`speech/speaker.py`=`9aa69a468acd8cd6542fea0afa45e18ec401c742df9f87e9a06b26cc8b045a7c`，
`speech/tts.py`=`a4ae43b3e654fdbf85569989f5b17f5242c34a7eb07835a653d7d68fbde4ac17`。此前候选包已逐文件校验并启动隔离 unit；本地这些修复仍需按同一白名单流程重新同步，不能只覆盖单个路由文件。

仓库新增了隔离服务模板 [sensevoice-next.service](../deploy/systemd/sensevoice-next.service)。待维护窗口和
正式持久化前，先核对 `/data/voice-stack/apps/sensevoice-next`、虚拟环境和真实配置，再由管理员按以下
顺序安装；本模板只监听 WSL 回环地址 `127.0.0.1:50001`，不会替换或重启 50000：

先执行只读预检（不会读取配置内容或修改任何文件；若真实配置已存在，还会检查其不对组/其他用户开放）：

```bash
bash deploy/scripts/preflight.sh
```

预检失败时先修复路径或隔离环境；端口占用、真实配置内容和 GPU 余量仍需人工结合当前服务记录确认。

源码同步并启动后，再执行只读诊断；它会检查候选源码逐文件清单、候选/旧服务、9880、配置和声纹目录权限、核心依赖、健康 HTTP 状态及 `/api/v1/health` 能力 JSON，并打印关键源码 SHA-256，不会输出密钥或配置值：

```bash
bash deploy/scripts/diagnose.sh
```

若隔离验证把声纹目录放在本批次运行目录（而不是默认的
`/data/voice-stack/data/voiceprints`），执行前显式传入实际路径，例如：

```bash
SENSEVOICE_VOICEPRINT_PATH="$SV_RUN_DIR/voiceprints" bash deploy/scripts/diagnose.sh
```

诊断输出中的 `FAIL` 必须先处理；`WARN` 需要结合是否启用对应能力判断。诊断脚本返回非零只表示存在阻断项，不代表可以跳过后续的真实录音、多人声纹和端到端延迟验收。

若 SSH 登录落在 Windows 而不是 WSL，可不落盘直接把脚本送入 Ubuntu 执行：

```bash
ssh windows-lan 'wsl.exe -d Ubuntu -- bash -s' < deploy/scripts/diagnose.sh
```

```bash
sudo install -m 0644 deploy/systemd/sensevoice-next.service /etc/systemd/system/sensevoice-next.service
sudo systemctl daemon-reload
sudo systemctl enable --now sensevoice-next.service
curl --fail http://localhost:50001/api/v1/health
```

安装前后都要记录 `realtime_api.py` 哈希和服务日志；若健康检查失败，停止并禁用该隔离 unit，确认
50000 与 9880 未受影响，再按回退快照恢复。不要用该模板覆盖现有 `sensevoice.service`。

本轮新增 TTS 适配验证：`/api/v1/tts/voices` 返回 `assistant_default`；已同步候选 `/api/v1/tts/stream`
在模式 2 下的中文/英语/粤语请求均返回 32 kHz、单声道、16 bit PCM16；`/api/v1/realtime`
已完成 HKGAI → GPT-SoVITS PCM 流冒烟。`/api/v1/dev/tts` 的完整 WAV 回读和取消语义沿用此前记录。
以上仍是单机单请求样本，未覆盖 BFF、浏览器播放和多人声纹质量。

上述 manifest 位于 Windows/WSL，不复制回 Mac 或 Git。`master` 会变化，FunASR 正式上线前仍需替换为经授权审核的固定 revision/快照，并完成 P1–P4 真实验收。本次已用这些路径启动并验证 50001 隔离服务，HTTP/WS 基线与 TTS 流式冒烟通过；当前 50000 已在 2026-09-04 10:47 CST 重启并完成线上回归。

### 10.1 旧版兼容实时 ASR 热修复（2026-09-04）

| 项目 | 实际记录 |
|---|---|
| 线上目录 | `/data/voice-stack/apps/sensevoice` |
| 变更文件 | `api.py`、`realtime_api.py`；新增 `/api/v1/asr/stream`，离线/实时共用模型锁 |
| 备份目录 | `/data/voice-stack/backups/sensevoice-20260904-1048` |
| 线上版本 SHA-256 | `api.py`=`6eedb8911aa430374d5dacd6594e4a8d084466fd711499613c997407027aeab`；`realtime_api.py`=`5473c15cd61a17e144fadc0096aacba3133fa9363e2b937b6933981401ae2c3a` |
| 验收 | 有效 Key 握手成功；无效/缺失 Key 返回 403；中文、英语、粤语均通过 `processing → partial → final → completed`；离线 `/api/v1/asr` 通过；`/api/v1/realtime` 鉴权及心跳通过 |
| 兼容边界 | 16 kHz、单声道、PCM16 little-endian；手动 `commit`；不启用自动 VAD、时间线或声纹字段 |

本次验证使用的公开 SenseVoice 示例音频；未打印或写入任何 API/LLM/TTS 密钥。中断下载产生的用户缓存目录已清理，未触碰 `/data/voice-stack` 既有模型缓存。
