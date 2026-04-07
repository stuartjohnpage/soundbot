from typing import TypeVar

T = TypeVar("T")


def paginate(items: list[T], per_page: int) -> list[list[T]]:
    """Split a list into pages of at most per_page items each."""
    if not items:
        return []
    return [items[i : i + per_page] for i in range(0, len(items), per_page)]
