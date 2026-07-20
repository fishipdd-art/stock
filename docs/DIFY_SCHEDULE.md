# Dify 调度配置（生产环境唯一主调度器）

Python APScheduler 默认关闭；Dify 与 Python 禁止同时调度同一生产任务。

## 生产计划

| 北京时间 | Dify 工作流 | Pipeline | 作用 |
|---|---|---|---|
| 工作日 05:30 | `01_stock每日根采集` | `daily_workflow` | 串行执行期货、股票/ETF、新闻、热度、事件、错配、评分和持仓诊断 |
| 工作日 08:20 | `05_stock持仓诊断早报` | `build_morning_report` | 读取同一 `business_date` 的最终结果，质量通过后才允许推送 |
| 工作日 20:30 | `06_stock盘后复盘` | `build_evening_review` | 盘后验证与简短复盘 |

`02_news事件抽取`、`03_错配图谱传播`、`04_评分` 保留为人工重跑入口，不配置生产定时触发器。`00_stock运行监控` 可人工执行完整日流程或查询运行状态。

## 为什么改成一个每日根工作流

旧版 02～06 各自定时且只记录 HTTP `202`，Dify 可能显示成功，而后台最终质量已经失败。现在 05:30 根工作流按依赖串行创建子 `PipelineRun`，等待每一步最终状态；硬失败立即停止，`warn` 则继续但沿链路保持“仅观察”。

08:20 和 20:30 报告工作流是独立计划，便于采集结束后按固定时间生成并推送，同时复用相同的质量门禁与推送幂等规则。

## 业务日期与幂等

每个请求必须携带上海时区的 `business_date`，依赖检查只接受同一业务日的结果。推荐幂等键格式：

```text
dify:<pipeline>:<YYYY-MM-DD>[:<suffix>]
```

重复键返回原始 `run_id`，不会重复执行或重复推送。异步 HTTP `202` 仅表示已受理；生产 DSL 对长任务使用根工作流，并以最终 `status` 与 `quality_status` 为准。

## 状态语义

| 执行/质量 | HTTP（同步等待） | Dify 行为 |
|---|---:|---|
| `succeeded / pass` | 200 | 可进入下一步；报告可按配置推送 |
| `degraded / warn` | 206 | 继续记录，但只允许观察；报告自动关闭推送 |
| `failed / fail` | 424 | 阻断后续强建议并在监控中告警 |

行情过期、来源不足、依赖告警、MiniMax 未复核等都会触发降级或阻断。

## 调度锁定

`.env` 中保持：

```text
SCHEDULER_OWNER=dify
```

此时 Python 服务不会启动 APScheduler，`GET /api/scheduler/status` 只展示 Dify 的三个生产计划。只有 Dify 故障且人工确认无重复执行风险时，才可临时切换为 `python` 灾备。

## DSL 与原位更新

主要 DSL：

- `dify/00_stock总控工作流.yml`
- `dify/01_stock数据采集执行器.yml`
- `dify/02_news事件抽取.yml`
- `dify/03_错配图谱传播.yml`
- `dify/04_评分.yml`
- `dify/05_持仓诊断早报.yml`
- `dify/06_盘后复盘.yml`

更新已有 Dify 应用时使用：

```bash
.venv/bin/python scripts/update_dify_workflows_in_place.py
```

脚本保留原 app id、更新草稿与已发布工作流，并重建应有的 3 个计划，避免重复应用和重复触发器。执行前会在 Dify 数据库创建带日期的备份表。

## 运维检查

```bash
curl -s http://localhost:8000/api/scheduler/status
curl -s http://localhost:8000/api/v1/health/data
curl -s 'http://localhost:8000/api/v1/runs?limit=20'
```

发布或重启后应确认：

1. 调度归属为 `dify`，Python scheduler 为 disabled。
2. Dify `workflow_schedule_plans` 只有 05:30、08:20、20:30 三条。
3. 没有长时间停留在 `queued/running` 的 PipelineRun。
4. 当日行情覆盖全部持仓，今日热度非空。
5. 质量 `warn/fail` 时早报候选为空且没有飞书成功推送记录。

---

最后更新：2026-07-16
