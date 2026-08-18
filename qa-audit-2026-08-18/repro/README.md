# 复现脚本说明

本目录下的脚本用于复现 `../REPORT.md` 中列出的问题。所有脚本均为独立的一次性诊断脚本，
不属于项目正式测试套件，仅用于本次审计留档和复现验证。

## 运行环境准备

```bash
# 1) 安装依赖（若尚未安装）
uv sync --all-extras

# 2) 启动一个临时 OceanBase 实例（与项目 e2e 测试使用的镜像一致）
docker run -d --name ob-test \
  -p 2881:2881 \
  -e MODE=slim \
  -e OB_DATABASE=powercontext \
  -e OB_TENANT_PASSWORD=powercontext-e2e \
  ghcr.io/oceanbase/oceanbase-ce:4.3.5.6-106000012026040916

# 等待约 30-60 秒直到健康检查通过：
docker exec ob-test obclient -h127.0.0.1 -P2881 -uroot@test -ppowercontext-e2e -Dpowercontext -e 'SELECT 1'

export POWERCONTEXT_TEST_OCEANBASE_URL="mysql+aoceanbase://root%40test:powercontext-e2e@127.0.0.1:2881/powercontext?charset=utf8mb4"
```

如需运行 DeepSeek 相关脚本，请先设置：

```bash
export DEEPSEEK_API_KEY=sk-...   # 你自己的 DeepSeek 兼容 API Key
```

## 脚本清单

| 脚本 | 对应问题 | 依赖 |
| --- | --- | --- |
| `test_rollback_atomicity.py` | 问题 1：OceanBase 事务不会真正回滚 | OceanBase |
| `test_rollback_atomicity_sqlite.py` | 问题 1 对照组：SQLite 回滚正常 | 无（本地 SQLite） |
| `test_for_update_raw.py` | 问题 1：`SELECT ... FOR UPDATE` 行锁未生效 | OceanBase |
| `test_multi_process_capture_race.py` | 问题 2：跨进程并发写入 Source 导致 journal_position 冲突 | OceanBase |
| `test_memory_fts_initialize_race.py` | 问题 3a：Memory 全文索引并发初始化崩溃 | OceanBase |
| `test_experience_fts_initialize_race.py` | 问题 3b：Experience 全文索引并发初始化崩溃 | OceanBase |
| `test_vector_index_clean_race.py` | 问题 3c：向量索引并发初始化崩溃 | OceanBase |
| `test_candidate_pagination_bug.py` | 问题 4：Review Inbox 分页丢失并发新增的候选项 | 无（本地 SQLite） |
| `test_deepseek_direct.py` | 验证：真实 DeepSeek 模型记忆抽取端到端可用（非缺陷，作对照） | DeepSeek API |
| `test_deepseek_injection.py` | 验证：真实 DeepSeek 模型对提示词注入的抵抗力（非缺陷，作对照） | DeepSeek API |

用完后清理容器：

```bash
docker rm -f ob-test
```
