# FunASR 时间线与声纹改造方案

- 日期：2026-09-14。
- 状态（2026-09-14 更新）：旧版实时 ASR 路由已修复上线；FunASR 新版已切换到 `/data/voice-stack/apps/sensevoice-next` 的隔离 `50001`，完成模型加载、中文/英语/粤语 HTTP/WS P1 冒烟、自动 VAD、独立 TTS、HKGAI 全链路及 ASR 门禁繁忙后的原音频重试验证。`50001` 是手动启动的 transient 服务，WSL 重启后不会恢复。FSMN-VAD 开始/结束哨兵兼容和实时对话任务回收均已现场验证。声纹存储/拒识/租户隔离/删除逻辑已在本地临时未加密模式完成合同检查，但生产加密依赖与目标机跨重启仍待验证。此前的压缩音频误用拒绝、匿名/持久声纹状态区分、BFF 会话密钥校验、声纹样本质量门禁、进程级共享 ASR 门禁、VAD 缺口基准、超限话轮重置、规划说话人接口别名和可选句段事件均已随本次候选实现。多人质量、声纹跨重启、集中延迟和 `50000` 切换仍未完成，不能标记为新版改造完成或已切换。
- 新增总方案：[实时语音与复刻音色](./realtime-voice-plan.zh-CN.md)。先交付固定授权音色的流式合成和播放，再衔接本文的 ASR 时间线/人员识别验收；ASR 声纹库与 TTS 音色库分开。
- 执行位置：模型下载、依赖安装与部署只在 Windows/WSL 执行；Mac 仅保留源码与文档。步骤见 [Windows / WSL 下载与部署指南](./windows-wsl-deployment.zh-CN.md)。
- 需求来源：本任务确认的中文、英语、粤语 ASR，以及当前 `moautoai/admin-web/src/features/voice_quality/Plan.md` 中的时间线、匿名说话人、持久声纹需求。
- 职责：本文维护 SenseVoice 服务端实施方案；管理端展示与 BFF 由 moautoai 对应模块维护，不在此任务中顺带改动。

## 1. 推荐决策

直接使用 FunASR 的固定版本 Python 依赖，由本项目提供薄适配层。保留现有 API 网关、HKGAI 和 GPT-SoVITS；不整体替换为 `funasr-server`，不复制整个 FunASR 仓库，也不修改已安装包的源码。

一期统一使用：

- SenseVoiceSmall：转写，保留中文、英语、粤语入口。
- FSMN-VAD：语音活动边界与在线分段。
- CAM++：会话内说话人分离与声纹向量提取。
- 本项目声纹模块：登记、持久化、匹配、拒识、权限与删除。

相较此前“另接 3D-Speaker 服务”的建议，本版先复用 FunASR 内置 CAM++。已核对的官方源码提供独立声纹向量输出和管线说话人中心输出，能作为身份匹配的输入；这不代表它自带生产级人员库。3D-Speaker/ERes2NetV2 仅作为后续准确率不达标时的备选，避免一期同时维护两套声纹模型。

候选版本为 FunASR `1.4.13`，先在隔离环境验证并锁定完整依赖及模型 revision，不直接升级在线环境。模型权重授权与代码许可证分别核对，不能只依据 FunASR 代码的 MIT 许可推定所有模型授权。

已将官方 `iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch` 纳入候选引擎配置，作为句段标点和
FunASR 说话人分段的必要输入；配置名为 `SENSEVOICE_PUNC_MODEL` 与
`SENSEVOICE_PUNC_MODEL_REVISION`。SenseVoice 自带的 ITN 在实时链路关闭，避免与标点模型重复输出。
仍不把标点模型加载成功当作时间线或多人声纹质量验收，需使用独立授权录音复测。

## 2. 复用与自建边界

| 能力 | 复用 FunASR | 本项目负责 |
|---|---|---|
| 转写 | SenseVoiceSmall 与 `AutoModel.generate` | 输入解码、统一参数、兼容旧响应 |
| 时间线 | VAD 边界、`sentence_info` | 毫秒单位、会话音频偏移、事件顺序 |
| 匿名说话人 | CAM++、聚类和句段 `spk` | 将每次推理的局部标签映射为稳定会话编号 |
| 声纹向量 | CAM++ `spk_embedding`（分块向量，仅作诊断/匿名辅助）；管线可请求 `spk_embedding_center` | 仅用中心向量做登记、匹配、单人质量门禁、归一化和版本隔离 |
| 具体人员识别 | 提供可供比较的向量，不提供本项目人员库 | 已授权登记、持久化、相似度检索、未知人员拒识 |
| 实时会话 | 在线 VAD 构件 | PCM 接收、独立缓存、有界推理队列、字幕与话轮状态机 |

CAM++ 的局部 `spk=0` 仅在一次推理结果中有意义；下次请求的 `spk=0` 不保证还是同一个人。不能直接把这个数字保存为人员身份。

## 3. 能力边界

- 一期交付句段级时间范围，不承诺逐字逐词准确时间戳；`words` 未验证时返回空或省略，不按文本长度平均分配伪造时间。
- SenseVoice 仍是非原生流式转写模型。在线 VAD 加分段转写属于准实时服务，不能宣传成原生增量 ASR。
- VAD 静音边界不等于说话人切换边界。同一语音段内无停顿换人、重叠讲话可能无法逐字归属；一期以轮流说话为验收场景，不能把整段强行认作某个已知人员。
- 声纹识别依赖足够的有效语音，不承诺第一个字就确认身份；普通话、英语、粤语以及跨语种登记/识别分别验证。
- 相似度分数不是概率。对外使用 `match_score`，不把未经校准的 `0.93` 表述为“93% 是该人”。
- 声纹只用于已授权的人员标注，不作为登录、支付或权限认证依据；本方案没有承诺录音重放或合成声音防伪能力。

## 4. 处理流程

```text
音频接收与 PCM 采样时钟
  ├─ 在线 VAD → 当前语音段 → SenseVoice partial/final → 文字与时间范围
  └─ 合格语音窗口 → CAM++ → 会话说话人编号 → 已登记声纹匹配
                         ↓
              聚合为 segments 与 speaker.update
                         ↓
            话轮结束后才进入 HKGAI → GPT-SoVITS
```

### 4.1 完整音频

- 离线或完成的音频段经 SoundFile 解码、单声道化、重采样为 16 kHz，再交给 FunASR，保留当前已经解决的解码兼容路径。
- 优先使用 FunASR 内置 SenseVoice 实现；与当前本地 `model.py` 输出做同录音对照。原有用户修改不覆盖，旧引擎保留作为回退。
- 句段模式使用 VAD 边界，首轮关闭跨 VAD 片段合并，避免为了长段吞吐混合不同说话人的短发言。
- 官方输出经适配器转换为本项目结构；上游字段或类型异常时明确报错，不静默返回空时间线。
- 声纹中心与单段向量都由服务内部处理，不能序列化进公开 ASR 响应或日志。

### 4.2 连续音频

- 收音任务与阻塞推理解耦，每个 WebSocket 独立维护 VAD 缓存、音频缓冲、采样偏移与说话人映射。
- 时间基准是该会话音频起点；即使静音被 VAD 丢弃，也必须保留原始时间偏移。网络断片需标记缺口，不能拼成无缝录音。
- partial 只重算有界的当前语音段，不反复识别整个会话；过期 partial 可取消，已接收的 final 不丢失。
- 对共享模型使用有界工作队列，避免不同线程并发修改 FunASR 运行参数。过载返回明确的 `server_busy`，不无限堆积 GPU 任务。
- 完整对话在 ASR 门禁未取得槽位时恢复原始话轮，实时 ASR 保留当前 PCM 缓冲，确保过载重试不丢失 final 输入。
- 先返回文本，身份可以 `pending`；有足够合格语音后用 `speaker.update` 补充人员标注，身份处理不阻塞普通语音回复。
- `segment_id`、`turn_id` 与递增 `revision` 分别管理句段、话轮和修订。最终文本不被旧 partial 覆盖；身份元数据允许通过更高版本事件明确更新。
- VAD 字幕段不等于 LLM 话轮。保留 `commit` 默认行为，自动结束话轮需显式开启；每个话轮只进入 LLM 一次，不在每次短停顿都触发回复。

## 5. API 与业务兼容

- 保留 `POST /api/v1/asr` 的 `files/lang/use_itn/keys` 以及 `result` 结构；以可选能力参数增加 `segments`，不强制旧客户端改接口。
- 保留 `WS /api/v1/asr/stream` 的 PCM 协议、`asr.partial`、`asr.final`、`asr.completed`。新客户端通过能力协商接收句段和说话人事件；不能因多发 completed 导致旧客户端提前断开。
- 保留 `WS /api/v1/realtime` 的完整音频加 `commit` 模式。PCM 与自动收口通过显式协商开启，不让同一个二进制帧被猜测为 WAV 或裸 PCM。
- 已提供 `/api/v1/voiceprints`，并增加与管理端规划契约兼容的 `/api/v1/speakers`、`/api/v1/speakers/enroll` 和 `/api/v1/speakers/{speaker_profile_id}` 别名；原始声纹向量不对客户端提供查询或下载。
- 声纹管理与普通 ASR 调用必须分权；普通识别请求只有在 BFF 同时提供 `X-Voiceprint-Tenant` 和 `X-Voiceprint-Session-Key` 时才读取对应人员库，不允许客户端随意选择别人的库。正式开放前必须完成 BFF 身份绑定与会话密钥轮换。
- 健康接口增加构建版本、模型 ID/版本与 `timestamps_ready/diarization_ready/speaker_identification_ready`；每项通过预热探测后才标记 ready，不能只检查配置是否非空。
- HKGAI、GPT-SoVITS 接口及参考音色保持不变。声纹故障降级为未知人员，不绕过授权、不让无关人员身份进入 LLM。

## 6. 声纹记忆

- 由业务后端建立人员内部 ID 并确认登记授权；语音服务不从说话内容猜姓名。
- 建议登记采集 3 至 5 段、每段约 5 至 10 秒的单人干净语音作为试点起点，不将此采集建议视为效果保证。
- 对样本做有效时长、音量、静音和单人一致性检查；提取多个归一化向量及聚合中心。
- 匹配按 Top-1 分数与 Top-1/Top-2 差值共同拒识，阈值由本地登记集之外的验证录音确定；测试集不参与调阈值。
- 小规模人员库先做直接向量比较，不额外引入向量数据库。保存模型 ID、revision 和预处理版本，不同向量空间不能混比；换模型需迁移或重新登记。
- 持久数据位于 `/data/voice-stack/data/voiceprints/`，加密并限制服务账户访问；加密密钥与数据分离，默认不长期保留登记录音。
- 删除覆盖持久样本、聚合中心与匹配缓存；备份按明确保留策略处理，并记录不包含声纹内容的审计。
- 若实施需要新增加密依赖、密钥或修改现有敏感配置，先列准确路径、用途与影响，再另行确认；本方案不授权写入这些文件。

## 7. 代码组织与部署

计划修改范围：

- `api.py`：取消直接操作底层模型，改为调用统一语音引擎，保留旧 HTTP 协议。
- `realtime_api.py`：接入统一引擎与实时会话模块，不改变 HKGAI/TTS 对外契约。
- 新增小型 `speech/` 模块：引擎适配、会话时间轴、说话人身份与存储；避免把所有功能继续堆入一个路由文件。
- `requirements.txt` 与部署模板：锁定通过验证的版本；真实环境配置不在未确认情况下修改。
- 调用文档：能力字段、时间单位、身份状态、兼容协商、错误码和实际验收结果。

验证版暂放 Windows/WSL 的 `/data/voice-stack/apps/sensevoice-next/` 与 `/data/voice-stack/environments/sensevoice-next/`。新模型下载到 `/data/voice-stack/models/funasr/<批次>/`，不覆盖在线缓存；已冻结且校验一致的模型可复用。临时音频和检查结果仅放 `/data/voice-stack/work/`，保留当前在线版本用于回退。Mac 不下载权重、不安装模型运行环境；本地项目维护操作指南与脱敏后的版本/验收记录。

试运行监听 WSL `0.0.0.0:50001`，通过 Windows `192.168.101.75:50001` 的局域网转发访问；
本次防火墙仅允许 Private/LocalSubnet，正式接入仍通过现有 `50000` 网关，不开放公网端口。先验证端口空闲和 GPU 余量，不能同时多实例占满显存影响 GPT-SoVITS。

## 8. 实施顺序与验收门禁

当前已有目标机隔离下载与 P1 冒烟证据；P0 中实时路由部署修复已完成，但不代表 P0–P3 全部完成。模型能力探测、CAM++ 向量与标签映射、
在线 VAD/自动收口已在隔离网关接入并完成单录音回归；过载 final 保留、质量口径及可信租户隔离仍需落实，详见下载与部署指南的上线阻断项。
P0 部署基线、P1 真实模型输出、P2 跨重启声纹、P3 端到端兼容和 P4 集中验收均需目标 Windows/WSL
证据；在此之前不切换在线服务，不标记为已支持或已部署。

| 阶段 | 工作 | 完成证据 |
|---|---|---|
| P0 | 核对并修复此前发现的实时路由部署差异，冻结旧版基线 | 版本/哈希、真实 WS 握手与一轮转写记录 |
| P1 | 隔离环境接通 FunASR、VAD、CAM++、句段时间和向量输出 | 同一录音的旧/新转写、非空有效时间范围、匿名说话人和向量映射 |
| P2 | 声纹登记、持久化、识别、拒识、隔离与删除 | 重启后仍能识别登记人员，未登记人被拒识，跨库不可访问，删除后不再匹配 |
| P3 | 接入实时缓存与兼容事件，恢复完整 ASR→HKGAI→TTS | 老客户端不变，新客户端可见时间线/身份，话轮不重复回复 |
| P4 | 集中验收与切换 | 中文/英语/粤语分别报告质量、延迟、资源占用及回退结果 |

指标分别统计：归一化 CER/WER 及替换/漏字/插字分解；时间边界 MAE/P95；DER（注明 collar 与重叠语音口径）；声纹 FAR/FRR 与开放集误认率；首字幕、收口、身份确认和 TTS 首音频的 P50/P95。字符级 CER 和混合中英粤语 WER 的统一归一化、分词与编辑距离实现位于 [`speech/metrics.py`](../speech/metrics.py)：默认移除 SenseVoice 控制标签、Unicode 标点/符号/空白并做 NFKC 与大小写归一化，不擅自改写数字；`character_error_counts`/`word_error_counts` 额外返回替换、漏字、插字数量，`strict_character_error_rate`/`strict_character_error_counts` 则保留标点与符号、忽略空白，用于把标点差异计入质量数据；`timeline_boundary_summary` 按时间排序逐段比较边界并报告起止 MAE/P95、缺失段和多余段，不能把缺失句段当作零误差；`voiceprint_error_summary` 按阈值输出 FRR、FAR 和开放集误接受率，空测试集返回 `None`，阈值/分差越界或非有限值直接报错；`latency_summary` 用线性插值输出延迟样本的 P50/P95；对外仍保留 `raw_text`，两种 CER 必须分栏记录，不能混为一个数。

现有 Plan 中 300 ms 时间偏差、DER 15%、识别率 95% 等仅是待验证目标，不是当前能力或合同保证。使用不同录音会话/设备划分登记、阈值验证与测试集，禁止同段录音既登记又验收。多人无停顿换人、短语音、噪声和跨语言结果单列。

实现阶段默认不新增测试框架、测试文件或基准工具。先运行可用的既有检查与少量授权录音验证；需要新增集中测试资产时再确认。通过真实 Windows/WSL 验证前，不标记为已支持或已部署。

## 9. 已核对的官方依据

- [FunASR v1.4.13](https://github.com/modelscope/FunASR/releases/tag/v1.4.13)：拟验证版本，不等于本机已安装。
- [AutoModel 固定版本源码](https://github.com/modelscope/FunASR/blob/v1.4.13/funasr/auto/auto_model.py)：VAD 组合、`sentence_info`、`return_spk_center` 和 `spk_embedding_center`。
- [CAM++ 固定版本源码](https://github.com/modelscope/FunASR/blob/v1.4.13/funasr/models/campplus/model.py)：`spk_embedding` 输出。
- [在线 VAD 官方示例](https://github.com/modelscope/FunASR/blob/v1.4.13/examples/industrial_data_pretraining/fsmn_vad_streaming/demo.py)：分帧缓存与结束标记的参考。
- [FunASR 模型许可说明](https://github.com/modelscope/FunASR/blob/v1.4.13/MODEL_LICENSE)：模型授权需单独核对。
