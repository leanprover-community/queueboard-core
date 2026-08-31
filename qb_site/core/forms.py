"""Forms over ``core`` models.

``ReviewerPreferenceForm`` is the single definition of "what a reviewer may edit about themselves",
rendered by the reviewer console at ``/console/preferences/`` — see
`docs/design-decisions/022-zulip-prefs-form-design.md`. Keep it auth-agnostic: callers supply the
already-authorized rows and the resolved timezone; nothing here decides *who* may edit *what*. The
paired assembly lives in ``core.services.reviewer_prefs``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import tzinfo

from django import forms
from django.conf import settings
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from core.models import ReviewerPreference
from core.services.reviewer_notification_settings import MAX_AUTO_UNASSIGN_DAYS, parse_notification_policy
from core.services.topic_labels import make_topic_label_matcher

REVIEWER_PREFERENCE_EDITABLE_FIELDS: tuple[str, ...] = (
    "maximum_capacity",
    "max_new_assignments_per_week",
    "auto_assign",
    "assignment_acceptance",
    "notifications_enabled",
    "away_until",
    "preferred_labels",
    "free_form",
    "conflict_of_interest",
)

REVIEWER_PREFERENCE_NON_FORM_FIELDS: tuple[str, ...] = (
    "id",
    "repository",
    "user",
    "created_at",
    "updated_at",
    "notification_settings",
)


def reviewer_preference_unaccounted_fields() -> tuple[set[str], set[str]]:
    model_fields = {field.name for field in ReviewerPreference._meta.fields}
    expected = set(REVIEWER_PREFERENCE_EDITABLE_FIELDS) | set(REVIEWER_PREFERENCE_NON_FORM_FIELDS)
    return (model_fields - expected, expected - model_fields)


def _dedupe_case_insensitive_preserve_first(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


class DelimitedListField(forms.CharField):
    """Accept comma/newline separated text and persist as list[str]."""

    def clean(self, value: object) -> list[str]:
        text = super().clean(value)
        if not text:
            return []
        parts = [chunk.strip() for chunk in re.split(r"[,\n]", text)]
        return _dedupe_case_insensitive_preserve_first(part for part in parts if part)

    def prepare_value(self, value: object) -> str:
        if isinstance(value, list):
            return "\n".join(str(item) for item in value)
        return str(value or "")


def _rate_limit_window_days() -> int:
    """The configured rolling window in days, for the rate-limit field's label and help text.

    Read from settings rather than hardcoded so the reviewer-facing copy always describes the window
    actually being enforced (``analyzer.services.assignment_rate_limit`` reads the same setting for
    the count itself; this module stays free of an ``analyzer`` import).
    """
    return int(settings.ANALYZER_ASSIGNMENT_RATE_WINDOW_DAYS)


def _rate_limit_help_text(*, recent_intake: int | None) -> str:
    """Help text for the rolling-window assignment cap (design doc 054).

    Carries only what the label cannot. The label already says "per N days", so this does not
    restate the cap; it adds the three things a reviewer cannot infer from a number:

    1. the window is *rolling*, not a calendar week — the "why am I blocked, it's Monday" case;
    2. blank means unlimited, which is the opt-in default;
    3. the limit throttles the push only, never PRs they request themselves (design doc 053).

    Then their own trailing intake, because measured median intake is ~2/week against a median
    *worst* week of 5 — a reviewer picking a number without that figure is guessing, and guessing
    badly in either direction (a limit above their peak does nothing; one far below it goes quiet).
    """
    days = _rate_limit_window_days()
    text = (
        f"Counted over a rolling {days} days, not a calendar week. "
        "Leave blank for no limit; PRs you request yourself never count."
    )
    if recent_intake is not None:
        text += f" You have been assigned {recent_intake} new PR{'' if recent_intake == 1 else 's'} in the last {days} days."
    return text


class ReviewerPreferenceForm(forms.ModelForm):
    # Acceptance-gate mode (design doc 050). Exposed as a two-option radio; the values are the
    # model's own choices ("auto"/"confirm") so the ModelForm persists it without any conversion.
    assignment_acceptance = forms.ChoiceField(
        required=True,
        choices=(
            (ReviewerPreference.ACCEPTANCE_AUTO, "Assign PRs to me directly"),
            (ReviewerPreference.ACCEPTANCE_CONFIRM, "Propose PRs and let me accept them first"),
        ),
        widget=forms.RadioSelect,
        label="New assignment handling",
    )
    away_until = forms.DateTimeField(
        required=False,
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(
            attrs={"type": "datetime-local", "step": 60},
            format="%Y-%m-%dT%H:%M",
        ),
    )
    preferred_labels = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple,
        choices=(),
        help_text="Select topic labels used by auto-assignment.",
    )
    conflict_of_interest = DelimitedListField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="GitHub handles of users whose PRs should not be assigned to you (comma or newline separated).",
    )
    stale_nudge_days = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=MAX_AUTO_UNASSIGN_DAYS - 1,
        widget=forms.NumberInput(attrs={"min": 1, "max": MAX_AUTO_UNASSIGN_DAYS - 1, "step": 1}),
    )
    auto_unassign_days = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=MAX_AUTO_UNASSIGN_DAYS,
        widget=forms.NumberInput(attrs={"min": 1, "max": MAX_AUTO_UNASSIGN_DAYS, "step": 1}),
    )

    class Meta:
        model = ReviewerPreference
        fields = REVIEWER_PREFERENCE_EDITABLE_FIELDS
        widgets = {
            "maximum_capacity": forms.NumberInput(attrs={"min": 1, "step": 1}),
            "max_new_assignments_per_week": forms.NumberInput(attrs={"min": 1, "step": 1, "placeholder": "no limit"}),
            "free_form": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(
        self,
        *args: object,
        user_timezone: tzinfo | None = None,
        label_catalog_by_repo: Mapping[int, list[str]] | None = None,
        topic_label_pattern_by_repo: Mapping[int, str] | None = None,
        recent_intake_by_repo: Mapping[int, int] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        # The acceptance-gate mode only does anything when the proposals pipeline is enabled
        # (design doc 050). Hide the control entirely when the feature is off rather than explaining
        # the caveat in help text; the stored value is left untouched and cannot be changed via POST.
        if not bool(getattr(settings, "ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED", False)):
            self.fields.pop("assignment_acceptance", None)
        self._user_timezone = user_timezone or timezone.get_current_timezone()
        self.legacy_preferred_labels: tuple[str, ...] = ()

        repo_id = getattr(self.instance, "repository_id", None)
        catalog_labels = list((label_catalog_by_repo or {}).get(int(repo_id), [])) if repo_id is not None else []
        pattern = (topic_label_pattern_by_repo or {}).get(int(repo_id)) if repo_id is not None else None
        is_topic_label = make_topic_label_matcher(pattern)
        topic_labels = [name for name in catalog_labels if is_topic_label(name)]
        topic_labels = sorted(_dedupe_case_insensitive_preserve_first(topic_labels), key=str.casefold)

        catalog_by_casefold = {name.casefold(): name for name in topic_labels}
        selected_labels = _dedupe_case_insensitive_preserve_first(self.instance.preferred_labels or [])
        selected_values: list[str] = []
        legacy_labels: list[str] = []

        for name in selected_labels:
            canonical = catalog_by_casefold.get(name.casefold())
            if canonical is not None:
                selected_values.append(canonical)
            else:
                selected_values.append(name)
                legacy_labels.append(name)

        self.legacy_preferred_labels = tuple(legacy_labels)
        choices: list[tuple[str, str]] = [(name, name) for name in topic_labels]
        choices.extend((name, f"{name} (legacy: not in synced topic labels)") for name in legacy_labels)
        self.fields["preferred_labels"].choices = choices
        self.initial["preferred_labels"] = selected_values

        tz_label = getattr(self._user_timezone, "key", str(self._user_timezone))
        community_team_page_warning = mark_safe(
            "<b>Publicly visible on <a href='https://leanprover-community.github.io/teams/reviewers.html'>the community team page</a>.</b>"
        )
        self.fields["away_until"].help_text = f"Temporary break end time. Leave blank if active. Interpreted in {tz_label}."
        self.fields["auto_assign"].help_text = "Turn this off to opt out of automatic reviewer assignment for this repository."
        # The two capacity gates sit side by side in the grid and are easy to confuse, so each says
        # which kind of limit it is: this one bounds the PRs held at once (stock), the next bounds
        # how fast new ones arrive (flow). Without this, `maximum_capacity` renders as a bare
        # unexplained number beside a fully-annotated neighbour.
        self.fields[
            "maximum_capacity"
        ].help_text = "How many assigned PRs you can hold at once. Auto-assignment pauses while you are at this number."
        # Label and help text both name the window from the setting that defines it, so the copy
        # cannot drift from the mechanism — and both say "N days" rather than "per week", because
        # the window is rolling (design doc 054).
        rate_window_days = _rate_limit_window_days()
        self.fields["max_new_assignments_per_week"].label = f"Max new assignments per {rate_window_days} days"
        self.fields["max_new_assignments_per_week"].help_text = _rate_limit_help_text(
            recent_intake=(recent_intake_by_repo or {}).get(int(repo_id)) if repo_id is not None else None
        )
        self.fields["notifications_enabled"].help_text = "Enable daily queue nudge notifications for this repository."
        self.fields["free_form"].help_text = format_html(
            "A free form description of your reviewing interests. {}", community_team_page_warning
        )
        # One escalation ladder (nudge at X days, unassign at Y > X), but the two halves answer to
        # different switches, which is exactly what the old split-across-sections layout hid: the
        # nudge is a notification and honours the toggle below, while the auto-unassign is an
        # assignment action that runs regardless of it. Each says which, because a reviewer who
        # turns notifications off would otherwise reasonably expect to stop being unassigned too.
        self.fields[
            "stale_nudge_days"
        ].help_text = "Nudge you when a PR has stayed on queue this many consecutive days. Only sent when notifications are on."
        self.fields["auto_unassign_days"].help_text = (
            f"Unassign you after this many consecutive queue days (maximum {MAX_AUTO_UNASSIGN_DAYS}). "
            "Must be greater than the nudge threshold, and happens whether or not notifications are on."
        )

        policy = parse_notification_policy(self.instance.notification_settings)
        self.initial["stale_nudge_days"] = policy.stale_nudge_days
        self.initial["auto_unassign_days"] = policy.auto_unassign_days
        if topic_labels:
            labels_help = "Select topic labels used by auto-assignment."
        else:
            labels_help = "No synced topic labels found for this repository yet."
        if legacy_labels:
            legacy_csv = ", ".join(legacy_labels)
            labels_help = format_html("{} Legacy saved labels: {}.", labels_help, legacy_csv)
        self.fields["preferred_labels"].help_text = format_html("{} {}", labels_help, community_team_page_warning)

    def clean_away_until(self) -> object:
        value = self.cleaned_data.get("away_until")
        if value is None:
            return None
        if timezone.is_naive(value):
            return timezone.make_aware(value, self._user_timezone)
        return value.astimezone(self._user_timezone)

    def clean_maximum_capacity(self) -> int:
        value = int(self.cleaned_data["maximum_capacity"])
        if value < 1:
            raise forms.ValidationError("Ensure this value is greater than or equal to 1.")
        return value

    def clean_max_new_assignments_per_week(self) -> int | None:
        """Blank clears the limit; a set value must be at least 1.

        ``0`` is rejected rather than accepted as "block everything": a reviewer who wants no
        auto-assignment at all turns off ``auto_assign``, which says so plainly on every surface,
        instead of encoding it as a rate of zero that only the engine gate would explain.
        """
        value = self.cleaned_data.get("max_new_assignments_per_week")
        if value in (None, ""):
            return None
        value = int(value)
        if value < 1:
            raise forms.ValidationError("Ensure this value is greater than or equal to 1, or leave it blank for no limit.")
        return value

    def clean_preferred_labels(self) -> list[str]:
        labels = self.cleaned_data.get("preferred_labels") or []
        return _dedupe_case_insensitive_preserve_first(str(label) for label in labels)

    def clean(self) -> dict[str, object]:
        cleaned_data = super().clean()

        stale_raw = cleaned_data.get("stale_nudge_days")
        unassign_raw = cleaned_data.get("auto_unassign_days")
        policy = parse_notification_policy(
            {
                "stale_nudge_days": stale_raw,
                "auto_unassign_days": unassign_raw,
            }
        )
        cleaned_data["stale_nudge_days"] = policy.stale_nudge_days
        cleaned_data["auto_unassign_days"] = policy.auto_unassign_days

        if stale_raw is not None and unassign_raw is not None and int(unassign_raw) <= int(stale_raw):
            self.add_error("auto_unassign_days", "Auto-unassign days must be greater than stale nudge days.")

        return cleaned_data

    def save(self, commit: bool = True) -> ReviewerPreference:
        instance = super().save(commit=False)
        instance.notification_settings = {
            "stale_nudge_days": int(self.cleaned_data["stale_nudge_days"]),
            "auto_unassign_days": int(self.cleaned_data["auto_unassign_days"]),
        }
        if commit:
            instance.save()
            self.save_m2m()
        return instance
