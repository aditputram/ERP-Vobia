import json
import re
from mimetypes import guess_type
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Max, Q
from django.http import FileResponse, Http404, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.clickjacking import xframe_options_sameorigin

from accounts.access import module_level

from .forms import ChatMessageForm
from .models import ChatMessage, ChatReadState, ChatThread, PushSubscription, accessible_threads
from .push import send_web_push


MENTION_PATTERN = re.compile(r"(?<!\w)@([\w.-]{1,150})")
PUSH_ENDPOINT_HOSTS = {
    "fcm.googleapis.com",
    "jmt17.google.com",
    "updates.push.services.mozilla.com",
    "web.push.apple.com",
}


@never_cache
def service_worker(request):
    response = render(request, "chat/service_worker.js", content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@login_required
@require_GET
def push_config(request):
    public_key = settings.WEB_PUSH_VAPID_PUBLIC_KEY
    return JsonResponse({"enabled": bool(public_key), "public_key": public_key})


@login_required
@require_POST
def push_subscribe(request):
    try:
        payload = json.loads(request.body)
        endpoint = payload["endpoint"].strip()
        keys = payload["keys"]
        p256dh = keys["p256dh"].strip()
        auth = keys["auth"].strip()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({"error": "Subscription browser tidak valid."}, status=400)
    endpoint_url = urlsplit(endpoint)
    endpoint_host = endpoint_url.hostname or ""
    trusted_endpoint = endpoint_host in PUSH_ENDPOINT_HOSTS or endpoint_host.endswith(".notify.windows.com")
    if endpoint_url.scheme != "https" or not trusted_endpoint or len(endpoint) > 2048 or not p256dh or not auth:
        return JsonResponse({"error": "Subscription browser tidak valid."}, status=400)
    if len(p256dh) > 255 or len(auth) > 255:
        return JsonResponse({"error": "Kunci subscription tidak valid."}, status=400)
    PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={"user": request.user, "p256dh": p256dh, "auth": auth},
    )
    return JsonResponse({"subscribed": True})


@login_required
@require_POST
def push_unsubscribe(request):
    try:
        endpoint = json.loads(request.body)["endpoint"].strip()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({"error": "Subscription browser tidak valid."}, status=400)
    PushSubscription.objects.filter(user=request.user, endpoint=endpoint).delete()
    return JsonResponse({"subscribed": False})


def _can_access(user, thread):
    return accessible_threads(user).filter(pk=thread.pk).exists()


def _thread_members(thread):
    if not thread:
        return []
    users = get_user_model().objects.filter(is_active=True)
    if thread.kind == ChatThread.Kind.MODULE:
        return [user for user in users if user.is_superuser or module_level(user, thread.module) != "none"]
    return list(users.filter(chat_threads=thread))


def _thread_rows(user):
    threads = list(
        accessible_threads(user)
        .prefetch_related("participants")
        .annotate(last_message_at=Max("messages__created_at"))
        .order_by("-updated_at")
    )
    states = {
        state.thread_id: state.last_read_at
        for state in ChatReadState.objects.filter(user=user, thread__in=threads)
    }
    for thread in threads:
        if thread.kind == ChatThread.Kind.MODULE:
            thread.display_name = thread.get_module_display()
            thread.subtitle = "Room modul"
        else:
            peer = next((participant for participant in thread.participants.all() if participant.pk != user.pk), user)
            thread.display_name = peer.get_full_name() or peer.username
            thread.subtitle = f"@{peer.username}"
        thread.is_unread = bool(
            thread.last_message_at and thread.last_message_at > states.get(thread.id, thread.created_at)
        )
    return threads


@login_required
@xframe_options_sameorigin
def inbox(request, thread_id=None):
    embedded = request.GET.get("embed") == "1" or request.POST.get("embed") == "1"
    threads = _thread_rows(request.user)
    selected = (
        next((thread for thread in threads if thread.pk == thread_id), None)
        if thread_id
        else (None if embedded else (threads[0] if threads else None))
    )
    if thread_id and not selected:
        thread = get_object_or_404(ChatThread, pk=thread_id)
        if not _can_access(request.user, thread):
            return HttpResponseForbidden("Anda tidak memiliki akses ke percakapan ini.")
        selected = thread

    form = ChatMessageForm(request.POST or None, request.FILES or None)
    thread_members = _thread_members(selected)
    if request.method == "POST":
        if not selected:
            return HttpResponseBadRequest("Pilih percakapan terlebih dahulu.")
        if form.is_valid():
            reply = None
            if form.cleaned_data.get("reply_to"):
                reply = get_object_or_404(
                    ChatMessage,
                    pk=form.cleaned_data["reply_to"],
                    thread=selected,
                )
            attachment = form.cleaned_data.get("attachment")
            message = ChatMessage.objects.create(
                thread=selected,
                sender=request.user,
                body=form.cleaned_data.get("body", "").strip(),
                attachment=attachment or "",
                original_name=getattr(attachment, "original_name", ""),
                reply_to=reply,
            )
            usernames = set(MENTION_PATTERN.findall(message.body))
            if usernames:
                message.mentions.set(user for user in thread_members if user.username in usernames)
            selected.save(update_fields=("updated_at",))
            ChatReadState.objects.update_or_create(
                thread=selected,
                user=request.user,
                defaults={"last_read_at": message.created_at},
            )
            if selected.kind == ChatThread.Kind.DIRECT:
                recipient_ids = [user.pk for user in thread_members if user.pk != request.user.pk]
            else:
                recipient_ids = list(message.mentions.exclude(pk=request.user.pk).values_list("pk", flat=True))
            if recipient_ids:
                transaction.on_commit(
                    lambda recipients=recipient_ids, message_id=message.id: send_web_push(
                        recipients,
                        title="Pesan baru di Vobia Space",
                        body="Buka Space untuk melihat pesan.",
                        url=reverse("chat:inbox"),
                        tag=f"chat:{message_id}",
                    )
                )
            target = reverse("chat:thread", args=[selected.id])
            return redirect(f"{target}?embed=1" if embedded else target)

    query = request.GET.get("q", "").strip()[:120]
    reply_message = None
    chat_messages = []
    if selected:
        message_query = selected.messages.select_related("sender", "reply_to__sender").prefetch_related("mentions")
        if query:
            message_query = message_query.filter(
                Q(body__icontains=query)
                | Q(sender__username__icontains=query)
                | Q(sender__first_name__icontains=query)
                | Q(sender__last_name__icontains=query)
                | Q(original_name__icontains=query)
            )
        chat_messages = list(message_query.order_by("-created_at")[:100])[::-1]
        for message in chat_messages:
            message.mentioned_user = any(user.pk == request.user.pk for user in message.mentions.all())
        reply_id = request.GET.get("reply", "")
        if reply_id:
            reply_message = selected.messages.select_related("sender").filter(pk=reply_id).first()
            if reply_message:
                form.initial["reply_to"] = reply_message.id
        latest = selected.messages.order_by("-created_at").values_list("created_at", flat=True).first()
        ChatReadState.objects.update_or_create(
            thread=selected,
            user=request.user,
            defaults={"last_read_at": latest or timezone.now()},
        )

    users = get_user_model().objects.filter(is_active=True).exclude(pk=request.user.pk).order_by(
        "first_name", "username"
    )
    return render(
        request,
        "chat/inbox.html",
        {
            "threads": threads,
            "selected": selected,
            "chat_messages": chat_messages,
            "form": form,
            "query": query,
            "reply_message": reply_message,
            "users": users,
            "mention_users": [user for user in thread_members if user.pk != request.user.pk],
            "embedded": embedded,
        },
    )


@login_required
@require_POST
def start_direct(request, user_id):
    target = get_object_or_404(get_user_model(), pk=user_id, is_active=True)
    if target.pk == request.user.pk:
        return HttpResponseBadRequest("Tidak dapat membuat chat personal dengan diri sendiri.")
    ids = sorted((str(request.user.pk), str(target.pk)))
    thread, _created = ChatThread.objects.get_or_create(
        key=f"direct:{ids[0]}:{ids[1]}",
        defaults={"kind": ChatThread.Kind.DIRECT, "created_by": request.user},
    )
    thread.participants.add(request.user, target)
    target_url = reverse("chat:thread", args=[thread.id])
    return redirect(f"{target_url}?embed=1" if request.POST.get("embed") == "1" else target_url)


@login_required
@xframe_options_sameorigin
def attachment(request, message_id):
    message = get_object_or_404(ChatMessage.objects.select_related("thread"), pk=message_id)
    if not message.attachment or not _can_access(request.user, message.thread):
        raise Http404
    served_name = message.original_name or message.attachment.name.rsplit("/", 1)[-1]
    content_type = guess_type(served_name)[0] or "application/octet-stream"
    if request.GET.get("download") == "1" or request.GET.get("raw") == "1":
        if request.GET.get("raw") == "1" and message.attachment_kind == "document":
            raise Http404
        response = FileResponse(
            message.attachment.open("rb"),
            as_attachment=request.GET.get("download") == "1",
            filename=served_name,
            content_type=content_type,
        )
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        return response
    return render(
        request,
        "chat/attachment_preview.html",
        {"message": message, "served_name": served_name},
    )
