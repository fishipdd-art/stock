"""Update the existing stock Dify apps without creating duplicate apps.

Run inside the Dify API container, where DB_* environment variables already
point at the production PostgreSQL service.  The operation is transactional
and snapshots affected rows into dated backup tables before changing them.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
from croniter import croniter


APP_FILES = {
    "f2795847-878f-4081-af2d-5b86ce52048c": "00_stock总控工作流.yml",
    "53b81c61-1c08-4df5-a9b7-88ef80ff04a3": "01_stock每日根采集.yml",
    "bff22ccf-43b7-4c8f-89ba-b4c8a0719f2e": "02_news事件抽取.yml",
    "5839980e-0de9-422a-932d-39ead0345983": "03_错配图谱传播.yml",
    "554fca85-0b3b-4c51-a029-fc16cf01f0a8": "04_评分.yml",
    "74d72dbd-2241-43f5-93a9-5c2110758001": "05_持仓诊断早报.yml",
    "d355e28a-a5e0-40a1-8d88-fd0ddd1b4520": "06_盘后复盘.yml",
}


def _connect():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ["DB_DATABASE"],
        user=os.environ["DB_USERNAME"],
        password=os.environ["DB_PASSWORD"],
    )


def _next_run(expression: str, timezone_name: str) -> datetime:
    zone = ZoneInfo(timezone_name)
    local_now = datetime.now(zone)
    next_local = croniter(expression, local_now).get_next(datetime)
    return next_local.astimezone(timezone.utc).replace(tzinfo=None)


def main(dsl_dir: str) -> int:
    root = Path(dsl_dir)
    documents = {
        app_id: json.loads((root / filename).read_text(encoding="utf-8"))
        for app_id, filename in APP_FILES.items()
    }
    affected = tuple(APP_FILES)
    now = datetime.utcnow()

    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS codex_backup_workflows_20260716 "
                "AS SELECT * FROM workflows WHERE app_id = ANY(%s::uuid[])",
                (list(affected),),
            )
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS codex_backup_schedule_plans_20260716 "
                "AS SELECT * FROM workflow_schedule_plans WHERE app_id = ANY(%s::uuid[])",
                (list(affected),),
            )
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS codex_backup_apps_20260716 "
                "AS SELECT * FROM apps WHERE id = ANY(%s::uuid[])",
                (list(affected),),
            )

            for app_id, document in documents.items():
                workflow = document["workflow"]
                graph_json = json.dumps(workflow["graph"], ensure_ascii=False)
                features_json = json.dumps(workflow.get("features") or {}, ensure_ascii=False)
                # Dify 1.15 persists these collections as name-keyed maps,
                # even though exported DSL represents an empty collection as [].
                env_json = json.dumps({}, ensure_ascii=False)
                conv_json = json.dumps({}, ensure_ascii=False)
                rag_json = json.dumps({}, ensure_ascii=False)

                cursor.execute(
                    "SELECT id, tenant_id, created_by FROM workflows "
                    "WHERE app_id=%s AND version='draft' FOR UPDATE",
                    (app_id,),
                )
                draft = cursor.fetchone()
                if draft is None:
                    raise RuntimeError(f"missing draft workflow for app {app_id}")
                draft_id, tenant_id, created_by = draft
                cursor.execute(
                    "UPDATE workflows SET graph=%s, features=%s, environment_variables=%s, "
                    "conversation_variables=%s, rag_pipeline_variables=%s, kind=%s, "
                    "updated_by=%s, updated_at=%s WHERE id=%s",
                    (
                        graph_json, features_json, env_json, conv_json, rag_json,
                        "standard", created_by, now, draft_id,
                    ),
                )

                published_id = str(uuid.uuid4())
                version = now.strftime("%Y-%m-%d %H:%M:%S.%f")
                cursor.execute(
                    "INSERT INTO workflows "
                    "(id,tenant_id,app_id,type,version,graph,features,created_by,created_at,"
                    "updated_by,updated_at,environment_variables,conversation_variables,"
                    "marked_name,marked_comment,rag_pipeline_variables,kind) "
                    "VALUES (%s,%s,%s,'workflow',%s,%s,%s,%s,%s,%s,%s,%s,%s,'v2-quality','',%s,%s)",
                    (
                        published_id, tenant_id, app_id, version, graph_json, features_json,
                        created_by, now, created_by, now, env_json, conv_json, rag_json,
                        "standard",
                    ),
                )
                app = document["app"]
                cursor.execute(
                    "UPDATE apps SET workflow_id=%s,name=%s,description=%s,icon=%s,"
                    "icon_background=%s,updated_at=%s WHERE id=%s",
                    (
                        published_id, app["name"], app.get("description", ""),
                        app.get("icon"), app.get("icon_background"), now, app_id,
                    ),
                )

                cursor.execute("DELETE FROM workflow_schedule_plans WHERE app_id=%s", (app_id,))
                for node in workflow["graph"].get("nodes", []):
                    data = node.get("data") or {}
                    if data.get("type") != "trigger-schedule":
                        continue
                    expression = data["cron_expression"]
                    timezone_name = data.get("timezone") or "Asia/Shanghai"
                    cursor.execute(
                        "INSERT INTO workflow_schedule_plans "
                        "(app_id,node_id,tenant_id,cron_expression,timezone,next_run_at,created_at,updated_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            app_id, node["id"], tenant_id, expression, timezone_name,
                            _next_run(expression, timezone_name), now, now,
                        ),
                    )

    print(f"updated {len(documents)} existing Dify apps in place")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
