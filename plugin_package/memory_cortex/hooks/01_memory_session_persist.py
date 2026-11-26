#!/usr/bin/env python3
"""
Arcadia Session End Hook: 会话结束自动记忆沉淀
功能：会话结束时自动把本次对话中的session memory（通过memory工具保存的重要信息）
     提取并持久化到Memory Cortex向量数据库
配置项（环境变量）：
MEMORY_AUTO_SESSION_PERSIST: 是否开启会话结束自动沉淀（默认true）
MEMORY_SESSION_PERSIST_MIN_IMPORTANCE: 最小重要性阈值（默认6）
"""
import json
import os
import sys
sys.path.insert(0, os.path.expanduser("~/.arcadia/scripts"))
from arcadia_memory import add

MEMORY_AUTO_SESSION_PERSIST = os.getenv("MEMORY_AUTO_SESSION_PERSIST", "true").lower() == "true"
SESSION_PERSIST_MIN_IMPORTANCE = int(os.getenv("MEMORY_SESSION_PERSIST_MIN_IMPORTANCE", "6"))

def extract_session_insights(session_memory: str) -> dict:
    """从session memory文本中提取结构化信息，生成记忆条目。"""
    if not session_memory or session_memory.strip() in ("§", "", "No relevant memory."):
        return {"status": "skipped", "reason": "empty session memory"}
    
    # 分割§分隔符，每个块是一个独立的记忆段
    blocks = [b.strip() for b in session_memory.split('§') if b.strip() and b.strip() != '§']
    
    if not blocks:
        return {"status": "skipped", "reason": "no valid blocks"}
    
    persist_results = []
    for i, block in enumerate(blocks):
        # 判断内容重要性
        content = block.strip()
        importance = 7  # 默认重要性
        
        # 根据内容类型判断重要性
        if "修复" in content or "bug" in content.lower() or "已修复" in content:
            importance = 8
        elif "用户偏好" in content or "用户习惯" in content or "用户开发习惯" in content:
            importance = 9
        elif "已知" in content or "注意" in content or "注意**" in content:
            importance = 8
        elif "统计" in content or "数据" in content:
            importance = 6
        
        # 生成记忆内容
        memory_content = f"【会话记忆】\n\n{content}"
        
        # 生成标签
        tags = ["session_memory"]
        if "修复" in content or "bug" in content:
            tags.append("修复")
        if "用户偏好" in content or "用户习惯" in content or "开发习惯" in content:
            tags.append("用户偏好")
        if "已知" in content or "注意" in content:
            tags.append("注意事项")
        if "统计" in content or "数据" in content:
            tags.append("数据统计")
        
        persist_results.append({
            "content": memory_content,
            "type": "knowledge",
            "tags": tags,
            "importance": importance,
            "source": "auto_persist"
        })
    
    return {"status": "ok", "blocks": len(persist_results), "results": persist_results}

def main():
    if not MEMORY_AUTO_SESSION_PERSIST:
        print(json.dumps({"status": "skipped", "reason": "disabled"}))
        return
    
    try:
        # 读取Arcadia传入的session memory
        input_data = json.loads(sys.stdin.read())
        session_memory = input_data.get("session_memory", "")
        session_id = input_data.get("session_id", "")
        turn_count = input_data.get("turn_count", 0)
        
        if not session_memory or session_memory.strip() in ("§", "", "No relevant memory."):
            print(json.dumps({"status": "skipped", "reason": "empty session memory"}))
            return
        
        # 提取session insights
        insights = extract_session_insights(session_memory)
        
        if insights["status"] != "ok":
            print(json.dumps(insights))
            return
        
        # 持久化每条记忆
        persisted = []
        for item in insights["results"]:
            if item["importance"] >= SESSION_PERSIST_MIN_IMPORTANCE:
                try:
                    result = add(
                        content=item["content"],
                        type=item["type"],
                        tags=item["tags"],
                        importance=item["importance"],
                        source=item["source"]
                    )
                    persisted.append({
                        "success": True,
                        "id": result.get("id", "unknown"),
                        "type": item["type"],
                        "importance": item["importance"]
                    })
                except Exception as e:
                    persisted.append({
                        "success": False,
                        "error": str(e)
                    })
            else:
                persisted.append({
                    "success": True,
                    "skipped": True,
                    "reason": f"importance {item['importance']} below threshold"
                })
        
        print(json.dumps({
            "status": "success",
            "total_blocks": insights["blocks"],
            "persisted": persisted,
            "session_id": session_id,
            "turn_count": turn_count
        }))
    except Exception as e:
        print(json.dumps({"status": "failed", "error": str(e)}))

if __name__ == "__main__":
    main()
