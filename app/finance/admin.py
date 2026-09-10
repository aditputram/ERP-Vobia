from django.contrib import admin

from .models import Account, JournalEntry, JournalLine, JournalNumberSequence


class JournalLineInline(admin.TabularInline):
    model = JournalLine
    extra = 0


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "account_type", "parent", "is_postable", "is_active")
    search_fields = ("code", "name")
    list_filter = ("account_type", "is_postable", "is_active")


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ("number", "entry_date", "description", "source", "status", "posted_at")
    search_fields = ("number", "description", "reference")
    list_filter = ("source", "status", "entry_date")
    inlines = (JournalLineInline,)


admin.site.register(JournalNumberSequence)
