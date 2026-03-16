from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("syncer", "0036_syncermetricssnapshot_sha_task_impacted_pr_fanout_total_and_more"),
    ]

    operations = [
        migrations.DeleteModel(
            name="CheckRun",
        ),
        migrations.DeleteModel(
            name="StatusContext",
        ),
    ]
