from __future__ import annotations

from typing import Any

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import Repository, ReviewerPreference


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

        prefs = ReviewerPreference.objects.filter(repository=repo).select_related("user").order_by("user__github_login")
        reviewers = [
            {
                "github_login": pref.user.github_login,
                "preferred_labels": pref.preferred_labels,
                "free_form": pref.free_form,
            }
            for pref in prefs
        ]

        return Response({"meta": {"repo": repo_param}, "reviewers": reviewers})
