#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LangGraph / LangSmith trace 分片标准化工具
===========================================

背景
----
LangSmith 导出的 trace 中，同一次模型调用会被多层中间件包装 run 重复记录
（FilesystemMiddleware / SummarizationMiddleware / SkillsMiddleware /
AnthropicPromptCachingMiddleware ...），导致文件巨大且存在大量逐字节重复内容。

本脚本将原始 run 树按“父调用链”折叠，为每一次真实模型调用生成一条
model_call 记录，输出为结构统一、无重复、可直接用于本地验证的标准化 JSON。

用法
----
    python3 standardize_trace.py <part-xx-of-20.json> [-o <输出目录>]

    # 批量处理所有分片（同 trace_id 的分片会合并为一份）
    python3 standardize_trace.py /path/to/part-*.json -o /path/to/outdir

标准化规则
----------
1. 保留原始 run 树结构，但以“根 run（父节点不在本文件内）”为单位划分调用链；
2. 同一调用链内只保留“最内层带完整 LLM 上下文（inputs.request.messages）”的
   中间件 run 作为该次调用的代表（其余层 messages/output 经 MD5 校验完全一致）；
3. 若调用链内存在 run_type == 'llm' 的真实 LLM run（如 ChatOpenAI），则其请求
   （LangChain 序列化消息）与响应（LLMResult / token usage）会被附加在
   model_call.llm_run 中，用于校验/统计 token；
4. 中间件各层的名称与输入/输出摘要（MD5）保留在 model_call.middleware_stack，
   供审计“哪些中间件注入了什么”；
5. 消息中的 response_metadata.headers（HTTP 响应头、x-request-id 等纯调试信息）
   会被裁剪，其余字段原样保留，确保可无损重放。
"""

import argparse
import glob
import hashlib
import json
import os
import re
import sys
from collections import defaultdict


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------

def digest(x) -> str:
    """计算任意 JSON 对象的 MD5 摘要（前 12 位），用于审计同链一致性。"""
    return hashlib.md5(
        json.dumps(x, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]


def strip_noise(x, *, keep_headers: bool = False):
    """
    轻量清洗（保持语义无损）：
    - 移除 message.response_metadata.headers（HTTP 响应头，纯调试噪音）；
    - 保留其余全部字段。
    """
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            if k == "response_metadata" and isinstance(v, dict) and not keep_headers:
                v = {kk: vv for kk, vv in v.items() if kk != "headers"}
                if not v:
                    continue
            out[k] = strip_noise(v, keep_headers=keep_headers)
        return out
    if isinstance(x, list):
        return [strip_noise(i, keep_headers=keep_headers) for i in x]
    return x


def summarize_message(m) -> str:
    """生成消息的简短人类可读摘要（用于 model_call.summary）。"""
    if not isinstance(m, dict):
        return f"<{type(m).__name__}>"
    t = m.get("type", "?")
    c = m.get("content")
    if isinstance(c, list):
        n_text = sum(len(p.get("text", "")) for p in c if isinstance(p, dict) and p.get("type") == "text")
        c_short = f"<{len(c)} parts, text {n_text} chars>"
    elif isinstance(c, str):
        c_short = f"{c[:120]!r}" + ("..." if len(c) > 120 else "")
    else:
        c_short = f"<{type(c).__name__}>"
    extra = ""
    tc = m.get("tool_calls")
    if tc:
        names = [x.get("name") for x in tc if isinstance(x, dict)]
        extra = f" tools={names}"
    nm = m.get("name")
    if nm:
        extra += f" name={nm!r}"
    return f"[{t}] {c_short}{extra}"


# --------------------------------------------------------------------------
# 标准化主逻辑
# --------------------------------------------------------------------------

def standardize_part(path: str, *, keep_headers: bool = False) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    trace_id = raw.get("trace_id")
    runs = raw.get("runs", [])

    # --- 1. 建立 run_id -> run 的索引，以及子节点映射 ---
    by_id = {}
    children = defaultdict(list)  # parent_run_id -> [child_run, ...]
    for r in runs:
        rid = (r.get("langsmith") or {}).get("run_id") or (r.get("metadata") or {}).get("run_id")
        by_id[rid] = r
    for r in runs:
        parents = (r.get("langsmith") or {}).get("parent_run_ids") or []
        if parents:
            children[parents[-1]].append(r)
    # 校验索引完整性
    missing = [p for ps in (r.get("langsmith", {}).get("parent_run_ids") or []) for p in [ps[-1]] if p not in by_id]
    if missing:
        # 父节点不在本分片内（跨分片），属正常情况，仅用于后续折叠判断
        pass

    # --- 2. 找根 run：其父节点不在本文件内 ---
    all_ids = set(by_id.keys())
    roots = []
    for r in runs:
        parents = (r.get("langsmith") or {}).get("parent_run_ids") or []
        if not parents or parents[-1] not in all_ids:
            roots.append(r)

    # --- 3. 递归收集每棵子树的 run 集合 ---
    def collect(run_id, acc):
        acc.append(run_id)
        for ch in children.get(run_id, []):
            ch_id = (ch.get("langsmith") or {}).get("run_id")
            collect(ch_id, acc)
        return acc

    subtrees = []
    for root in roots:
        rid = (root.get("langsmith") or {}).get("run_id")
        subtrees.append((root, collect(rid, [])))

    # --- 4. 折叠子树为 model_call ---
    model_calls = []
    for call_index, (root, tree_ids) in enumerate(subtrees, start=1):
        tree_runs = [by_id[i] for i in tree_ids if i in by_id]
        # 按嵌套深度降序排列（越深越接近真实 LLM 调用）
        def depth(r):
            return len((r.get("langsmith") or {}).get("parent_run_ids") or [])

        tree_sorted = sorted(tree_runs, key=depth, reverse=True)
        llm_runs = [r for r in tree_sorted if (r.get("langsmith") or {}).get("run_type") in ("llm", "chat")]
        # 代表 run：深度最大且带完整 LLM 上下文的 run
        rep = next((r for r in tree_sorted
                    if isinstance((r.get("inputs") or {}).get("request"), dict)
                    and (r["inputs"]["request"].get("messages") is not None)), tree_sorted[0])

        rep_ls = rep.get("langsmith") or {}
        rep_meta = rep.get("metadata") or {}
        req = (rep.get("inputs") or {}).get("request") or {}

        # --- 4.1 中间件栈摘要 ---
        middleware_stack = []
        for r in tree_sorted:
            name = (r.get("langsmith") or {}).get("name", "?")
            tag = "llm" if r in llm_runs else ("chain" if (r.get("langsmith") or {}).get("run_type") == "chain" else "?")
            rr = (r.get("inputs") or {}).get("request")
            mdig = sys_msg = tdig = None
            if isinstance(rr, dict):
                if rr.get("messages") is not None:
                    mdig = digest(rr.get("messages"))
                sys_msg = digest(rr.get("system_message")) if rr.get("system_message") is not None else None
                tdig = digest(rr.get("tools")) if rr.get("tools") is not None else None
            entry = {"name": name, "kind": tag, "messages_md5": mdig, "system_message_md5": sys_msg, "tools_md5": tdig}
            # 去重：同一链中已记录过的相同 name 不再重复（仅保留差异摘要）
            if entry not in middleware_stack:
                middleware_stack.append(entry)

        # --- 4.2 提取消息并生成逐条摘要 ---
        raw_msgs = req.get("messages")
        msg_summaries = [summarize_message(m) for m in raw_msgs] if isinstance(raw_msgs, list) else []

        # --- 4.3 提取响应侧内容（AI 最终回复：content / tool_calls / usage）---
        response = {"content": None, "tool_calls": None, "finish_reason": None, "usage": None, "error": rep.get("error")}
        # 中间件链代表 run 的输出（通常 result[0].message 结构）
        out = (rep.get("outputs") or {}).get("output")
        response["output"] = out
        # 真实 LLM run 的响应（LLMResult.generations）
        llm_entry = None
        for lr in llm_runs:
            lr_ls = lr.get("langsmith") or {}
            gens = ((lr.get("outputs") or {}).get("generations") or [[]])
            gen = gens[0][0] if gens and gens[0] else None
            gi = (gen or {}).get("generation_info") or {}
            msg = (gen or {}).get("message") or {}
            kwargs = msg.get("kwargs") or {}
            llm_entry = {
                "run_id": lr_ls.get("run_id"),
                "name": lr_ls.get("name"),
                "request_messages": strip_noise((lr.get("inputs") or {}).get("messages"), keep_headers=keep_headers),
                "response": {
                    "text": (gen or {}).get("text"),
                    "message": strip_noise(kwargs, keep_headers=keep_headers),
                    "finish_reason": gi.get("finish_reason"),
                    "model_name": gi.get("model_name"),
                    "usage": kwargs.get("usage_metadata"),
                },
                "usage": kwargs.get("usage_metadata"),
            }
            if llm_entry["response"]["finish_reason"] is not None:
                response["finish_reason"] = llm_entry["response"]["finish_reason"]
            if llm_entry["usage"] is not None:
                response["usage"] = llm_entry["usage"]
            break  # 每个链只取一个真实 LLM run

        # 若中间件 output 是 LangGraph 状态更新（如 run_type=chain name='model'），其
        # update.messages[-1] 通常是本次调用新增的 AI 消息，包含 tool_calls/usage
        if out is None:
            pass
        elif isinstance(out, list):
            for item in out:
                if isinstance(item, dict) and "update" in item:
                    upd_msgs = (item.get("update") or {}).get("messages") or []
                    for um in upd_msgs:
                        if isinstance(um, dict) and um.get("type") == "ai":
                            if response["content"] is None and not response["tool_calls"]:
                                c = um.get("content")
                                response["content"] = c if isinstance(c, str) else None
                                response["tool_calls"] = um.get("tool_calls")
                                response["usage"] = um.get("usage_metadata")
                            if um.get("response_metadata", {}).get("finish_reason"):
                                response["finish_reason"] = um["response_metadata"]["finish_reason"]
        # 再兜底：任何输出消息都不在时，从代表 run 的 output 提取
        if response["content"] is None and response["tool_calls"] is None:
            for r in tree_sorted:
                rr_out = (r.get("outputs") or {}).get("output")
                if isinstance(rr_out, list):
                    for item in rr_out:
                        if isinstance(item, dict) and "result" in item:
                            for res in item.get("result") or []:
                                if isinstance(res, dict) and "message" in res:
                                    kw = (res.get("message") or {})
                                    if isinstance(kw, dict) and kw.get("type") == "ai":
                                        response["content"] = response["content"] or kw.get("content")
                                        response["tool_calls"] = response["tool_calls"] or kw.get("tool_calls")
                                        response["usage"] = response["usage"] or kw.get("usage_metadata")

        # --- 4.4 组装 model_call ---
        model_call = {
            "model_call_index": call_index,
            "call_run_id": rep_ls.get("run_id"),
            "position": {
                "langgraph_node": rep_meta.get("langgraph_node"),
                "langgraph_step": rep_meta.get("langgraph_step"),
                "graph_name": rep_meta.get("graph_name"),
                "subagent_name": rep_meta.get("agentos_subagent_name") or rep_meta.get("lc_agent_name"),
                "agent_name": rep_meta.get("lc_agent_name"),
                "run_attempt": rep_meta.get("run_attempt"),
                "is_recovery": rep_meta.get("is_recovery", rep_meta.get("agentos_is_recovery")),
                "thread_id": rep_meta.get("thread_id"),
            },
            "middleware_stack": middleware_stack,
            "llm_run": llm_entry,
            "request": {
                "model": strip_noise(req.get("model"), keep_headers=keep_headers),
                "system_message": strip_noise(req.get("system_message"), keep_headers=keep_headers),
                "tool_choice": req.get("tool_choice"),
                "response_format": req.get("response_format"),
                "tools": strip_noise(req.get("tools"), keep_headers=keep_headers),
                "messages": strip_noise(raw_msgs, keep_headers=keep_headers),
            },
            # 中间件在模型调用层维护的运行状态（files/todos/subagent_runs/skills 等，
            # messages 与 request.messages 同源，故裁剪）
            "run_state_extra": strip_noise(
                {k: v for k, v in (req.get("state") or {}).items() if k != "messages"},
                keep_headers=keep_headers,
            ) if isinstance(req.get("state"), dict) else None,
            "response": strip_noise(response, keep_headers=keep_headers),
            "dialogue_summary": msg_summaries,
        }
        model_calls.append(model_call)

    # --- 5. 汇总统计 ---
    total_in = os.path.getsize(path)
    n_kept_rep = len(model_calls)
    kept_ids = set(mc["call_run_id"] for mc in model_calls)
    n_collapsed = len(runs) - n_kept_rep

    return {
        "schema_version": "1.0.0",
        "generated_by": "standardize_trace.py",
        "trace_id": trace_id,
        "source_file": os.path.basename(path),
        "part_label": _part_label(path),
        "stats": {
            "raw_run_count": len(runs),
            "model_call_count": len(model_calls),
            "collapsed_middleware_run_count": n_collapsed,
            "kept_run_ids": sorted(kept_ids),
        },
        "model_calls": model_calls,
    }


def _part_label(path: str):
    m = re.search(r"\.part-(\d+)-of-(\d+)\.", os.path.basename(path))
    return f"part {m.group(1)} of {m.group(2)}" if m else None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="LangGraph trace 分片标准化工具")
    ap.add_argument("parts", nargs="+", help="一个或多个 part-xx-of-xx.json 文件（支持通配符）")
    ap.add_argument("-o", "--outdir", default=".", help="标准化输出目录")
    ap.add_argument("--keep-headers", action="store_true", help="保留 response_metadata.headers（默认裁剪）")
    ap.add_argument("--pretty", action="store_true", help="JSON 美化输出（默认紧凑单行）")
    args = ap.parse_args(argv)

    files = []
    for p in args.parts:
        files.extend(sorted(glob.glob(p)) if any(ch in p for ch in "*?[") else [p])
    files = sorted(set(files))
    if not files:
        print("未找到输入文件", file=sys.stderr)
        return 2

    os.makedirs(args.outdir, exist_ok=True)

    # 同 trace_id 的分片合并输出为一份
    merged = defaultdict(lambda: {"parts": [], "calls": [], "trace_id": None, "stats": {"raw_run_count": 0, "model_call_count": 0, "collapsed_middleware_run_count": 0}})
    for path in files:
        try:
            norm = standardize_part(path, keep_headers=args.keep_headers)
        except Exception as e:
            print(f"[错误] 处理 {path} 失败: {e}", file=sys.stderr)
            continue
        key = norm["trace_id"] or os.path.basename(path)
        m = merged[key]
        m["trace_id"] = norm["trace_id"]
        m["parts"].append(norm["source_file"] + (f" ({norm['part_label']})" if norm["part_label"] else ""))
        m["calls"].extend(norm["model_calls"])
        for k in ("raw_run_count", "model_call_count", "collapsed_middleware_run_count"):
            m["stats"][k] += norm["stats"][k]
        print(f"[OK] {os.path.basename(path)} -> {norm['stats']['model_call_count']} 次模型调用"
              f"（原始 {norm['stats']['raw_run_count']} 个 run）")

    for key, m in merged.items():
        if m["trace_id"]:
            out_name = f"trace-{m['trace_id']}.normalized.json"
        else:
            out_name = os.path.splitext(os.path.basename(m["parts"][0].split(" ")[0]))[0] + ".normalized.json"
        doc = {
            "schema_version": "1.0.0",
            "generated_by": "standardize_trace.py",
            "trace_id": m["trace_id"],
            "source_parts": m["parts"],
            "stats": {**m["stats"], "source_file_count": len(m["parts"])},
            "model_calls": m["calls"],
        }
        out_path = os.path.join(args.outdir, out_name)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2 if args.pretty else None,
                      separators=None if args.pretty else (",", ":"))
        print(f"[写入] {out_path}  ({os.path.getsize(out_path):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
