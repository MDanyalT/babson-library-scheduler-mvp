"""Unit tests for time_utils helpers."""
import pytest
from app.utils.time_utils import (
    parse_time_minutes,
    effective_end_min,
    minutes_to_time_str,
    slot_covers_shift,
    shifts_are_consecutive,
    is_overnight_end,
    too_early_after_overnight,
)


def test_parse_time_basic():
    assert parse_time_minutes("07:30") == 450
    assert parse_time_minutes("10:00") == 600
    assert parse_time_minutes("23:00") == 1380
    assert parse_time_minutes("00:00") == 0


def test_parse_time_next_day():
    assert parse_time_minutes("02:00+1") == 1440 + 120
    assert parse_time_minutes("00:00+1") == 1440


def test_parse_time_24():
    assert parse_time_minutes("24:00") == 1440


def test_effective_end_min_same_day():
    assert effective_end_min("07:30", "09:30") == 570


def test_effective_end_min_cross_midnight():
    # 23:00 -> 02:00 crosses midnight; end should be 1440 + 120 = 1560
    result = effective_end_min("23:00", "02:00")
    assert result == 1560


def test_minutes_to_time_str():
    assert minutes_to_time_str(450) == "07:30"
    assert minutes_to_time_str(0) == "00:00"
    assert minutes_to_time_str(1380) == "23:00"
    # Wraps at 1440
    assert minutes_to_time_str(1440) == "00:00"
    assert minutes_to_time_str(1560) == "02:00"


def test_slot_covers_shift_true():
    assert slot_covers_shift(420, 600, 450, 570)  # 7:00–10:00 covers 7:30–9:30


def test_slot_covers_shift_false():
    assert not slot_covers_shift(480, 600, 450, 570)  # 8:00–10:00 does not cover 7:30–9:30


def test_shifts_are_consecutive():
    assert shifts_are_consecutive(570, 575)    # 9:30 end, 9:35 start — within 10 min
    assert shifts_are_consecutive(570, 570)    # exact
    assert not shifts_are_consecutive(570, 700)  # 9:30 end, 11:40 start — too far


def test_is_overnight_end():
    assert is_overnight_end(1560)   # 02:00+1
    assert is_overnight_end(1440)   # 00:00+1 — boundary
    assert not is_overnight_end(1380)  # 23:00 same day


def test_too_early_after_overnight():
    assert too_early_after_overnight(450)    # 7:30 < 10:00 cutoff
    assert not too_early_after_overnight(600)  # 10:00 — at cutoff, not too early
