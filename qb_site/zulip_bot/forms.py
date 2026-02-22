from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import tzinfo

from django import forms
from django.utils import timezone

from core.models import ReviewerPreference

REVIEWER_PREFERENCE_EDITABLE_FIELDS: tuple[str, ...] = (
    "maximum_capacity",
    "auto_assign",
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
    "notifications_enabled",
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


def _is_assignment_topic_label(name: str | None) -> bool:
    if not name:
        return False
    lowered = name.lower()
    return lowered.startswith("t-") or lowered in {"ci", "imo", "tech debt"}


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


class ReviewerPreferenceForm(forms.ModelForm):
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

    class Meta:
        model = ReviewerPreference
        fields = REVIEWER_PREFERENCE_EDITABLE_FIELDS
        widgets = {
            "maximum_capacity": forms.NumberInput(attrs={"min": 1, "step": 1}),
            "free_form": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(
        self,
        *args: object,
        user_timezone: tzinfo | None = None,
        label_catalog_by_repo: Mapping[int, list[str]] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._user_timezone = user_timezone or timezone.get_current_timezone()
        self.legacy_preferred_labels: tuple[str, ...] = ()

        repo_id = getattr(self.instance, "repository_id", None)
        catalog_labels = list((label_catalog_by_repo or {}).get(int(repo_id), [])) if repo_id is not None else []
        topic_labels = [name for name in catalog_labels if _is_assignment_topic_label(name)]
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
        self.fields["away_until"].help_text = f"Temporary break end time. Leave blank if active. Interpreted in {tz_label}."
        self.fields["auto_assign"].help_text = "Turn this off to opt out of automatic reviewer assignment for this repository."
        self.fields["free_form"].help_text = "A free form description of your reviewing interests."
        if topic_labels:
            labels_help = "Select topic labels used by auto-assignment (`t-*`, `CI`, `IMO`, `tech debt`)."
        else:
            labels_help = "No synced topic labels found for this repository yet."
        if legacy_labels:
            legacy_csv = ", ".join(legacy_labels)
            labels_help = f"{labels_help} Legacy saved labels: {legacy_csv}."
        self.fields["preferred_labels"].help_text = labels_help

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

    def clean_preferred_labels(self) -> list[str]:
        labels = self.cleaned_data.get("preferred_labels") or []
        return _dedupe_case_insensitive_preserve_first(str(label) for label in labels)
