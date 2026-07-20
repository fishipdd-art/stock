"""Allow ``python -m collector.news ...`` invocation."""
from collector.news import main

if __name__ == "__main__":
    raise SystemExit(main())