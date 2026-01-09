from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("syncer", "0020_syncermetricssnapshot_token_cost_total"),
    ]

    operations = [
        migrations.AddField(
            model_name="syncerconvergencesnapshot",
            name="prs_missing_head_sha",
            field=models.IntegerField(default=0),
        ),
    ]
