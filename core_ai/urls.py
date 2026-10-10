from django.urls import path
from . import api, portal

urlpatterns = [
    path('organizations/<slug:slug>/ai/', portal.index, name='ai'),
    path('organizations/<slug:slug>/ai/admin/', portal.administration, name='ai_admin'),
    path('organizations/<slug:slug>/ai/sources/new/', portal.source_edit, name='ai_source_add'),
    path('organizations/<slug:slug>/ai/sources/<uuid:pk>/', portal.source_edit, name='ai_source_edit'),
    path('api/platform/v1/ai/sources/', api.sources),
    path('api/platform/v1/ai/query/', api.unified_query),
]
