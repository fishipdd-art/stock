"""Import and publish one Dify workflow DSL into an existing local app.

Run this script inside the Dify API container.  It deliberately uses Dify's
own DSL and publishing services so drafts, versions, and trigger metadata stay
consistent; it is safer than modifying workflow rows directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import app as flask_app
from extensions.ext_database import db
from models.account import Account
from models.model import App
from services.app_dsl_service import AppDslService
from services.entities.dsl_entities import ImportMode, ImportStatus
from services.workflow_service import WorkflowService


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit("usage: deploy_dify_workflow.py APP_ID ACCOUNT_ID DSL_PATH")

    app_id, account_id, dsl_path = sys.argv[1:]
    yaml_content = Path(dsl_path).read_text(encoding="utf-8")

    with flask_app.app_context():
        with Session(db.engine, expire_on_commit=False) as session:
            app = session.scalar(select(App).where(App.id == app_id))
            account = session.scalar(select(Account).where(Account.id == account_id))
            if app is None or account is None:
                raise SystemExit("Dify app or account was not found")
            account.set_tenant_id(str(app.tenant_id))

            result = AppDslService(session).import_app(
                account=account,
                import_mode=ImportMode.YAML_CONTENT,
                yaml_content=yaml_content,
                app_id=app_id,
            )
            if result.status not in {ImportStatus.COMPLETED, ImportStatus.COMPLETED_WITH_WARNINGS}:
                session.rollback()
                raise SystemExit(f"DSL import failed: {result.model_dump_json()}")
            session.commit()

        with Session(db.engine, expire_on_commit=False) as session:
            app = session.scalar(select(App).where(App.id == app_id))
            account = session.scalar(select(Account).where(Account.id == account_id))
            if app is None or account is None:
                raise SystemExit("Dify app or account was not found after import")
            account.set_tenant_id(str(app.tenant_id))
            published = WorkflowService().publish_workflow(
                session=session,
                app_model=app,
                account=account,
                marked_name="stock workflow reliability repair",
                marked_comment="Synchronous final-status response; schedules remain disabled pending validation.",
            )
            app.workflow_id = published.id
            app.updated_by = account.id
            session.commit()
            print(f"published app={app_id} workflow={published.id}")


if __name__ == "__main__":
    main()
