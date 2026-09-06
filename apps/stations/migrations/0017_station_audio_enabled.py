from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('stations', '0016_station_current_image_variant'),
    ]

    operations = [
        migrations.AddField(
            model_name='station',
            name='audio_enabled',
            field=models.BooleanField(default=False, help_text='Whether this station has audio hardware and should run the audio engine. Written into the provisioning config.yml; existing stations pick up a change only via re-provisioning (config.yml is preserved across OTA).', verbose_name='audio enabled'),
        ),
    ]
