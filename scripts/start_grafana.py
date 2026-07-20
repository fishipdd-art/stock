"""
One-click Grafana setup.

Starts Prometheus + Grafana + imports the dashboard.
Requires Docker Compose.

Usage:
  python scripts/start_grafana.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run shell command."""
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=PROJECT_ROOT, check=check)


def main():
    print("=" * 60)
    print("📊 Stock Analysis System - Grafana Setup")
    print("=" * 60)

    compose = PROJECT_ROOT / "docker-compose.yml"
    if not compose.exists():
        print(f"[ERR] docker-compose.yml not found at {compose}")
        return 1

    # Step 1: Start app + redis + prometheus + grafana
    print("\n[1/4] Starting services (app + redis + prometheus + grafana)...")
    run([
        "docker", "compose",
        "--profile", "monitoring",
        "up", "-d", "--build",
    ])

    # Step 2: Wait for services to be ready
    print("\n[2/4] Waiting for services to be ready...")
    for service in ["stock-app", "stock-prometheus", "stock-grafana"]:
        print(f"  Waiting for {service}...")
        for _ in range(60):
            result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Health.Status}}", service],
                capture_output=True, text=True,
            )
            if "healthy" in result.stdout or "running" in result.stdout:
                break
            time.sleep(2)
        else:
            print(f"  [WARN] {service} not healthy after 120s, continuing...")

    # Step 3: Verify endpoints
    print("\n[3/4] Verifying endpoints...")
    endpoints = [
        ("App", "http://localhost:8000/api/health"),
        ("Prometheus", "http://localhost:9090/-/ready"),
        ("Grafana", "http://localhost:3000/api/health"),
    ]
    import urllib.request
    for name, url in endpoints:
        try:
            r = urllib.request.urlopen(url, timeout=5)
            print(f"  ✅ {name}: {url} (status {r.status})")
        except Exception as e:
            print(f"  ❌ {name}: {url} ({e})")

    # Step 4: Print access info
    print("\n[4/4] Setup complete!")
    print()
    print("=" * 60)
    print("🎉 Access URLs:")
    print("=" * 60)
    print(f"  📊 Grafana:      http://localhost:3000  (admin/admin)")
    print(f"  📈 Prometheus:   http://localhost:9090")
    print(f"  🖥  App:          http://localhost:8000")
    print()
    print("=" * 60)
    print("📋 Grafana Dashboard Import:")
    print("=" * 60)
    print("  Option A (auto-provisioned): Dashboard already loaded!")
    print("    → 打开 Grafana → 左侧 Dashboards → 找 'Stock Analysis System'")
    print()
    print("  Option B (manual import):")
    print("    1. 下载 dashboard: curl http://localhost:8000/api/metrics/dashboard -o stock.json")
    print("    2. 打开 Grafana → 左侧 + → Import → 上传 stock.json")
    print("    3. 选择 Prometheus 数据源 → Import")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())