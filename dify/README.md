# Stock × Dify 工作流

这些 DSL 与当前本机 Dify 1.15.x 的 Workflow DSL 格式配套。Dify 容器通过
`http://host.docker.internal:8000` 访问 stock API。

导入顺序：

1. `00_stock运行监控.yml`
2. `01_stock数据采集执行器.yml`
3. 后续的新闻分析、图谱传播、持仓诊断和总控工作流

当前 Dify 应用：

- `00_stock运行监控`：`8a332ac9-127f-498f-849f-d06f41db6f60`，已发布并实测。
- `01_stock数据采集执行器`：`53b81c61-1c08-4df5-a9b7-88ef80ff04a3`，已发布并实测。

生产约束：Dify 是唯一主调度器；Python APScheduler 默认关闭。所有写操作
必须使用 `Idempotency-Key`，所有下游节点必须同时检查 `status` 和
`quality_status`。
