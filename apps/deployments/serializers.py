from rest_framework import serializers

from apps.deployments.models import DeploymentResult


class DeploymentCheckRequestSerializer(serializers.Serializer):
    current_version = serializers.CharField(
        max_length=100, required=False, default="", allow_blank=True
    )


class DeploymentCheckResponseSerializer(serializers.Serializer):
    deployment_result_id = serializers.IntegerField()
    deployment_id = serializers.IntegerField()
    target_tag = serializers.CharField()
    checksum_sha256 = serializers.CharField()
    size_bytes = serializers.IntegerField()
    download_url = serializers.CharField()


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
