#!/usr/bin/env python3
"""
Arcadia Pre-Prompt Hook: 记忆自动注入钩子
功能：用户提问后自动语义搜索相关记忆，注入到System Prompt最前面，零侵入，所有模型通用
配置项（环境变量）：
MEMORY_AUTO_INJECT: 是否开启自动注入（默认true）
MEMORY_AUTO_INJECT_LIMIT: 最大注入条数（默认3）
MEMORY_AUTO_INJECT_THRESHOLD: 最小相似度阈值（默认0.5，50%）
MEMORY_AUTO_INJECT_TYPE: 按类型过滤，比如只注入knowledge类型，默认不过滤
"""
import json
import os
import sys
sys.path.insert(0, os.path.expanduser("~/.arcadia/scripts"))
from arcadia_memory import search
# 配置读取
MEMORY_AUTO_INJECT = os.getenv("MEMORY_AUTO_INJECT", "true").lower() == "true"
MEMORY_AUTO_INJECT_LIMIT = int(os.getenv("MEMORY_AUTO_INJECT_LIMIT", "3"))
MEMORY_AUTO_INJECT_THRESHOLD = float(os.getenv("MEMORY_AUTO_INJECT_THRESHOLD", "0.5"))
MEMORY_AUTO_INJECT_TYPE = os.getenv("MEMORY_AUTO_INJECT_TYPE", None)
def main():
    if not MEMORY_AUTO_INJECT:
        # 关闭的话直接返回空，不修改
        print(json.dumps({}))
        return
    try:
        # 读取Arcadia传入的输入
        input_data = json.loads(sys.stdin.read())
        user_input = input_data.get("user_input", "").strip()
        system_prompt = input_data.get("system_prompt", "")
        if not user_input:
            print(json.dumps({}))
            return
        # 搜索相关记忆
        memories = search(
            user_input,
            limit=MEMORY_AUTO_INJECT_LIMIT,
            type=MEMORY_AUTO_INJECT_TYPE,
            trigger_reason="上下文自动注入",
            show_process=False
        )
        # 过滤符合阈值的
        valid_memories = [
            mem for mem in memories
            if mem.get("similarity", 1 - mem.get("distance", 0)) >= MEMORY_AUTO_INJECT_THRESHOLD
        ]
        if not valid_memories:
            print(json.dumps({}))
            return
        # 格式化记忆块
        memory_block = "===== 【自动关联历史记忆，仅供参考】 =====\n"
        for idx, mem in enumerate(valid_memories, 1):
            memory_block += f"{idx}. {mem.get('content', '')}\n"
        memory_block += "===== 【历史记忆结束】 =====\n\n"
        # 注入到system prompt最前面
        new_system_prompt = memory_block + system_prompt
        # 返回修改后的prompt
        print(json.dumps({
            "system_prompt": new_system_prompt
        }))
    except Exception as e:
        # 异常时不修改任何内容，不影响正常对话
        print(json.dumps({}))
        return
if __name__ == "__main__":
    main()
