import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job_title = models.CharField(max_length=120, blank=True)
    module_access = models.JSONField(default=dict, blank=True)


class LoginThrottle(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = models.CharField(max_length=150)
    ip_address = models.CharField(max_length=64, blank=True)
    failure_count = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_failed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["username", "ip_address"],
                name="accounts_unique_login_throttle",
            )
        ]
        verbose_name = "Login throttle"
        verbose_name_plural = "Login throttles"

    def __str__(self):
        return f"{self.username} @ {self.ip_address or 'unknown'}"
