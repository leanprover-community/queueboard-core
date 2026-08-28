"""Assembly of the reviewer-preferences formset (design doc 022).

Consumed by the reviewer console's ``/console/preferences/`` page. It lives in ``core`` — with
``core.forms.ReviewerPreferenceForm`` — because the model is ``core``'s: the caller supplies the rows
it has already authorized, and everything after that (queryset scoping, label catalog, topic-label
pattern, form kwargs) is decided in one place. A second surface, the retired Zulip token page, used
to share it.

Ownership is enforced here rather than at the call site: the queryset is always narrowed to the
supplied rows **and** their owner, on GET and POST alike, so a posted ``form-<n>-id`` naming another
reviewer's row fails validation instead of being saved (Django validates formset ids against the
queryset it was handed). See invariant 3 in design doc 022.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import tzinfo

from django.db.models import Case, IntegerField, QuerySet, When
from django.forms import BaseModelFormSet, modelformset_factory
from django.http import QueryDict

from core.forms import ReviewerPreferenceForm
from core.models import ReviewerPreference, User
from syncer.models import LabelDef

ReviewerPreferenceFormSet = modelformset_factory(
    ReviewerPreference,
    form=ReviewerPreferenceForm,
    extra=0,
    can_delete=False,
)


def preferences_for_user(user: User) -> list[ReviewerPreference]:
    """Every preference row owned by ``user``, ordered by repository.

    The session-authenticated surface edits *current* rows (not a snapshot taken when a link was
    issued), so a repository activated after registration shows up on its own.
    """
    return list(
        ReviewerPreference.objects.filter(user=user)
        .select_related("repository", "user")
        .order_by("repository__owner", "repository__name", "id")
    )


def build_preferences_formset(
    *,
    preferences: Sequence[ReviewerPreference],
    user_timezone: tzinfo,
    data: QueryDict | None = None,
    recent_intake_by_repo: Mapping[int, int] | None = None,
) -> BaseModelFormSet:
    """Build the formset over ``preferences`` (already authorized by the caller).

    ``data`` bound → a POST; ``None`` → a fresh render. ``user_timezone`` is what naive
    ``away_until`` input is interpreted in (see ``zulip_bot.services.user_timezone``).

    ``recent_intake_by_repo`` (``repository_id -> new PRs in the rolling window``) is displayed
    beside the rate-limit field so a reviewer can pick a number against their own history rather
    than blind (design doc 054). It is *supplied by the caller* rather than computed here: the
    figure lives in ``analyzer``, and ``core`` does not import ``analyzer``. Omitting it drops the
    sentence and nothing else.
    """
    return ReviewerPreferenceFormSet(
        data,
        queryset=_ownership_scoped_queryset(preferences),
        form_kwargs={
            "user_timezone": user_timezone,
            "label_catalog_by_repo": _label_catalog_by_repo(preferences),
            "topic_label_pattern_by_repo": _topic_label_pattern_by_repo(preferences),
            "recent_intake_by_repo": dict(recent_intake_by_repo or {}),
        },
    )


def _ownership_scoped_queryset(preferences: Sequence[ReviewerPreference]) -> QuerySet[ReviewerPreference]:
    """The supplied rows, narrowed to their owner and kept in the caller's order."""
    if not preferences:
        return ReviewerPreference.objects.none()
    owner_ids = {int(pref.user_id) for pref in preferences}
    if len(owner_ids) != 1:
        raise ValueError("preferences must all belong to one user")
    pref_ids = [pref.pk for pref in preferences]
    ordering = Case(
        *[When(pk=pref_id, then=position) for position, pref_id in enumerate(pref_ids)],
        output_field=IntegerField(),
    )
    return (
        ReviewerPreference.objects.filter(pk__in=pref_ids, user_id=owner_ids.pop())
        .select_related("repository", "user")
        .order_by(ordering)
    )


def _label_catalog_by_repo(preferences: Sequence[ReviewerPreference]) -> dict[int, list[str]]:
    """``repository_id -> [label name, ...]`` for the repos in play, in one query."""
    repo_ids = sorted({int(pref.repository_id) for pref in preferences})
    catalog: dict[int, list[str]] = {}
    if not repo_ids:
        return catalog
    for repository_id, label_name in LabelDef.objects.filter(repository_id__in=repo_ids).values_list("repository_id", "name"):
        catalog.setdefault(int(repository_id), []).append(str(label_name))
    return catalog


def _topic_label_pattern_by_repo(preferences: Sequence[ReviewerPreference]) -> dict[int, str]:
    return {int(pref.repository_id): (pref.repository.assignment_topic_label_pattern or "") for pref in preferences}


__all__ = [
    "ReviewerPreferenceFormSet",
    "preferences_for_user",
    "build_preferences_formset",
]
