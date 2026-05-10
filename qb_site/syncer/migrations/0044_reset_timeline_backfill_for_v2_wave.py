# Reset timeline backfill flags for v2 wave (Chunk 5).
#
# Pairs with bumping CURRENT_SYNC_SCHEMA_VERSION = 2. Without this reset, PRs
# whose v1-era timeline walk left timeline_backfill_done=True would short-
# circuit UpgradeToV2.is_complete to True and the dispatcher would auto-stamp
# them to v=2 without ever re-walking history under the v2 fragments. See
# docs/design-decisions/044-... §Chunk 5 (option (a)).

from django.db import migrations


def reset_timeline_backfill(apps, schema_editor):
    PullRequest = apps.get_model("syncer", "PullRequest")
    PullRequest.objects.filter(sync_schema_version__lt=2).update(
        timeline_backfill_done=False,
        timeline_backfill_cursor=None,
    )


def noop_reverse(apps, schema_editor):
    # No reverse migration: re-flipping timeline_backfill_done would require
    # remembering each PR's prior state, which we don't preserve. The forward
    # migration is idempotent (running twice on the same row is a no-op once
    # sync_schema_version reaches 2), so leaving the reverse as a no-op is
    # safe — operationally we'd roll back code, not data, and the next wave
    # pass would re-converge from whatever state the rows are in.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("syncer", "0043_add_prtimelineevent_engagement_check_constraints"),
    ]

    operations = [
        migrations.RunPython(reset_timeline_backfill, reverse_code=noop_reverse),
    ]
