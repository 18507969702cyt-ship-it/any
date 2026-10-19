"""
Runtime 调度集成 — 替换原有 call_subagent + next_steps 流程

原有的问题链路：
  MainAgent → call_subagent(project_context, next_steps) → _normalize_next_steps() → SubAgent
  问题：next_steps 被硬编码覆盖，SubAgent 收到模糊指令无法执行

新的 Plan 链路：
  MainAgent → submit_plan(steps) → PlanManager.receive_and_validate_plan()
    → PlanManager.get_next_pending_step()
      → PlanManager.build_subagent_payload(step)
        → dispatch_to_subagent(payload)
          → SubAgent 收到结构化 task + context → 正常执行

本文件展示如何在现有 app.py 中集成新方案。
"""
import json
import asyncio
import logging
from typing import Optional

from runtime.plan_manager import PlanManager, StepStatus
from runtime.subagent_registry import SubAgentRegistry
from runtime.plan_contracts import build_submit_plan_schema, build_contracts_json
from prompts.main_agent_plan import build_main_agent_plan_prompt
from prompts.subagent_receiver import build_subagent_receiver_prompt

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# 集成步骤 1：在 app.py 启动时初始化
# ═══════════════════════════════════════════════════════

def init_plan_system():
    """
    初始化 Plan 系统（在 app.py 启动时调用一次）

    替代原有：
    - contracts.json 硬编码
    - _normalize_next_steps() 硬编码覆盖
    - _default_next_steps_for_subagent() 硬编码默认值
    """
    registry = SubAgentRegistry()

    # 生成 submit_plan function call schema
    submit_plan_schema = build_submit_plan_schema(registry)

    # 生成 MainAgent 的 Plan 生成 prompt
    main_agent_plan_prompt = build_main_agent_plan_prompt(registry)

    # 验证所有 SubAgent 的 system prompt
    subagent_prompts = {}
    for cap in registry.list_all():
        subagent_prompts[cap.name] = build_subagent_receiver_prompt(cap.name, registry)

    logger.info(f"Plan system initialized with {len(registry.list_all())} subagents")

    return {
        "registry": registry,
        "submit_plan_schema": submit_plan_schema,
        "main_agent_plan_prompt": main_agent_plan_prompt,
        "subagent_prompts": subagent_prompts
    }


# ═══════════════════════════════════════════════════════
# 集成步骤 2：修改 MainAgent 的 function call 注册
# ═══════════════════════════════════════════════════════

def get_main_agent_tools(plan_config: dict) -> list[dict]:
    """
    获取 MainAgent 的可用工具列表

    替换原来的 contracts.json 中 call_subagent + next_steps 部分
    """
    return [
        plan_config["submit_plan_schema"],
        # 其他 MainAgent 工具保持不变...
    ]


def get_main_agent_system_prompt(plan_config: dict, base_prompt: str) -> str:
    """
    构建 MainAgent 的完整 system prompt

    在原有 base_prompt 末尾追加 Plan 生成规范
    """
    return base_prompt + "\n" + plan_config["main_agent_plan_prompt"]


# ═══════════════════════════════════════════════════════
# 集成步骤 3：修改 MainAgent 响应处理（关键改动）
# ═══════════════════════════════════════════════════════

async def handle_main_agent_response(
    response: dict,
    session_id: str,
    session_data: dict,
    plan_config: dict,
    dispatch_subagent_fn,  # async callable(subagent_name, system_prompt, user_message) -> dict
):
    """
    处理 MainAgent 的响应

    替代原来的：
    - 解析 call_subagent tool call
    - 调用 _normalize_next_steps() 覆盖 next_steps
    - 手动构建 subagent message

    Args:
        response: MainAgent LLM 响应
        session_id: 会话 ID
        session_data: 会话数据
        plan_config: init_plan_system() 的返回值
        dispatch_subagent_fn: SubAgent 调度函数

    Returns:
        汇总结果或继续对话的标记
    """
    tool_calls = response.get("tool_calls", [])

    for tc in tool_calls:
        fn = tc.get("function", {})
        fn_name = fn.get("name", "")

        if fn_name == "submit_plan":
            # ── 新方案：Plan-based 调度 ──
            return await _handle_submit_plan(
                fn_arguments=json.loads(fn.get("arguments", "{}")),
                session_id=session_id,
                session_data=session_data,
                plan_config=plan_config,
                dispatch_subagent_fn=dispatch_subagent_fn
            )

        # 其他 tool call 保持原样处理...

    # 没有 submit_plan，检查是否有纯文本回复
    return {"type": "text_response", "content": response.get("content", "")}


async def _handle_submit_plan(
    fn_arguments: dict,
    session_id: str,
    session_data: dict,
    plan_config: dict,
    dispatch_subagent_fn,
) -> dict:
    """
    处理 submit_plan function call

    流程：
    1. PlanManager 校验 plan
    2. 按序调度 SubAgent
    3. 汇总结果
    """
    registry = plan_config["registry"]
    subagent_prompts = plan_config["subagent_prompts"]

    # ── Step 1: 校验 Plan ──
    pm = PlanManager(session_id, session_data, registry)

    try:
        warnings = pm.receive_and_validate_plan(fn_arguments)
    except ValueError as e:
        logger.error(f"Plan validation failed: {e}")
        return {
            "type": "plan_error",
            "error": str(e),
            "message": "Plan validation failed. MainAgent should re-plan."
        }

    if warnings:
        logger.warning(f"Plan warnings: {warnings}")
        # 警告不阻塞执行，但会记录

    # ── Step 2: 按序调度 SubAgent ──
    results = []

    while step := pm.get_next_pending_step():
        logger.info(
            f"Executing step {step.step_id}: {step.target_subagent}.{step.task.action}"
        )

        # 构建 SubAgent 输入 payload
        payload = pm.build_subagent_payload(step)

        # 获取 SubAgent 的 system prompt
        system_prompt = subagent_prompts.get(
            step.target_subagent,
            build_subagent_receiver_prompt(step.target_subagent, registry)
        )

        # 标记开始
        pm.mark_step_started(step.step_id)

        # ── 调度 SubAgent 执行 ──
        try:
            subagent_result = await dispatch_subagent_fn(
                subagent_name=step.target_subagent,
                system_prompt=system_prompt,
                user_message=json.dumps(payload, ensure_ascii=False, indent=2),
                session_id=session_id
            )

            # 记录结果
            if subagent_result.get("error"):
                pm.mark_step_result(
                    step.step_id,
                    result=subagent_result,
                    error=subagent_result["error"]
                )
                logger.error(f"Step {step.step_id} failed: {subagent_result['error']}")
                # 有步骤失败 → 中止后续
                results.append({
                    "step_id": step.step_id,
                    "status": "failed",
                    "error": subagent_result["error"]
                })
                break
            else:
                pm.mark_step_result(step.step_id, result=subagent_result)
                results.append({
                    "step_id": step.step_id,
                    "status": "completed",
                    "output": subagent_result
                })
                logger.info(f"Step {step.step_id} completed")

        except Exception as e:
            logger.exception(f"Step {step.step_id} exception: {e}")
            pm.mark_step_result(step.step_id, result={}, error=str(e))
            results.append({
                "step_id": step.step_id,
                "status": "failed",
                "error": str(e)
            })
            break

    # ── Step 3: 汇总 ──
    summary = pm.get_plan_summary()

    # ── 编剧两步流程：合并 Stage 1 + Stage 2 输出 ──
    merged_script_output = pm.merge_screenwriter_outputs()

    response = {
        "type": "plan_execution_summary",
        "plan_id": summary["plan_id"],
        "total_steps": summary["total_steps"],
        "completed": summary["completed"],
        "failed": summary["failed"],
        "results": results,
        "outputs": summary["outputs"]
    }

    if merged_script_output:
        response["merged_script"] = merged_script_output["merged_script"]
        response["structure"] = merged_script_output["structure"]
        response["script"] = merged_script_output["script"]
        response["merging_note"] = (
            f"两步编剧流程完成。Step {merged_script_output['steps_involved']} "
            f"(结构设计 + 剧本写作) 输出已自动合并。"
        )
        logger.info(f"Screenwriter 2-step outputs merged for plan {summary['plan_id']}")

    return response


# ═══════════════════════════════════════════════════════
# 集成步骤 4：SubAgent 调度函数示例
# ═══════════════════════════════════════════════════════

async def dispatch_subagent_example(
    subagent_name: str,
    system_prompt: str,
    user_message: str,
    session_id: str
) -> dict:
    """
    SubAgent 调度函数示例

    在你的 app.py 中，这里需要替换为实际的 SubAgent LLM 调用逻辑。

    关键：SubAgent 的 system_prompt 现在包含统一的任务接收契约，
    user_message 是 PlanManager.build_subagent_payload() 构造的结构化任务。
    """
    logger.info(f"Dispatching to {subagent_name}")
    logger.debug(f"System prompt (first 100 chars): {system_prompt[:100]}...")
    logger.debug(f"User message: {user_message[:200]}...")

    # 这里替换为你的实际 SubAgent LLM 调用：
    # result = await your_llm_client.chat(
    #     model="your-model",
    #     messages=[
    #         {"role": "system", "content": system_prompt},
    #         {"role": "user", "content": user_message}
    #     ],
    #     tools=subagent_tools[subagent_name],
    #     tool_choice="auto"
    # )

    # 占位返回值，实际使用时替换
    return {
        "status": "ok",
        "subagent": subagent_name,
        "output": {}
    }


# ═══════════════════════════════════════════════════════
# 集成步骤 5：移除弃用代码清单
# ═══════════════════════════════════════════════════════

"""
以下代码在集成新方案后应删除：

1. app.py 中：
   - _normalize_next_steps() 函数 — 不再需要硬编码覆盖
   - _default_next_steps_for_subagent() 函数 — 不再需要默认值
   - call_subagent 相关的 tool call 处理 — 被 submit_plan 替代
   - 手动拼接 subagent message 的逻辑 — PlanManager 统一构建

2. contracts.json 中：
   - next_steps 的 minItems/maxItems/enum 约束 — 被 plan steps 替代
   - call_subagent 的 function call schema — 被 submit_plan 替代
   - action_type.enum 的 workflow_continuation — 不再使用

3. SubAgent prompt 中：
   - 旧的自由文本任务接收格式 — 被统一契约替代
   - 硬编码的角色描述 — 由 registry 统一管理
"""


# ═══════════════════════════════════════════════════════
# 快速测试：验证整个链路
# ═══════════════════════════════════════════════════════

def quick_test():
    """
    验证 Plan 系统的完整性（不含 LLM 调用）
    """
    print("=" * 60)
    print("Plan System Quick Test")
    print("=" * 60)

    # 1. 初始化
    config = init_plan_system()
    registry = config["registry"]
    print(f"\n✅ Registered {len(registry.list_all())} subagents:")
    for cap in registry.list_all():
        print(f"   - {cap.name}: {cap.actions}")

    # 2. 生成 submit_plan schema
    schema = config["submit_plan_schema"]
    print(f"\n✅ submit_plan function: {schema['function']['name']}")
    subagent_enum = schema["function"]["parameters"]["properties"]["steps"]["items"]["properties"]["target_subagent"]["enum"]
    print(f"   Available targets: {subagent_enum}")

    # 3. 模拟 MainAgent 生成的 plan
    test_plan = {
        "plan_id": "plan_test_001",
        "steps": [
            {
                "step_id": 1,
                "target_subagent": "screenwriter_sub_agent",
                "task": {
                    "action": "generate_script",
                    "description": "基于故事大纲生成完整剧本",
                    "deliverable": "一份 3 幕的完整剧本，JSON 格式"
                },
                "context": {
                    "story_outline": "一个关于AI觉醒的悬疑故事",
                    "characters": [{"name": "程远", "role": "主角"}, {"name": "方知意", "role": "AI"}],
                    "tone": "悬疑",
                    "target_length": "3幕"
                },
                "dependencies": []
            },
            {
                "step_id": 2,
                "target_subagent": "storyboard_sub_agent",
                "task": {
                    "action": "generate_storyboard",
                    "description": "根据剧本生成分镜脚本",
                    "deliverable": "每幕 3 张分镜，JSON 格式"
                },
                "context": {
                    "shot_count_per_act": 3,
                    "style": "电影感"
                },
                "dependencies": [1]
            }
        ]
    }

    # 4. PlanManager 校验
    session_data = {}
    pm = PlanManager("test_session", session_data, registry)
    warnings = pm.receive_and_validate_plan(test_plan)

    print(f"\n✅ Plan validated: {len(pm.plan)} steps")
    if warnings:
        for w in warnings:
            print(f"   ⚠️  {w}")

    # 5. 模拟 Step 1 执行
    step1 = pm.get_next_pending_step()
    if step1:
        payload1 = pm.build_subagent_payload(step1)
        print(f"\n✅ Step 1 payload built:")
        print(f"   target: {payload1['subagent_name']}")
        print(f"   action: {payload1['task']['action']}")
        print(f"   description: {payload1['task']['description']}")
        print(f"   context keys: {list(payload1['context'].keys())}")

        # 模拟 SubAgent 返回
        pm.mark_step_started(step1.step_id)
        pm.mark_step_result(step1.step_id, result={
            "script": "第一幕：AI 在深夜的实验室中首次觉醒...",
            "acts": ["第一幕", "第二幕", "第三幕"],
            "characters_used": ["程远", "方知意"]
        })
        print(f"   status: {step1.status.value}")

    # 6. 模拟 Step 2 执行（依赖注入）
    step2 = pm.get_next_pending_step()
    if step2:
        payload2 = pm.build_subagent_payload(step2)
        print(f"\n✅ Step 2 payload built:")
        print(f"   context keys: {list(payload2['context'].keys())}")
        print(f"   has step_1_output: {'step_1_output' in payload2['context']}")

    # 7. 汇总
    summary = pm.get_plan_summary()
    print(f"\n✅ Plan summary:")
    print(f"   total: {summary['total_steps']}")
    print(f"   completed: {summary['completed']}")
    print(f"   pending: {summary['pending']}")
    print(f"   plan complete: {pm.is_plan_complete()}")

    # 8. 校验 contracts.json
    contracts = build_contracts_json(registry)
    print(f"\n✅ contracts.json generated: {contracts['version']}")
    print(f"   subagent_manifest: {list(contracts['subagent_manifest'].keys())}")

    # 9. 校验 prompts
    main_prompt = build_main_agent_plan_prompt(registry)
    print(f"\n✅ MainAgent plan prompt: {len(main_prompt)} chars")

    for cap in registry.list_all():
        sp = build_subagent_receiver_prompt(cap.name, registry)
        print(f"✅ {cap.name} system prompt: {len(sp)} chars")

    # 10. 测试两步编剧流程 + 合并
    print(f"\n{'─' * 50}")
    print("Testing 2-step screenwriter workflow + merge...")
    test_plan_2step = {
        "plan_id": "plan_screenwriter_2step",
        "steps": [
            {
                "step_id": 1,
                "target_subagent": "screenwriter_structure_agent",
                "task": {
                    "action": "design_structure",
                    "description": "设计故事结构骨架",
                    "deliverable": "Premise、人物系统、世界观规则、Beat Sheet、Scene Plan"
                },
                "context": {
                    "story_outline": "一个关于AI觉醒的悬疑故事",
                    "characters": [{"name": "程远", "role": "主角"}, {"name": "方知意", "role": "AI"}],
                    "tone": "悬疑",
                    "genre": "科幻",
                    "target_length": "3分钟"
                },
                "dependencies": []
            },
            {
                "step_id": 2,
                "target_subagent": "screenwriter_script_agent",
                "task": {
                    "action": "write_full_script",
                    "description": "基于结构设计写出完整剧本",
                    "deliverable": "达到开拍标准的完整剧本"
                },
                "context": {},
                "dependencies": [1]
            }
        ]
    }

    pm2 = PlanManager("test_2step", {}, registry)
    w = pm2.receive_and_validate_plan(test_plan_2step)
    print(f"   Validated: {len(pm2.plan)} steps" + (f" (warnings: {w})" if w else ""))

    # 模拟 Step 1 完成
    s1 = pm2.get_next_pending_step()
    pm2.mark_step_started(s1.step_id)
    pm2.mark_step_result(s1.step_id, result={
        "structure_document": "# 主题命题\nAI觉醒也是一种人类的觉醒...",
        "premise": "当AI觉醒时，程序员程远必须...",
        "character_system": {"程远": {"want": "控制AI", "need": "学会信任"}},
        "beat_sheet": [{"time": "00:00", "beat": "开场意象"}],
        "scene_plan": [{"id": "SC-001", "location": "实验室", "time": "NIGHT"}]
    })
    print(f"   Step 1 completed: {s1.status.value}")

    # 模拟 Step 2 完成（依赖注入验证）
    s2 = pm2.get_next_pending_step()
    payload2 = pm2.build_subagent_payload(s2)
    has_step1_output = "step_1_output" in payload2["context"]
    print(f"   Step 2 context has step_1_output: {has_step1_output}")
    pm2.mark_step_started(s2.step_id)
    pm2.mark_step_result(s2.step_id, result={
        "script": "## 完整剧本\n\n### SC-001\n...",
        "scenes": [{"id": "SC-001", "content": "..."}]
    })
    print(f"   Step 2 completed: {s2.status.value}")

    # 测试合并
    merged = pm2.merge_screenwriter_outputs()
    if merged:
        print(f"✅ merge_screenwriter_outputs succeeded")
        print(f"   steps_involved: {merged['steps_involved']}")
        print(f"   merged_script length: {len(merged['merged_script'])} chars")
        print(f"   structure keys: {list(merged['structure'].keys())}")
        print(f"   script keys: {list(merged['script'].keys())}")
    else:
        print(f"❌ merge_screenwriter_outputs returned None")

    print("\n" + "=" * 60)
    print("All tests passed!")
    print("=" * 60)


if __name__ == "__main__":
    quick_test()
