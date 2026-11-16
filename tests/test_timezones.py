"""Timezones: short names are accepted, and a missing tz database never breaks a page."""

import zoneinfo

import pytest

from nft_ops import scheduler
from nft_ops.scheduler import ScheduleError, Scheduler, get_zone, tz_ok


@pytest.mark.parametrize("name", ["Asia/Jakarta", "asia/jakarta", "asia/jkt", "JKT", " wib "])
def test_jakarta_aliases(name):
    assert tz_ok(name) == "Asia/Jakarta"


def test_unknown_timezone_rejected():
    with pytest.raises(ScheduleError):
        tz_ok("Mars/Olympus")


def test_works_without_tz_database(monkeypatch):
    def missing(key):
        raise zoneinfo.ZoneInfoNotFoundError(f"No time zone found with key {key}")
    monkeypatch.setattr(scheduler, "ZoneInfo", missing)
    assert tz_ok("Asia/Jakarta") == "Asia/Jakarta"
    assert get_zone("asia/jkt").utcoffset(None).total_seconds() == 7 * 3600
    assert Scheduler._when(3600, "Asia/Jakarta") == "01 Jan 1970 08:00 Asia/Jakarta"


def test_bad_saved_timezone_does_not_raise():
    assert Scheduler._when(3600, "Not/AZone") == "01 Jan 1970 01:00 UTC"
