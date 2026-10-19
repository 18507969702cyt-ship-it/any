"""
PlanManager — Runtime 层计划管理器

职责：
1. 接收 MainAgent 生成的 plan JSON
2. 校验 plan 合法性（SubAgent 是否存在、action 是否受支持、依赖是否有效）
3. 按序调度 SubAgent 执行（处理依赖注入）
4. 管理 plan 生命周期状态
5. 构建 SubAgent 输入 payload（确保格式与 SubAgent 接收端契约一致）

数据流：
MainAgent ─(submit_plan)─→ PlanManager.receive_and_validate_plan()
    └─ 校验通过 ─→ PlanManager.get_next_pending_step()
        └─ 构建 payload ─→ SubAgent 执行
            └─ 结果回写 ─→ PlanManager.mark_step_result()
                └─ 下一 step 自动注入依赖输出
"""
import json
import uuid
import time
from typing import Optional, Any
from dataclasses import dataclass, field
from enum import Enum

from .subagent_registry import SubAgentRegistry, SubAgentCapability


# ─── 数据模型 ───

class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class TaskSpec:
    """SubAgent 执行任务规格 — 与 SubAgent 接收端契约保持一致"""
    action: str              # 动作类型，必须匹配 SubAgent 能力
    description: str         # 简短任务描述，仅描述"产出什么"
    deliverable: str         # 交付物描述，限定输出格式


@dataclass
class PlanStep:
    """计划中的单步"""
    step_id: int
    target_subagent: str
    task: TaskSpec
    context: dict                        # 执行所需上下文
    dependencies: list[int] = field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    output: Optional[dict] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "target_subagent": self.target_subagent,
            "task": {
                "action": self.task.action,
                "description": self.task.description,
                "deliverable": self.task.deliverable
            },
            "context": self.context,
            "dependencies": self.dependencies,
            "status": self.status.value,
            "output": self.output,
            "error": self.error
        }


# ─── PlanManager ───

class PlanManager:
    """
    Runtime 层计划管理器

    使用方式：
        pm = PlanManager(session_id, session_data)

        # Phase 1: MainAgent 产出 plan
        plan_raw = main_agent_response["submit_plan"]
        warnings = pm.receive_and_validate_plan(plan_raw)
        if warnings:
            # 可选择回传 MainAgent 修正
            pass

        # Phase 2: Runtime 按序调度
        while step := pm.get_next_pending_step():
            payload = pm.build_subagent_payload(step)
            result = await dispatch_to_subagent(step.target_subagent, payload)
            pm.mark_step_result(step.step_id, result)

        # Phase 3: 汇总
        summary = pm.get_plan_summary()
    """

    def __init__(self, session_id: str, session_data: dict, registry: Optional[SubAgentRegistry] = None):
        self.session_id = session_id
        self.session_data = session_data
        self.registry = registry or SubAgentRegistry()
        self.plan: list[PlanStep] = []
        self.plan_id: str = ""
        self.validation_warnings: list[str] = []
        self.created_at: float = 0.0

    # ═══════════════════════════════════════════════
    # Phase 1: MainAgent 生成 Plan → 接收 & 校验
    # ═══════════════════════════════════════════════

    def receive_and_validate_plan(self, raw_plan: dict) -> list[str]:
        """
        接收 LLM 生成的 plan JSON，完成三步校验后持久化。

        校验规则：
        1. steps 至少 1 个
        2. 每个 target_subagent 必须在注册表中存在
        3. 每个 task.action 必须在 SubAgent 能力列表中
        4. dependencies 引用的 step_id 必须存在且不形成环

        Returns:
            警告列表（非阻塞警告，可用于告知 MainAgent 修正）
        Raises:
            ValueError: 致命错误（如 steps 为空）
        """
        self.created_at = time.time()
        self.plan_id = raw_plan.get("plan_id", f"plan_{uuid.uuid4().hex[:12]}")
        self.validation_warnings = []
        steps_raw = raw_plan.get("steps", [])
        warnings: list[str] = []

        if len(steps_raw) == 0:
            raise ValueError("Plan must contain at least 1 step")

        validated_steps: list[PlanStep] = []
        for i, step_raw in enumerate(steps_raw):
            step_id = step_raw.get("step_id", i + 1)
            target = step_raw.get("target_subagent", "")
            task_raw = step_raw.get("task", {})
            action = task_raw.get("action", "")

            # ── 校验 1：SubAgent 是否注册 ──
            capability = self.registry.get(target)
            if capability is None:
                warnings.append(
                    f"[Step {step_id}] Unknown subagent '{target}'. "
                    f"Available: {self.registry.get_subagent_names()}. This step will be skipped."
                )
                validated_steps.append(self._make_skipped_step(step_id, target, task_raw, step_raw, "Unknown subagent"))
                continue

            # ── 校验 2：action 是否在能力范围内 ──
            if action not in capability.actions:
                warnings.append(
                    f"[Step {step_id}] '{target}' does not support action '{action}'. "
                    f"Available actions: {capability.actions}. "
                    f"MainAgent should re-plan this step."
                )
                validated_steps.append(self._make_skipped_step(step_id, target, task_raw, step_raw, f"Unsupported action: {action}"))
                continue

            # ── 校验 3：context 必要字段检查 ──
            context_warnings = self._validate_context(capability, step_raw.get("context", {}))
            for cw in context_warnings:
                warnings.append(f"[Step {step_id}] {cw}")

            # ── 校验 4：dependencies 引用有效性 ──
            deps = step_raw.get("dependencies", [])
            dep_warnings = self._validate_dependencies(deps, step_id)
            warnings.extend(dep_warnings)

            validated_steps.append(PlanStep(
                step_id=step_id,
                target_subagent=target,
                task=TaskSpec(
                    action=action,
                    description=task_raw.get("description", ""),
                    deliverable=task_raw.get("deliverable", "")
                ),
                context=step_raw.get("context", {}),
                dependencies=deps
            ))

        self.plan = validated_steps
        self.validation_warnings = warnings
        self.session_data["plan"] = {
            "plan_id": self.plan_id,
            "steps": [s.to_dict() for s in self.plan],
            "warnings": warnings
        }
        return warnings

    def _make_skipped_step(self, step_id: int, target: str, task_raw: dict, step_raw: dict, reason: str) -> PlanStep:
        """构造一个已跳过的 step"""
        return PlanStep(
            step_id=step_id,
            target_subagent=target,
            task=TaskSpec(
                action=task_raw.get("action", ""),
                description=task_raw.get("description", ""),
                deliverable=task_raw.get("deliverable", "")
            ),
            context=step_raw.get("context", {}),
            dependencies=step_raw.get("dependencies", []),
            status=StepStatus.SKIPPED,
            error=reason
        )

    def _validate_context(self, capability: SubAgentCapability, context: dict) -> list[str]:
        """检查 context 中是否缺少 SubAgent 的必要字段"""
        warnings = []
        input_schema = capability.input_schema.get("context", {})
        for field, schema in input_schema.items():
            if not schema.get("optional", False) and field not in context:
                warnings.append(
                    f"Missing recommended context field '{field}' for {capability.name}. "
                    f"The subagent may have insufficient information to complete the task."
                )
        return warnings

    def _validate_dependencies(self, deps: list[int], current_step_id: int) -> list[str]:
        """校验依赖引用有效性"""
        warnings = []
        existing_ids = {s.step_id for s in self.plan}

        for dep_id in deps:
            if dep_id == current_step_id:
                warnings.append(f"[Step {current_step_id}] Self-dependency detected: step depends on itself")
            elif dep_id >= current_step_id:
                warnings.append(
                    f"[Step {current_step_id}] Future dependency: step {current_step_id} "
                    f"depends on step {dep_id} which hasn't been defined yet"
                )
        return warnings

    # ═══════════════════════════════════════════════
    # Phase 2: Runtime 调度执行
    # ═══════════════════════════════════════════════

    def build_subagent_payload(self, step: PlanStep) -> dict:
        """
        构建给 SubAgent 的标准化输入 payload。

        关键：这里确保格式与 SubAgent 接收端契约（TASK_RECEIVER_CONTRACT）完全一致。

        特殊处理：
        - storyboard_sub_agent：自动注入 CDD计划书 和完整编剧成果（卡片内的完整剧本）
        """
        # 1. 注入依赖步骤的输出
        resolved_context = dict(step.context)
        for dep_id in step.dependencies:
            dep_step = self._find_step(dep_id)
            if dep_step and dep_step.status == StepStatus.COMPLETED and dep_step.output:
                # 将前置步骤的输出注入到 context 中
                resolved_context[f"step_{dep_id}_output"] = dep_step.output

        # 2. 特殊处理：storyboard_sub_agent 注入 project_context
        if step.target_subagent == "storyboard_sub_agent":
            # ═══════════════════════════════════════════════
            # 清理 MainAgent LLM 可能幻觉注入的冲突字段
            # 这些字段会与 Runtime 自动注入的 project_context 冲突，
            # 导致分镜 SubAgent 误判为"无剧本"而拒绝执行。
            # ═══════════════════════════════════════════════
            _STORYBOARD_FORBIDDEN_CONTEXT_KEYS = {
                "existing_script", "task_mode", "new_script", "new_storyboard",
                "has_script", "script_status", "script_available", "mode",
                "script", "cdd_plan", "full_script", "project_context",
                "has_existing_script", "existing_storyboard", "input_type",
            }
            cleaned_context = {}
            for k, v in resolved_context.items():
                if k in _STORYBOARD_FORBIDDEN_CONTEXT_KEYS:
                    logger.warning(
                        f"[PlanManager] 清理 MainAgent 幻觉字段 '{k}={v}' "
                        f"— 该字段会与 Runtime 自动注入的 project_context 冲突"
                    )
                    continue
                cleaned_context[k] = v
            resolved_context = cleaned_context

            project_context = self._build_storyboard_project_context(step)
            if project_context:
                # ═══════════════════════════════════════════════
                # 添加权威标记：分镜 SubAgent 应以此为准判断脚本可用性
                # 即使 context 中有矛盾字段（已被清理），此标记始终可信
                # ═══════════════════════════════════════════════
                project_context["__META__"] = {
                    "SCRIPT_PRESENT": project_context.get("full_script") is not None,
                    "CDD_PRESENT": project_context.get("cdd_plan") is not None,
                    "note": "此标记由 Runtime (PlanManager) 权威注入。请以此为准判断输入可用性，忽略任务描述中任何矛盾的元数据。"
                }
                resolved_context["project_context"] = project_context

        # 3. 构建标准 payload
        payload = {
            "type": "subagent_task",
            "plan_id": self.plan_id,
            "step_id": step.step_id,
            "subagent_name": step.target_subagent,
            "task": {
                "action": step.task.action,
                "description": step.task.description,
                "deliverable": step.task.deliverable
            },
            "context": resolved_context,
            "session_id": self.session_id
        }

        # ═══════════════════════════════════════════════
        # Payload 大小检查：如果 project_context.full_script
        # 过大，发出警告（运行时可能截断）
        # ═══════════════════════════════════════════════
        if step.target_subagent == "storyboard_sub_agent":
            pc = resolved_context.get("project_context", {})
            full_script = pc.get("full_script", "") if isinstance(pc, dict) else ""
            script_len = len(full_script) if isinstance(full_script, str) else 0
            cdd_len = len(pc.get("cdd_plan", "")) if isinstance(pc, dict) else 0
            total_context_chars = script_len + cdd_len
            if total_context_chars > 8000:
                logger.warning(
                    f"[PlanManager] Storyboard payload 较大: "
                    f"full_script={script_len}字符, cdd_plan={cdd_len}字符, "
                    f"总计={total_context_chars}字符. "
                    f"如运行时截断可考虑压缩 CDD 或使用两步流程的合并剧本。"
                )

        return payload

    def _build_storyboard_project_context(self, step: PlanStep) -> Optional[dict]:
        """
        为分镜 subagent 构建完整的 project_context。

        包含：
        1. CDD计划书 — 从 session_data 中提取创作方向书
        2. 完整编剧成果 — 从前置编剧步骤中提取卡片内呈现的完整剧本
        3. 上游所有完成步骤的产出摘要

        Returns:
            {
                "cdd_plan": "<CDD计划书完整文本>",
                "full_script": "<卡片内完整编剧成果>",
                "upstream_outputs": {...}
            }
        """
        result = {}

        # ── 1. 提取 CDD 计划书 ──
        cdd_plan = self._extract_cdd_plan()
        if cdd_plan:
            result["cdd_plan"] = cdd_plan

        # ── 2. 提取完整编剧成果 ──
        full_script = self._extract_full_script()
        if full_script:
            result["full_script"] = full_script

        # ── 3. 收集上游所有完成步骤的产出 ──
        upstream = {}
        for s in self.plan:
            if s.status == StepStatus.COMPLETED and s.output:
                upstream[f"step_{s.step_id}_{s.target_subagent}_{s.task.action}"] = {
                    "subagent": s.target_subagent,
                    "action": s.task.action,
                    "description": s.task.description,
                    "output": s.output
                }
        if upstream:
            result["upstream_outputs"] = upstream

        return result if result else None

    def _extract_cdd_plan(self) -> Optional[str]:
        """
        从 session_data 中提取 CDD 计划书。

        按优先级查找以下键：
        1. cdd / cdd_plan
        2. creative_direction / creative_brief
        3. project_context（如果是字符串）
        4. user_original_request（用户原始创作需求）

        Returns:
            CDD 计划书文本，未找到返回 None
        """
        sd = self.session_data

        # 直接存储的 CDD 计划书
        for key in ("cdd_plan", "cdd", "creative_direction_doc"):
            val = sd.get(key)
            if isinstance(val, str) and val.strip():
                return val
            if isinstance(val, dict):
                # 尝试序列化为 JSON 文本
                import json
                return json.dumps(val, ensure_ascii=False, indent=2)

        # 创作方向书结构体
        creative_dir = sd.get("creative_direction", {})
        if isinstance(creative_dir, dict) and creative_dir:
            import json
            return json.dumps(creative_dir, ensure_ascii=False, indent=2)

        creative_brief = sd.get("creative_brief", {})
        if isinstance(creative_brief, dict) and creative_brief:
            import json
            return json.dumps(creative_brief, ensure_ascii=False, indent=2)

        # project_context 可能是字符串
        pc = sd.get("project_context")
        if isinstance(pc, str) and pc.strip():
            return pc
        if isinstance(pc, dict) and pc:
            import json
            return json.dumps(pc, ensure_ascii=False, indent=2)

        # 兜底：用户原始请求
        user_req = sd.get("user_original_request") or sd.get("user_request")
        if isinstance(user_req, str) and user_req.strip():
            return f"# 用户原始创作需求\n\n{user_req}"

        return None

    def _extract_full_script(self) -> Optional[str]:
        """
        从已完成的编剧步骤中提取完整编剧成果。

        优先级：
        1. 两步编剧流程合并结果（merge_screenwriter_outputs）
        2. 单步编剧 screenwriter_sub_agent 的 full_script 输出
        3. 任一步骤中 script/content/output 字段

        返回的是「卡片内呈现的完整剧本」——即编剧 Agent 以 markdown 格式
        直接交货给用户阅读的那份文本。

        Returns:
            完整剧本 markdown 文本，未找到返回 None
        """
        # ── 尝试两步流程合并 ──
        merged = self.merge_screenwriter_outputs()
        if merged and merged.get("merged_script"):
            return merged["merged_script"]

        # ── 尝试单步编剧 screenwriter_sub_agent ──
        for s in self.plan:
            if (s.target_subagent == "screenwriter_sub_agent"
                    and s.status == StepStatus.COMPLETED
                    and s.output):
                output = s.output
                # screenwriter_sub_agent 的 output_schema 规定 full_script 是唯一交付物
                full_script = output.get("full_script") or output.get("script") or output.get("output") or output.get("content")
                if isinstance(full_script, str) and full_script.strip():
                    return full_script
                # 如果 output 本身就是字符串
                if isinstance(output, str) and output.strip():
                    return output
                # 如果是 dict，尝试序列化
                if isinstance(output, dict):
                    import json
                    return json.dumps(output, ensure_ascii=False, indent=2)

        # ── 尝试两步流程中各步骤的输出 ──
        script_docs = []
        for s in self.plan:
            if (s.target_subagent in ("screenwriter_structure_agent", "screenwriter_script_agent")
                    and s.status == StepStatus.COMPLETED
                    and s.output):
                output = s.output
                doc = output.get("script") or output.get("structure_document") or output.get("output") or output.get("content")
                if isinstance(doc, str) and doc.strip():
                    script_docs.append(f"# Step {s.step_id}: {s.target_subagent}\n\n{doc}")

        if script_docs:
            return "\n\n---\n\n".join(script_docs)

        return None

    def get_next_pending_step(self) -> Optional[PlanStep]:
        """
        获取下一个可执行的 step

        条件：
        1. status == PENDING
        2. 所有依赖的 step 状态为 COMPLETED
        """
        for step in self.plan:
            if step.status != StepStatus.PENDING:
                continue

            deps_met = all(
                self._find_step(d).status == StepStatus.COMPLETED
                for d in step.dependencies
            )
            if deps_met:
                return step
        return None

    def mark_step_started(self, step_id: int):
        """标记 step 开始执行"""
        step = self._find_step(step_id)
        if step:
            step.status = StepStatus.IN_PROGRESS
            step.started_at = time.time()

    def mark_step_result(self, step_id: int, result: dict, error: Optional[str] = None):
        """
        记录 step 执行结果

        Args:
            step_id: 步骤 ID
            result: SubAgent 返回的输出
            error: 如果失败，失败原因
        """
        step = self._find_step(step_id)
        if not step:
            return

        step.finished_at = time.time()

        if error or result.get("error"):
            step.status = StepStatus.FAILED
            step.error = error or result.get("error")
        else:
            step.status = StepStatus.COMPLETED
            step.output = result

        # 同步回 session_data
        self._sync_to_session()

    def get_system_prompt_for(self, subagent_name: str) -> str:
        """获取指定 SubAgent 的完整 system prompt（含能力声明 + 接收契约）"""
        return self.registry.build_system_prompt(subagent_name)

    # ═══════════════════════════════════════════════
    # Phase 3: 汇总
    # ═══════════════════════════════════════════════

    def get_plan_summary(self) -> dict:
        """获取计划执行汇总"""
        total = len(self.plan)
        completed = sum(1 for s in self.plan if s.status == StepStatus.COMPLETED)
        failed = sum(1 for s in self.plan if s.status == StepStatus.FAILED)
        skipped = sum(1 for s in self.plan if s.status == StepStatus.SKIPPED)
        pending = sum(1 for s in self.plan if s.status == StepStatus.PENDING)

        return {
            "plan_id": self.plan_id,
            "total_steps": total,
            "completed": completed,
            "failed": failed,
            "skipped": skipped,
            "pending": pending,
            "steps": [s.to_dict() for s in self.plan],
            "warnings": self.validation_warnings,
            "outputs": {
                s.step_id: s.output
                for s in self.plan
                if s.output is not None
            }
        }

    def merge_screenwriter_outputs(self) -> Optional[dict]:
        """
        将两步编剧流程（Stage 1 结构设计 + Stage 2 剧本写作）的输出合并为统一剧本文档。

        检索 plan 中的 steps，检测是否包含 screenwriter_structure_agent 的 design_structure
        和 screenwriter_script_agent 的 write_full_script。如果两者均完成，将它们的输出
        合并为结构体供 Runtime 下发。

        Returns:
            {
                "merged_script": "<合并后的完整文档>",
                "structure": { ... Stage 1 结构化数据 ... },
                "script": { ... Stage 2 结构化数据 ... },
                "steps_involved": [1, 2]
            }
            如果两步流程未完成，返回 None。
        """
        structure_step = None
        script_step = None

        for s in self.plan:
            if (s.target_subagent == "screenwriter_structure_agent"
                    and s.task.action in ("design_structure", "revise_structure")
                    and s.status == StepStatus.COMPLETED
                    and s.output):
                structure_step = s
            elif (s.target_subagent == "screenwriter_script_agent"
                  and s.task.action in ("write_full_script", "revise_script")
                  and s.status == StepStatus.COMPLETED
                  and s.output):
                script_step = s

        if not structure_step or not script_step:
            return None

        # 提取结构化字段
        structure_output = structure_step.output
        script_output = script_step.output

        structure_doc = structure_output.get("structure_document", "") or structure_output.get("output", "")
        script_doc = script_output.get("script", "") or script_output.get("output", "")

        # 构建合并文档
        merged = []
        if structure_doc:
            merged.append(f"# 故事结构设计\n\n{structure_doc}")
        if script_doc:
            merged.append(f"{'---' if structure_doc else ''}\n\n# 完整剧本\n\n{script_doc}")

        return {
            "merged_script": "\n\n".join(merged),
            "structure": structure_output,
            "script": script_output,
            "steps_involved": [structure_step.step_id, script_step.step_id]
        }

    def is_plan_complete(self) -> bool:
        """计划是否已全部执行完毕（无 pending 和 in_progress 的 step）"""
        return all(
            s.status in (StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED)
            for s in self.plan
        )

    def has_failures(self) -> bool:
        """是否有失败的 step"""
        return any(s.status == StepStatus.FAILED for s in self.plan)

    # ═══════════════════════════════════════════════
    # 内部工具方法
    # ═══════════════════════════════════════════════

    def _find_step(self, step_id: int) -> Optional[PlanStep]:
        for s in self.plan:
            if s.step_id == step_id:
                return s
        return None

    def _sync_to_session(self):
        """将当前 plan 状态同步到 session_data"""
        self.session_data["plan"] = {
            "plan_id": self.plan_id,
            "steps": [s.to_dict() for s in self.plan],
            "warnings": self.validation_warnings
        }
