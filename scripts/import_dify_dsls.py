"""Import all WF-00..WF-06 + CF-01 DSL files into the running Dify instance.

Logs in via the console API (POST /console/api/login with Base64-encoded
password), then imports each yml in ``dify/`` via
``POST /console/api/apps/imports`` and publishes the resulting app.

Usage::

    .venv/bin/python scripts/import_dify_dsls.py \\
        --email <user@example.com> --password <plaintext-password>
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

import httpx


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DSL_DIR = PROJECT_ROOT / "dify"

DIFY_BASE = "http://localhost"
TARGETS = [
    "01_stock每日根采集.yml",
    "00_stock总控工作流.yml",
    "02_news事件抽取.yml",
    "03_错配图谱传播.yml",
    "04_评分.yml",
    "05_持仓诊断早报.yml",
    "06_盘后复盘.yml",
    "cf_01_投资控制台.yml",
]


def _login(client: httpx.Client, email: str, password: str) -> dict:
    """Log in via /console/api/login and return {access_token, csrf_token}."""
    encoded = base64.b64encode(password.encode("utf-8")).decode("ascii")
    resp = client.post(
        f"{DIFY_BASE}/console/api/login",
        json={"email": email, "password": encoded},
    )
    if resp.status_code != 200:
        raise SystemExit(f"login failed: HTTP {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    if data.get("result") != "success":
        raise SystemExit(f"login returned non-success: {data}")
    # httpx.Client stores cookies on the client; subsequent requests in the
    # same session carry them automatically. Dify's console API also requires
    # X-CSRF-Token + Authorization headers on mutating endpoints, so we lift
    # them out of the cookies dict.
    cookies = dict(client.cookies)
    return {
        "csrf_token": cookies.get("csrf_token") or cookies.get("_csrf_token") or "",
        "access_token": cookies.get("access_token") or "",
    }


def _import_one(client: httpx.Client, dsl_path: Path, csrf: str, access: str) -> dict:
    payload = dsl_path.read_text(encoding="utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf,
    }
    if access:
        headers["Authorization"] = f"Bearer {access}"
    resp = client.post(
        f"{DIFY_BASE}/console/api/apps/imports",
        headers=headers,
        json={"mode": "yaml-content", "yaml_content": payload},
    )
    return {
        "path": dsl_path.name,
        "status": resp.status_code,
        "body": resp.text,
    }


def _publish(client: httpx.Client, app_id: str, csrf: str, access: str) -> dict:
    headers = {"X-CSRF-Token": csrf, "Content-Type": "application/json"}
    if access:
        headers["Authorization"] = f"Bearer {access}"
    # Dify 1.x publish endpoint expects POST + JSON body.
    resp = client.post(
        f"{DIFY_BASE}/console/api/apps/{app_id}/workflows/publish",
        headers=headers,
        json={"marked_name": "v1", "marked_comment": ""},
    )
    return {"publish_status": resp.status_code, "body": resp.text}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True, help="Dify console email")
    parser.add_argument("--password", required=True, help="Dify console password")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(f"importing {len(TARGETS)} DSLs from {DSL_DIR.relative_to(PROJECT_ROOT)}")
    if args.dry_run:
        for n in TARGETS:
            print(f"  - {n}: dry-run (would import)")
        return 0

    with httpx.Client(timeout=60.0) as client:
        try:
            tokens = _login(client, args.email, args.password)
            print(f"logged in (csrf={len(tokens['csrf_token'])}, access={len(tokens['access_token'])})")
        except SystemExit as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        ok = 0
        for name in TARGETS:
            path = DSL_DIR / name
            if not path.exists():
                print(f"  - {name}: MISSING (skip)")
                continue
            try:
                result = _import_one(client, path, tokens["csrf_token"], tokens["access_token"])
            except Exception as exc:
                print(f"  - {name}: HTTP error {exc}")
                continue
            status = result["status"]
            try:
                body = json.loads(result["body"])
            except json.JSONDecodeError:
                body = {"raw": result["body"][:300]}
            app_id = body.get("app_id") or body.get("id") or "?"
            print(f"  - {name}: HTTP {status}, app_id={app_id}")
            if status in (200, 202) and app_id != "?":
                pub = _publish(client, app_id, tokens["csrf_token"], tokens["access_token"])
                print(f"      publish: HTTP {pub['publish_status']}")
                ok += 1

    print(f"done. {ok}/{len(TARGETS)} imported+published.")
    return 0 if ok == len(TARGETS) else 1


if __name__ == "__main__":
    sys.exit(main())
