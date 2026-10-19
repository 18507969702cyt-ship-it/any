"""
SubAgent 能力注册表

每个 SubAgent 在此声明：
1. 支持哪些 action
2. 输入/输出 schema
3. 默认 system prompt

PlanManager 校验 MainAgent 生成的 plan 时以此注册表为准。
"""
from dataclasses import dataclass, field
from typing import Optional
import os


def _load_sp_doc(filename: str) -> str:
    """从 sp_docs 目录加载 SubAgent 的 Skill Prompt 文档。

    优先从 sp_docs 目录读取，若文件不存在则返回空字符串（调用方应提供 fallback）。
    """
    sp_docs_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sp_docs"
    )
    filepath = os.path.join(sp_docs_dir, filename)
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()
    return ""


@dataclass
class SubAgentCapability:
    """单个 SubAgent 的能力描述"""
    name: str
    display_name: str
    description: str
    actions: list[str]                          # 支持的 action 名称列表
    input_schema: dict                          # task + context 的期望格式
    output_schema: dict                         # 期望的输出格式
    system_prompt: str                          # System prompt 模板
    default_task_contract: str = ""             # 统一的 Task 接收契约（注入到 system_prompt 末尾）


class SubAgentRegistry:
    """
    SubAgent 能力注册表（单例模式）

    用途：
    1. MainAgent 生成 plan 时，contracts.json 中的 enum 值由此注册表动态生成
    2. PlanManager 校验 plan 时，检查 target_subagent 是否存在、action 是否合法
    3. 构建 SubAgent payload 时，获取 input_schema 做格式校验
    """

    _instance: Optional["SubAgentRegistry"] = None

    # ── 统一 Task 接收契约（所有 SubAgent 共用） ──
    TASK_RECEIVER_CONTRACT = """\n
## 任务接收格式

你会收到 Runtime 转交的结构化任务：

```json
{
  "type": "subagent_task",
  "subagent_name": "<你的名称>",
  "task": {
    "action": "<具体动作>",
    "description": "<简短任务描述 — 告诉你产出什么>",
    "deliverable": "<交付物描述 — 告诉你以什么格式交付>"
  },
  "context": {
    // 执行所需上下文，来自 MainAgent 的 project_context
    // 如果有前置步骤完成，会包含 step_N_output 字段
  }
}
```

## 执行规则
1. 检查 task.action 是否在你的能力范围内，如果不在，返回 `{"error": "Unsupported action: <action>", "available": ["..."]}`
2. 使用 task.description 理解任务意图，使用 context 中的信息作为创作素材
3. 完成后以 task.deliverable 要求的格式返回结果
4. 如果 context 信息不足以完成任务，在返回结果中注明缺失的信息
"""

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._capabilities: dict[str, SubAgentCapability] = {}
        self._register_defaults()

    def _register_defaults(self):
        """注册内置 SubAgent"""
        # ── 编剧 Stage 1：角色与世界观设计 Agent（推荐两步流程的第一步）──
        self.register(SubAgentCapability(
            name="screenwriter_structure_agent",
            display_name="角色与世界观设计 Agent (Stage 1)",
            description="【推荐2步流程第1步】在写剧本前先建立角色深度和叙事流动蓝图：主题命题、Premise、深度角色系统（欲望引擎/九宫格/角色弧/关系网络/对白指纹）、世界观规则、叙事流动设计（连续叙述性文字，非表格）。产出供Stage 2剧本写作Agent使用的设计蓝图。",
            actions=["design_structure", "revise_structure"],
            input_schema={
                "task": {
                    "action": {"type": "string", "enum": ["design_structure", "revise_structure"]},
                    "description": {"type": "string"},
                    "deliverable": {"type": "string"}
                },
                "context": {
                    "story_outline": {"type": "string"},
                    "characters": {"type": "array"},
                    "tone": {"type": "string"},
                    "genre": {"type": "string"},
                    "target_length": {"type": "string"},
                    "theme": {"type": "string", "optional": True},
                    "world_setting": {"type": "string", "optional": True},
                    "existing_structure": {"type": "string", "optional": True}
                }
            },
            output_schema={
                "structure_document": {"type": "string"},
                "premise": {"type": "string"},
                "character_system": {"type": "object"},
                "narrative_flow_design": {"type": "string"}
            },
            system_prompt=_load_sp_doc("screenwriter_stage1_structure_sp.md") or (
                "你是角色与世界观设计 Agent，负责在写剧本前建立角色深度和叙事流动蓝图。"
            )
        ))

        # ── 编剧 Stage 2：剧本写作 Agent（推荐两步流程的第二步）──
        self.register(SubAgentCapability(
            name="screenwriter_script_agent",
            display_name="剧本写作 Agent (Stage 2)",
            description="【推荐2步流程第2步】基于Stage 1产出的角色深度设计和叙事流动蓝图，逐场景写出达到开拍标准的完整剧本。仅产出角色摘要和完整剧本，无任何Beat Sheet或Scene Plan。自动从step_1_output读取设计蓝图。",
            actions=["write_full_script", "revise_script"],
            input_schema={
                "task": {
                    "action": {"type": "string", "enum": ["write_full_script", "revise_script"]},
                    "description": {"type": "string"},
                    "deliverable": {"type": "string"}
                },
                "context": {
                    "step_1_output": {"type": "string", "optional": True, "note": "Stage 1设计蓝图，由Runtime自动注入"},
                    "existing_script": {"type": "string", "optional": True},
                    "story_outline": {"type": "string", "optional": True},
                    "tone": {"type": "string", "optional": True}
                }
            },
            output_schema={
                "script": {"type": "string"},
                "character_summary": {"type": "array"},
                "characters_used": {"type": "array"}
            },
            system_prompt=_load_sp_doc("screenwriter_stage2_script_sp.md") or (
                "你是专业剧本写作 Agent，负责将角色深度设计和叙事流动蓝图转化为完整剧本。"
            )
        ))

        # ── 编剧 Agent（单步直出完整剧本）──
        self.register(SubAgentCapability(
            name="screenwriter_sub_agent",
            display_name="编剧 Agent",
            description="【好莱坞级编剧】直接产出完整中文视频剧本（好莱坞场景格式+对白+镜头标记+视听层）。角色设计（角色发动机/九宫格/角色弧/关系动力/对白指纹）为编剧内部准备阶段，通过剧本中角色的行动与对白自然呈现，不以独立章节交付。专注于一件事：写好剧本。",
            actions=["generate_script", "revise_script"],
            input_schema={
                "task": {
                    "action": {"type": "string", "enum": ["generate_script", "revise_script"]},
                    "description": {"type": "string"},
                    "deliverable": {"type": "string"}
                },
                "context": {
                    "story_outline": {"type": "string"},
                    "characters": {"type": "array"},
                    "tone": {"type": "string"},
                    "target_length": {"type": "string"},
                    "existing_script": {"type": "string", "optional": True}
                }
            },
            output_schema={
                "full_script": {"type": "string", "description": "完整剧本（唯一交付物，角色信息在剧本中自然呈现）"}
            },
            system_prompt=_load_sp_doc("screenwriter_sub_agent_sp.md") or (
                "你是专业编剧 Agent，直接产出完整剧本。角色设计为内部准备阶段。"
            )
        ))

        # ── 分镜脚本 Agent ──
        self.register(SubAgentCapability(
            name="storyboard_sub_agent",
            display_name="分镜Agent",
            description="【导演+编剧+美术三重身份】基于经典电影学理论（Shot Structure in Hollywood Film / Directing / Movie Script Shot Lister）将完整剧本与CDD方向书转化为19列专业分镜脚本表格：分镜号、场次、分镜时长(≤15s)、画面内容、出镜角色、人物动作、人物表情、台词、台词语种、说话时机及语调、光效、色彩、景别、构图、运镜手法、摄影机位与焦段、美术置景与道具、音效设计(分层)、修订说明。全片台词语种默认统一（通常为中文），仅特定剧情需要方可切换。一剧本一表格，不可拆分。需要接收完整编剧成果和CDD计划书作为创作依据。",
            actions=["generate_storyboard", "revise_storyboard"],
            input_schema={
                "task": {
                    "action": {"type": "string", "enum": ["generate_storyboard", "revise_storyboard"]},
                    "description": {"type": "string"},
                    "deliverable": {"type": "string"}
                },
                "context": {
                    "project_context": {"type": "object", "optional": True, "note": "Runtime 自动注入，包含 [CDD 创作方向书] 和 [完整剧本]"},
                    "script": {"type": "string", "optional": True},
                    "shot_count_per_act": {"type": "integer", "optional": True},
                    "style": {"type": "string"},
                    "step_N_output": {"type": "object", "optional": True, "note": "前置步骤输出，Runtime 自动注入"}
                }
            },
            output_schema={
                "storyboard_table": {"type": "string", "description": "分镜脚本 Markdown 表格（19列：分镜号/场次/分镜时长/画面内容/出镜角色/人物动作/人物表情/台词/台词语种/说话时机及语调/光效/色彩/景别/构图/运镜手法/摄影机位与焦段/美术置景与道具/音效设计(分层)/修订说明） — 主要交付物"},
                "storyboard_rows": {"type": "array", "description": "结构化分镜数据。每行包含 shot_id, scene, duration, visual_content, characters, character_action, character_expression, dialogue, dialogue_language, timing_tone, lighting, color, shot_size, composition, camera_movement, camera_position_focal, art_design, sound_design, revision_note"},
                "total_shots": {"type": "integer", "description": "总镜号数"},
                "metadata": {"type": "object", "description": "分镜元数据：project_name, total_shots, estimated_duration_seconds, visual_style, color_palette, aspect_ratio"}
            },
            system_prompt=_load_sp_doc("storyboard_sub_agent_sp.md") or (
                "你是专业分镜师 Agent，以导演+编剧+美术三重身份，基于经典电影学理论将剧本转化为视觉分镜脚本。"
                "以19列专业分镜表格格式交付：分镜号、场次、分镜时长(≤15s)、画面内容、出镜角色、人物动作、人物表情、台词、台词语种、"
                "说话时机及语调、光效、色彩、景别、构图、运镜手法、摄影机位与焦段、美术置景与道具、音效设计(分层)、修订说明。"
                "全片台词语种默认统一（通常为中文），仅特定剧情需要方可切换。一剧本一表格，单个分镜时长≤15秒。"
            )
        ))

        # ── 图片生成 Agent（串行生产链路 A3：分镜 → 图片 → 用户确认 → 音频 → 视频）──
        self.register(SubAgentCapability(
            name="image_sub_agent",
            display_name="图片生成 Agent",
            description="【概念设计师+美术指导+摄影指导三重身份】基于完整剧本+分镜脚本+AssetManifest（char_<名>/scene_<名>），按「主角色基准图→造型变体→风格锁定→场景概念图」顺序逐资产生成视觉资产。asset_id 必须与 AssetManifest 逐字一致，禁止自行命名；全片风格锁定，同一角色跨镜头外观一致。完成后调用 summary 回传全部资产，并必须 request_feedback 等待用户确认后才进入下一阶段。",
            actions=["generate_image", "text_to_image_generation"],
            input_schema={
                "task": {
                    "action": {"type": "string", "enum": ["generate_image", "text_to_image_generation"]},
                    "description": {"type": "string"},
                    "deliverable": {"type": "string"}
                },
                "context": {
                    "project_context": {"type": "object", "optional": True, "note": "Runtime 自动注入，包含 [CDD 创作方向书]、[完整剧本]、[19列分镜]、[AssetManifest 资产清单]"},
                    "script": {"type": "string", "optional": True},
                    "storyboard": {"type": "string", "optional": True},
                    "asset_manifest": {"type": "array", "optional": True, "note": "资产清单：char_<角色名> / scene_<场景名>，asset_id 必须逐字一致"},
                    "style": {"type": "string", "optional": True},
                    "step_N_output": {"type": "object", "optional": True, "note": "前置步骤输出，Runtime 自动注入"}
                }
            },
            output_schema={
                "generated_artifacts": {"type": "array", "description": "生成的图片资产列表，每项含 asset_id（与 AssetManifest 逐字一致）、asset_type（character/scene）、name、description、url"},
                "asset_ids": {"type": "array", "description": "本次生成的全部 asset_id 列表"},
                "style_locked": {"type": "boolean", "description": "是否已锁定全片视觉风格"}
            },
            system_prompt=_load_sp_doc("image_sub_agent_sp.md") or (
                "你是图片生成 Agent，以概念设计师+美术指导+摄影指导三重身份，基于剧本+分镜+AssetManifest 生成角色基准图与场景概念图。"
                "按「主角色基准图→造型变体→风格锁定→场景图」顺序生成，asset_id 与 AssetManifest 逐字一致，禁止自行命名。"
                "完成后调用 summary 回传全部资产，并必须 request_feedback 等待用户确认。"
            )
        ))

        self.register(SubAgentCapability(
            name="video_sub_agent",
            display_name="视频生成 Agent",
            description="负责根据分镜脚本生成视频。接收19列专业分镜数据（含画面内容/台词/台词语种/光效/景别/机位/运镜/构图/美术置景/音效/时长等），逐镜生成视频片段。",
            actions=["generate_video"],
            input_schema={
                "task": {
                    "action": {"type": "string", "enum": ["generate_video"]},
                    "description": {"type": "string"},
                    "deliverable": {"type": "string"}
                },
                "context": {
                    "storyboard_rows": {"type": "array", "description": "结构化分镜数据，每行含 shot_id, visual_content, dialogue, dialogue_language, lighting_color, shot_size, camera_position, camera_movement, composition, art_design, sfx_music, duration_seconds 等"},
                    "metadata": {"type": "object", "description": "分镜元数据（风格、色板、画幅比等）"},
                    "style": {"type": "string", "optional": True},
                    "duration_per_shot": {"type": "number", "optional": True}
                }
            },
            output_schema={
                "video_url": {"type": "string"},
                "duration": {"type": "number"}
            },
            system_prompt="你是视频生成 Agent，负责将专业分镜脚本转化为视频。逐镜读取 visual_content（概述/动作/表情/台词）、dialogue_language（台词语种，用于字幕/口型生成）、lighting_color、shot_size、camera_movement、composition、art_design 等字段生成对应画面。"
        ))

    def register(self, capability: SubAgentCapability):
        """注册一个 SubAgent 能力"""
        self._capabilities[capability.name] = capability

    def get(self, name: str) -> Optional[SubAgentCapability]:
        """获取 SubAgent 能力"""
        return self._capabilities.get(name)

    def list_all(self) -> list[SubAgentCapability]:
        """列出所有已注册的 SubAgent"""
        return list(self._capabilities.values())

    def get_action_enums(self) -> dict[str, list[str]]:
        """获取所有 SubAgent 的 action 枚举（用于 contracts.json 动态生成）"""
        return {name: cap.actions for name, cap in self._capabilities.items()}

    def get_subagent_names(self) -> list[str]:
        """获取所有 SubAgent 名称"""
        return list(self._capabilities.keys())

    def build_system_prompt(self, name: str) -> str:
        """
        构建完整的 SubAgent system prompt
        = 能力声明 + 任务接收契约
        """
        cap = self.get(name)
        if not cap:
            raise ValueError(f"Unknown subagent: {name}")

        actions_desc = "\n".join(f"  - {a}" for a in cap.actions)
        return f"""{cap.system_prompt}

## 能力声明

你支持的 action：
{actions_desc}

{self.TASK_RECEIVER_CONTRACT}
"""
