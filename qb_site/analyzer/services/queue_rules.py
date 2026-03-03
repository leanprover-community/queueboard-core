from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Set

from django.db import models

from core.models import Repository
from analyzer.models import QueueRuleSet
from analyzer.models.queue_rule import resolve_ci_gating_mode


def _normalize_label(name: str) -> str:
    return name.strip().lower()


@dataclass
class QueueRules:
    """In-memory representation of queue rules for a repository."""

    require_open: bool = True
    require_not_draft: bool = True
    require_ci_success: bool = False
    ci_gating_mode: str | None = None
    # All of these labels must be present (AND semantics).
    required_labels: Set[str] | None = None
    # None of these labels may be present.
    forbidden_labels: Set[str] | None = None
    # CI contexts that must succeed for this rule set. Interpretation is delegated
    # to CI helper services; QueueRules treats ``ci_ok`` as an aggregate boolean.
    required_ci_contexts: Set[str] | None = None

    def is_on_queue(
        self,
        *,
        is_open: bool,
        is_draft: bool,
        labels: Iterable[str],
        ci_ok: Optional[bool] = None,
    ) -> bool:
        """Return True if the given state satisfies the queue rules."""
        if self.require_open and not is_open:
            return False
        if self.require_not_draft and is_draft:
            return False

        label_set = {_normalize_label(label_name) for label_name in labels}

        if self.required_labels:
            # Require every configured label to be present.
            if not self.required_labels.issubset(label_set):
                return False
        if self.forbidden_labels:
            # No forbidden labels may be present.
            if label_set & self.forbidden_labels:
                return False

        if self.require_ci_success:
            # CI eligibility is mode-dependent and pre-evaluated by callers.
            if ci_ok is not True:
                return False

        return True


def rules_for_rule_set(obj: QueueRuleSet) -> QueueRules:
    required = {_normalize_label(n) for n in (obj.required_label_names or []) if isinstance(n, str) and n.strip()}
    forbidden = {_normalize_label(n) for n in (obj.forbidden_label_names or []) if isinstance(n, str) and n.strip()}
    required_ci = {_normalize_label(n) for n in (obj.required_ci_contexts or []) if isinstance(n, str) and n.strip()}
    ci_mode = resolve_ci_gating_mode(require_ci_success=obj.require_ci_success, ci_gating_mode=obj.ci_gating_mode)
    return QueueRules(
        require_open=obj.require_open,
        require_not_draft=obj.require_not_draft,
        require_ci_success=ci_mode is not None,
        ci_gating_mode=ci_mode,
        required_labels=required or None,
        forbidden_labels=forbidden or None,
        required_ci_contexts=required_ci or None,
    )


def load_rules_for_repo(repo: Repository, at: Optional[datetime] = None) -> QueueRules:
    """Load the appropriate QueueRuleSet for a repository at time ``at``.

    Behavior
    - If ``at`` is provided, prefer the latest ruleset whose effective window
      contains ``at`` (effective_from <= at < effective_to when set).
    - If no such ruleset exists, fall back to the latest active ruleset by version/id.
    - If no active rulesets exist at all, return default rules (open/not-draft only).
    """
    qs = QueueRuleSet.objects.filter(repository=repo, is_active=True)
    obj = None
    if at is not None:
        qs_eff = qs.filter(
            models.Q(effective_from__isnull=True) | models.Q(effective_from__lte=at),
            models.Q(effective_to__isnull=True) | models.Q(effective_to__gt=at),
        ).order_by("-version", "-id")
        obj = qs_eff.first()
    if obj is None:
        obj = qs.order_by("-version", "-id").first()
    if obj is None:
        return QueueRules()
    return rules_for_rule_set(obj)


__all__ = ["QueueRules", "load_rules_for_repo", "rules_for_rule_set"]
