"""Tests for the Excel availability normalizer."""
import pytest
from app.intake.normalizer import parse_availability_level, parse_shift_column_header


def test_parse_level_preferred():
    assert parse_availability_level("P") == "preferred"
    assert parse_availability_level("p") == "preferred"
    assert parse_availability_level("Preferred") == "preferred"
    assert parse_availability_level("preferred") == "preferred"


def test_parse_level_available():
    assert parse_availability_level("A") == "available"
    assert parse_availability_level("a") == "available"
    assert parse_availability_level("Available") == "available"


def test_parse_level_cannot_work():
    assert parse_availability_level("C") == "cannot_work"
    assert parse_availability_level("X") == "cannot_work"
    assert parse_availability_level("x") == "cannot_work"
    assert parse_availability_level("Cannot") == "cannot_work"
    assert parse_availability_level("No") == "cannot_work"


def test_parse_level_none_for_empty():
    assert parse_availability_level(None) is None
    assert parse_availability_level("") is None
    import math
    assert parse_availability_level(float("nan")) is None


def test_parse_shift_header_full():
    result = parse_shift_column_header("Monday 7:30-9:30")
    assert result is not None
    dow, start, end = result
    assert dow == 0  # Monday
    assert start == "07:30"
    assert end == "09:30"


def test_parse_shift_header_am_pm():
    result = parse_shift_column_header("Fri 7:30 AM - 9:30 AM")
    if result is not None:
        dow, start, end = result
        assert dow == 4  # Friday


def test_parse_shift_header_abbreviations():
    result = parse_shift_column_header("Mon 10:00")
    if result is not None:
        dow, start, end = result
        assert dow == 0


def test_parse_shift_header_invalid():
    result = parse_shift_column_header("Student Name")
    assert result is None

    result = parse_shift_column_header("Email")
    assert result is None
