# A 股供应链错配研究系统

面向个人投资研究的本地系统。系统每天从免费公开来源采集新闻、公告、期货与 A 股行情，识别供需错配，映射行业、ETF 和股票，并结合持仓生成 1～4 周观察建议。系统不自动下单。

所有结论必须带时间、来源、置信度、风险与失效条件；当数据缺失、来源不足、MiniMax 未复核或上下游质量门禁告警时，只允许输出“观察”，不得生成强推荐或自动推送。

## 当前生产架构

```text
Dify（唯一生产调度器）
  05:30 daily_workflow
    期货 → 股票/ETF → 新闻 → 热度 → 事件 → 错配 → 评分 → 持仓诊断
  08:20 build_morning_report
  20:30 build_evening_review
          │
          ▼
Python/FastAPI（确定性、长耗时能力）
  采集、正文处理、去重、数据库、行情指标、图谱传播、评分、回测、报告、飞书
          │
          ▼
SQLite（业务数据、PipelineRun、质量快照、推送审计）
```

- Dify 地址：`http://localhost`
- Python 服务：`http://localhost:8000`
- `SCHEDULER_OWNER=dify` 时 Python APScheduler 强制关闭，只保留人工灾备开关。
- 每次 Pipeline 调用都记录 `run_id`、`business_date`、执行状态、质量状态、数量、耗时和错误。
- “执行成功”与“质量合格”是两个独立状态。

## 核心能力

- 多源新闻采集、正文相关性检查、规范化 URL、同源标题去重与发布时间校验。
- 严格关键词匹配与确定性 `match_key`，避免一条新闻重复膨胀为大量信号。
- 期货、A 股和 ETF 行情采集；行情覆盖范围自动包含知识图谱股票与全部持仓。
- 21 个行业热度 v2：关注度 30% + 市场表现 45% + 证据质量 25%，按单股均值与并列感知百分位计算，前 8 个行业深度处理。
- 事件 → 供需变量 → 价格/库存/产能 → 行业 → 股票的供应链传导路径。
- 候选评分、持仓诊断、早报、盘后复盘、回测和飞书幂等推送。
- Web 控制台展示今日热度、质量门禁、持仓、运行记录与调度归属。

## 快速启动

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt

./start.sh init       # 初始化或幂等更新知识图谱
./start.sh web        # 启动 8000 Web/API 服务
./start.sh stats      # 查看数据库统计
./start.sh report     # 人工生成一次报告
```

生产环境由 Dify 触发，不要同时运行 Python APScheduler。只有在 Dify 故障且人工确认不会重复执行时，才临时将 `SCHEDULER_OWNER` 改为 `python`。

## Pipeline API

```bash
curl -X POST http://localhost:8000/api/v1/pipeline/daily_workflow \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: manual:daily_workflow:2026-07-16' \
  -d '{"business_date":"2026-07-16","wait_for_completion":true,"push":false}'
```

主要接口：

- `POST /api/v1/pipeline/{pipeline_name}`：创建幂等运行。
- `GET /api/v1/runs`：查询 PipelineRun。
- `GET /api/v1/health/data`：查询各数据集最新质量快照。
- `GET /api/v1/portfolio/default`：查询当前持仓与诊断。
- `GET /api/scheduler/status`：查询调度归属和生产计划。

同步等待时，HTTP `200` 表示通过，`206` 表示仅观察，`424` 表示质量阻断。异步 `202` 只表示已受理，Dify 必须继续查询最终状态，不能把回执当成业务成功。

## 质量规则

- 每个运行显式绑定上海时区的 `business_date`，依赖只能读取同一业务日结果。
- 行情覆盖率目标：知识图谱与持仓并集不低于 95%，持仓必须 100%。
- 新闻至少两个有效来源；无发布时间的内容不得伪装为“刚刚发布”。
- 历史信号不能混入当日证据；自动发现但未审核的关键词默认禁用。
- MiniMax 未完成事件复核时，事件、错配、评分和早报沿链路降级为 `warn`。
- 最新评分运行未通过时，持仓只能给出 hold/观察，早报候选清零。
- 飞书按 `run_id + payload_kind + chat` 幂等，失败最多重试 3 次，成功后不重复发送。

## 项目结构

```text
collector/       新闻、期货、股票/ETF 行情采集
processor/       匹配、去重、时间衰减、错配与报告基础逻辑
pipeline/        Dify 可调用的执行器、质量门禁、每日串行工作流
storage/         SQLAlchemy 模型、SQLite 与增量迁移
notifier/        飞书发送、重试与推送审计
scheduler/       Python 灾备调度器（生产默认关闭）
web/             FastAPI 与本地控制台
dify/            工作流 DSL
scripts/         运维、Dify 原位更新与修复脚本
tests/           单元与集成测试
```

## 2026-07-16 验证状态

- 今日热度：计算 21 个类别（隐藏 1 个内部自动发现类别，页面展示 20 个连续排名），前 8 个深度处理，热度范围 27.5～81.5。
- 行情：153 个标的，用户 9 个持仓全部有最新价格。
- 清洗后近 72 小时：816 条新闻中 184 条相关、632 条噪声；183 条有效新闻对应 183 个去重信号。
- 下游重建：67 个事件、12 个错配、30 个评分候选、9 个持仓诊断。
- 当前 Python 未配置 MiniMax，整条研究链正确降级为仅观察；早报候选为 0，未发送飞书。
- Dify 生产计划只有 3 个：05:30 每日工作流、08:20 早报、20:30 复盘。

## 运维注意事项

- 8000 端口为本项目固定端口，不要复用。
- Dify Code 节点不做网络采集或复杂计算；这些能力统一走 Python HTTP API。
- 修改 Dify DSL 后运行 `scripts/update_dify_workflows_in_place.py` 原位更新，避免产生重复应用和重复计划。
- 升级 Dify 后，需重新验证自定义 API 镜像和工作流触发器。

详细调度说明见 [docs/DIFY_SCHEDULE.md](docs/DIFY_SCHEDULE.md)。

---

最后更新：2026-07-16
