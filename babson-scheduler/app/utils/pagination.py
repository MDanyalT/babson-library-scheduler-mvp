"""Simple pagination helpers used across routers."""
from __future__ import annotations
from typing import TypeVar, Generic
from pydantic import BaseModel

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    skip: int
    limit: int


def paginate(items: list, skip: int = 0, limit: int = 200) -> tuple[list, int]:
    """Slice a list and return (page_items, total_count)."""
    return items[skip: skip + limit], len(items)
