from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("passive", "0003_alter_aprssettings_aprsis_port_and_more"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="aprspacket",
            index=models.Index(
                condition=models.Q(("gate_skip", ""), ("gated_at__isnull", True)),
                fields=["id"],
                name="aprs_pkt_pending_idx",
            ),
        ),
    ]
