from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache

from audit.services import record_audit
from accounts.access import first_allowed_route, module_level
from chat.context_processors import unread_chat
from chat.models import ChatMessage, ChatThread, accessible_threads
from rnd.context_processors import rnd_notifications


MODULES = (
    {
        "slug": "sales",
        "name": "Sales",
        "eyebrow": "REVENUE & CUSTOMER",
        "description": "Dashboard penjualan, performa produk, Pareto, transaksi, dan data import.",
        "status": "Aktif",
        "available": True,
        "accent": "lime",
        "image": "img/modules/module-sales.jpg",
        "image_position": "center 58%",
    },
    {
        "slug": "operation",
        "name": "Operation",
        "eyebrow": "SUPPLY CHAIN",
        "description": "Merchandising, PPIC, production, warehouse, inventory, dan purchasing.",
        "status": "Fondasi aktif",
        "available": True,
        "accent": "teal",
        "image": "img/modules/module-operation.jpg",
        "image_position": "center 60%",
    },
    {
        "slug": "rnd",
        "name": "RnD",
        "eyebrow": "PRODUCT DEVELOPMENT",
        "description": "Riset produk, sampling, material, costing awal, dan lifecycle development.",
        "status": "Fondasi aktif",
        "available": True,
        "accent": "violet",
        "image": "img/modules/module-rnd.jpg",
        "image_position": "center 52%",
    },
    {
        "slug": "marketing",
        "name": "Marketing",
        "eyebrow": "BRAND & CAMPAIGN",
        "description": "Campaign, content plan, performance marketing, dan kalender peluncuran.",
        "status": "Instagram Report",
        "available": True,
        "accent": "coral",
        "image": "img/modules/module-marketing.jpg",
        "image_position": "center 52%",
    },
    {
        "slug": "finance",
        "name": "Finance",
        "eyebrow": "FINANCIAL CONTROL",
        "description": "Cash flow, payable, receivable, budgeting, dan financial reporting.",
        "status": "UAT",
        "available": True,
        "accent": "blue",
        "image": "img/modules/module-finance.jpg",
        "image_position": "center 54%",
    },
    {
        "slug": "human-resource",
        "name": "Human Resource",
        "eyebrow": "PEOPLE & ORGANIZATION",
        "description": "Employee data, attendance, payroll, performance, dan organization.",
        "status": "Segera hadir",
        "available": False,
        "accent": "amber",
        "image": "img/modules/module-human-resource.jpg",
        "image_position": "center 54%",
    },
)


@login_required
@never_cache
def live_status(request):
    chat_count = unread_chat(request)["chat_unread_count"]
    chat_notifications = []
    recent_messages = (
        ChatMessage.objects.filter(thread__in=accessible_threads(request.user))
        .exclude(sender=request.user)
        .select_related("thread", "sender")
        .order_by("-created_at")[:12]
    )
    for message in recent_messages:
        sender_name = message.sender.get_full_name() or message.sender.username
        if message.thread.kind == ChatThread.Kind.MODULE:
            title = f"{message.thread.get_module_display()} · {sender_name}"
        else:
            title = sender_name
        preview = message.body.strip()[:180]
        if not preview:
            preview = f"Lampiran: {message.original_name}" if message.original_name else "Mengirim lampiran."
        chat_notifications.append(
            {
                "id": str(message.id),
                "open_url": reverse("chat:thread", args=[message.thread_id]),
                "title": title,
                "message": preview,
            }
        )
    notification_context = rnd_notifications(request)
    notifications = [
        {
            "id": str(notification.id),
            "open_url": reverse("rnd:notification_open", args=[notification.id]),
            "title": notification.title,
            "message": notification.message,
            "created_at": timezone.localtime(notification.created_at).strftime("%d %b %Y · %H:%M"),
            "unread": notification.read_at is None,
            "category": notification.category,
            "thumbnail_url": notification.thumbnail_url,
        }
        for notification in notification_context["rnd_notification_items"]
    ]
    approvals = [
        {
            "open_url": item["target_url"],
            "title": item["title"],
            "message": item["message"],
            "created_at": timezone.localtime(item["created_at"]).strftime("%d %b %Y · %H:%M"),
            "thumbnail_url": item["thumbnail_url"],
        }
        for item in notification_context["rnd_approval_items"]
    ]
    return JsonResponse(
        {
            "chat_unread_count": chat_count,
            "chat_notifications": chat_notifications,
            "rnd_unread_count": notification_context["rnd_notification_unread_count"],
            "rnd_notifications": notifications,
            "rnd_approval_count": notification_context["rnd_approval_count"],
            "rnd_approvals": approvals,
        }
    )


@login_required
def index(request):
    modules = [
        {
            **module,
            "accessible": not module["available"]
            or request.user.is_superuser
            or (
                module_level(request.user, module["slug"]) != "none"
                and first_allowed_route(request.user, module["slug"]) is not None
            ),
        }
        for module in MODULES
    ]
    return render(request, "dashboard/index.html", {"modules": modules})


@login_required
def enter_module(request, module_slug):
    module = next((item for item in MODULES if item["slug"] == module_slug), None)
    if module is None:
        messages.error(request, "Modul tidak ditemukan.")
        return redirect("dashboard:index")
    if not module["available"]:
        messages.info(
            request,
            f"Modul {module['name']} sudah masuk roadmap dan akan diaktifkan setelah proses bisnisnya siap.",
        )
        return redirect("dashboard:index")
    destination = first_allowed_route(request.user, module_slug)
    if not request.user.is_superuser and (
        module_level(request.user, module_slug) == "none" or destination is None
    ):
        messages.error(request, "yang tidak berkepentingan dilarang masuk!")
        return redirect("dashboard:index")

    request.session["active_module"] = module_slug
    record_audit(
        actor=request.user,
        action="module_entered",
        entity_type="navigation.module",
        entity_id=module_slug,
        metadata={"module_name": module["name"]},
    )
    return redirect(destination or first_allowed_route(request.user, module_slug))


@login_required
def guide(request):
    return render(request, "dashboard/guide.html")
