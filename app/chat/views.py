import re
from mimetypes import guess_type

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Max, Q
from django.http import FileResponse, Http404, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import ChatMessageForm
from .models import ChatMessage, ChatReadState, ChatThread, accessible_threads


MENTION_PATTERN = re.compile(r"(?<!\w)@([\w.-]{1,150})")


def _can_access(user, thread):
    return accessible_threads(user).filter(pk=thread.pk).exists()


def _thread_rows(user):
    threads = list(
        accessible_threads(user)
        .prefetch_related("participants")
        .annotate(last_message_at=Max("messages__created_at"))
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
def inbox(request, thread_id=None):
    embedded = request.GET.get("embed") == "1" or request.POST.get("embed") == "1"
    threads = _thread_rows(request.user)
    selected = (
        next((thread for thread in threads if thread.pk == thread_id), None)
        if thread_id
        else (threads[0] if threads else None)
    )
    if thread_id and not selected:
        thread = get_object_or_404(ChatThread, pk=thread_id)
        if not _can_access(request.user, thread):
            return HttpResponseForbidden("Anda tidak memiliki akses ke percakapan ini.")
        selected = thread

    form = ChatMessageForm(request.POST or None, request.FILES or None)
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
                message.mentions.set(get_user_model().objects.filter(is_active=True, username__in=usernames))
            selected.save(update_fields=("updated_at",))
            ChatReadState.objects.update_or_create(
                thread=selected,
                user=request.user,
                defaults={"last_read_at": message.created_at},
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
def attachment(request, message_id):
    message = get_object_or_404(ChatMessage.objects.select_related("thread"), pk=message_id)
    if not message.attachment or not _can_access(request.user, message.thread):
        raise Http404
    served_name = message.original_name or message.attachment.name.rsplit("/", 1)[-1]
    return FileResponse(
        message.attachment.open("rb"),
        as_attachment=True,
        filename=served_name,
        content_type=guess_type(served_name)[0] or "application/octet-stream",
    )
