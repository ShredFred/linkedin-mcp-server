"""Narrow optional parser results while asserting the test's required outcome."""

from typing import TypeVar

T = TypeVar("T")


def require_value(value: T | None) -> T:
    assert value is not None
    return value
