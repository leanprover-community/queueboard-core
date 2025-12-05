from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from analyzer.models import PRDependency
from analyzer.services.dependencies import parse_dependency_numbers, rebuild_pr_dependencies
from core.models import Repository
from syncer.models import PullRequest


class DependencyServiceTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="main", is_active=True)

    def _mk_pr(self, number: int, *, body: str = "") -> PullRequest:
        now = timezone.now()
        return PullRequest.objects.create(
            repository=self.repo,
            number=number,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at=now - timezone.timedelta(days=1),
            gh_updated_at=now - timezone.timedelta(hours=1),
            base_ref_name="main",
            head_ref_name=f"branch-{number}",
            head_repo_owner_login=self.repo.owner,
            head_repo_name=self.repo.name,
            title=f"PR {number}",
            body=body,
            additions=1,
            deletions=0,
            changed_files_count=0,
        )

    def test_parse_dependency_numbers_handles_variations(self) -> None:
        body = """
        Some intro
        - [ ] depends on: #12 extra
        - [x] depends on: #21
          - [ ] depends on: #21
        - [ ] DEPENDS ON: #30
        - [ ] depends on: #notanumber
        """
        numbers = parse_dependency_numbers(body)
        self.assertEqual(numbers, [12, 21, 30])

    def test_rebuild_pr_dependencies_creates_updates_and_deletes(self) -> None:
        pr1 = self._mk_pr(1, body="- [ ] depends on: #1\n- [ ] depends on: #2\n- [x] depends on: #3")
        pr2 = self._mk_pr(2)
        res1 = rebuild_pr_dependencies(pr1)
        self.assertEqual(res1.created, 2)
        self.assertEqual(res1.updated, 0)
        self.assertEqual(res1.deleted, 0)
        self.assertEqual(res1.resolved_numbers, [2])
        self.assertEqual(res1.unresolved_numbers, [3])
        deps = PRDependency.objects.filter(pull_request=pr1).order_by("depends_on_number")
        self.assertEqual(deps.count(), 2)
        self.assertEqual(deps[0].depends_on_pull_request_id, pr2.id)
        self.assertIsNone(deps[1].depends_on_pull_request)

        # Add the missing PR and re-run to update the link.
        pr3 = self._mk_pr(3)
        res2 = rebuild_pr_dependencies(pr1)
        self.assertEqual(res2.created, 0)
        self.assertEqual(res2.updated, 1)
        self.assertEqual(res2.deleted, 0)
        self.assertEqual(set(res2.resolved_numbers), {2, 3})
        deps = {dep.depends_on_number: dep for dep in PRDependency.objects.filter(pull_request=pr1)}
        self.assertEqual(deps[3].depends_on_pull_request_id, pr3.id)

        # Remove a dependency from the body and ensure the edge is deleted.
        pr1.body = "- [ ] depends on: #2"
        pr1.save(update_fields=["body"])
        res3 = rebuild_pr_dependencies(pr1)
        self.assertEqual(res3.created, 0)
        self.assertEqual(res3.updated, 0)
        self.assertEqual(res3.deleted, 1)
        self.assertEqual(list(PRDependency.objects.filter(pull_request=pr1).values_list("depends_on_number", flat=True)), [2])
