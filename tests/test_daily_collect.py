from pipeline import daily_collect


def test_daily_collect_fails_when_any_root_source_is_empty(monkeypatch):
    monkeypatch.setattr(daily_collect, "job_collect_futures", lambda days_back=1: "0 prices")
    monkeypatch.setattr(daily_collect, "job_collect_stocks", lambda: "142 quotes")
    monkeypatch.setattr(daily_collect, "job_collect_news_high", lambda hours_back=24: "12 news")
    result = daily_collect.run_persist()
    assert result["status"] == "failed"
    assert result["quality_status"] == "fail"
    assert result["counts"]["collect_futures"] == 0


def test_daily_collect_passes_with_minimum_root_outputs(monkeypatch):
    monkeypatch.setattr(daily_collect, "job_collect_futures", lambda days_back=1: "83 prices")
    monkeypatch.setattr(daily_collect, "job_collect_stocks", lambda: "142 quotes")
    monkeypatch.setattr(daily_collect, "job_collect_news_high", lambda hours_back=24: "12 news")
    result = daily_collect.run_persist()
    assert result["status"] == "succeeded"
    assert result["quality_status"] == "pass"
