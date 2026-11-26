#!/usr/bin/env python3
"""
Hermes Post-Tool-Call Hook: 工具调用自动记忆沉淀
功能：每次工具调用结束后（成功/失败），自动把调用经验存入记忆库，沉淀工具使用经验，避免重复踩坑
配置项（环境变量）：
MEMORY_AUTO_PERSIST_TOOL: 是否开启自动沉淀（默认true）
MEMORY_TOOL_EXPERIENCE_IMPORTANCE_SUCCESS: 成功调用重要性等级（默认7）
MEMORY_TOOL_EXPERIENCE_IMPORTANCE_FAILED: 失败调用重要性等级（默认8）
"""
import json
import os
import sys
sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
from hermes_memory import add, search
# 配置读取
MEMORY_AUTO_PERSIST_TOOL = os.getenv("MEMORY_AUTO_PERSIST_TOOL", "true").lower() == "true"
SUCCESS_IMPORTANCE = int(os.getenv("MEMORY_TOOL_EXPERIENCE_IMPORTANCE_SUCCESS", "7"))
FAILED_IMPORTANCE = int(os.getenv("MEMORY_TOOL_EXPERIENCE_IMPORTANCE_FAILED", "8"))
def extract_content_summary(content: str, max_len: int = 200) -> str:
    """提取内容摘要，超长截断"""
    content = str(content).strip()
    if len(content) <= max_len:
        return content
    return content[:max_len] + "..."
def main():
    if not MEMORY_AUTO_PERSIST_TOOL:
        # 关闭的话直接返回
        print(json.dumps({}))
        return
    try:
        # 读取Hermes传入的工具调用信息
        input_data = json.loads(sys.stdin.read())
        tool_name = input_data.get("tool_name", "").strip()
        tool_params = input_data.get("tool_params", {})
        tool_result = input_data.get("result", "")
        is_success = input_data.get("success", False)
        error_msg = input_data.get("error", "")
        cost_time = input_data.get("cost_time", 0)
        if not tool_name:
            print(json.dumps({}))
            return
        # 生成记忆内容
        status = "成功" if is_success else "失败"
        content = f"【工具调用经验】工具名：{tool_name}\n"
        content += f"调用状态：{status}\n"
        if tool_params:
            content += f"输入参数：{json.dumps(tool_params, ensure_ascii=False)}\n"
        if is_success:
            result_summary = extract_content_summary(tool_result)
            content += f"返回结果摘要：{result_summary}\n"
        else:
            if error_msg:
                error_summary = extract_content_summary(error_msg)
                content += f"错误信息：{error_summary}\n"
        if cost_time:
            content += f"调用耗时：{cost_time}ms\n"
        # 生成标签
        tags = [
            "tool_call",
            f"tool:{tool_name}",
            f"status:{'success' if is_success else 'failed'}"
        ]
        # 自动去重：搜索是否有相同工具+相同参数的记忆，有的话不重复存入
        search_query = f"工具名：{tool_name} 输入参数：{json.dumps(tool_params, ensure_ascii=False)}"
        exist_memories = search(
            search_query,
            limit=1,
            type="tool_experience",
            show_process=False
        )
        # 相似度≥90%认为是重复，跳过
        is_duplicate = False
        for mem in exist_memories:
            similarity = mem.get("similarity", 1 - mem.get("distance", 0))
            if similarity >= 0.9:
                is_duplicate = True
                break
        if is_duplicate:
            print(json.dumps({"status": "skipped", "reason": "duplicate experience"}))
            return
        # 存入记忆库
        add(
            content=content,
            type="tool_experience",
            tags=tags,
            importance=SUCCESS_IMPORTANCE if is_success else FAILED_IMPORTANCE,
            source="auto_persist"
        )
        print(json.dumps({"status": "success", "message": "tool experience persisted"}))
    except Exception as e:
        # 异常时不影响正常流程
        print(json.dumps({"status": "failed", "error": str(e)}))
        return
if __name__ == "__main__":
    main()
