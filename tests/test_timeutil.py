from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from canvas_assistant.timeutil import (
    format_date_local,
    format_local,
    parse_dt,
    period_bounds,
    period_label,
)

TZ = ZoneInfo("America/Sao_Paulo")


def test_parse_and_format_utc_in_local_timezone() -> None:
    parsed = parse_dt("2026-11-10T02:59:00Z")

    assert parsed is not None
    assert format_local(parsed, TZ) == "seg 09/11 23:59"
    assert format_date_local(parsed, TZ) == "seg 09/11/2026"


def test_parse_invalid_date_returns_none() -> None:
    assert parse_dt("not-a-date") is None
    assert parse_dt(None) is None
    assert format_local(None, TZ) == "sem prazo"


def test_period_bounds_today_and_this_week() -> None:
    now = datetime(2026, 11, 9, 10, 30, tzinfo=TZ)

    start, end = period_bounds("today", TZ, now)
    assert start == datetime(2026, 11, 9, tzinfo=TZ)
    assert end == datetime(2026, 11, 10, tzinfo=TZ)

    start, end = period_bounds("this_week", TZ, now)
    assert start == datetime(2026, 11, 9, tzinfo=TZ)
    assert end == datetime(2026, 11, 16, tzinfo=TZ)


def test_period_bounds_tomorrow_and_30_days() -> None:
    now = datetime(2026, 11, 9, 10, 30, tzinfo=TZ)

    start, end = period_bounds("tomorrow", TZ, now)
    assert start == datetime(2026, 11, 10, tzinfo=TZ)
    assert end == datetime(2026, 11, 11, tzinfo=TZ)

    start, end = period_bounds("next_30_days", TZ, now)
    assert (end - start).days == 30


def test_invalid_period_raises() -> None:
    with pytest.raises(ValueError):
        period_bounds("whenever", TZ)


def test_period_labels() -> None:
    assert period_label("today") == "hoje"
    assert period_label("next_7_days") == "próximos 7 dias"
