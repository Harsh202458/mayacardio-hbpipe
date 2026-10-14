"""Sets of covered sample indices, as merged half-open intervals."""
from __future__ import annotations

from bisect import bisect_left, bisect_right

INF = float("inf")


class IntervalSet:
    """Merged, sorted half-open intervals [start, end)."""

    def __init__(self):
        self.starts: list = []
        self.ends: list = []

    def add(self, s, e) -> None:
        if s >= e:
            return
        i = bisect_left(self.ends, s)      # first interval that ends at/after s
        j = bisect_right(self.starts, e)   # intervals that start at/before e
        if i < j:
            s = min(s, self.starts[i])
            e = max(e, self.ends[j - 1])
        self.starts[i:j] = [s]
        self.ends[i:j] = [e]

    def covers(self, s, e) -> bool:
        if s >= e:
            return True
        i = bisect_right(self.starts, s) - 1
        return i >= 0 and self.ends[i] >= e

    def contiguous_end(self, start=0):
        """End of the interval containing `start`, or `start` if not covered."""
        i = bisect_right(self.starts, start) - 1
        if i >= 0 and self.ends[i] > start:
            return self.ends[i]
        return start

    @property
    def max_end(self):
        return self.ends[-1] if self.ends else 0

    def __repr__(self):
        return "IntervalSet(%s)" % list(zip(self.starts, self.ends))


class PrefixCoverage:
    """Device-side coverage: [0, cursor) is final; everything is final once the sensor ended."""

    def __init__(self, cursor: int, ended: bool):
        self.cursor = cursor
        self.ended = ended

    def covers(self, s, e) -> bool:
        return self.ended or e <= self.cursor
