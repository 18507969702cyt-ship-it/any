"""
Plan 契约 — submit_plan Function Call Schema

MainAgent 调用 submit_plan 将执行计划交给 Runtime。
此 schema 由 SubAgentRegistry 动态生成，确保 enum 值与注册表一致。
"""

from .subagent_registry import SubAgentRegistry


def build_submit_plan_schema(registry: SubAgentRegistry = None) -> dict:
    """
    构建 submit_plan 的 Function Call JSON Schema

    此函数动态从 SubAgentRegistry 读取所有已注册的 SubAgent 和 action，
    生成 contracts.json 中需要的 Function Call schema。

    Returns:
        OpenAI-compatible function call schema
    """
    if registry is None:
        registry = SubAgentRegistry()

    subagent_names = registry.get_subagent_names()
    action_enums = registry.get_action_enums()

    # 构建每个 SubAgent 的 action 描述
    action_descriptions = []
    for name in subagent_names:
        cap = registry.get(name)
        if cap:
            actions_str = " | ".join(cap.actions)
            action_descriptions.append(
                f"{name}: {actions_str} ({cap.description})"
            )

    return {
        "type": "function",
        "function": {
            "name": "submit_plan",
            "description": (
                "将执行计划提交给 Runtime。"
                "Runtime 会按序校验每个 step 的 target_subagent 和 action，"
                "然后依次调度对应 SubAgent 执行任务。"
                "你只需要规划'谁做什么、交付什么'，无需操心执行细节。"
            ),
            "parameters": {
                "type": "object",
                "required": ["steps"],
                "properties": {
                    "plan_id": {
                        "type": "string",
                        "description": "计划唯一标识，可选。不提供则自动生成。"
                    },
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "description": (
                            "执行步骤列表，按序执行。"
                            "每个 step 指定一个 SubAgent、其需要执行的任务、以及执行所需上下文。"
                        ),
                        "items": {
                            "type": "object",
                            "required": ["target_subagent", "task", "context"],
                            "properties": {
                                "step_id": {
                                    "type": "integer",
                                    "description": "步骤序号，从 1 开始递增"
                                },
                                "target_subagent": {
                                    "type": "string",
                                    "enum": subagent_names,
                                    "description": (
                                        f"调用哪个 SubAgent。可用选项及对应 action：\n"
                                        + "\n".join(f"  - {d}" for d in action_descriptions)
                                    )
                                },
                                "task": {
                                    "type": "object",
                                    "required": ["action", "description", "deliverable"],
                                    "description": (
                                        "任务规格。简单明了地告诉 SubAgent 要做什么。\n"
                                        "description 只描述产出目标，不要把 project_context 内容复述到这里。\n"
                                        "action 必须严格使用 SubAgent 支持的 action 名称。"
                                    ),
                                    "properties": {
                                        "action": {
                                            "type": "string",
                                            "description": (
                                                "动作类型，必须匹配目标 SubAgent 的能力。\n"
                                                + "\n".join(
                                                    f"  {name}: {', '.join(actions)}"
                                                    for name, actions in action_enums.items()
                                                )
                                            )
                                        },
                                        "description": {
                                            "type": "string",
                                            "maxLength": 200,
                                            "description": (
                                                "简短具体的任务描述，仅描述产出什么。\n"
                                                "正确：'基于故事大纲生成完整剧本'\n"
                                                "错误：'用户想要一个古装悬疑剧，故事发生在唐朝，主角是...'（这些属于 context）"
                                            )
                                        },
                                        "deliverable": {
                                            "type": "string",
                                            "maxLength": 200,
                                            "description": (
                                                "明确说明交付物格式和内容。\n"
                                                "例如：'一份 3-5 幕的完整剧本，JSON 格式，包含 acts 数组和 characters 数组'"
                                            )
                                        }
                                    }
                                },
                                "context": {
                                    "type": "object",
                                    "description": (
                                        "执行所需上下文。这是 project_context 的内容组织。\n"
                                        "只放该 SubAgent 执行任务时确实需要的信息。\n"
                                        "不要把所有信息无差别塞入，避免 SubAgent 信息过载。"
                                    ),
                                    "additionalProperties": True
                                },
                                "dependencies": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "description": (
                                        "依赖的前置 step_id 列表。\n"
                                        "如果本 step 需要前面某 step 的产出，在此声明。\n"
                                        "Runtime 会自动将前置 step 的输出注入到 context 的 step_N_output 字段。\n"
                                        "空数组表示无依赖，可立即执行。"
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }
    }


def build_contracts_json(registry: SubAgentRegistry = None):
    """
    生成完整的 contracts.json 内容（用于前端展示或调试）

    包含：
    1. submit_plan function call schema
    2. 所有 SubAgent 的能力摘要
    3. next_steps schema（兼容旧版）
    """
    if registry is None:
        registry = SubAgentRegistry()

    submit_plan = build_submit_plan_schema(registry)

    subagent_manifest = {}
    for cap in registry.list_all():
        subagent_manifest[cap.name] = {
            "display_name": cap.display_name,
            "description": cap.description,
            "actions": cap.actions,
            "input_schema": cap.input_schema,
            "output_schema": cap.output_schema
        }

    return {
        "version": "2.0.0",
        "description": "Plan-based Multi-Agent Orchestration Contracts",
        "function_calls": {
            "submit_plan": submit_plan
        },
        "subagent_manifest": subagent_manifest,
        "task_receiver_contract": registry.TASK_RECEIVER_CONTRACT.strip(),
        # 兼容旧版 next_steps（不再使用，保留引用）
        "_deprecated_next_steps": {
            "note": "next_steps 已被 submit_plan 替代。PlanManager 不再依赖 next_steps。",
            "migration_guide": "MainAgent 现在使用 submit_plan function call 替代 next_steps 自由文本"
        }
    }
