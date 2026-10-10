from django.urls import path
from . import api, portal

urlpatterns = [
    path('organizations/<slug:slug>/fabric/', portal.index, name='fabric'),
    path('organizations/<slug:slug>/fabric/peers/new/', portal.peer_edit, name='fabric_peer_add'),
    path('organizations/<slug:slug>/fabric/peers/<uuid:pk>/', portal.peer_edit, name='fabric_peer_edit'),
    path('organizations/<slug:slug>/fabric/contracts/new/', portal.contract_edit, name='fabric_contract_add'),
    path('organizations/<slug:slug>/fabric/import/', portal.import_terms, name='fabric_import'),
    path('organizations/<slug:slug>/fabric/contracts/<uuid:pk>/', portal.contract_edit, name='fabric_contract_edit'),
    path('organizations/<slug:slug>/fabric/contracts/<uuid:pk>/approve/', portal.approve_contract, name='fabric_approve'),
    path('organizations/<slug:slug>/fabric/contracts/<uuid:pk>/revoke/', portal.revoke_contract, name='fabric_revoke'),
    path('api/platform/v1/fabric/contracts/', api.contracts),
    path('api/platform/v1/fabric/contracts/<uuid:pk>/records/', api.records),
    path('api/platform/v1/fabric/contracts/<uuid:pk>/events/', api.write),
    path('api/platform/v1/fabric/permits/<uuid:pk>/verify/', api.verify_permit),
    path('api/platform/v1/fabric/exports/<uuid:agreement_id>/read/', api.export_read),
    path('api/platform/v1/fabric/exports/<uuid:agreement_id>/write/', api.export_write),
]
