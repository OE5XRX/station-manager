from rest_framework import serializers


class ReconcileCheckRequestSerializer(serializers.Serializer):
    """Optional ist-report for crash recovery. No required fields."""


class ReconcileCheckResponseSerializer(serializers.Serializer):
    convergence_id = serializers.IntegerField()
    module_uid = serializers.CharField()
    slot = serializers.CharField()
    module_type = serializers.CharField()
    variant = serializers.CharField(allow_blank=True)
    target_version = serializers.CharField()
    download_url = serializers.CharField()
    checksum_sha256 = serializers.CharField()
    size_bytes = serializers.IntegerField()


class ReconcileStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=[
            "downloading", "flashing", "verifying", "failed", "rolled_back", "rejected",
        ]
    )
    error_message = serializers.CharField(required=False, default="", allow_blank=True)


class ReconcileCommitSerializer(serializers.Serializer):
    convergence_id = serializers.IntegerField()
    version = serializers.CharField(max_length=64)
