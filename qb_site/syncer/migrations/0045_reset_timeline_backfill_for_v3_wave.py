# Reset timeline backfill flags for v3 wave (Chunk 5b).
#
# Pairs with bumping CURRENT_SYNC_SCHEMA_VERSION = 3 and registering
# UpgradeToV3. The v2 wave shipped with a wire-up gap (the forward and
# backward timeline-page loops did not invoke the inline-comments service);
# PRs whose v=2 walk completed before the fix sit at sync_schema_version=2
# with missing PRReviewInlineComment rows. Without this reset, those PRs'
# timeline_backfill_done=True would short-circuit UpgradeToV3.is_complete to
# True and the dispatcher would auto-stamp them to v=3 without a rewalk —
# repeating the v=2-era pitfall fixed by 0044.
#
# Pre-v=2 PRs (still at v=1 because the v=2 wave hadn't reached them) also
# get reset; they go through the v=3 walk under the fixed code and end up at
# v=3 directly. Same option (a) trade-off as 0044: bounded redundant rewalks
# in exchange for a known-good starting state. See design doc 044 §Chunk 5b.

from django.db import migrations


def reset_timeline_backfill(apps, schema_editor):
    PullRequest = apps.get_model("syncer", "PullRequest")
    PullRequest.objects.filter(sync_schema_version__lt=3).update(
        timeline_backfill_done=False,
        timeline_backfill_cursor=None,
    )


def noop_reverse(apps, schema_editor):
    # Same rationale as 0044: re-flipping timeline_backfill_done would require
    # remembering each PR's prior state, which we don't preserve. Forward
    # migration is idempotent (running twice on the same row is a no-op once
    # sync_schema_version reaches 3); operationally we'd roll back code, not
    # data, and the next wave pass would re-converge from whatever state the
    # rows are in.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("syncer", "0044_reset_timeline_backfill_for_v2_wave"),
    ]

    operations = [
        migrations.RunPython(reset_timeline_backfill, reverse_code=noop_reverse),
    ]
