# Dify DSL 导入指南

> 配套脚本：`scripts/import_dify_dsls.py`
> DSL 文件位置：`diy/*.yml`（共 7 个：00_stock总控工作流 / 02..06 / cf_01_投资控制台）

## 方式 A — 浏览器手工导入（推荐，无需凭据）

Dify Console 原生支持拖入 `.yml` 文件：

1. 浏览器打开 <http://localhost/signin>，用 Dify 账号登录。
2. 顶部 "Studio" → 右上角 "**Create App**" → 选择 "**Import from DSL**"。
3. 上传对应的 `dify/02_news事件抽取.yml` 等 7 个文件。
4. 导入完成后在每个 app 详情页点 "**Publish**"。

## 方式 B — 脚本导入（CI 友好）

登录态走 console API，不需要 OAuth；密码走 Base64 编码（与前端一致）：

```bash
.venv/bin/python scripts/import_dify_dsls.py \
    --email 16853653@qq.com \
    --password '<your-password>'
```

可选参数：

| 参数 | 含义 |
|---|---|
| `--dry-run` | 只列出 7 个目标文件，不实际调用 Dify |
| `--email` | Dify console 登录邮箱（必填） |
| `--password` | Dify console 明文密码（脚本内做 Base64） |

成功标准：每个 yml 输出 `HTTP 200, app_id=<uuid>` + `publish: HTTP 200`。

## 导入顺序

任意顺序都可以，但建议按以下顺序（与调度时间表一致）：

1. `00_stock总控工作流.yml`
2. `02_news事件抽取.yml`
3. `03_错配图谱传播.yml`
4. `04_评分.yml`
5. `05_持仓诊断早报.yml`
6. `06_盘后复盘.yml`
7. `cf_01_投资控制台.yml`

## 导入后的步骤

- 在 Dify web UI 给每个 Workflow 创建定时触发器（10 个 cron，参考 `docs/DIFY_SCHEDULE.md` 表格）
- 给 cf_01_投资控制台 添加飞书消息推送（可选）
- 触发一次冒烟：每个 app 点 "Run" → 验证 stock API 返回 200/202 + run_id

## 故障排查

| 现象 | 原因 | 修复 |
|---|---|---|
| HTTP 401 Unauthorized | 邮箱或密码错误 | 用方式 A，或确认密码正确 |
| HTTP 400 "Import failed" | DSL 文件结构不符合 Dify schema | 重新运行 `scripts/build_dify_dsls.py` 重新生成 |
| HTTP 422 "Validation error" | mode 字段值不合法 | 确认 yaml_content 非空且 base64 不是必需 |
| publish HTTP 409 | app 已经有同名发布版本 | 在 console UI 取消旧发布再运行 |

## 与 stock API 的网络连通性

`dify/` 下的所有 DSL 都把 `host.docker.internal:8000` 作为 stock API 地址。验证容器内可达：

```bash
docker exec docker-api-1 curl -s -o /dev/null -w "%{http_code}\n" \
    http://host.docker.internal:8000/api/v1/health/data
```

预期：`200`。

## 凭据安全

- 不要把 `--password` 写到任何脚本或文档里。
- 生产环境使用 Dify 提供的 service API token（`POST /console/api/apps/{app_id}/api-keys`），它不需要用户名密码。
- `scripts/import_dify_dsls.py` 支持从 `DIFY_API_TOKEN` 环境变量读取 token（未来增强）。