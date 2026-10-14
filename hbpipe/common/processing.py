"""Section 3 processing logic. Imported unchanged by the device (offline) and the cloud (online),
so both modes produce identical results for identical data.

A `loader(sensor, lo, hi)` returns a list of length hi-lo+1 where element k is the tuple of
channel values for index lo+k, or None when that sample is not available.
"""
from __future__ import annotations

import math
from collections import deque
from fractions import Fraction
from typing import Callable, Optional

SR = 400                 # samples per second = window length
PEAK_LOOKBACK = 800      # samples that must precede a peak
PEAK_MIN_GAP = 100       # minimum distance from the previous peak
HR_WINDOWS = 5           # heart rate looks at the last 5 seconds
CHAIN_WARMUP = 2000      # previous-peak chain starts this many samples before the HR span

Loader = Callable[[int, int, int], list]


def round1(q) -> float:
    """Round to 1 decimal place, half away from zero, computed exactly."""
    n = Fraction(q) * 10
    half = Fraction(1, 2)
    r = math.floor(n + half) if n >= 0 else -math.floor(-n + half)
    return float(Fraction(r, 10))


def s1_range(w: int):
    """Inclusive index range of sensor 1 needed to compute window w."""
    lo = SR * w
    if w >= HR_WINDOWS - 1:
        lo = max(0, SR * (w - (HR_WINDOWS - 1)) - CHAIN_WARMUP - PEAK_LOOKBACK)
    return lo, SR * (w + 1)  # +1 sample: a peak at the window's last index needs its successor


def s2_range(w: int):
    return SR * w, SR * w + SR - 1


def window_ready(cov1, cov2, w: int) -> bool:
    """True when every input of window w is final (data, recorded loss, or past session end).

    Computing earlier would count samples still in transit as missing.
    """
    lo1, hi1 = s1_range(w)
    lo2, hi2 = s2_range(w)
    return cov1.covers(lo1, hi1 + 1) and cov2.covers(lo2, hi2 + 1)


def detect_peaks(x: list, base: int, last: int) -> list:
    """Peaks among indices [base, last] of series x (x[k] is index base+k, None = missing).

    The previous-peak chain starts at `base`; callers start early enough (CHAIN_WARMUP) that
    the chain is synchronised with the full-history chain by the time it matters.
    """
    peaks = []
    prev = None
    dq: deque = deque()   # indices of a decreasing run of values: max of the last 800 at dq[0]
    run = 0               # consecutive available samples ending just before i
    n = len(x)
    for i in range(base, last + 1):
        k = i - base
        v = x[k]
        if v is not None and run >= PEAK_LOOKBACK and k + 1 < n:
            nxt = x[k + 1]
            if (nxt is not None and v > x[k - 1] and v > nxt
                    and 2 * v > x[dq[0] - base]
                    and (prev is None or i - prev >= PEAK_MIN_GAP)):
                peaks.append(i)
                prev = i
        if v is None:
            dq.clear()
            run = 0
        else:
            while dq and x[dq[-1] - base] <= v:
                dq.pop()
            dq.append(i)
            if dq[0] <= i - PEAK_LOOKBACK:
                dq.popleft()
            run += 1
    return peaks


def heart_rate(ch2: list, base: int, w: int) -> Optional[float]:
    """ch2 covers sensor 1 channel 2 from index `base` through SR*(w+1)."""
    if w < HR_WINDOWS - 1:
        return None
    span_lo = SR * (w - (HR_WINDOWS - 1))
    span_hi = SR * (w + 1) - 1
    for i in range(span_lo, span_hi + 1):
        if ch2[i - base] is None:
            return None
    peaks = [p for p in detect_peaks(ch2, base, span_hi) if p >= span_lo]
    if len(peaks) < 2:
        return None
    # 60 / (mean gap in seconds) = 60 * SR * (k-1) / (last - first)
    return round1(Fraction(60 * SR * (len(peaks) - 1), peaks[-1] - peaks[0]))


def _channel_stats(rows: list, nch: int):
    if not rows:
        return None, None, None
    cols = list(zip(*rows))
    return ([round1(Fraction(sum(c), len(c))) for c in cols],
            [min(c) for c in cols],
            [max(c) for c in cols])


def compute_window(load: Loader, w: int, mode: str) -> dict:
    lo1, hi1 = s1_range(w)
    lo2, hi2 = s2_range(w)
    s1 = load(1, lo1, hi1)
    s2 = load(2, lo2, hi2)
    win_lo = SR * w
    win1 = s1[win_lo - lo1: win_lo - lo1 + SR]
    avail1 = [r for r in win1 if r is not None]
    avail2 = [r for r in s2 if r is not None]

    mean1, min1, max1 = _channel_stats(avail1, 3)
    mean2, min2, max2 = _channel_stats(avail2, 2)
    magnitude = None
    if avail2:
        total = math.fsum(math.sqrt(a * a + b * b) for a, b in avail2)
        magnitude = round1(Fraction(total) / len(avail2))
    ch2 = [None if r is None else r[1] for r in s1]

    missing_1 = SR - len(avail1)
    missing_2 = SR - len(avail2)
    return {
        "window": w,
        "missing_1": missing_1,
        "missing_2": missing_2,
        "complete": missing_1 == 0 and missing_2 == 0,
        "mean_1": mean1, "min_1": min1, "max_1": max1,
        "mean_2": mean2, "min_2": min2, "max_2": max2,
        "magnitude": magnitude,
        "heart_rate": heart_rate(ch2, lo1, w),
        "mode": mode,
    }


def last_window(end_1: Optional[int], end_2: Optional[int]) -> Optional[int]:
    """Last window of a session once both sensors have ended; None while still running.

    Returns -1 for an empty session.
    """
    if end_1 is None or end_2 is None:
        return None
    end = max(end_1, end_2)
    return (end - 1) // SR if end > 0 else -1
