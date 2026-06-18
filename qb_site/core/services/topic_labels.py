"""Per-repository configuration of reviewer "topic" labels.

A reviewer "topic" label is a label that participates in reviewer auto-assignment:
the reviewer preferences form offers these labels for selection, and the assignment
engine matches a PR's topic labels against each reviewer's ``preferred_labels``.

Historically this set was hardcoded as ``t-*`` plus ``CI``/``IMO``/``tech debt``.
It is now configurable per repository via ``Repository.assignment_topic_label_pattern``
(a case-insensitive regex matched against the full label name). An empty/blank
pattern falls back to :data:`DEFAULT_TOPIC_LABEL_PATTERN`.

This module intentionally avoids importing Django models so it can be referenced
from model field validators without creating an import cycle. ``repository`` is
duck-typed (only ``assignment_topic_label_pattern`` is read).
"""

from __future__ import annotations

import logging
import re
from typing import Callable

from django.core.exceptions import ValidationError

logger = logging.getLogger(__name__)

# The default topic-label rule: the legacy hardcoded set (``t-*`` plus
# ``ci``/``imo``/``tech debt``) extended with ``documentation``. Matched
# case-insensitively against the full label name (see :func:`make_topic_label_matcher`).
DEFAULT_TOPIC_LABEL_PATTERN = r"t-.*|ci|imo|tech debt|documentation"

# A matcher takes a (possibly missing) label name and reports whether it is a topic label.
TopicLabelMatcher = Callable[["str | None"], bool]


def compile_topic_label_pattern(pattern: str | None) -> re.Pattern[str]:
    """Compile ``pattern`` (or the default when blank) case-insensitively.

    Raises ``re.error`` if the pattern is not a valid regular expression.
    """
    raw = (pattern or "").strip() or DEFAULT_TOPIC_LABEL_PATTERN
    return re.compile(raw, re.IGNORECASE)


def make_topic_label_matcher(pattern: str | None) -> TopicLabelMatcher:
    """Build a matcher from ``pattern`` (full-match, case-insensitive).

    Raises ``re.error`` if the pattern is invalid; use :func:`topic_label_matcher_for_repo`
    for a non-raising variant that falls back to the default on bad input.
    """
    compiled = compile_topic_label_pattern(pattern)

    def _match(name: str | None) -> bool:
        if not name:
            return False
        return compiled.fullmatch(name) is not None

    return _match


# Module-level matcher implementing the default behavior; safe to reuse as a shared default.
default_topic_label_matcher: TopicLabelMatcher = make_topic_label_matcher(None)


def topic_label_matcher_for_repo(repository: object) -> TopicLabelMatcher:
    """Return a topic-label matcher for ``repository``.

    Non-raising: if the repository's stored pattern is invalid (which model-level
    validation should normally prevent), logs a warning and falls back to the
    default matcher so background assignment builds never crash on bad config.
    """
    pattern = getattr(repository, "assignment_topic_label_pattern", "") or ""
    try:
        return make_topic_label_matcher(pattern)
    except re.error as exc:
        logger.warning(
            "invalid_assignment_topic_label_pattern",
            extra={"repository": str(repository), "pattern": pattern, "error": str(exc)},
        )
        return default_topic_label_matcher


def validate_topic_label_pattern(value: str | None) -> None:
    """Model/field validator: ensure ``value`` is a compilable regex (blank allowed)."""
    if not value:
        return
    try:
        re.compile(value)
    except re.error as exc:
        raise ValidationError(f"Not a valid regular expression: {exc}") from exc
