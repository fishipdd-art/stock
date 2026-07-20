FROM python:3.11-slim AS base

WORKDIR /app

# System deps for AKShare / Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright browser (optional, for JS-rendered sites)
RUN playwright install chromium 2>/dev/null || true

# Copy project
COPY . .

# Ensure data dirs
RUN mkdir -p data/db data/cache data/reports data/logs data/knowledge_graph

# Init schema
RUN .venv/bin/python -c "import sys; sys.path.insert(0, '.'); from storage import init_db; init_db()" 2>/dev/null || \
    python -c "import sys; sys.path.insert(0, '.'); from storage import init_db; init_db()"

EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

# Default: start web (scheduler can be added via CMD override)
CMD ["python", "main.py", "start", "--host", "0.0.0.0", "--port", "8000"]
