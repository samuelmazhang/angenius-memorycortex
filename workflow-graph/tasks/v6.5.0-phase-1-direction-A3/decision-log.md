# Decision Log — v6.5.0-phase-1-direction-A3 (auto-pilot 5/7)

## 11:48 — dispatch #A.3
- claude-code proc_9dc4e448e9b5

## 11:55 — claude-code 跑完
- 完整干完 (scheduler.py 312 行 + state.py 71 行 + test 514 行 + report 247 行)
- 23/23 tests PASS
- exit 0

## 11:55 — 主 arcadia 6 步验收
- 116/116 server/tests/ PASS
- commit f48a04e + merge + task-dir de7dcb8
- mcp-graph finish node_da33db15e3ec

## P32 + P33 + P34 + P35 加固

本 sprint 暴露了几个 auto-pilot 模式的问题, 用户一一指出, 全部修复:

1. **P32 (不每分钟 poll)**: 用户原话"你每分钟去看进度就会卡死主进程吗?"
   - fix: 用 notify_on_complete, 不主动 poll

2. **P33 (cron fallback)**: 用户问"那子代理卡死永远不结束怎么办?"
   - fix: cronjob 66497dff5185 (每 5 分钟) + auto-pilot-monitor.py (8.5KB)
   - 处理 stall: kill + write STALL_REPORT.md + redispatch

3. **P34 (lock-file 互斥)**: 用户问"主控还能主动查看吗?"
   - fix: .auto-pilot-{name}.lock (TTL 10 分钟) 防主 arcadia + cron 双杀

4. **P35 (trigger file 间接通知)**: 用户问"cron 能自动通知主控吗?"
   - fix: auto-pilot-trigger.py 写 trigger file, 主 arcadia 下次 tool_call 前 cat

## 下一步

auto-pilot 6/7: #B graph PPR (HippoRAG, 3-5 人天)
auto-pilot 7/7: #C summary compaction (2-3 人天)
