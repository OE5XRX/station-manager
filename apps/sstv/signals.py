"""Delete stored files when their row goes (direct delete, queryset delete, cascade)."""

import logging

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import SstvImage, SstvTransmission

logger = logging.getLogger(__name__)


def _delete_files_on_commit(*fieldfiles):
    """Remove the blobs once the deleting transaction commits (never on rollback).

    Best effort: a storage failure is logged and never breaks the DB delete.
    """
    blobs = [(f.storage, f.name) for f in fieldfiles if f and f.name]
    if not blobs:
        return

    def _delete():
        for storage, name in blobs:
            try:
                storage.delete(name)
            except Exception:
                logger.exception("sstv: could not delete stored file %s", name)

    transaction.on_commit(_delete)


@receiver(post_delete, sender=SstvImage)
def _on_image_delete(sender, instance, **kwargs):
    _delete_files_on_commit(instance.image)


@receiver(post_delete, sender=SstvTransmission)
def _on_transmission_delete(sender, instance, **kwargs):
    _delete_files_on_commit(instance.preview, instance.audio)
