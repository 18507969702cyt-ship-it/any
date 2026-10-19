# 叙事类视频创作 Multi-Agent 系统串行生产方案（v3）

> **变更记录**
>
> - **v1 / v2（并行化改造方案）**：曾设计"分镜完成后 image / audio / video 三路并行 fan-out + 物料协同层（EventBus / ReadinessChecker / TaskQueue）"。
> - **v3（本版，已落地）**：**并行方案因线上并行问题多而被废弃**，生产链路回退为严格串行：
>   `screenwriter_sub_agent → storyboard_sub_agent → image_sub_agent（生成图片）→ 用户确认图片 → audio_sub_agent（角色音色生成）→ video_sub_agent（视频生成）`。
> - 本版保留并行时代沉淀下来的**确定性资产命名契约**与 **video P1/P2 门控**（二者在串行下依然有效且被线上采用）。

---

## 0. 当前线上实现（lumen_web_export）

| 资产 | 位置 | 说明 |
|---|---|---|
| image_sub_agent SP | `sp_docs/agents/image_subagent/【线上版】图片SubAgentSP.docx` | 完整 SP：工具决策树、参考图 Role、角色一致性、summary 协议 |
| video_sub_agent SP | `sp_docs/agents/video_subagent/【线上版】视频SubAgent.docx` | Seedance 工具决策树、生成模式优先级（首帧>参考>首尾帧>文生）、P1/P2 双 Skill 无状态模式 |
| audio_sub_agent SP | `sp_docs/agents/audio_sub_agent/【线上版】音频SubAgentSP.md` | 角色音色资产（voice asset）设计 + 全片音频方案（audio_plan） |
| 资产命名契约 | `app.py` 主 prompt 内嵌 ASSET MANIFEST CONTRACT | `CHAR_角色名 / SCENE_场景名 / AUDIO_角色名_voice` 全局统一命名 |
| 物料协同层 | `app.py` AssetExtractor / AssetRegistry / VideoTaskGate | 分镜提取确定性命名的 AssetManifest；累积已注册资产（图片/音色）；video P1（产 prompt）与 P2（物料齐备自动生成）门控 |
| 调度机制 | `app.py` 主循环 | `_PARALLEL_FANOUT_SUBAGENTS = ()` 置空 → 恒走串行分支，逐个执行 `call_subagent` |

---

## 1. 串行生产流程总览（v3 落地形态）

```
screenwriter_sub_agent（剧本）
  → storyboard_sub_agent（19 列分镜，含台词语种）
  → image_sub_agent（逐资产生成角色/场景公共美术资产）
  → 【用户确认图片】（request_feedback 门控，未确认不进入下一阶段）
  → audio_sub_agent（关键主角/副角音色资产 char_<名>_voice + 全片音频方案）
  → video_sub_agent（P1 产 clip Task Board → P2 消费已确认图片+音色逐 clip 生成）
```

**铁律**：任何阶段必须完成并经用户确认后才可进入下一阶段；Runtime 依赖锁会**拒绝**跨阶段调用（见 §3）。

---

## 2. 各阶段职责与资产契约

### 2.1 image_sub_agent（图片阶段）

- 输入：完整剧本 + 分镜脚本 + AssetManifest（`char_<名>` / `scene_<名>`）。
- 按「主角色基准图 → 造型变体 → 风格锁定 → 场景图」顺序逐资产生成，输出 `asset_id` 回写（与 AssetManifest 逐字一致，禁止自行命名）。
- 产出注册进 AssetRegistry，供 video P2 消费。
- 完成全部资产生成后调用 `summary` 回传，**必须** `request_feedback` 等待用户确认。

### 2.2 用户确认门控（关键节点）

- 图片产出后 MainAgent 必须请求用户确认；用户确认前**不得**调度 audio_sub_agent。
- 用户确认后，确认后的图片资产进入"已确认"状态，作为后续阶段的前置依赖。

### 2.3 audio_sub_agent（音色阶段）

- 输入：完整剧本 + 分镜（含台词）+ **已确认的图片资产**。
- 只为关键主角/副角（Key Roles）产出音色资产（`char_<名>_voice`，≤5 秒样音），路人/群像/无名角色一律跳过。
- 通过 `text_to_audio_generation`（MiniMax T2A v2，首选）或 `text_to_speech_generation`（备选）生成；每轮最多 3 个音频工具调用。
- 同时产出全片 Sound Style Bible（audio_plan）。
- 依赖锁：image 阶段未完成（含用户未确认）时，audio 分支直接返回 blocked。

### 2.4 video_sub_agent（视频阶段）

- 输入：完整剧本 + 分镜 + **已确认的图片资产 + 音色资产**（video 依赖锁：storyboard + image + audio 全部完成后才允许调度）。
- 双 Skill 无状态模式：首次唤醒由 Runtime 以 PLANNING 模式调用（Skill A 产出 Task Board：clip_id / shot_range / duration / video_prompt / dependencies / status）；此后为 INCREMENTAL_GEN 模式（Skill B 单 clip 增量生成）。
- 生成模式优先级：首帧 > 参考 > 首尾帧 > 文生；clip dependencies 使用 AssetManifest 的 asset_id，Runtime 用集合运算（dependencies − 已注册资产）判定缺料。
- P2 兜底：`_auto_trigger_video_p2()` 在图片/音色齐备后自动触发，无需 MainAgent 重复调度。

---

## 3. 串行依赖锁（Runtime 强制）

| 阶段 | 前置依赖 | 未满足时的行为 |
|---|---|---|
| storyboard | 完整剧本已确认 | 无剧本 → 拒绝，返回需先走 screenwriter |
| image | 剧本 + 分镜已确认 | 分镜未完成 → blocked |
| audio | **image 已生成且经用户确认** | 返回 blocked 结果，提示先完成图片并确认 |
| video | **storyboard + image（已确认） + audio 全部完成** | 返回 blocked，列出缺失依赖（`_missing_video_deps`） |

即使 LLM 违规在同一轮并行发起多个 call_subagent，Runtime 也会通过上述依赖锁拒绝非串行阶段。

---

## 4. 物料协同层（串行下保留的作用）

| 组件 | 作用（串行语义） |
|---|---|
| AssetExtractor | 从分镜提取确定性命名的 AssetManifest（`scene_<code>` / `char_<名>` / `char_<名>_voice`），LLM 不参与命名 |
| AssetRegistry | `session_data["asset_registry"]` 累积已注册资产（图片/音色），供 video P2 用集合运算判定缺料 |
| VideoTaskGate / `_auto_trigger_video_p2` | video P1 产出 Task Board 后，等图片/音色齐备自动启动 P2 生成（P2 不在 plan 中，由 Runtime 驱动） |

> 说明：并行时代设计的 EventBus / ReadinessChecker / TaskQueue / ProductionOrchestrator 等组件**未落地**；线上采用更简单的"分支完成回调 + 依赖锁 + P2 自动触发"实现相同目标。

---

## 5. 失败 / 降级

| 场景 | 处理 |
|---|---|
| 图片资产生成失败 | image 内部重试/降级（i2i_failed_override / anti_regen_override）；失败资产不进入已确认集 |
| 用户拒绝/修改图片 | 回到 image 阶段重新生成，确认前不进入 audio |
| audio 生成失败 | 关键角色可重试；全部失败 → 不进入 video，回传 MainAgent 汇总 |
| video P2 生成失败 | 单 clip 重试，`attempts+1`；耗尽 → FAILED 回传 |
| 物料缺失 | video 依赖锁列出缺失项，等待补齐，无无限等待 |

---

## 6. 验收标准

1. 分镜确认后**仅**调度 image_sub_agent；图片生成后**必须**出现用户确认环节，确认前日志无 audio 调用。
2. 用户确认图片后才调度 audio_sub_agent；audio 完成前无 video 调用。
3. video 仅在 storyboard + image + audio 全部完成后被调度（日志验证三段式时间顺序）。
4. 全片 asset_id 与分镜字段一一对应（manifest 每个 asset_id 可回溯到具体分镜行）。
5. 即使 LLM 违规并行调用，Runtime 依赖锁返回 blocked，日志可见拒绝提示。

---

## 7. 与线上实现的对应关系

| 本方案章节 | 线上实现（app.py / sp_docs） |
|---|---|
| §2.1 image | image 分支：剧本+分镜+manifest → 逐资产生成 → summary 收口 |
| §2.2 用户确认门控 | 图片产出后强制 request_feedback；audio 分支硬性依赖锁（image 未完成返回 blocked） |
| §2.3 audio | A4 audio 分支：工具型循环 + TTS 工具 + 音色/音频方案回传 |
| §2.4 video | video 分支：PLANNING（Task Board）/ INCREMENTAL_GEN 双模式 + `_auto_trigger_video_p2` |
| §3 依赖锁 | `_completed_subagents` 集合 + video 依赖锁（storyboard+image+audio）+ audio 依赖锁 |
| §4 物料协同 | AssetManifest 命名契约 + AssetRegistry + VideoTaskGate |
