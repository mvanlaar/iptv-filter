from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("iptv_filter", "0008_playlistchannel_group_index"),
    ]

    operations = [
        migrations.CreateModel(
            name="RadioChannel",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("tvg_name", models.CharField(max_length=100, unique=True)),
            ],
        ),
    ]
