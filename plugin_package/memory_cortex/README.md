# Memory Cortex 独立语义记忆服务
100%兼容Hermes原生记忆接口，零侵入替换，支持语义搜索、多Agent共享、完全离线部署

## 部署信息（2026-04-19更新）
- 部署路径：`/root/hermes_plugin/memory_cortex/`
- 服务配置：Systemd系统级监护，开机自启，崩溃自动重启
- 服务地址：`http://localhost:1880`
- 数据存储：
  - 向量数据库：`./data/chroma/`
  - 元数据数据库：`./data/metadata/memory_cortex.db`
  - 离线模型：`./models/`
- Hermes对接：`/root/.hermes/scripts/hermes_memory.py` 已更新对接新服务
- 自动备份：
  - 备份脚本：`./backup.sh`
  - 备份路径：`./Angenius-Agent/memory-cortex/backups/`
  - 自动备份：每天凌晨3点运行，保留7天备份，区分不同实例ID
  - 实例ID规则：`hermes_wsl_主机名`

## 快速使用
```bash
# 查看服务状态
systemctl status memory-cortex
# 查看服务日志
journalctl -u memory-cortex -f
# 手动备份
/root/hermes_plugin/memory_cortex/backup.sh
# 测试记忆添加
python /root/.hermes/scripts/hermes_memory.py add "测试记忆内容" --tags test,demo
# 测试语义搜索
python /root/.hermes/scripts/hermes_memory.py search "测试"
```

## 特性
- ✅ 完全离线可用，不需要访问HuggingFace，模型全部本地存储
- ✅ 100%兼容Hermes原生记忆API，上层代码无需修改
- ✅ 自动语义搜索，比原生关键词搜索更准确
- ✅ 多Agent共享记忆，不同Hermes实例可以访问同一套记忆
- ✅ 双重备份机制：服务不可用时自动切换到本地缓存，恢复后自动同步
- ✅ 自动备份到源码目录，支持多实例区分

## 特性
✅ 纯Python依赖，无需编译，所有平台通用
✅ 100%兼容原有Memory Cortex API接口
✅ 语义相似度检索，支持结构化过滤
✅ 完全独立部署，不侵入原有系统代码
✅ 本地优先，无外部服务依赖
✅ 自动备份简单，直接备份data目录即可

## 快速开始
### 1. 安装依赖
```bash
cd /root/memory-cortex
pip install -r requirements.txt --break-system-packages
```

### 2. 配置
```bash
cp .env.example .env
# 编辑.env文件，修改端口、模型等配置
```

### 3. 启动服务
```bash
# 开发模式
python -m src.api

# 生产模式
uvicorn src.api:app --host 0.0.0.0 --port 1880 --workers 2
```

### 4. 验证
```bash
# 健康检查
curl http://localhost:1880/health

# 创建记忆
curl -X POST http://localhost:1880/chunks \
  -H "Content-Type: application/json" \
  -d '{
    "content": "这是测试记忆内容",
    "type": "knowledge",
    "tags": ["test", "memory"],
    "importance": 6
  }'

# 语义搜索
curl -X POST http://localhost:1880/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "测试记忆",
    "limit": 3
  }'
```

## 接口说明
完全兼容原有API协议：
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /health | 健康检查 |
| POST | /chunks | 创建记忆片段 |
| GET | /chunks/{id} | 获取记忆片段 |
| DELETE | /chunks/{id} | 删除记忆片段 |
| POST | /search | 语义搜索 |
| GET | /list | 列表查询记忆 |
| POST | /reindex | 重建向量索引 |
| GET | /stats | 获取统计信息 |

## 与原有系统集成
只需将原有系统中Memory Cortex的API地址修改为当前服务地址，无需修改其他任何代码，即可无缝替换。

## 数据备份
直接备份`/root/memory-cortex/data/`目录即可，包含所有元数据和向量数据，恢复时直接覆盖即可。

## 性能优化
- 大流量场景下可将Chroma切换为客户端/服务端模式
- 更换更大的Embedding模型提升检索精度（如all-mpnet-base-v2）
- 多Agent系统可共享同一个Memory Cortex服务，实现跨系统知识复用
## 📦 模型下载说明
发布包默认不包含450MB的Embedding模型文件，部署前先执行下载脚本：
```bash
# 国内网络一键下载，不需要翻墙
./download_model.sh
```
下载完成后再执行`./deploy.sh`一键部署即可。
