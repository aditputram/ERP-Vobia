import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from .context_processors import unread_chat
from .models import ChatMessage, ChatReadState, ChatThread, PushSubscription, ensure_module_rooms


class ChatTests(TestCase):
    def setUp(self):
        self.upload_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.upload_directory.cleanup)
        self.media_override = override_settings(MEDIA_ROOT=self.upload_directory.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        no_access = {
            "sales": "none",
            "operation": "none",
            "rnd": "none",
            "marketing": "none",
            "finance": "none",
        }
        self.rnd = get_user_model().objects.create_user(
            "rnd.user",
            password="test-password",
            module_access={**no_access, "rnd": "edit"},
        )
        self.marketing = get_user_model().objects.create_user(
            "marketing.user",
            password="test-password",
            module_access={**no_access, "marketing": "edit"},
        )
        self.rnd_peer = get_user_model().objects.create_user(
            "rnd.peer",
            password="test-password",
            first_name="Rani",
            last_name="Development",
            module_access={**no_access, "rnd": "edit"},
        )
        self.outsider = get_user_model().objects.create_user(
            "outsider",
            password="test-password",
            module_access=no_access,
        )
        ensure_module_rooms()

    def test_module_rooms_follow_module_access(self):
        self.client.force_login(self.rnd)
        response = self.client.get(reverse("chat:inbox"))

        self.assertEqual(
            [thread.module for thread in response.context["threads"]],
            ["rnd"],
        )
        finance = ChatThread.objects.get(key="module:finance")
        self.assertEqual(
            self.client.get(reverse("chat:thread", args=[finance.id])).status_code,
            403,
        )
        self.assertContains(response, 'data-mention-username="rnd.peer"')
        self.assertNotContains(response, 'data-mention-username="marketing.user"')
        self.assertContains(response, "body.setRangeText")
        self.assertContains(response, "personalMenu.contains")
        self.assertContains(response, "pointerdown")

    def test_laptop_notification_activation_is_in_message_popup_for_all_users(self):
        for user in (self.marketing, self.rnd):
            self.client.force_login(user)
            response = self.client.get(reverse("dashboard:index"))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'class="chat-push-button"')
            self.assertEqual(response.content.count(b"type=\"button\" data-push-enable"), 1)
            self.assertContains(response, 'data-config-url="/messages/push/config/"')
            self.assertContains(response, 'data-subscribe-url="/messages/push/subscribe/"')

    def test_recently_active_conversation_moves_to_top(self):
        first = ChatThread.objects.create(
            kind=ChatThread.Kind.DIRECT,
            key=f"direct:{self.rnd.id}:{self.marketing.id}",
            created_by=self.rnd,
        )
        first.participants.add(self.rnd, self.marketing)
        second = ChatThread.objects.create(
            kind=ChatThread.Kind.DIRECT,
            key=f"direct:{self.rnd.id}:{self.outsider.id}",
            created_by=self.rnd,
        )
        second.participants.add(self.rnd, self.outsider)
        self.client.force_login(self.rnd)

        self.client.post(reverse("chat:thread", args=[first.id]), {"body": "Pesan terbaru"})
        response = self.client.get(reverse("chat:inbox"))

        self.assertEqual(response.context["threads"][0], first)

    def test_personal_chat_supports_mentions_replies_and_private_attachments(self):
        self.client.force_login(self.rnd)
        started = self.client.post(reverse("chat:start_direct", args=[self.marketing.id]))
        thread = ChatThread.objects.get(kind=ChatThread.Kind.DIRECT)
        self.assertRedirects(started, reverse("chat:thread", args=[thread.id]))
        composer = self.client.get(reverse("chat:thread", args=[thread.id]))
        self.assertContains(composer, 'data-mention-username="marketing.user"')
        self.assertNotContains(composer, 'data-mention-username="outsider"')

        sent = self.client.post(
            reverse("chat:thread", args=[thread.id]),
            {
                "body": "Halo @marketing.user",
                "attachment": SimpleUploadedFile(
                    "brief.pdf",
                    b"%PDF-1.4\nchat-test",
                    content_type="application/pdf",
                ),
            },
        )
        self.assertEqual(sent.status_code, 302)
        first = ChatMessage.objects.get(thread=thread)
        self.assertEqual(list(first.mentions.all()), [self.marketing])
        self.assertEqual(first.original_name, "brief.pdf")
        thread_page = self.client.get(reverse("chat:thread", args=[thread.id]))
        self.assertContains(thread_page, 'aria-label="Balas pesan"')
        self.assertNotContains(thread_page, ">Balas</a>")
        self.assertContains(thread_page, "Lihat lampiran")
        self.assertContains(thread_page, "?download=1")
        self.assertContains(thread_page, "URL.createObjectURL")
        self.assertContains(thread_page, "data-chat-attachment-preview")

        replied = self.client.post(
            reverse("chat:thread", args=[thread.id]),
            {"body": "Sudah dikirim.", "reply_to": str(first.id)},
        )
        self.assertEqual(replied.status_code, 302)
        self.assertEqual(ChatMessage.objects.exclude(pk=first.pk).get().reply_to, first)

        image_sent = self.client.post(
            reverse("chat:thread", args=[thread.id]),
            {
                "attachment": SimpleUploadedFile(
                    "sample.png",
                    b"\x89PNG\r\n\x1a\nchat-test",
                    content_type="image/png",
                ),
            },
        )
        self.assertEqual(image_sent.status_code, 302)
        image_message = ChatMessage.objects.get(original_name="sample.png")
        self.assertEqual(image_message.attachment_kind, "image")
        image_page = self.client.get(reverse("chat:thread", args=[thread.id]))
        self.assertContains(image_page, f'{reverse("chat:attachment", args=[image_message.id])}?raw=1')
        self.assertContains(image_page, "data-chat-image-open")
        self.assertContains(image_page, "data-chat-image-dialog")
        self.assertContains(image_page, "imageDialog.showModal()")
        self.assertContains(image_page, "chat-dialog-close")
        self.assertContains(image_page, "chat-dialog-download")

        self.client.force_login(self.marketing)
        attachment_url = reverse("chat:attachment", args=[first.id])
        preview = self.client.get(attachment_url)
        self.assertContains(preview, "<iframe")
        self.assertContains(preview, f"{attachment_url}?raw=1")
        self.assertContains(preview, f"{attachment_url}?download=1")
        raw = self.client.get(f"{attachment_url}?raw=1")
        self.assertEqual(raw.status_code, 200)
        self.assertIn("inline", raw["Content-Disposition"])
        self.assertEqual(raw["X-Frame-Options"], "SAMEORIGIN")
        download = self.client.get(f"{attachment_url}?download=1")
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download["Content-Disposition"])
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(attachment_url).status_code, 404)

    def test_unread_badge_clears_when_thread_is_opened(self):
        thread = ChatThread.objects.create(
            kind=ChatThread.Kind.DIRECT,
            key=f"direct:{self.rnd.id}:{self.marketing.id}",
            created_by=self.rnd,
        )
        thread.participants.add(self.rnd, self.marketing)
        ChatMessage.objects.create(thread=thread, sender=self.rnd, body="Pesan baru")
        request = RequestFactory().get("/")
        request.user = self.marketing

        self.assertEqual(unread_chat(request)["chat_unread_count"], 1)
        self.client.force_login(self.marketing)
        live_status = self.client.get(reverse("dashboard:live_status")).json()
        self.assertEqual(live_status["chat_unread_count"], 1)
        self.assertEqual(live_status["rnd_notifications"], [])
        self.client.get(reverse("chat:thread", args=[thread.id]))
        self.assertTrue(ChatReadState.objects.filter(thread=thread, user=self.marketing).exists())
        self.assertEqual(unread_chat(request)["chat_unread_count"], 0)

    def test_embedded_chat_stays_embedded_after_sending(self):
        self.client.force_login(self.rnd)
        thread = ChatThread.objects.get(key="module:rnd")

        embedded = self.client.get(f"{reverse('chat:thread', args=[thread.id])}?embed=1")
        self.assertEqual(embedded.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertContains(embedded, "composer.requestSubmit()")
        self.assertContains(embedded, "!event.shiftKey")

        response = self.client.post(
            f"{reverse('chat:thread', args=[thread.id])}?embed=1",
            {"body": "Pesan popup", "embed": "1"},
        )

        self.assertRedirects(response, f"{reverse('chat:thread', args=[thread.id])}?embed=1")

    @override_settings(WEB_PUSH_VAPID_PUBLIC_KEY="public-test-key")
    def test_browser_can_register_push_subscription_and_load_service_worker(self):
        worker = self.client.get(reverse("service_worker"))
        self.assertEqual(worker.status_code, 200)
        self.assertEqual(worker["Service-Worker-Allowed"], "/")
        self.assertContains(worker, "showNotification")

        self.client.force_login(self.rnd)
        config = self.client.get(reverse("chat:push_config")).json()
        self.assertEqual(config, {"enabled": True, "public_key": "public-test-key"})
        response = self.client.post(
            reverse("chat:push_subscribe"),
            data={
                "endpoint": "https://fcm.googleapis.com/subscription-1",
                "keys": {"p256dh": "browser-key", "auth": "browser-auth"},
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        subscription = PushSubscription.objects.get()
        self.assertEqual(subscription.user, self.rnd)

        self.client.force_login(self.rnd_peer)
        self.client.post(
            reverse("chat:push_subscribe"),
            data={
                "endpoint": "https://fcm.googleapis.com/subscription-1",
                "keys": {"p256dh": "new-browser-key", "auth": "new-browser-auth"},
            },
            content_type="application/json",
        )
        subscription.refresh_from_db()
        self.assertEqual(subscription.user, self.rnd_peer)

    @override_settings(WEB_PUSH_VAPID_PRIVATE_KEY="private-test-key")
    @patch("chat.push.webpush")
    def test_pushes_personal_chat_and_group_mentions_only(self, webpush_mock):
        PushSubscription.objects.create(
            user=self.marketing,
            endpoint="https://fcm.googleapis.com/marketing",
            p256dh="marketing-key",
            auth="marketing-auth",
        )
        PushSubscription.objects.create(
            user=self.rnd_peer,
            endpoint="https://fcm.googleapis.com/rnd-peer",
            p256dh="rnd-key",
            auth="rnd-auth",
        )
        direct = ChatThread.objects.create(
            kind=ChatThread.Kind.DIRECT,
            key=f"direct:{self.rnd.id}:{self.marketing.id}",
            created_by=self.rnd,
        )
        direct.participants.add(self.rnd, self.marketing)
        self.client.force_login(self.rnd)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("chat:thread", args=[direct.id]), {"body": "Isi privat"})
        self.assertEqual(webpush_mock.call_count, 1)
        self.assertEqual(webpush_mock.call_args.kwargs["data"].count("Isi privat"), 0)

        room = ChatThread.objects.get(key="module:rnd")
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("chat:thread", args=[room.id]), {"body": "Tanpa mention"})
        self.assertEqual(webpush_mock.call_count, 1)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("chat:thread", args=[room.id]), {"body": "Halo @rnd.peer"})
        self.assertEqual(webpush_mock.call_count, 2)
        self.assertEqual(webpush_mock.call_args.kwargs["subscription_info"]["endpoint"], "https://fcm.googleapis.com/rnd-peer")
