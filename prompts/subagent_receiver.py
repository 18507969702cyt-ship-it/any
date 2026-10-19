"""
SubAgent 统一接收契约 Prompt

此 prompt 追加到每个 SubAgent 的 System Prompt 中，
确保所有 SubAgent 以统一格式接收 Runtime 转交的任务。

如果 SubAgent 发现自己的 task.action 不在能力范围内，
应立即返回错误而非空转。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from runtime.subagent_registry import SubAgentRegistry


def build_subagent_receiver_prompt(subagent_name: str, registry: SubAgentRegistry = None) -> str:
    """
    构建 SubAgent 的完整 System Prompt（能力声明 + 任务接收契约）

    Args:
        subagent_name: SubAgent 名称，如 "screenwriter_sub_agent"
        registry: SubAgentRegistry 实例

    Returns:
        完整的 SubAgent System Prompt
    """
    if registry is None:
        registry = SubAgentRegistry()

    return registry.build_system_prompt(subagent_name)


def build_task_receiver_contract() -> str:
    """
    返回所有 SubAgent 共用的任务接收契约文本

    可以直接追加到现有 SubAgent prompt 末尾
    """
    registry = SubAgentRegistry()
    return registry.TASK_RECEIVER_CONTRACT
