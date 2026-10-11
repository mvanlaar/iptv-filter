from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("iptv_filter", "0006_logocache"),
    ]

    operations = [
        migrations.CreateModel(
            name="EpgMapping",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("tvg_name", models.CharField(max_length=100, unique=True)),
                ("epg_id", models.CharField(blank=True, default="", max_length=50)),
                ("no_guide", models.BooleanField(default=False)),
                ("updated", models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
