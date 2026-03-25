from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone

from django.test import TestCase

from analyzer.models import PRQueueWindow, QueueRuleSet
from analyzer.models.queue_snapshot import QueueSnapshot
from analyzer.services.pr_info import (
    get_pr_queue_info,
    off_queue_reasons_from_labels,
)
from analyzer.services.queue_rules import QueueRules
from core.models import Repository, User
from analyzer.models import PRDependency
from syncer.models import LabelDef, PRLabel, PullRequest
from syncer.models.commit_check_run import CommitCheckRun
from syncer.models.ci_enums import CheckRunConclusion, CheckRunStatus


def _dt(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=dt_timezone.utc)


def _mk_pr(
    repo: Repository,
    number: int,
    *,
    state: str = "open",
    is_draft: bool = False,
    author: User | None = None,
    assignees: list[str] | None = None,
    merged_at: datetime | None = None,
    closed_at: datetime | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    ci_state: str | None = None,
    head_sha: str | None = None,
) -> PullRequest:
    now = _dt(2026, 3, 1)
    return PullRequest.objects.create(
        repository=repo,
        number=number,
        author=author,
        state=state,
        is_draft=is_draft,
        gh_created_at=created_at or now - timedelta(days=30),
        gh_updated_at=updated_at or now,
        closed_at=closed_at,
        merged_at=merged_at,
        base_ref_name="master",
        head_ref_name=f"branch-{number}",
        head_repo_owner_login="leanprover-community",
        head_repo_name="mathlib4",
        title=f"PR title {number}",
        body="",
        additions=0,
        deletions=0,
        changed_files_count=0,
        assignees=assignees or [],
        approvals=[],
        commenters=[],
        files=[],
        head_ci_state=ci_state,
        head_sha=head_sha,
    )


def _mk_snapshot(
    repo: Repository,
    generated_at: datetime,
    prs_data: dict,
    dashboards: dict,
    pr_count: int = 0,
    queue_count: int = 0,
    cache_key: str = "default",
) -> QueueSnapshot:
    all_pr_nums = list(prs_data.keys())
    return QueueSnapshot.objects.create(
        repository=repo,
        cache_key=cache_key,
        generated_at=generated_at,
        payload={
            "meta": {
                "schema_version": "v1-draft",
                "generated_at": generated_at.isoformat(),
                "repository": f"{repo.owner}/{repo.name}",
                "rule_set_id": "default",
                "require_ci_success": False,
                "ci_gating_mode": None,
                "required_ci_contexts": [],
            },
            "prs": prs_data,
            "lists": {
                "draft_prs": [],
                "nondraft_prs": all_pr_nums,
                "dashboards": {"All": all_pr_nums, **dashboards},
            },
        },
        etag="test-etag",
        pr_count=pr_count or len(prs_data),
        queue_count=queue_count or len(dashboards.get("Queue", [])),
    )


def _pr_entry(
    number: int,
    *,
    title: str = "PR title",
    author: str | None = "alice",
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    ci_status: str = "pass",
    is_draft: bool = False,
    on_queue_since: datetime | None = None,
    total_queue_seconds: int = 0,
    created_at: str | None = "2026-01-01T00:00:00+00:00",
    last_updated: str = "2026-02-28T00:00:00+00:00",
    direct_dependencies: list[int] | None = None,
) -> dict:
    queue_since = on_queue_since or _dt(2026, 2, 1)
    return {
        "state": "open",
        "is_draft": is_draft,
        "created_at": created_at,
        "base_branch": "master",
        "last_updated": last_updated,
        "author": author,
        "title": title,
        "labels": [{"name": lbl, "color": "abc", "url": f"https://github.com/labels/{lbl}"} for lbl in (labels or [])],
        "assignees": assignees or [],
        "approvals": [],
        "ci_status": ci_status,
        "pr_status": "awaiting-review",
        "last_queue_status_change": {
            "status": "valid",
            "time": queue_since.isoformat(),
            "delta": {"days": 0, "hours": 0, "minutes": 0, "seconds": 0},
            "current_status": "OnQueue",
        },
        "first_on_queue": {"status": "valid", "date": queue_since.isoformat()},
        "total_queue_time": {
            "status": "valid",
            "value_td": total_queue_seconds,
            "value_rd": {},
            "explanation": "",
        },
        "direct_dependencies": direct_dependencies or [],
        "data_status": {
            "files": "valid",
            "assignees": "valid",
            "approvals": "valid",
            "comments": "valid",
            "queue": "valid",
        },
    }


class GetPrQueueInfoSnapshotTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            is_active=True,
            required_label_names=[],
            forbidden_label_names=[],
        )
        self.cache_key = str(self.rule_set.id)
        self.now = _dt(2026, 3, 1, 12)

    def test_on_queue_pr_returns_correct_fields(self) -> None:
        queue_since = _dt(2026, 2, 1)
        entry = _pr_entry(
            123,
            title="Fix the thing",
            author="alice",
            labels=["awaiting-review", "t-algebra"],
            assignees=["bob"],
            ci_status="pass",
            on_queue_since=queue_since,
            total_queue_seconds=28 * 86400,
        )
        _mk_snapshot(self.repo, self.now, {"123": entry}, {"Queue": [123]}, cache_key=self.cache_key)

        info = get_pr_queue_info("leanprover-community", "mathlib4", 123)

        self.assertIsNotNone(info)
        assert info is not None
        self.assertTrue(info.on_queue)
        self.assertEqual(info.title, "Fix the thing")
        self.assertEqual(info.author_login, "alice")
        self.assertEqual(info.assignee_logins, ["bob"])
        self.assertIn("awaiting-review", info.labels)
        self.assertIn("t-algebra", info.labels)
        self.assertEqual(info.ci_status, "pass")
        self.assertEqual(info.total_queue_seconds, 28 * 86400)
        self.assertEqual(info.queue_since, queue_since)
        self.assertEqual(info.source, "snapshot")
        self.assertEqual(info.url, "https://github.com/leanprover-community/mathlib4/pull/123")

    def test_snapshot_created_at_populated(self) -> None:
        entry = _pr_entry(123, created_at="2026-01-15T10:00:00+00:00")
        _mk_snapshot(self.repo, self.now, {"123": entry}, {"Queue": [123]}, cache_key=self.cache_key)

        info = get_pr_queue_info("leanprover-community", "mathlib4", 123)

        assert info is not None
        self.assertEqual(info.created_at, _dt(2026, 1, 15, 10))

    def test_missing_created_at_in_old_snapshot_returns_none(self) -> None:
        entry = _pr_entry(123, created_at=None)
        _mk_snapshot(self.repo, self.now, {"123": entry}, {"Queue": [123]}, cache_key=self.cache_key)

        info = get_pr_queue_info("leanprover-community", "mathlib4", 123)

        assert info is not None
        self.assertIsNone(info.created_at)

    def test_not_on_queue_draft_pr(self) -> None:
        entry = _pr_entry(456, is_draft=True)
        entry["last_queue_status_change"] = None
        _mk_snapshot(self.repo, self.now, {"456": entry}, {"OtherBase": [], "Unlabelled": [456]}, cache_key=self.cache_key)

        info = get_pr_queue_info("leanprover-community", "mathlib4", 456)

        assert info is not None
        self.assertFalse(info.on_queue)
        self.assertEqual(info.off_queue_reasons, ["draft PR"])

    def test_not_on_queue_label_reason(self) -> None:
        entry = _pr_entry(789, labels=["awaiting-author"], ci_status="pass")
        entry["last_queue_status_change"] = {
            "status": "valid",
            "time": self.now.isoformat(),
            "delta": {},
            "current_status": "OffQueue",
        }
        _mk_snapshot(self.repo, self.now, {"789": entry}, {}, cache_key=self.cache_key)

        info = get_pr_queue_info("leanprover-community", "mathlib4", 789)

        assert info is not None
        self.assertFalse(info.on_queue)
        self.assertIn("awaiting author", info.off_queue_reasons)

    def test_stale_snapshot_flagged(self) -> None:
        from datetime import timezone as tz

        stale_time = datetime.now(tz.utc) - timedelta(hours=3)
        entry = _pr_entry(123)
        _mk_snapshot(self.repo, stale_time, {"123": entry}, {"Queue": [123]}, cache_key=self.cache_key)

        info = get_pr_queue_info("leanprover-community", "mathlib4", 123)

        assert info is not None
        self.assertTrue(info.snapshot_is_stale)

    def test_fresh_snapshot_not_flagged(self) -> None:
        from datetime import timezone as tz

        fresh_time = datetime.now(tz.utc) - timedelta(minutes=10)
        entry = _pr_entry(123)
        _mk_snapshot(self.repo, fresh_time, {"123": entry}, {"Queue": [123]}, cache_key=self.cache_key)

        info = get_pr_queue_info("leanprover-community", "mathlib4", 123)

        assert info is not None
        self.assertFalse(info.snapshot_is_stale)

    def test_pr_not_in_snapshot_falls_back_to_db(self) -> None:
        # Snapshot exists but doesn't contain PR 999
        entry = _pr_entry(123)
        _mk_snapshot(self.repo, self.now, {"123": entry}, {"Queue": [123]}, cache_key=self.cache_key)
        # PR 999 exists in DB as merged
        _mk_pr(self.repo, 999, state="closed", merged_at=_dt(2026, 2, 20))

        info = get_pr_queue_info("leanprover-community", "mathlib4", 999)

        assert info is not None
        self.assertEqual(info.state, "merged")
        self.assertEqual(info.source, "db")

    def test_dependency_in_snapshot_shows_open(self) -> None:
        dep_entry = _pr_entry(200, title="Dep PR")
        main_entry = _pr_entry(100, direct_dependencies=[200])
        _mk_snapshot(self.repo, self.now, {"100": main_entry, "200": dep_entry}, {"Queue": [100, 200]}, cache_key=self.cache_key)

        info = get_pr_queue_info("leanprover-community", "mathlib4", 100)

        assert info is not None
        self.assertEqual(len(info.dependencies), 1)
        self.assertEqual(info.dependencies[0].number, 200)
        self.assertEqual(info.dependencies[0].state, "open")
        self.assertEqual(info.dependencies[0].title, "Dep PR")

    def test_dependency_not_in_snapshot_looked_up_in_db(self) -> None:
        _mk_pr(self.repo, 200, state="closed", merged_at=_dt(2026, 2, 15))
        main_entry = _pr_entry(100, direct_dependencies=[200])
        _mk_snapshot(self.repo, self.now, {"100": main_entry}, {"Queue": [100]}, cache_key=self.cache_key)

        info = get_pr_queue_info("leanprover-community", "mathlib4", 100)

        assert info is not None
        self.assertEqual(len(info.dependencies), 1)
        self.assertEqual(info.dependencies[0].number, 200)
        self.assertEqual(info.dependencies[0].state, "merged")

    def test_repo_not_found_returns_none(self) -> None:
        info = get_pr_queue_info("nonexistent", "repo", 1)
        self.assertIsNone(info)

    def test_case_insensitive_repo_lookup(self) -> None:
        entry = _pr_entry(123)
        _mk_snapshot(self.repo, self.now, {"123": entry}, {"Queue": [123]}, cache_key=self.cache_key)

        info = get_pr_queue_info("LEANPROVER-COMMUNITY", "MATHLIB4", 123)

        self.assertIsNotNone(info)


class GetPrQueueInfoDbTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            is_active=True,
            required_label_names=[],
            forbidden_label_names=[],
        )
        self.now = _dt(2026, 3, 1, 12)

    def test_merged_pr_from_db(self) -> None:
        author = User.objects.create(github_login="alice")
        pr = _mk_pr(
            self.repo,
            42,
            state="closed",
            merged_at=_dt(2026, 2, 20),
            author=author,
            created_at=_dt(2026, 1, 10),
            updated_at=_dt(2026, 2, 20),
        )
        label_def = LabelDef.objects.create(repository=self.repo, name="awaiting-review", color="abc")
        PRLabel.objects.create(pull_request=pr, label_def=label_def)

        info = get_pr_queue_info("leanprover-community", "mathlib4", 42)

        assert info is not None
        self.assertEqual(info.state, "merged")
        self.assertEqual(info.author_login, "alice")
        self.assertIn("awaiting-review", info.labels)
        self.assertEqual(info.merged_at, _dt(2026, 2, 20))
        self.assertEqual(info.created_at, _dt(2026, 1, 10))
        self.assertEqual(info.source, "db")
        self.assertFalse(info.on_queue)
        self.assertEqual(info.off_queue_reasons, [])  # closed PRs don't get reasons

    def test_pr_with_active_queue_window(self) -> None:
        pr = _mk_pr(self.repo, 55, assignees=["bob"])
        window_start = self.now - timedelta(days=5)
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            from_ts=window_start,
            to_ts=None,
            cycle_index=0,
            duration_seconds_closed=0,
            cumulative_seconds_closed=0,
            window_count=1,
            first_on_queue_ts=window_start,
        )

        info = get_pr_queue_info("leanprover-community", "mathlib4", 55)

        assert info is not None
        self.assertTrue(info.on_queue)
        self.assertIsNotNone(info.queue_since)
        self.assertIsNotNone(info.total_queue_seconds)
        assert info.total_queue_seconds is not None
        self.assertGreater(info.total_queue_seconds, 0)

    def test_pr_with_closed_queue_window(self) -> None:
        pr = _mk_pr(self.repo, 66)
        window_start = self.now - timedelta(days=10)
        window_end = self.now - timedelta(days=2)
        closed_seconds = int((window_end - window_start).total_seconds())
        PRQueueWindow.objects.create(
            pull_request=pr,
            rule_set=self.rule_set,
            from_ts=window_start,
            to_ts=window_end,
            cycle_index=0,
            duration_seconds_closed=closed_seconds,
            cumulative_seconds_closed=closed_seconds,
            window_count=1,
            first_on_queue_ts=window_start,
        )

        info = get_pr_queue_info("leanprover-community", "mathlib4", 66)

        assert info is not None
        self.assertFalse(info.on_queue)
        self.assertEqual(info.total_queue_seconds, closed_seconds)

    def test_pr_not_in_db_returns_none(self) -> None:
        info = get_pr_queue_info("leanprover-community", "mathlib4", 99999)
        self.assertIsNone(info)

    def test_dependency_resolved_via_pr_model(self) -> None:
        pr = _mk_pr(self.repo, 77)
        dep_pr = _mk_pr(self.repo, 78, state="closed", merged_at=_dt(2026, 2, 10))
        PRDependency.objects.create(
            pull_request=pr,
            depends_on_repository=self.repo,
            depends_on_number=78,
            depends_on_pull_request=dep_pr,
        )

        info = get_pr_queue_info("leanprover-community", "mathlib4", 77)

        assert info is not None
        self.assertEqual(len(info.dependencies), 1)
        dep = info.dependencies[0]
        self.assertEqual(dep.number, 78)
        self.assertEqual(dep.state, "merged")


class CiStatusFromRulesetTests(TestCase):
    """DB path derives ci_status from required-context check run data, not head_ci_state."""

    SHA = "abc123def456"

    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master")

    def _rule_set(self, contexts: list[str]) -> None:
        QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            is_active=True,
            is_default=True,
            require_ci_success=True,
            ci_gating_mode="all_required_success",
            required_ci_contexts=contexts,
        )

    def _check_run(self, name: str, conclusion: str) -> None:
        now = _dt(2026, 3, 1)
        CommitCheckRun.objects.create(
            repository=self.repo,
            head_sha=self.SHA,
            name=name,
            status=CheckRunStatus.COMPLETED,
            conclusion=conclusion,
            gh_completed_at=now,
        )

    def test_no_required_contexts_returns_pass(self) -> None:
        QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            is_active=True,
            require_ci_success=False,
        )
        _mk_pr(self.repo, 1, head_sha=self.SHA)

        info = get_pr_queue_info("o", "r", 1)

        assert info is not None
        self.assertEqual(info.ci_status, "pass")

    def test_passing_check_run_returns_pass(self) -> None:
        self._rule_set(["build"])
        self._check_run("build / lint", CheckRunConclusion.SUCCESS)
        _mk_pr(self.repo, 1, head_sha=self.SHA)

        info = get_pr_queue_info("o", "r", 1)

        assert info is not None
        self.assertEqual(info.ci_status, "pass")

    def test_failing_check_run_returns_fail(self) -> None:
        self._rule_set(["build"])
        self._check_run("build / lint", CheckRunConclusion.FAILURE)
        _mk_pr(self.repo, 1, head_sha=self.SHA)

        info = get_pr_queue_info("o", "r", 1)

        assert info is not None
        self.assertEqual(info.ci_status, "fail")

    def test_missing_check_run_returns_missing(self) -> None:
        self._rule_set(["build"])
        _mk_pr(self.repo, 1, head_sha=self.SHA)

        info = get_pr_queue_info("o", "r", 1)

        assert info is not None
        self.assertEqual(info.ci_status, "missing")

    def test_no_head_sha_returns_missing(self) -> None:
        self._rule_set(["build"])
        _mk_pr(self.repo, 1, head_sha=None)

        info = get_pr_queue_info("o", "r", 1)

        assert info is not None
        self.assertEqual(info.ci_status, "missing")


class CiRequiresSuccessTests(TestCase):
    """ci_requires_success should be False in NO_REQUIRED_FAILURES mode."""

    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master")

    def test_no_required_failures_mode_missing_ci_is_pass(self) -> None:
        QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            is_active=True,
            require_ci_success=True,
            ci_gating_mode="no_required_failures",
        )
        _mk_pr(self.repo, 1)

        info = get_pr_queue_info("o", "r", 1)

        assert info is not None
        self.assertFalse(info.ci_requires_success)

    def test_all_required_success_mode_missing_ci_is_fail(self) -> None:
        QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            is_active=True,
            require_ci_success=True,
            ci_gating_mode="all_required_success",
        )
        _mk_pr(self.repo, 1)

        info = get_pr_queue_info("o", "r", 1)

        assert info is not None
        self.assertTrue(info.ci_requires_success)

    def test_no_ci_gating_missing_ci_is_pass(self) -> None:
        QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            is_active=True,
            require_ci_success=False,
        )
        _mk_pr(self.repo, 1)

        info = get_pr_queue_info("o", "r", 1)

        assert info is not None
        self.assertFalse(info.ci_requires_success)


class OffQueueReasonsTests(TestCase):
    def _rules(self, *, required: set[str] | None = None, forbidden: set[str] | None = None) -> QueueRules:
        return QueueRules(
            require_open=True,
            require_not_draft=True,
            required_labels=required,
            forbidden_labels=forbidden,
        )

    def test_draft_pr(self) -> None:
        reasons = off_queue_reasons_from_labels(set(), self._rules(), "pass", is_draft=True)
        self.assertEqual(reasons, ["draft PR"])

    def test_awaiting_author_label(self) -> None:
        reasons = off_queue_reasons_from_labels({"awaiting-author"}, self._rules(), "pass", False)
        self.assertIn("awaiting author", reasons)

    def test_blocked_by_label(self) -> None:
        reasons = off_queue_reasons_from_labels({"blocked-by-other-pr"}, self._rules(), "pass", False)
        self.assertIn("blocked-by label present", reasons)

    def test_ready_to_merge_label(self) -> None:
        reasons = off_queue_reasons_from_labels({"ready-to-merge"}, self._rules(), "pass", False)
        self.assertIn("labeled ready-to-merge", reasons)

    def test_missing_required_label(self) -> None:
        rules = self._rules(required={"t-algebra"})
        reasons = off_queue_reasons_from_labels({"awaiting-review"}, rules, "pass", False)
        self.assertTrue(any("missing required label" in r for r in reasons))

    def test_ci_not_passing_when_gated(self) -> None:
        rules = QueueRules(require_open=True, require_not_draft=True, require_ci_success=True)
        reasons = off_queue_reasons_from_labels(set(), rules, "fail", False)
        self.assertTrue(any("CI not passing" in r for r in reasons))

    def test_no_known_reason_falls_back(self) -> None:
        reasons = off_queue_reasons_from_labels(set(), self._rules(), "pass", False)
        self.assertEqual(reasons, ["not queue-labeled"])

    def test_multiple_reasons_combined(self) -> None:
        reasons = off_queue_reasons_from_labels({"awaiting-author", "wip"}, self._rules(), "pass", False)
        self.assertIn("awaiting author", reasons)
        self.assertIn("labeled WIP", reasons)
