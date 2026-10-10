from datetime import datetime

from django import forms
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from identity.access import effective_tags, record_decision, request_principal, require_access, visible_resources
from identity.views import organization_admin
from platform_services.models import AgentBinding, Dataset, Installation
from .models import FabricContract, FabricPeer
from .services import administer, approve, canonical, capability, fingerprint, revoke, terms, validate_terms


@organization_admin
def index(request, organization):
    principal = request_principal(request)
    capability(principal, organization, 'fabric.manage')
    rows = []
    for item in visible_resources(principal, organization.fabric_contracts.select_related('peer').order_by('-created')[:100]):
        rows.append({'contract': item, 'terms_json': canonical(terms(item)), 'hash': fingerprint(item),
            'approved': bool(item.approved_at and item.approved_hash == fingerprint(item))})
    return render(request, 'core_fabric/index.html', {'organization': organization, 'contracts': rows,
        'peers': visible_resources(principal, organization.fabric_peers.all())})


@organization_admin
def peer_edit(request, organization, pk=None):
    principal = request_principal(request)
    capability(principal, organization, 'fabric.manage')
    item = get_object_or_404(FabricPeer, organization=organization, pk=pk) if pk else FabricPeer(organization=organization)
    if pk:
        require_access(principal, item, 'fabric.peer_manage')
    class PeerForm(forms.ModelForm):
        class Meta:
            model = FabricPeer
            fields = ['name', 'issuer', 'remote_organization_id', 'inbound_installation', 'credential_file', 'active']
    form = PeerForm(request.POST or None, instance=item)
    apps = Installation.objects.filter(organization=organization, package__kind='application')
    form.fields['inbound_installation'].queryset = apps.filter(pk__in=[app.pk for app in visible_resources(principal, apps)])
    if pk:
        for field in ['issuer', 'remote_organization_id', 'inbound_installation']:
            form.fields[field].disabled = True
    if request.method == 'POST' and form.is_valid():
        require_access(principal, form.cleaned_data['inbound_installation'], 'fabric.peer_application')
        item = form.save()
        record_decision(principal, item, 'fabric.peer_configured', True, 'administrator_routing', effective_tags(item))
        return redirect('fabric', slug=organization.slug)
    return render(request, 'identity/form.html', {'title': 'Data fabric peer', 'form': form})


def consumer_fields(form, organization, principal):
    for field, queryset in [('allowed_installations', Installation.objects.filter(organization=organization, package__kind='application', enabled=True)),
            ('allowed_bindings', AgentBinding.objects.filter(account__organization=organization, active=True))]:
        form.fields[field].queryset = queryset.filter(pk__in=[item.pk for item in visible_resources(principal, queryset)])


@organization_admin
def contract_edit(request, organization, pk=None):
    principal = request_principal(request)
    capability(principal, organization, 'fabric.manage')
    item = get_object_or_404(FabricContract, organization=organization, pk=pk) if pk else FabricContract(organization=organization, direction='export')
    if pk:
        administer(principal, item)
    class ContractForm(forms.ModelForm):
        class Meta:
            model = FabricContract
            fields = ['name', 'peer', 'dataset', 'purpose_code', 'fields', 'filters', 'allow_read', 'allow_write', 'expires_at',
                'allowed_installations', 'allowed_bindings', 'active']

        def clean(self):
            values = super().clean()
            if self.instance.direction == 'export' and values.get('dataset'):
                self.instance.data_class = values['dataset'].data_class
            return values
    form = ContractForm(request.POST or None, instance=item)
    for field, queryset in [('peer', FabricPeer.objects.filter(organization=organization, active=True)),
            ('dataset', Dataset.objects.filter(organization=organization, active=True))]:
        form.fields[field].queryset = queryset.filter(pk__in=[resource.pk for resource in visible_resources(principal, queryset)])
    consumer_fields(form, organization, principal)
    if pk:
        form.fields['peer'].disabled = form.fields['dataset'].disabled = True
    if request.method == 'POST' and form.is_valid():
        for resource in [form.cleaned_data['peer'], form.cleaned_data.get('dataset'),
                *form.cleaned_data['allowed_installations'], *form.cleaned_data['allowed_bindings']]:
            if resource:
                require_access(principal, resource, 'fabric.scope_manage')
        item = form.save()
        record_decision(principal, item, 'fabric.contract_configured', True, 'approval_required', effective_tags(item))
        return redirect('fabric', slug=organization.slug)
    return render(request, 'identity/form.html', {'title': 'Data federation agreement (changes require matching approval on both deployments)', 'form': form})


@organization_admin
def import_terms(request, organization):
    principal = request_principal(request)
    capability(principal, organization, 'fabric.manage')
    class ImportForm(forms.Form):
        name = forms.CharField(max_length=160)
        peer = forms.ModelChoiceField(queryset=FabricPeer.objects.none())
        terms = forms.JSONField(label='Exact exported agreement terms (contains no credentials)')
        allowed_installations = forms.ModelMultipleChoiceField(queryset=Installation.objects.none(), required=False)
        allowed_bindings = forms.ModelMultipleChoiceField(queryset=AgentBinding.objects.none(), required=False)
    form = ImportForm(request.POST or None)
    peers = FabricPeer.objects.filter(organization=organization, active=True)
    form.fields['peer'].queryset = peers.filter(pk__in=[item.pk for item in visible_resources(principal, peers)])
    consumer_fields(form, organization, principal)
    if request.method == 'POST' and form.is_valid():
        values, peer = form.cleaned_data['terms'], form.cleaned_data['peer']
        try:
            validate_terms(values)
            from django.conf import settings
            if (values['recipient'] != {'issuer': settings.ISSUER, 'organization_id': str(organization.pk)} or
                    values['source']['issuer'] != peer.issuer or values['source']['organization_id'] != str(peer.remote_organization_id)):
                raise ValidationError('The terms do not bind this organization to the selected peer.')
            item = FabricContract(organization=organization, agreement_id=values['agreement_id'], name=form.cleaned_data['name'], peer=peer,
                direction='import', remote_dataset_id=values['source']['dataset_id'], data_class=values['source']['data_class'],
                purpose_code=values['purpose_code'], fields=values['fields'], filters=values['filters'],
                allow_read=values['allow_read'], allow_write=values['allow_write'],
                expires_at=datetime.fromisoformat(values['expires_at']) if values['expires_at'] else None)
            item.full_clean()
            for resource in [peer, *form.cleaned_data['allowed_installations'], *form.cleaned_data['allowed_bindings']]:
                require_access(principal, resource, 'fabric.scope_manage')
            item.save()
            item.allowed_installations.set(form.cleaned_data['allowed_installations'])
            item.allowed_bindings.set(form.cleaned_data['allowed_bindings'])
            record_decision(principal, item, 'fabric.contract_imported', True, 'recipient_approval_required', effective_tags(item))
        except ValidationError:
            form.add_error('terms', 'Use a valid matching agreement that has not already been imported.')
        else:
            return redirect('fabric', slug=organization.slug)
    return render(request, 'identity/form.html', {'title': 'Import and review a federation agreement', 'form': form})


@organization_admin
@require_POST
def approve_contract(request, organization, pk):
    item = get_object_or_404(FabricContract, organization=organization, pk=pk)
    try:
        approve(item, request_principal(request), request.POST.get('terms_hash'))
    except ValidationError:
        return render(request, 'core_fabric/error.html', {'message': 'Review the current agreement terms and peer before approving.'}, status=400)
    return redirect('fabric', slug=organization.slug)


@organization_admin
@require_POST
def revoke_contract(request, organization, pk):
    item = get_object_or_404(FabricContract, organization=organization, pk=pk)
    revoke(item, request_principal(request))
    return redirect('fabric', slug=organization.slug)
