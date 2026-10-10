from django.contrib import admin
from identity.admin import PlatformOnly
from .models import AgentListing, MarketplacePackage
from .models import DocumentationSource


@admin.register(MarketplacePackage)
class MarketplaceAdmin(PlatformOnly, admin.ModelAdmin):
    list_display = ["name", "slug", "version", "kind", "active"]
    search_fields = ["name", "slug"]

    def get_readonly_fields(self, request, obj=None):
        return ["slug", "kind"] if obj else []


@admin.register(AgentListing)
class AgentListingAdmin(PlatformOnly, admin.ModelAdmin):
    list_display = ['name', 'slug', 'version', 'kind', 'active']


@admin.register(DocumentationSource)
class DocumentationSourceAdmin(PlatformOnly, admin.ModelAdmin):
    list_display = ['title', 'package', 'version', 'revision', 'active']
    readonly_fields = [field.name for field in DocumentationSource._meta.fields if field.name != 'active']

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
