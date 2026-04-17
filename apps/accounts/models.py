from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _

from .managers import UserManager


class User(AbstractUser):
    """Custom user model with role and language preferences."""

    class Role(models.TextChoices):
        ADMIN = "admin", _("Admin")
        OPERATOR = "operator", _("Operator")
        MEMBER = "member", _("Member")

    class Language(models.TextChoices):
        ENGLISH = "en", _("English")
        GERMAN = "de", _("German")

    role = models.CharField(
        _("role"),
        max_length=10,
        choices=Role.choices,
        default=Role.MEMBER,
    )
    language = models.CharField(
        _("language"),
        max_length=2,
        choices=Language.choices,
        default=Language.ENGLISH,
    )

    objects = UserManager()

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        ordering = ["username"]

    def __str__(self):
        return self.username

    @property
    def is_admin(self):
        return self.role == self.Role.ADMIN

    @property
    def is_operator(self):
        return self.role == self.Role.OPERATOR
