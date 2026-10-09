from django.urls import path
from . import api, portal

urlpatterns = [
    path('organizations/<slug:slug>/chat/', portal.index, name='chat'),
    path('organizations/<slug:slug>/chat/new/', portal.create, name='chat_create'),
    path('organizations/<slug:slug>/chat/admin/', portal.administration, name='chat_admin'),
    path('organizations/<slug:slug>/chat/connections/new/', portal.connection_edit, name='chat_connection_add'),
    path('organizations/<slug:slug>/chat/connections/<uuid:pk>/', portal.connection_edit, name='chat_connection_edit'),
    path('organizations/<slug:slug>/chat/<uuid:pk>/', portal.detail, name='chat_room'),
    path('organizations/<slug:slug>/chat/<uuid:pk>/feed/', portal.feed, name='chat_feed'),
    path('organizations/<slug:slug>/chat/<uuid:pk>/participants/', portal.settings, name='chat_settings'),
    path('organizations/<slug:slug>/chat/<uuid:pk>/settings/', portal.edit, name='chat_edit'),
    path('api/platform/v1/chat/conversations/', api.conversations),
    path('api/platform/v1/chat/conversations/create/', api.create),
    path('api/platform/v1/chat/conversations/<uuid:pk>/messages/', api.messages),
    path('api/platform/v1/chat/conversations/<uuid:pk>/send/', api.send),
    path('api/platform/v1/chat/conversations/<uuid:pk>/participants/', api.participant),
]
