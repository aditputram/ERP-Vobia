import uuid

from django.conf import settings
from django.db import models


BUSINESS_MODULES = (
    ("sales", "Sales"),
    ("operation", "Operation"),
    ("rnd", "R&D"),
    ("marketing", "Marketing"),
    ("finance", "Finance"),
)


class ChatThread(models.Model):
    class Kind(models.TextChoices):
        MODULE = "MODULE", "Room Modul"
        DIRECT = "DIRECT", "Personal"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    key = models.CharField(max_length=160, unique=True)
    module = models.CharField(max_length=30, choices=BUSINESS_MODULES, blank=True)
    participants = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="chat_threads",
        blank=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_chat_threads",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at",)

    def __str__(self):
        return self.get_module_display() if self.kind == self.Kind.MODULE else self.key


class ChatMessage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thread = models.ForeignKey(ChatThread, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_messages",
    )
    body = models.TextField(blank=True, max_length=4000)
    attachment = models.FileField(upload_to="chat/attachments/%Y/%m/", blank=True)
    original_name = models.CharField(max_length=255, blank=True)
    reply_to = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="replies",
    )
    mentions = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="chat_mentions",
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at",)
        indexes = [models.Index(fields=("thread", "created_at"))]

    def __str__(self):
        return f"{self.sender} · {self.created_at:%d %b %Y %H:%M}"


class ChatReadState(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thread = models.ForeignKey(ChatThread, on_delete=models.CASCADE, related_name="read_states")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_read_states",
    )
    last_read_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("thread", "user"),
                name="chat_unique_read_state_per_user",
            )
        ]


def ensure_module_rooms():
    for module, _label in BUSINESS_MODULES:
        ChatThread.objects.get_or_create(
            key=f"module:{module}",
            defaults={"kind": ChatThread.Kind.MODULE, "module": module},
        )


def accessible_threads(user):
    from accounts.access import module_level

    modules = [
        module
        for module, _label in BUSINESS_MODULES
        if user.is_superuser or module_level(user, module) != "none"
    ]
    return ChatThread.objects.filter(
        models.Q(kind=ChatThread.Kind.MODULE, module__in=modules)
        | models.Q(kind=ChatThread.Kind.DIRECT, participants=user)
    ).distinct()
