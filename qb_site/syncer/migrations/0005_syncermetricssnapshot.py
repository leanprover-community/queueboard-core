from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("syncer", "0004_prtimelineevent_after_sha_prtimelineevent_before_sha_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="SyncerMetricsSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("window_start", models.DateTimeField(db_index=True)),
                ("window_seconds", models.PositiveIntegerField(default=900)),
                ("pr_tasks", models.IntegerField(default=0)),
                ("pr_deferred", models.IntegerField(default=0)),
                ("pr_failures", models.IntegerField(default=0)),
                ("pr_avg_duration_s", models.FloatField(default=0.0)),
                ("pr_token_cost", models.IntegerField(default=0)),
                ("repo_tasks", models.IntegerField(default=0)),
                ("repo_low_budget", models.IntegerField(default=0)),
                ("repo_avg_duration_s", models.FloatField(default=0.0)),
                ("repo_discovered", models.IntegerField(default=0)),
                ("repo_enqueued", models.IntegerField(default=0)),
                ("repo_discovery_cost", models.IntegerField(default=0)),
                ("rows_pull_request", models.IntegerField(default=0)),
                ("rows_timeline_event", models.IntegerField(default=0)),
                ("rows_check_run", models.IntegerField(default=0)),
                ("rows_status_context", models.IntegerField(default=0)),
                ("rows_pr_label", models.IntegerField(default=0)),
                ("rows_label_def", models.IntegerField(default=0)),
                ("db_size_bytes", models.BigIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["-window_start"],
            },
        ),
    ]

