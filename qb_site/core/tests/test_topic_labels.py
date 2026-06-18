from __future__ import annotations

from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from core.services.topic_labels import (
    DEFAULT_TOPIC_LABEL_PATTERN,
    default_topic_label_matcher,
    make_topic_label_matcher,
    topic_label_matcher_for_repo,
    validate_topic_label_pattern,
)


class DefaultTopicLabelMatcherTests(SimpleTestCase):
    def test_default_matches_legacy_rule(self) -> None:
        match = default_topic_label_matcher
        # t-* prefix (case-insensitive), plus the fixed legacy names.
        self.assertTrue(match("t-algebra"))
        self.assertTrue(match("T-Analysis"))
        self.assertTrue(match("CI"))
        self.assertTrue(match("ci"))
        self.assertTrue(match("IMO"))
        self.assertTrue(match("tech debt"))
        self.assertTrue(match("Tech Debt"))
        self.assertTrue(match("documentation"))
        self.assertTrue(match("Documentation"))

    def test_default_rejects_non_topic_labels(self) -> None:
        match = default_topic_label_matcher
        self.assertFalse(match("maintainer-merge"))
        self.assertFalse(match("WIP"))
        self.assertFalse(match(""))
        self.assertFalse(match(None))
        # Full-match semantics: "t-" must be a prefix, not appear mid-string.
        self.assertFalse(match("not-t-algebra"))
        self.assertFalse(match("ci-extra"))

    def test_blank_pattern_uses_default(self) -> None:
        for blank in (None, "", "   "):
            match = make_topic_label_matcher(blank)
            self.assertTrue(match("t-algebra"))
            self.assertTrue(match("CI"))
            self.assertFalse(match("maintainer-merge"))


class CustomTopicLabelMatcherTests(SimpleTestCase):
    def test_custom_pattern_overrides_default(self) -> None:
        match = make_topic_label_matcher(r"area-.*")
        self.assertTrue(match("area-algebra"))
        self.assertTrue(match("AREA-Topology"))
        # The legacy labels are no longer topic labels under this pattern.
        self.assertFalse(match("t-algebra"))
        self.assertFalse(match("CI"))

    def test_full_match_semantics(self) -> None:
        # fullmatch: anchors implicitly at both ends.
        match = make_topic_label_matcher(r"topic")
        self.assertTrue(match("topic"))
        self.assertFalse(match("topical"))
        self.assertFalse(match("my-topic"))


class TopicLabelMatcherForRepoTests(SimpleTestCase):
    def test_reads_repo_pattern(self) -> None:
        repo = SimpleNamespace(assignment_topic_label_pattern=r"area-.*")
        match = topic_label_matcher_for_repo(repo)
        self.assertTrue(match("area-algebra"))
        self.assertFalse(match("t-algebra"))

    def test_blank_repo_pattern_falls_back_to_default(self) -> None:
        repo = SimpleNamespace(assignment_topic_label_pattern="")
        match = topic_label_matcher_for_repo(repo)
        self.assertTrue(match("t-algebra"))
        self.assertTrue(match("CI"))

    def test_missing_attribute_falls_back_to_default(self) -> None:
        match = topic_label_matcher_for_repo(object())
        self.assertTrue(match("t-algebra"))

    def test_invalid_pattern_falls_back_to_default_without_raising(self) -> None:
        repo = SimpleNamespace(assignment_topic_label_pattern="t-[")  # unbalanced bracket
        match = topic_label_matcher_for_repo(repo)
        # Falls back to the default matcher rather than crashing.
        self.assertTrue(match("t-algebra"))
        self.assertTrue(match("CI"))


class ValidateTopicLabelPatternTests(SimpleTestCase):
    def test_blank_is_allowed(self) -> None:
        validate_topic_label_pattern("")
        validate_topic_label_pattern(None)

    def test_valid_pattern_passes(self) -> None:
        validate_topic_label_pattern(DEFAULT_TOPIC_LABEL_PATTERN)
        validate_topic_label_pattern(r"area-.*|special")

    def test_invalid_pattern_raises_validation_error(self) -> None:
        with self.assertRaises(ValidationError):
            validate_topic_label_pattern("t-[")
