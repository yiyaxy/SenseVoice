# 语音对话服务调用文档

本文面向接入方。你的项目只需要调用 SenseVoice 实时网关；不应直接暴露或调用内部的
HKGAI LLM、GPT-SoVITS 服务。

模型下载和服务部署见 [Windows / WSL 部署指南](./windows-wsl-deployment.zh-CN.md)：模型只在
Windows/WSL 下载，本地项目保留代码与文档。`50000` 已部署旧版兼容的缓冲式实时 ASR；下文
新增的时间线/声纹字段属于 FunASR 待验收初版，不代表现有 50000 已部署这些能力。

```text
你的项目 → SenseVoice 网关 → ASR → HKGAI LLM → GPT-SoVITS → 语音输出
```

## 服务地址与鉴权

当前已核实的访问方式如下。Windows 主机上的 PC Web 使用回环地址；同一局域网可直接访问新版
`50001`。旧版 `50000` 仍保留兼容入口。

```text
# Windows 主机 / PC Web（旧版兼容入口）
HTTP_BASE=http://localhost:50000
WS_BASE=ws://localhost:50000
# 同一局域网直接访问新版隔离入口
HTTP_BASE=http://192.168.101.75:50001
WS_BASE=ws://192.168.101.75:50001
# 也可使用 SSH 隧道访问新版隔离入口
# ssh -N -o ExitOnForwardFailure=yes -L localhost:50001:localhost:50001 windows-lan
# HTTP_BASE=http://localhost:50001
# WS_BASE=ws://localhost:50001
```

当前新网关（含实时 TTS、复刻音色和 FunASR 适配）已同步到 Windows/WSL 隔离端口 `50001`
并完成候选冒烟，未切换在线 `50000`。`50001` 为手动启动的隔离服务；WSL 重启后先手动启动服务。

`50001` 不安装开机 unit，当前通过 Windows 局域网地址 `192.168.101.75:50001` 提供联调，
不能把隔离端口的验证结果当作生产切换或多人声纹质量验收。

新版网关地址为 `http://192.168.101.75:50001` / `ws://192.168.101.75:50001`。Windows
已将局域网端口转发到 WSL `50001`，防火墙仅允许 Private/LocalSubnet；若 DHCP 改变 Windows
地址，需要同步更新调用端地址。SSH 隧道仍可作为备用访问方式；`50000` 仍是旧版兼容服务，不能从中调用下文新增的 TTS 流接口。

如需从其他机器直接访问，需先诊断既有 Windows 入站和 WSL 转发链路；规则变更另行确认，不盲目新增重复规则。实时 TTS 与复刻音色使用下方独立流式接口；播放器仍需由业务 BFF 接收 PCM16 并边收边播，完整对话接口继续兼容旧客户端。

除健康检查外，HTTP 接口使用请求头鉴权；直接运行兼容入口 `api:app` 时也同样生效：

```text
X-API-Key: <SENSEVOICE_API_KEY>
```

浏览器原生 WebSocket 不能自行设置请求头，需把 API Key 作为第二个子协议传入：

```javascript
new WebSocket("ws://localhost:50001/api/v1/realtime", ["sensevoice", apiKey]);
```

API Key 应使用 URL-safe 字符，例如随机生成的十六进制字符串或不带 `=` 填充的 Base64URL。
不要把 HKGAI Key、GPT-SoVITS 参考音频路径或任何内部地址交给客户端。

## 1.1 可用 TTS 音色

```bash
curl -sS http://localhost:50001/api/v1/tts/voices \
  -H "X-API-Key: <SENSEVOICE_API_KEY>"
```

响应只包含 `voice_id`、显示名称、支持语言、版本和状态，不返回参考音频路径或参考稿。
客户端只能传服务端已登记的 `voice_id`；首个默认 ID 通常为 `assistant_default`。

## 1. 健康检查

```bash
# 新版 FunASR 候选网关（时间线、自动 VAD、TTS）：在 WSL/Windows 本机，
# 或先建立 ssh -N -L localhost:50001:localhost:50001 windows-lan 后从 Mac 执行
curl -sS http://localhost:50001/api/v1/health
# 旧版兼容网关仍在 50000
curl -sS http://<Windows-LAN-IP>:50000/api/v1/health
```

字段格式示例（不是目标机实测结果）：

```json
{
  "status": "ready",
  "asr_languages": ["zh", "en", "yue", "ja", "ko"],
  "build_version": "development",
  "model_version": "master",
  "timestamps_ready": true,
  "punc_model_version": "master",
  "spk_model_id": "cam++",
  "spk_model_version": "master",
  "diarization_ready": true,
  "speaker_identification_ready": true,
  "speaker_identification_mode": "persistent",
  "voiceprint_management_ready": true,
  "voiceprint_session_ready": true,
  "voiceprint_storage_ready": true,
  "voiceprint_encryption_ready": true,
  "dialogue_ready": true,
  "authentication_ready": true,
  "missing_settings": []
}
```

`status=configuration_required` 表示 ASR 进程已运行，但 HKGAI、GPT-SoVITS 或网关 API Key
尚未配齐；`tts_missing_settings` 还会报告授权音色或参考音频文件不可用。音色 JSON 中的启用
profile 可以自带参考音频、逐字稿和参考语言，此时不必再设置默认音色的
`GPT_SOVITS_REF_AUDIO_PATH` / `GPT_SOVITS_PROMPT_LANG`。健康检查不会返回任何密钥或内部服务地址。
LLM 地址可配置 `LLM_BASE_URL`，也可只配置完整的 `LLM_CHAT_COMPLETIONS_URL`，两者不必同时填写。

当前能力 ready 字段包含一次短静音预热探测，但尚未逐项执行真实质量探测；不能只凭它们判断
时间线、说话人或人员识别已经可用，必须通过部署指南中的实际验收。

`speaker_identification_mode` 明确当前身份能力：`anonymous` 表示只能在会话内输出匿名说话人
编号，`persistent` 才表示已配置加密声纹库、管理权限、BFF 会话密钥且引擎报告
`speaker_identification_ready=true`，可以匹配已登记人员。BFF
必须同时发送 `X-Voiceprint-Tenant` 与 `X-Voiceprint-Session-Key`；只发送租户头会安全降级为匿名。两者都不等同于
“已经通过多人质量验收”。`voiceprint_storage_ready` 只有在现有声纹目录 owner-only（通常为 `0700`）且服务账户可写时才会为 `true`；首次登记创建目录时服务端会尝试自动收紧权限。`WS /api/v1/asr/stream` 只接受 16 kHz、单声道、16 bit 小端 PCM；
WAV/MP3 等文件必须先解码，文件格式应改用离线 `/api/v1/asr` 或完整对话接口提交。

## 2. 独立 ASR 接口

适用于只需要语音转文字的场景。完整实时对话请使用下一节 WebSocket。

```bash
# 在 WSL/Windows 本机，或经 SSH 隧道从 Mac 调用候选端口
curl -sS -X POST "http://localhost:50001/api/v1/asr" \
  -H "X-API-Key: <SENSEVOICE_API_KEY>" \
  -F "files=@./speech.wav" \
  -F "lang=auto" \
  -F "use_itn=true"
```

参数：

| 字段 | 必填 | 说明 |
|---|---|---|
| `files` | 是 | 音频文件字段；支持 WAV、MP3 及 SoundFile 可解码格式。建议 16 kHz 单声道 WAV；同一请求累计受 `SENSEVOICE_MAX_AUDIO_BYTES` 限制。 |
| `lang` | 否 | `auto`、`zh`、`en`、`yue`、`ja`、`ko`。默认 `auto`。 |
| `keys` | 否 | 多文件请求时的名称，以英文逗号分隔；服务端会去除首尾空白，名称不能为空。 |
| `use_itn` | 否 | 是否做逆文本规范化；默认 `false`。 |
| `segments` | 否 | 设为 `true` 时追加句段级时间线；时间单位为毫秒，无可信边界时为空数组。 |

响应示例：

```json
{
  "result": [
    {
      "key": "speech.wav",
      "text": "你好，今天有什么可以帮你？",
      "clean_text": "你好，今天有什么可以帮你？",
      "raw_text": "<|zh|><|NEUTRAL|><|Speech|><|woitn|>你好，今天有什么可以帮你？"
    }
  ]
}
```

## 3. 实时语音对话 WebSocket

地址：

```text
ws://localhost:50001/api/v1/realtime
```

这里使用已接入 HKGAI、GPT-SoVITS 复刻音色和 PCM TTS 流的新版隔离网关；在线兼容入口
`50000` 仍保留旧版分轮协议。

一轮对话的时序：

```text
客户端                网关
  │  二进制音频帧       │
  │───────────────────>│
  │  {"type":"commit"} │
  │───────────────────>│
  │<───────────────────│  asr.processing
  │<───────────────────│  asr.final
  │<───────────────────│  多个 llm.delta
  │<───────────────────│  tts.start → WAV/PCM 二进制帧 → tts.end
  │<───────────────────│  turn.completed
```

### 客户端发送事件

| 类型 | JSON | 说明 |
|---|---|---|
| 配置 | `{"type":"configure","language":"auto","tts_text_lang":"auto","tts_output_format":"pcm_s16le","streaming_mode":3,"segment_events":true,"voice_id":"assistant_default"}` | 可选。ASR 语言可为 `auto`、`zh`、`en`、`yue`、`ja`、`ko`；TTS 语言可用相同业务语言名或 GPT-SoVITS 语言码（如 `all_yue`）；`voice_id` 必须来自 `session.ready` 的授权列表；`tts_output_format` 可选 `wav`（旧客户端）或 `pcm_s16le`（边收边播）；`streaming_mode` 可选 `1/2/3`，会透传 GPT-SoVITS；`segment_events=true` 时额外发送独立句段事件；完整对话仍需手动 `commit`。 |
| 音频 | 二进制帧 | 发送一个完整音频文件，或按原始字节顺序分帧发送。 |
| 提交 | `{"type":"commit"}` | 表示一句话结束，触发 ASR → LLM → TTS。 |
| 取消 | `{"type":"cancel"}` | 取消当前完整对话话轮；服务端返回 `turn.cancelled`，客户端应丢弃该话轮尚未播放的音频。 |
| 清空上下文 | `{"type":"clear_history"}` | 清空当前 WebSocket 会话的对话历史。 |
| 音频缺口 | `{"type":"audio.gap","duration_ms":500}` | 仅在没有活动话轮且当前音频缓冲为空时使用；服务端保留时间偏移。 |
| 心跳 | `{"type":"ping"}` | 服务端返回 `pong`。 |

单轮音频默认限制为 20 MiB、30 秒。当前服务是“实时分轮”模式：录音停止、客户端 VAD
检测到停顿，或用户点击发送后，客户端可以发送 `commit`。新版隔离网关也支持显式
`auto_vad:true`，由 FSMN-VAD 自动收口；默认仍关闭，生产上线前需用目标录音继续验收。
连续 PCM 期间如发生网络断片，可在清空当前缓冲后发送
`{"type":"audio.gap","duration_ms":500}`；服务端会保留该缺口的时间偏移并重置 VAD 缓存，后续句段时间不会回退。

### 旧版兼容实时 ASR

在线 `50000` 的旧版 `/api/v1/asr/stream` 接收 16 kHz、单声道、16 bit little-endian PCM 二进制帧，
约每累计 800 ms 返回一次 `asr.partial`；客户端必须发送 `{"type":"commit"}` 才会返回
`asr.final` 和 `asr.completed`。此接口只做 ASR，不调用 LLM/TTS；暂不支持 `auto_vad`、时间线
或声纹字段。新版 `50001` 的同一路由可通过 `auto_vad:true` 启用 FSMN-VAD；需要完整
ASR → HKGAI → GPT-SoVITS 对话时，请使用 `/api/v1/realtime`。

## 3. 独立实时 TTS（复刻音色）

地址：

```text
ws://localhost:50001/api/v1/tts/stream
```

连接后先接收 `session.ready`，再发送配置和文本：

```javascript
const apiKey = "<SENSEVOICE_API_KEY>";
const ws = new WebSocket(
  "ws://localhost:50001/api/v1/tts/stream",
  ["sensevoice", apiKey],
);
ws.binaryType = "arraybuffer";
ws.onopen = () => {
  ws.send(JSON.stringify({
    type: "configure",
    voice_id: "assistant_default",
    text_lang: "zh",       // 也可使用 en、yue
    streaming_mode: 2,
  }));
  ws.send(JSON.stringify({ type: "synthesize", request_id: "r-1", text: "你好，欢迎使用。" }));
};
```

事件顺序为 `tts.start` → 多个 PCM16 二进制帧 → `tts.end`。`tts.start` 给出实际
`sample_rate`、`channels=1`、`sample_width=2` 和 `voice_id`；二进制帧不含 WAV 头，
应直接交给 AudioWorklet 或其他 PCM 播放队列。`tts.end` 的 `first_audio_ms` 是服务端收到
首个有效 PCM 样本的耗时，不包含 WAV 头；`audio_duration_ms` 按实际输出 PCM 字节、采样率、
声道和位深计算，不是文本时长估算。

完整对话选择 `tts_output_format=wav` 时，网关会先解析上游流并重新封装真实长度的 WAV，保证
回读兼容；这一路径要等当前句段合成完才发送音频。需要边收边播时应选择
`tts_output_format=pcm_s16le`。

发送 `{"type":"cancel","request_id":"r-1"}` 可停止当前请求；服务端会返回
`tts.cancelled`，客户端应立即丢弃该请求尚未播放的缓冲。一次连接同一时间只允许一个合成请求，
避免 GPT-SoVITS 共享实例串音。

`streaming_mode=1` 作为质量基线，`2` 作为均衡选项，`3` 作为低延迟选项；三种模式必须在目标
音色和语言上实测后再固定，参数不代表已经通过音色验收。参考音频、参考稿和上游地址始终只在服务端配置。

## 4. 服务端事件

| 类型 | 含义 |
|---|---|
| `session.ready` | 连接、鉴权、LLM 与 TTS 配置均已就绪。 |
| `session.configured` | 已接受本轮会话语言设置。 |
| `asr.processing` | 已开始转写。 |
| `asr.final` | 最终转写，含 `text`、`language`、`duration_seconds`、可选 `segments`、`turn_id` 和递增 `revision`。 |
| `asr.segment.partial` / `asr.segment.final` | 仅在 `configure.segment_events=true` 时发送；每个事件包含句段时间、文本、语言和规范化的 `speaker` 对象。partial 可被后续事件修订，final 不被旧 partial 覆盖。 |
| `speaker.update` | 句段身份补充或修订；含 `segment_id`、`speaker_status`（`identified`、`unknown` 或 `ambiguous`）和 `match_score`。 |
| `asr.no_speech` | 未识别到有效语音；该音频段已消费，实时 ASR 随后会发送 `asr.completed`，完整对话不会调用 LLM/TTS，客户端可继续提交下一段。 |
| `llm.delta` | HKGAI 流式文本片段，字段为 `delta`。 |
| `tts.start` | 一个 TTS 语音段开始，含 `segment_index`、`format`、`text`、`language`；PCM 模式还含实际采样率、声道和位深。 |
| 二进制帧 | 当前 TTS 段的 WAV 或 PCM16 字节，格式由 `tts.start.format` 决定。 |
| `tts.end` | 当前 TTS 语音段结束；包含 `audio_duration_ms`（实际 PCM 时长）以及服务端生成耗时字段。 |
| `turn.completed` | 本轮完成，含完整 `text` 与 `tts_segments`。 |
| `turn.cancelled` | 当前完整对话话轮已取消；有活动话轮时含 `turn_id` 和递增 `revision`，无活动话轮时仅表示当前没有可取消任务。可能已产生的 PCM/WAV 音频必须从客户端播放队列丢弃。 |
| `error` / `tts.error` | 可恢复或不可恢复错误，字段 `code` 表示原因。 |

当 `tts_output_format=wav` 时，`tts.start` 到对应 `tts.end` 之间的所有二进制帧按顺序拼接后，
就是一个完整 WAV 文件；当设置为 `pcm_s16le` 时，二进制帧不含 WAV 头，按 `tts.start` 的
采样率、声道和位深直接送入 PCM 播放队列。一轮 LLM 回复可按句子产生多个 TTS 段。

### 浏览器最小示例

```javascript
const apiKey = "<SENSEVOICE_API_KEY>";
const ws = new WebSocket(
  "ws://localhost:50001/api/v1/realtime",
  ["sensevoice", apiKey],
);
ws.binaryType = "arraybuffer";

let currentAudio = null;

ws.addEventListener("open", () => {
  ws.send(JSON.stringify({ type: "configure", language: "auto", tts_text_lang: "auto" }));
});

ws.addEventListener("message", ({ data }) => {
  if (typeof data !== "string") {
    if (currentAudio) currentAudio.chunks.push(data);
    return;
  }

  const event = JSON.parse(data);
  switch (event.type) {
    case "asr.final":
      console.log("识别文本：", event.text, "语言：", event.language);
      break;
    case "llm.delta":
      console.log("LLM 增量：", event.delta);
      break;
    case "tts.start":
      currentAudio = { index: event.segment_index, chunks: [] };
      break;
    case "tts.end": {
      if (!currentAudio || currentAudio.index !== event.segment_index) break;
      const wav = new Blob(currentAudio.chunks, { type: "audio/wav" });
      const audio = new Audio(URL.createObjectURL(wav));
      void audio.play();
      currentAudio = null;
      break;
    }
    case "error":
    case "tts.error":
      console.error("服务错误：", event);
      break;
  }
});

async function submitAudioFile(file) {
  if (ws.readyState !== WebSocket.OPEN) throw new Error("WebSocket 未连接");
  ws.send(await file.arrayBuffer());
  ws.send(JSON.stringify({ type: "commit" }));
}
```

示例中接收完整 WAV 后播放，适合先完成接入。要验证真正的边收边播，可直接打开仓库内的
[实时 TTS 播放器](./realtime-tts-player.html)：它在 `tts.start` 后建立 AudioWorklet，收到
首个 PCM16 帧即入播放队列，并在 AudioWorklet 输出首个样本时单独记录客户端首播延迟，同时处理
GPT-SoVITS 实际采样率与浏览器设备采样率不同的情况。该页
只适合 Windows/WSL 联调，生产环境应由业务 BFF 代理 WebSocket，不把服务 Key 长期放在浏览器。
不要把不同 `request_id` 或 `segment_index` 的字节混入同一个播放队列。

## 5. 语言行为

### 时间线与声纹

句段 `start_ms`/`end_ms` 使用会话音频起点作为零点，静音不会压缩时间；`asr.partial.segments`
与 `asr.segment.partial` 也使用同一绝对时间基准。CAM++ 的局部说话人标签
只在一次推理内有效，服务端会映射为当前会话的匿名 `speaker_0` 等编号；跨会话不会复用。可信
BFF 必须同时发送 `X-Voiceprint-Tenant` 和 `X-Voiceprint-Session-Key` 才能启用已登记人员匹配，身份状态可能先为 `pending`，随后
以更高 `revision` 的 `speaker.update` 修订；低于阈值为 `unknown`，候选分差不足为 `ambiguous`。相似度 `match_score` 不是概率。

声纹管理需要额外的 `X-Voiceprint-Admin-Key` 和 `X-Voiceprint-Tenant`，并且服务端配置独立加密密钥：

```text
GET    /api/v1/voiceprints
POST   /api/v1/voiceprints   (files、person_id、authorized=true；建议 3–5 段，最多 5 段；每段受 `SENSEVOICE_MAX_VOICEPRINT_AUDIO_BYTES` 限制)
DELETE /api/v1/voiceprints/{person_id}

# 与管理端规划契约等价的别名
GET    /api/v1/speakers
POST   /api/v1/speakers/enroll   (files、speaker_profile_id、authorized=true)
DELETE /api/v1/speakers/{speaker_profile_id}
```

管理接口只返回人员元数据（含模型 ID、revision 和预处理版本），不提供向量下载；登记音频默认不持久化。升级后缺少模型 ID 的历史声纹记录不会参与匹配，需使用当前模型重新登记。`X-Voiceprint-Session-Key` 仅用于识别请求，不能替代管理密钥。
`authorized` 必须是表单布尔值 `true`；其他字符串或数值不会被视为授权。

| ASR 识别结果 | 默认 GPT-SoVITS `text_lang` |
|---|---|
| `zh` | `all_zh` |
| `en` | `en` |
| `yue` | `all_yue` |
| `ja` | `all_ja` |
| `ko` | `all_ko` |

如需固定 TTS 语言，在 `configure` 时传入对应 `tts_text_lang`；例如粤语：

```json
{"type":"configure","language":"yue","tts_text_lang":"all_yue"}
```

## 6. 错误与重试

| 场景 | 表现 | 接入方处理 |
|---|---|---|
| HTTP API Key 不正确 | HTTP `401` | 更新网关 API Key 后重试。 |
| 网关未设置 API Key | HTTP `503` | 联系服务管理员完成部署配置。 |
| WebSocket Key 不正确 | 握手 HTTP `403`（服务端尚未接受连接） | 不重试，检查客户端配置。 |
| LLM/TTS 未配置 | `error`，`code=configuration_required` | 联系服务管理员，不应由客户端传入上游密钥。 |
| 上游 URL 为空或格式无效 | `error`，`code=configuration_required`，`missing_settings` 指出配置名 | 由管理员修正 `LLM_*_URL` 或 `GPT_SOVITS_BASE_URL`；客户端不要自行改写上游地址。 |
| LLM 回复超过服务端上限 | `error`，`code=llm_output_too_large` | 调整服务端 `SENSEVOICE_MAX_LLM_OUTPUT_CHARS` 或缩短系统提示/业务请求；不要无限重试同一请求。 |
| LLM 返回空回复 | `error`，`code=llm_empty` | 记录上游响应并有限退避重试；不要把空文本当作成功回复写入会话历史。 |
| TTS 请求 ID 无效 | `error`，`code=invalid_request_id` | 使用不超过 128 个字符且不含控制字符的字符串 ID。 |
| 空音频提交 | `error`，`code=invalid_audio` | 先发送至少一个非空音频二进制帧，再发送 `commit`；空提交不会创建话轮或推进时间轴。 |
| 配置时仍有未提交音频 | `error`，`code=configure_requires_empty_buffer` | 等待当前话轮提交或清空后，再切换语言、VAD 或音色。 |
| JSON 语法正确但不是对象 | `error`，`code=invalid_event` | 发送 JSON 对象；不要发送数组、字符串或 `null` 作为事件。 |
| TTS 语言不受支持 | `error`，`code=unsupported_tts_language` | 使用 `auto`、`zh`、`en`、`yue`、`ja`、`ko` 或对应 GPT-SoVITS 语言码。 |
| 模型并发达到上限 | `error`，`code=server_busy` 或 HTTP `503` | 实时 ASR 与完整对话会保留尚未进入 ASR 的音频，采用退避后重试；不要在客户端无限并行提交同一实例。 |
| TTS 文本时长预算小于单个短句 | `error`，`code=tts_queue_limit` | 提高 `SENSEVOICE_TTS_QUEUE_MAX_SECONDS`，或让服务端使用更细的短句切分；客户端不要盲目重试同一整段文本。 |
| 音频太大、超长或格式不支持 | `error`，通常为 `invalid_audio`、`invalid_audio_format`、`invalid_audio_alignment`、`audio_too_large` 或 `utterance_too_long` | 实时 ASR 先转换为 16 kHz 单声道 PCM16；每个二进制帧保持偶数字节；超过字节或时长上限时当前缓冲会被丢弃并重置话轮，错误可带 `max_seconds`/`dropped_duration_ms`；文件音频改用离线 ASR 或完整对话接口。 |
| LLM 或 TTS 上游暂时失败 | `error`，`llm_failed` 或 `tts.error` | 可对当前句子做有限重试，避免重复播放已成功的 TTS 段。 |

网络断线后应新建 WebSocket。会话历史只保存在单一连接内，重连后如需保留上下文，应由你的项目
自行保存并用新的用户输入重新建立会话逻辑。

## 7. HKGAI 服务端配置

HKGAI 是网关的内部 LLM 上游，客户端无需也不能传入其 Key。服务管理员在
`/data/voice-stack/config/sensevoice.env` 填入：

```env
LLM_BASE_URL=https://test-new-api.hkchat.app/v1
LLM_MODEL=t2_hkgai-v3_fp8_1m_e7
LLM_API_KEY=<HKGAI_API_KEY>
```

`LLM_MODEL` 应以 HKGAI `GET /v1/models` 实际返回的可用模型 ID 为准。修改服务端配置后重启：

```bash
sudo systemctl restart sensevoice.service
curl -sS http://127.0.0.1:50000/api/v1/health
```

真实 Key 只应存在于该服务器配置文件或部署平台 Secret 中，不得写入代码、调用文档、浏览器前端
或 Git 仓库。
