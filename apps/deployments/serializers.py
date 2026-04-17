from rest_framework import serializers

from apps.deployments.models import DeploymentResult


class DeploymentCheckResponseSerializer(serializers.Serializer):
    result_id = serializers.IntegerField()
    deployment_id = serializers.IntegerField()
    firmware_name = serializers.CharField()
    firmware_version = serializers.CharField()
    download_url = serializers.CharField()
    checksum_sha256 = serializers.CharField()
    file_size = serializers.IntegerField()
    is_delta = serializers.BooleanField(default=False)
    delta_checksum_sha256 = serializers.CharField(required=False, default="")
    delta_file_size = serializers.IntegerField(required=False, default=0)


class DeploymentStatusUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=[
            DeploymentResult.Status.DOWNLOADING,
            DeploymentResult.Status.INSTALLING,
            DeploymentResult.Status.REBOOTING,
            DeploymentResult.Status.VERIFYING,
            DeploymentResult.Status.FAILED,
            DeploymentResult.Status.ROLLED_BACK,
        ]
    )
    error_message = serializers.CharField(required=False, default="", allow_blank=True)


class DeploymentCommitSerializer(serializers.Serializer):
    version = serializers.CharField(max_length=100)
