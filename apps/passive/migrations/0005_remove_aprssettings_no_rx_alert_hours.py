from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("passive", "0004_aprspacket_pending_idx"),
    ]

    operations = [
        migrations.RemoveField(model_name="aprssettings", name="no_rx_alert_hours"),
    ]
