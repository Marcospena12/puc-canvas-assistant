from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS_PT = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")
PERIODS = ("today", "tomorrow", "this_week", "next_7_days", "next_30_days")


def get_timezone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_dt(value: str | None) -> datetime | None:
    """Converte uma data ISO do Canvas (UTC) em datetime aware."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def to_utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def local_now(tz: ZoneInfo) -> datetime:
    return datetime.now(UTC).astimezone(tz)


def format_local(value: str | datetime | None, tz: ZoneInfo) -> str:
    dt = parse_dt(value) if isinstance(value, str) or value is None else value
    if dt is None:
        return "sem prazo"
    local = dt.astimezone(tz)
    return f"{WEEKDAYS_PT[local.weekday()]} {local:%d/%m %H:%M}"


def format_date_local(value: str | datetime | None, tz: ZoneInfo) -> str:
    dt = parse_dt(value) if isinstance(value, str) or value is None else value
    if dt is None:
        return "sem data"
    local = dt.astimezone(tz)
    return f"{WEEKDAYS_PT[local.weekday()]} {local:%d/%m/%Y}"


def period_bounds(
    period: str, tz: ZoneInfo, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """Retorna [início, fim) do período, no timezone local."""
    current = (now or utc_now()).astimezone(tz)
    today = current.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == "today":
        return today, today + timedelta(days=1)
    if period == "tomorrow":
        start = today + timedelta(days=1)
        return start, start + timedelta(days=1)
    if period == "this_week":
        days_until_sunday = 6 - today.weekday()
        end = today + timedelta(days=days_until_sunday + 1)
        return today, end
    if period == "next_7_days":
        return today, today + timedelta(days=7)
    if period == "next_30_days":
        return today, today + timedelta(days=30)
    raise ValueError(f"período inválido: {period}")


def period_label(period: str) -> str:
    return {
        "today": "hoje",
        "tomorrow": "amanhã",
        "this_week": "esta semana",
        "next_7_days": "próximos 7 dias",
        "next_30_days": "próximos 30 dias",
    }[period]
