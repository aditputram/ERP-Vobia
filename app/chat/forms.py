from pathlib import Path

from django import forms


class ChatMessageForm(forms.Form):
    MAX_FILE_BYTES = 10 * 1024 * 1024
    FILE_TYPES = {
        "application/pdf": (b"%PDF",),
        "image/jpeg": (b"\xff\xd8\xff",),
        "image/png": (b"\x89PNG\r\n\x1a\n",),
        "image/webp": (b"RIFF",),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (b"PK\x03\x04",),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (b"PK\x03\x04",),
    }

    body = forms.CharField(
        required=False,
        max_length=4000,
        widget=forms.Textarea(
            attrs={"rows": 3, "placeholder": "Tulis pesan. Gunakan @username untuk mention."}
        ),
    )
    attachment = forms.FileField(
        required=False,
        widget=forms.FileInput(attrs={"accept": ".jpg,.jpeg,.png,.webp,.pdf,.docx,.xlsx"}),
    )
    reply_to = forms.UUIDField(required=False, widget=forms.HiddenInput)

    def clean_attachment(self):
        uploaded = self.cleaned_data.get("attachment")
        if not uploaded:
            return uploaded
        if uploaded.size > self.MAX_FILE_BYTES:
            raise forms.ValidationError("Lampiran maksimal 10 MB.")
        signatures = self.FILE_TYPES.get(uploaded.content_type, ())
        header = uploaded.read(12)
        uploaded.seek(0)
        if not signatures or not any(header.startswith(signature) for signature in signatures):
            raise forms.ValidationError("Lampiran harus berupa JPG, PNG, WebP, PDF, DOCX, atau XLSX yang valid.")
        if uploaded.content_type == "image/webp" and header[8:12] != b"WEBP":
            raise forms.ValidationError("File WebP tidak valid.")
        uploaded.original_name = Path(uploaded.name).name[:255]
        return uploaded

    def clean(self):
        cleaned = super().clean()
        if not (cleaned.get("body") or cleaned.get("attachment")):
            raise forms.ValidationError("Tulis pesan atau pilih lampiran.")
        return cleaned

