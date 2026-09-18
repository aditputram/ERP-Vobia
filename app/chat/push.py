import json
import logging

from django.conf import settings
from pywebpush import WebPushException, webpush

from .models import PushSubscription


logger = logging.getLogger(__name__)


def send_web_push(user_ids, *, title, body, url, tag):
    private_key = settings.WEB_PUSH_VAPID_PRIVATE_KEY
    if not private_key:
        return 0

    payload = json.dumps(
        {"title": title, "body": body, "url": url, "tag": tag},
        ensure_ascii=False,
    )
    sent = 0
    subscriptions = PushSubscription.objects.filter(user_id__in=set(user_ids))
    for subscription in subscriptions:
        try:
            webpush(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
                data=payload,
                vapid_private_key=private_key,
                vapid_claims={"sub": settings.WEB_PUSH_VAPID_SUBJECT},
                timeout=3,
                ttl=24 * 60 * 60,
            )
            sent += 1
        except WebPushException as exc:
            if exc.status_code in {404, 410}:
                subscription.delete()
            else:
                logger.warning("Web Push gagal untuk subscription %s: %s", subscription.pk, exc)
        except Exception:
            logger.exception("Web Push gagal untuk subscription %s", subscription.pk)
    return sent
