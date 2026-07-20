"""One-shot Tavily backfill for news_raw.

For each day in the gap, query Tavily with broad market topics using an
exclusive end_date. Save results to news_raw (deduped by URL).

Idempotent: re-running won't insert duplicates (URL has unique constraint).
"""
from __future__ import annotations
import os, sys, json, time, urllib.request, urllib.error
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

API_KEY = os.environ.get('TAVILY_API_KEY')
if not API_KEY:
    print('ERROR: TAVILY_API_KEY not set'); sys.exit(1)

from storage import get_db
from storage.models import NewsRaw

API_URL = 'https://api.tavily.com/search'
MAX_RESULTS_PER_QUERY = 10
DAYS_GAP_START = date.fromisoformat('2026-07-09')
DAYS_GAP_END   = date.fromisoformat('2026-07-19')  # inclusive

BACKFILL_QUERIES = (
    'A股 市场 板块 龙头 主力资金 证券新闻',
    '中国 宏观 央行 财政 政策 经济数据',
    '美股 美联储 CPI 非农 美债 美元指数',
    '黄金 原油 天然气 铜 有色金属 期货',
    '半导体 芯片 GPU 存储 HBM 人工智能',
    'AI 大模型 DeepSeek AI应用 商业化',
    '商业航天 卫星 军工 国防 装备',
    '新能源 电池 光伏 风电 储能 汽车',
    '医药 创新药 医保 中药 医疗',
    '消费 白酒 食品 农业 生猪 零售',
    '地产 银行 保险 券商 金融',
    '化工 航运 钢铁 水泥 涨价 供需',
)


def tavily_search(query: str, day: date) -> list[dict]:
    next_day = day + timedelta(days=1)
    body = {
        'query': query,
        'max_results': MAX_RESULTS_PER_QUERY,
        'start_date': day.isoformat(),
        'end_date': next_day.isoformat(),
        'include_raw_content': False,
        'topic': 'news',
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        API_URL, data=data, method='POST',
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {API_KEY}',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read()).get('results', [])
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors='replace')[:500]
        raise RuntimeError(f'HTTP {exc.code}: {detail}') from exc


def parse_published_at(value: str | None, day: date) -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    return datetime.combine(day, datetime.min.time()) + timedelta(hours=12)


def main():
    db = get_db()
    queries = BACKFILL_QUERIES
    print(f'>> {len(queries)} broad market queries loaded')

    days = []
    d = DAYS_GAP_START
    while d <= DAYS_GAP_END:
        days.append(d)
        d += timedelta(days=1)
    total_queries = len(days) * len(queries)
    print(f'>> {len(days)} days x {len(queries)} topics = {total_queries} Tavily queries')
    print(f'>> date range: {days[0]} -> {days[-1]}')
    print(f'>> started at {datetime.now().strftime("%H:%M:%S")}\n')

    saved_total = 0
    skipped_dup = 0
    q_count = 0
    t0 = time.time()

    for day in days:
        day_saved = 0
        for query in queries:
            q_count += 1
            try:
                results = tavily_search(query, day)
            except Exception as e:
                print(f'  [WARN] {day} "{query}" failed: {e}')
                continue
            for r in results:
                url = r.get('url') or ''
                if not url:
                    continue
                title = (r.get('title') or '')[:500]
                summary = (r.get('content') or '')[:2000]
                try:
                    published_at = parse_published_at(r.get('published_date'), day)
                except Exception:
                    published_at = datetime.combine(day, datetime.min.time()) + timedelta(hours=12)

                # Insert; URL unique constraint will skip duplicates
                with db.session() as s:
                    existing = s.query(NewsRaw).filter(NewsRaw.url == url).first()
                    if existing:
                        skipped_dup += 1
                        continue
                    row = NewsRaw(
                        url=url[:500],
                        title=title or '(no title)',
                        summary=summary,
                        source='tavily',
                        source_label='Tavily',
                        published_at=published_at,
                        fetched_at=datetime.utcnow(),
                        content='',
                        keywords_matched=query,
                    )
                    s.add(row)
                    s.commit()
                    day_saved += 1
                    saved_total += 1
            # small throttle
            time.sleep(0.15)

        elapsed = time.time() - t0
        rate = q_count / elapsed if elapsed > 0 else 0
        eta_sec = (total_queries - q_count) / rate if rate > 0 else 0
        print(f'  [{day}] saved={day_saved:3d}  total_saved={saved_total:4d}  '
              f'q={q_count}/{total_queries}  elapsed={elapsed:.0f}s  eta={eta_sec:.0f}s')

    print(f'\n=== DONE ===')
    print(f'total_saved={saved_total}  skipped_dup={skipped_dup}  queries={q_count}')
    print(f'elapsed: {(time.time()-t0):.0f}s')


if __name__ == '__main__':
    main()
