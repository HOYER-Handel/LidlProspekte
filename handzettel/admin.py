from django.contrib import admin
from .models import Handzettel

admin.site.register(Handzettel)


class HandzettelAdmin(admin.ModelAdmin):
    list_display = ("created_at", "supermarkt", "titel", "open_file")
    list_filter = ("supermarkt",)
    search_fields = ("titel",)

    def open_file(self, obj):
        if obj.datei:
            return format_html('<a href="{}" target="_blank">Öffnen</a>', obj.datei.url)
        return "-"

    open_file.short_description = "Datei"
