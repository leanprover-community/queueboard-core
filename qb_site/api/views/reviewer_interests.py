from __future__ import annotations

from typing import Any

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import Repository, ReviewerPreference
from syncer.models.label_def import LabelDef


class ReviewerInterestsView(APIView):
    """Return public reviewer interest fields for a repository."""

    authentication_classes: list = []
    permission_classes: list = []

    def get(self, request, *args: Any, **kwargs: Any) -> Response:
        repo_param = request.query_params.get("repo")
        if not repo_param:
            return Response({"detail": "repo query parameter is required"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            owner, name = repo_param.split("/", 1)
        except ValueError:
            return Response({"detail": "repo must be formatted as owner/name"}, status=status.HTTP_400_BAD_REQUEST)

        repo = Repository.objects.filter(owner=owner, name=name).first()
        if repo is None:
            return Response({"detail": "repository not found"}, status=status.HTTP_404_NOT_FOUND)

        # Map lower(name) -> canonical name from the repo's label catalog. Used to drop
        # preferred_labels entries that no longer correspond to a known label and to
        # canonicalize survivors to the casing currently stored in LabelDef.
        canonical_by_lower = {ld.name.lower(): ld.name for ld in LabelDef.objects.filter(repository=repo).only("name")}

        prefs = ReviewerPreference.objects.filter(repository=repo).select_related("user").order_by("user__github_login")
        reviewers = []
        for pref in prefs:
            filtered_labels: list[str] = []
            seen_lower: set[str] = set()
            for label in pref.preferred_labels or []:
                if not isinstance(label, str):
                    continue
                key = label.lower()
                canonical = canonical_by_lower.get(key)
                if canonical is None or key in seen_lower:
                    continue
                filtered_labels.append(canonical)
                seen_lower.add(key)
            reviewers.append(
                {
                    "github_login": pref.user.github_login,
                    "preferred_labels": filtered_labels,
                    "free_form": pref.free_form,
                }
            )

        return Response({"meta": {"repo": repo_param}, "reviewers": reviewers})
