from django.contrib import admin
from identity.admin import PlatformOnly
from .models import AgentListing, MarketplacePackage


@admin.register(MarketplacePackage)
class MarketplaceAdmin(PlatformOnly, admin.ModelAdmin):
    list_display = ["name", "slug", "version", "kind", "active"]
    search_fields = ["name", "slug"]

    def get_readonly_fields(self, request, obj=None):
        return ["slug", "kind"] if obj else []


@admin.register(AgentListing)
class AgentListingAdmin(PlatformOnly, admin.ModelAdmin):
    list_display = ['name', 'slug', 'version', 'kind', 'active']
