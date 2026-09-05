"""Shared pattern classifiers used by Slack, harness, and later the web app."""

from signals.patterns import (
    PATTERN_LABELS,
    REPLAY_PATTERNS,
    check_inside_day,
    check_upper_shadow_reversal,
    classify_pattern,
    last_bar_date,
    matches_pattern,
    pattern_on_trailing_window,
    trailing_window,
)

__all__ = [
    "PATTERN_LABELS",
    "REPLAY_PATTERNS",
    "check_inside_day",
    "check_upper_shadow_reversal",
    "classify_pattern",
    "last_bar_date",
    "matches_pattern",
    "pattern_on_trailing_window",
    "trailing_window",
]
