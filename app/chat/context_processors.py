from django.db.models import Max

from .models import ChatReadState, accessible_threads


def unread_chat(request):
    if not getattr(request.user, "is_authenticated", False) or not getattr(request.user, "pk", None):
        return {"chat_unread_count": 0}
    threads = list(accessible_threads(request.user).annotate(last_message_at=Max("messages__created_at")))
    states = {
        state.thread_id: state.last_read_at
        for state in ChatReadState.objects.filter(user=request.user, thread__in=threads)
    }
    unread = sum(
        bool(thread.last_message_at and thread.last_message_at > states.get(thread.id, thread.created_at))
        for thread in threads
    )
    return {"chat_unread_count": unread}
