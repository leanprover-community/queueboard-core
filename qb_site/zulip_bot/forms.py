from __future__ import annotations

import re
from collections.abc import Iterable
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


class ReviewerPreferenceForm(forms.ModelForm):
    away_until = forms.DateTimeField(
        required=False,
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(
            attrs={"type": "datetime-local", "step": 60},
            format="%Y-%m-%dT%H:%M",
        ),
    )
    preferred_labels = DelimitedListField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="One label per line (or comma-separated).",
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

    def __init__(self, *args: object, user_timezone: tzinfo | None = None, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._user_timezone = user_timezone or timezone.get_current_timezone()
        tz_label = getattr(self._user_timezone, "key", str(self._user_timezone))
        self.fields["away_until"].help_text = f"Temporary break end time. Leave blank if active. Interpreted in {tz_label}."
        self.fields["auto_assign"].help_text = "Turn this off to opt out of automatic reviewer assignment for this repository."
        self.fields["free_form"].help_text = "A free form description of your reviewing interests."

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
