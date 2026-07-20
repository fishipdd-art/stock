#!/usr/bin/env bash
# macOS / Linux startup script
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Activate venv
if [ ! -d ".venv" ]; then
    echo "[ERROR] Virtual environment not found at $SCRIPT_DIR/.venv"
    echo "Run: uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt"
    exit 1
fi

source .venv/bin/activate

case "${1:-start}" in
    init)
        python main.py init
        ;;
    start)
        echo "Starting scheduler + web (Ctrl+C to stop)..."
        python main.py start "${@:2}"
        ;;
    run)
        echo "Starting scheduler only (Ctrl+C to stop)..."
        python main.py run
        ;;
    once)
        echo "Running all jobs once (smoke test)..."
        python main.py once
        ;;
    backfill)
        shift
        python main.py backfill "$@"
        ;;
    report)
        python main.py report
        ;;
    stats)
        python main.py stats
        ;;
    web)
        shift
        python main.py web "$@"
        ;;
    collect)
        shift
        python main.py collect "$@"
        ;;
    *)
        echo "Usage: $0 {init|start|run|once|backfill|web|collect|report|stats} [args]"
        exit 1
        ;;

esac