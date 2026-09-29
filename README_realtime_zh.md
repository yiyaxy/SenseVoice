# 实时语音对话服务

服务以 SenseVoice 提供 ASR，以既有的 OpenAI 兼容 LLM 生成回复，并调用独立部署的
GPT-SoVITS v2 `/tts` 接口合成语音。网关不保存模型密钥、LLM 密钥，也不复制请求中的
参考音频；复刻音色所需的参考音频由 GPT-SoVITS 主机按音色授权和生命周期管理，并通过
运行时配置引用。

当前候选/部署路径使用已配置的 HKGAI OpenAI-compatible LLM；历史开发基线的上游结果不属于当前部署或对外服务依据。

面向业务客户端的参数、事件、浏览器示例与 HKGAI 配置见
[语音对话服务调用文档](./docs/api-call-guide.zh-CN.md)。

实时 TTS、复刻音色、边收边播及分阶段交付见
[实时语音与复刻音色方案](./docs/realtime-voice-plan.zh-CN.md)。当前代码已提供独立
`/api/v1/tts/stream`；最新白名单包已通过 staging 验证并切换至 WSL 隔离服务目录
`/data/voice-stack/apps/sensevoice-next`，`50001` 已完成自动 VAD、实时 TTS、HKGAI 全链路及门禁繁忙重试验证；在线 `50000` 尚未切换这版代码。仓库提供了可直接联调的
[AudioWorklet 播放页](./docs/realtime-tts-player.html)；播放器会在收到首个 PCM 后立即排队播放，且在取消/end 后丢弃晚到的旧帧；生产环境仍应由业务 BFF 代理并托管 API Key。
`50001` 是手动启动的隔离测试服务，当前通过 Windows 局域网地址 `192.168.101.75:50001`
提供访问；WSL 重启后不会自动恢复，联调前先手动启动并检查健康接口。

时间线、匿名说话人和持久声纹识别的实现边界见
[FunASR 改造方案](./docs/funasr-integration-plan.zh-CN.md)。当前是适配层与接口初版，远端同步、
真实多人映射/质量、实时调度和可信租户隔离仍是上线门禁，不能作为已验收能力使用。

模型下载、依赖安装、试运行和回退统一见
[Windows / WSL 部署指南](./docs/windows-wsl-deployment.zh-CN.md)：全部重型操作在 Windows/WSL，
Mac 只维护代码和文档，不下载模型。旧版兼容的缓冲式 `/api/v1/asr/stream` 已部署到在线
50000；FunASR 时间线/声纹验证版仍不替换在线服务。

## 服务链路

```text
客户端音频 → WebSocket → SenseVoice ASR → OpenAI 兼容 LLM → GPT-SoVITS → 客户端音频
```

识别语言可选 `zh`、`en`、`yue`、`ja`、`ko` 或 `auto`。`auto` 检测到中文、英语或粤语后，
默认分别映射到 GPT-SoVITS 的 `all_zh`、`en`、`all_yue`。如你的 GPT-SoVITS 版本使用
不同语言标识，可通过 `GPT_SOVITS_TEXT_LANG` 或 WebSocket `configure` 事件覆盖。

## 必要环境变量

| 变量 | 说明 |
|---|---|
| `SENSEVOICE_API_KEY` | 外部访问 ASR 与 WebSocket 的 API Key，必须设置。 |
| `LLM_BASE_URL` | OpenAI 兼容服务根地址，例如 `https://llm.example.com/v1`。 |
| `LLM_MODEL` | 对话模型名。 |
| `LLM_API_KEY` | LLM 的访问密钥；本地无鉴权服务可留空。 |
| `GPT_SOVITS_BASE_URL` | GPT-SoVITS v2 服务地址，例如 `http://gpt-sovits:9880`。 |
| `GPT_SOVITS_REF_AUDIO_PATH` | 默认音色的参考音频绝对路径；若 `SENSEVOICE_VOICE_PROFILE_FILE` 中每个音色都自带路径，可留空。 |
| `GPT_SOVITS_PROMPT_LANG` | 默认音色参考稿的语言标识；使用自带 `prompt_lang` 的音色 JSON 时可留空。 |
| `SENSEVOICE_VOICEPRINT_ADMIN_KEY` | 声纹登记/删除的独立管理密钥；仅可信 BFF 使用。 |
| `SENSEVOICE_VOICEPRINT_KEY` | 声纹持久化加密密钥；必须与数据分离保管。 |
| `SENSEVOICE_VOICEPRINT_SESSION_KEY` | 声纹识别会话密钥；BFF 必须与 `X-Voiceprint-Tenant` 一起携带，网关才会执行记名匹配。 |

可选变量：

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `LLM_CHAT_COMPLETIONS_URL` | 空 | 可填写完整 Chat Completions 地址；它与 `LLM_BASE_URL` 二选一即可。 |
| `LLM_SYSTEM_PROMPT` | 简洁口语助手 | 每轮发送给 LLM 的系统提示词。 |
| `SENSEVOICE_MAX_LLM_OUTPUT_CHARS` | `12000` | 单轮 LLM 回复的最大字符数；超限返回 `llm_output_too_large`，避免无界输出占用内存。 |
| `SENSEVOICE_ALLOWED_ORIGINS` | 本机地址 | 允许的浏览器来源，以逗号分隔。生产环境请显式填写前端域名。 |
| `SENSEVOICE_MAX_AUDIO_BYTES` | 20 MiB | 单轮累计音频的最大字节数。 |
| `SENSEVOICE_MAX_UTTERANCE_SECONDS` | 30 | 单轮语音最长秒数；PCM 实时流超过该值会立即丢弃并重置当前话轮。 |
| `SENSEVOICE_MAX_HISTORY_TURNS` | `10` | 每个完整对话 WebSocket 会话保留的最大历史轮数。 |
| `SENSEVOICE_SKIP_STARTUP_WARMUP` | `0` | 仅在受控调试时跳过启动预热；生产应保持 `0`。 |
| `SENSEVOICE_STREAM_QUEUE_SIZE` | `8` | LLM/TTS 阻塞流的有界缓冲条目数；过小会增加背压，过大增加内存和取消滞后。 |
| `SENSEVOICE_TTS_QUEUE_SIZE` | `4` | 完整对话中等待 GPT-SoVITS 的短句数上限；队列满时对 LLM 施加背压，避免长回复占满内存。 |
| `SENSEVOICE_TTS_QUEUE_MAX_SECONDS` | `30` | 完整对话中等待或正在合成的短句总时长预算（按文本长度估算）；用于背压，不是 GPT-SoVITS 实际音频时长。 |
| `SENSEVOICE_TTS_ESTIMATED_CHARS_PER_SECOND` | `12` | 文本时长估算速度（字符/秒）；应按目标语言、音色和语速实测后调整。 |
| `SENSEVOICE_MAX_VOICEPRINT_AUDIO_BYTES` | 20 MiB | 单段声纹登记音频的最大读取字节数；超限样本会被拒绝。 |
| `SENSEVOICE_PUNC_MODEL` | `iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch` | FunASR 句段标点与 CAM++ 分段使用的模型；置空可在受控环境禁用。 |
| `SENSEVOICE_VAD_CHUNK_MS` | `60` | FSMN-VAD 在线推理的分帧大小（20–200 ms）；越小延迟越低但调度开销越高。 |
| `GPT_SOVITS_TEXT_LANG` | `auto` | 固定 TTS 语言；`auto` 时跟随 ASR 语言映射。 |
| `GPT_SOVITS_STREAMING_MODE` | `3` | GPT-SoVITS v2 流式模式：`3` 优先低延迟，`2` 更均衡，`1` 优先质量。 |
| `LLM_TIMEOUT_SECONDS` / `GPT_SOVITS_TIMEOUT_SECONDS` | `90` | 上游请求超时；应结合目标网络和延迟验收调整。 |
| `GPT_SOVITS_SPEED_FACTOR` | `1.0` | GPT-SoVITS 输出语速；音色验收时需固定记录。 |
| `GPT_SOVITS_DEFAULT_VOICE_ID` / `GPT_SOVITS_DEFAULT_VOICE_NAME` | `assistant_default` / 默认助手音色 | 默认授权音色的 ID 和显示名。 |
| `SENSEVOICE_VOICE_PROFILE_FILE` | 空 | 可选的服务端音色 JSON；每个 profile 可自带参考音频、逐字稿和语言，且只由管理员维护。 |
| `SENSEVOICE_VOICEPRINT_PATH` | `/data/voice-stack/data/voiceprints` | 按租户隔离的加密声纹文件目录。 |
| `SENSEVOICE_MODEL_REVISION` / `SENSEVOICE_VAD_MODEL_REVISION` / `SENSEVOICE_PUNC_MODEL_REVISION` / `SENSEVOICE_SPK_MODEL_REVISION` | `master` | 生产环境应替换为验收后冻结的模型 revision；标点模型用于完整句段时间线和说话人分段。 |
| `SENSEVOICE_VOICEPRINT_THRESHOLD` / `SENSEVOICE_VOICEPRINT_MARGIN` | `0.72` / `0.04` | 仅为起始值；正式阈值必须用登记集之外的验证录音确定。 |

不要把真实值写入仓库或提交到 Git。Docker Compose 会透传当前 shell 环境变量；也可由你的
部署平台通过 Secret 注入。

## 启动

GPT-SoVITS 必须先以 v2 API 模式启动，并且服务端能读取默认音色路径，或
`SENSEVOICE_VOICE_PROFILE_FILE` 中启用音色的 `ref_audio_path`。
然后在 SenseVoice 项目中启动：

```bash
export SENSEVOICE_API_KEY='请替换为部署平台注入的密钥'
export LLM_BASE_URL='https://llm.example.com/v1'
export LLM_MODEL='your-model'
export LLM_API_KEY='请替换为部署平台注入的密钥'
export GPT_SOVITS_BASE_URL='http://gpt-sovits:9880'
export GPT_SOVITS_REF_AUDIO_PATH='/data/voices/assistant.wav'
export GPT_SOVITS_PROMPT_LANG='zh'

uvicorn realtime_api:app --host localhost --port 50000
```

如果已有 GPU 容器环境，可用 `docker compose up --build` 启动。请将以上变量以部署平台的
Secret 或当前终端环境注入，勿创建或提交包含真实凭据的配置文件。Docker 容器内为便于端口映射，
由 [Dockerfile](./Dockerfile) 使用 `0.0.0.0` 监听；WSL/前台试运行保持 `localhost`。

## WebSocket 协议

连接地址：`ws(s)://<host>/api/v1/realtime`。服务端 SDK、移动端或命令行客户端可在请求头中
携带 `X-API-Key: <SENSEVOICE_API_KEY>`；即使直接运行兼容入口 `api:app`，HTTP 接口也会执行同一鉴权。浏览器原生 WebSocket 不能自定义请求头，应改为：

```javascript
new WebSocket("wss://<host>/api/v1/realtime", ["sensevoice", apiKey]);
```

服务端会选择 `sensevoice` 子协议，第二个子协议值用于鉴权；请使用符合 HTTP 子协议字符规则的
API Key。

1. 连接成功后服务端发送 `session.ready`。
2. 可选发送配置事件：

```json
{"type":"configure","language":"auto","tts_text_lang":"auto","tts_output_format":"pcm_s16le","streaming_mode":3,"segment_events":true,"voice_id":"assistant_default"}
```

3. 新客户端如需边收边播，在 `configure` 中增加 `tts_output_format:"pcm_s16le"`；旧客户端
   不传时保持 WAV 兼容。发送一个完整的 WAV、MP3 或其他 SoundFile 可解码音频的二进制帧；分帧传输时按原始
文件顺序连续发送即可。
4. 发送提交事件：`{"type":"commit"}`。
5. 服务端依次回传：`asr.processing`、带 `segments`/`turn_id`/`revision` 的 `asr.final`，可选的
`speaker.update`，多个 `llm.delta`、`tts.start`、若干
二进制音频帧、`tts.end` 与 `turn.completed`。生成期间发送 `{"type":"cancel"}` 会取消当前话轮并返回
`turn.cancelled`；客户端必须丢弃该话轮尚未播放的音频。

`tts_output_format=wav` 时，网关会先解析上游流并重新封装真实长度的 WAV；每个 `tts.start` 至
对应 `tts.end` 之间的二进制帧按顺序拼接即可得到独立音频段，但要等当前句段合成完成才发送。
设置为 `pcm_s16le` 时应按 `tts.start` 的采样率、声道和位深直接送入播放队列，才能边收边播。
客户端可以收到首段后立即播放；一轮回复可能按句子产生多个音频段。发送
`{"type":"clear_history"}` 可清空当前 WebSocket 会话的上下文。`/api/v1/asr/stream` 默认只在
收到 `commit` 时收口。新版隔离网关可在 `configure` 中显式设置 `auto_vad:true`，由独立
FSMN-VAD 识别语音结束并自动提交；默认仍关闭，生产开放前继续按目标录音验收。在线 `50000`
旧兼容接口保持手动 `commit`。完整对话接口继续使用完整音频与手动 `commit`。

### 时间线与声纹

`segments` 只包含 FunASR 返回的句段边界，单位为毫秒；没有可信边界时返回空数组，不按文本长度
伪造时间。CAM++ 的局部 `spk` 会映射为会话内 `speaker_0` 等匿名编号，跨会话不复用。可信 BFF
必须同时注入 `X-Voiceprint-Tenant` 和 `X-Voiceprint-Session-Key`，服务端才会在有足够语音时尝试登记库匹配，并通过 `speaker.update`
发送 `identified`、`unknown` 或 `ambiguous` 状态；低于阈值为 `unknown`，候选分差不足为 `ambiguous`；`match_score` 是相似度分数而非概率。

声纹管理接口要求 API Key、独立的 `X-Voiceprint-Admin-Key` 和 `X-Voiceprint-Tenant`：

```text
GET    /api/v1/voiceprints
POST   /api/v1/voiceprints   (files、person_id、authorized；建议 3–5 段，最多 5 段)
DELETE /api/v1/voiceprints/{person_id}

# 与管理端规划契约等价的别名
GET    /api/v1/speakers
POST   /api/v1/speakers/enroll   (files、speaker_profile_id、authorized)
DELETE /api/v1/speakers/{speaker_profile_id}
```

原始向量不会返回给客户端。`X-Voiceprint-Tenant` 单独出现时会被忽略；只有会话密钥校验通过，
网关才会读取对应租户。验证版默认不配置声纹管理/加密/会话密钥，也不使用真实人员库。生产开放需先通过方案门禁。

### 开发回归接口

管理端回归台通过服务端 BFF 调用以下两个接口，浏览器不直接接触 `SENSEVOICE_API_KEY`：

- `WS /api/v1/asr/stream`：接收 `16 kHz / 16 bit / mono` PCM 二进制分帧（每帧必须按 16 bit 样本对齐）。连接后发送
  `configure`（可选）和 `commit`，服务端返回 `asr.partial`、`asr.final`、首字延迟与收口延迟。
  该接口通过短滑窗重复 SenseVoice 推理提供准实时 partial，不代表模型原生增量推理。禁止直接发送
  WAV/MP3 等压缩文件；网关会返回 `invalid_audio_format`，文件应改用离线 ASR 或完整对话接口。
- `POST /api/v1/dev/tts`：请求 JSON `{"text":"...","text_lang":"zh","streaming_mode":3}`，
  返回完整、可回读的 WAV（为兼容旧质量台，内部固定使用非流式请求；`streaming_mode` 仅保留旧调用方字段）。响应头
  `X-TTS-First-Audio-Ms`、`X-TTS-Total-Ms` 和 `X-TTS-Duration-Ms` 用于质量台记录首个有效 PCM 样本、总耗时和音频时长；WAV 头不计入首音频延迟。
  `voice_id` 可选，`zh`、`yue`、`en` 会分别映射为 GPT-SoVITS 的 `all_zh`、`all_yue`、`en`。
- `GET /api/v1/tts/voices`：返回已授权音色 ID、名称、语言、版本和状态，不返回参考音频路径。
- 音色配置中的 `languages` 同时接受业务语言名（如 `zh`、`yue`）和 GPT-SoVITS 语言码（如 `all_zh`、`all_yue`）；网关会统一映射后再请求上游。
- `WS /api/v1/tts/stream`：发送 `configure` 和 `synthesize` 文本事件，收到 `tts.start` 后立即
  消费 PCM16 二进制帧，`tts.end` 表示生成结束；支持同一 `request_id` 的 `cancel`。该接口是真正
  的实时播放入口，不要将 PCM16 帧拼成 WAV Blob 后才开始播放。

这两个接口与其他 `/api/v1/*` 接口一样要求 `X-API-Key`，只建议在开发网段开放。

如果管理端接入的不是本服务，可使用 `custom-v1` 适配器：离线 ASR 使用 multipart 字段
`audio`、`language`、`use_itn`，返回 `{"text":"..."}` 或 `{"data":{"text":"..."}}`；
实时 ASR 和完整对话沿用本节的 JSON 事件与二进制音频约定；TTS 接收
`{"text":"...","text_lang":"..."}` 并返回 WAV（也可返回 `audio_base64` JSON）。

## 运行状态

无需认证即可访问 `GET /api/v1/health`，它只返回整体就绪状态、ASR/TTS 分项就绪状态、声纹
存储/加密状态与缺失配置名，不返回密钥、服务地址或参考音频路径；若音色文件不可读，会在
`tts_missing_settings` 中明确报告。`/api/v1/asr` 也必须使用 `X-API-Key` 请求头。
其中 `speaker_identification_mode=anonymous` 只表示会话内匿名说话人编号；只有声纹管理、加密、
BFF 会话密钥和引擎的 `speaker_identification_ready` 均就绪时才会返回 `persistent`，此时也仍需
通过多人质量验收后才能对外宣称记名识别。

质量评测使用 [`speech/metrics.py`](./speech/metrics.py) 的统一 CER/WER 口径：默认移除 SenseVoice
控制标签、标点/符号/空白，并进行 NFKC 与大小写归一化；数字不自动改写。服务对外仍保留
`raw_text`，不要用归一化文本替代审计原文。`character_error_counts` 与
`word_error_counts` 可进一步拆分替换、漏字和插字；需要把标点差异计入质量数据时使用
`strict_character_error_rate` / `strict_character_error_counts`（保留标点和符号、忽略空白），
两种 CER 分栏记录，不混成一个数。时间线验收可使用
`timeline_boundary_summary`，它会按时间排序计算起止边界 MAE/P95，并单独报告缺失和多余句段。
声纹阈值验收可使用 `voiceprint_error_summary`，分别报告 FRR、FAR 和开放集误接受率；测试集为空时
返回 `None`，不能按零误差记录。
`latency_summary` 可对首字幕、收口、身份确认和 TTS 首音频样本统一输出 P50/P95；空样本和
非法时钟值不会被当成零延迟。

## Windows RTX 5090 验证结果

以下是仓库保留的旧版 `windows-lan` Windows 11 + WSL2 + RTX 5090（32 GiB）热机记录，
本次没有重新执行；不能用于证明新时间线、CAM++ 或声纹改造已通过验收：

| 环节 | 结果 |
|---|---|
| 中文 ASR | `zh.mp3` 正确识别为“开饭时间早上9点至下午五点。” |
| 英语 ASR | `en.mp3` 可识别英语内容，热机推理约 0.31 秒 |
| 粤语 ASR | `yue.mp3` 可识别粤语内容，热机推理约 0.26 秒 |
| GPT-SoVITS 中文 | 热机请求约 2.32 秒，模型日志音频首包约 0.20 秒 |
| GPT-SoVITS 英文 | 请求约 0.61 秒，HTTP 首字节约 0.001 秒 |
| GPT-SoVITS 粤语 | 请求约 1.26 秒，HTTP 首字节约 0.001 秒 |
| WebSocket 全链路（历史开发基线，已废弃） | ASR → 旧 OpenAI-compatible SSE → GPT-SoVITS，首轮总耗时约 1.83 秒；仅用于早期适配调试，不是当前部署路径或验收证据 |
| WebSocket 全链路（HKGAI） | ASR → HKGAI OpenAI-compatible SSE → GPT-SoVITS 已成功；中文测试生成 477,318 字节 WAV。 |

测试输出保留在远端 `/data/voice-stack/bench/results/`，包括 `tts-smoke-2.wav`、
`tts-en.wav` 与 `tts-yue.wav`。HTTP 首字节只表示响应头到达时间，不能直接当作音频首包
播放时间。这些数据是单机热机参考值，真实 LLM 网络、回答长度、参考音频和客户端播放缓冲
会改变端到端延迟；上线前应使用目标 LLM 和目标说话人音频复测。

## 当前部署状态

时间线、CAM++ 和声纹改造仍为初版。2026-09-04 已在 Windows/WSL 隔离端口 `50001` 完成模型加载、
短静音预热、HTTP/WS 冒烟和三份示例音频直调；2026-09-10 已同步候选版本并完成中文、英语、粤语
HTTP/WS ASR、实时 TTS 和 HKGAI 全链路冒烟。这些证明调用链可运行，不等于质量、跨重启声纹或生产
端到端验收。`warmup()` 会执行一次短静音推理，但没有覆盖每项能力的真实质量探测；健康字段仍不能
单独作为时间线、说话人或人员识别可用的证据。`50001` 使用 transient systemd user unit 手动运行，
WSL 重启后不会恢复；在线 `50000` 保持旧版不变。

候选版本已配置 `punc_model`，实时链路关闭 SenseVoice ITN 以避免重复标点；FunASR 仍可能提示
SenseVoice 没有逐 token timestamp，因此当前只承诺 VAD/句段级边界，不按文本长度生成伪时间。
多人分段和目标机 `spk` 与中心向量映射仍需使用获准的验证录音完成，声纹阈值也必须独立校准，不能用三份单人示例替代。

验证机已启用 `gpt-sovits.service` 与 `sensevoice.service`。统一网关监听 `50000`，GPT-SoVITS
仅监听 WSL 回环地址 `9880`，不会直接对外暴露。Windows 主机上的 PC Web 使用
`http://localhost:50000`；Mac 先执行 `ssh -N -L localhost:50000:localhost:50000 windows-lan`，
再使用同一地址。已查到 Windows 的 50000 转发规则和监听，但 Mac 的 LAN 直连仍失败，实际链路待排查；具体调用方式见
[语音对话服务调用文档](./docs/api-call-guide.zh-CN.md)。

当前参考音频是 `/data/voice-stack/voices/assistant-test-zh.mp3`，仅为端到端联调使用的样例；隔离
`50001` 已用 `assistant_default` 完成三语 TTS PCM 流冒烟，在线 `50000` 尚未切换新网关代码。
正式服务前必须换成已获得授权的目标说话人参考音频，并同步更新其文字稿和
`GPT_SOVITS_PROMPT_LANG`。

## 接入限制

- 当前版本默认是“实时分轮”服务：客户端可在录音停止时发送 `commit`；新版 `50001` 也支持显式
  `auto_vad:true`，由 FSMN-VAD 自动收口，旧版 `50000` 不提供该能力。
- SenseVoice 的模型实例被串行化，优先稳定性；高并发时请横向扩容服务实例，并让每个实例绑定可用 GPU。
- GPT-SoVITS 使用官方 v2 `/tts` 协议；参考音频路径是 TTS 服务器本地路径，不能由外部客户端覆盖。

## Windows + WSL `/data` 部署

本项目沿用 WSL2，不在 Mac 或 Docker Desktop 新建模型环境。完整命令以
[Windows / WSL 模型下载与隔离部署](./docs/windows-wsl-deployment.zh-CN.md)为准，包含 Windows
物理磁盘检查、源码白名单传输、隔离依赖、四模型下载与 SHA-256、localhost 试运行、验收和回退。

旧版 CUDA 轮子记录为 `torch==2.11.0+cu128` 与 `torchaudio==2.11.0+cu128`；新 FunASR
组合仍需实测，不在现有在线虚拟环境直接升级。目录分工如下：

```text
/data/voice-stack/
├── apps/           # sensevoice、sensevoice-next 与 gpt-sovits 源码
├── environments/   # 在线与验证版分开的 Python 环境
├── models/         # 在线模型缓存；funasr/<批次>/ 保存新版冻结快照
├── voices/         # 参考音频
├── config/         # 本机配置（不提交真实密钥）
├── logs/           # 服务日志
├── cache/          # 安装缓存
├── work/           # 本次临时音频、校验与验证结果
└── bench/          # 历史质量与延迟结果
```

FunASR 验证版使用 Windows 局域网转发的 `http://192.168.101.75:50001`，本次旧版热修复沿用
`50000`。Windows `portproxy` 指向当前 WSL 地址 `172.31.30.214`，防火墙仅允许 Private/LocalSubnet；
WSL 或 DHCP 地址变化后需重新核对转发，不照抄历史 WSL IP。SSH 隧道仍可备用。

`deploy/config/sensevoice.env.example` 和 `deploy/systemd/` 是参考模板，不是自动切换命令。
真实配置文件和部署参数修改须先明确路径、内容与影响并取得确认；不得用模板覆盖
`/data/voice-stack/config/sensevoice.env`。新能力通过验收后再安排正式切换与回退窗口。

源码同步并启动后，可在 WSL 运行只读诊断：

```bash
bash deploy/scripts/diagnose.sh
```

该脚本检查服务状态、50000/50001/9880 端口、依赖、配置与声纹目录权限、健康能力 JSON、
候选源码逐文件清单和关键源码哈希；不读取或打印密钥，不替代真实录音、多人声纹和端到端延迟验收。
