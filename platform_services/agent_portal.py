from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from identity.access import effective_tags, record_decision, request_principal, require_access, visible_resources
from identity.models import Role
from identity.views import organization_admin
from .agent_identity import install_listing, issue_credential
from .models import AgentAccount, AgentBinding, AgentCredential, AgentListing, AgentTask


@organization_admin
def install(request, organization, listing_slug):
    listing = get_object_or_404(AgentListing, slug=listing_slug, active=True)
    class InstallForm(forms.Form):
        slug = forms.SlugField(label='Agent name in this organization')
    form = InstallForm(request.POST or None, initial={'slug': listing.slug})
    if request.method == 'POST' and form.is_valid():
        try:
            agent = install_listing(organization, listing, form.cleaned_data['slug'], request_principal(request))
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect('agent_account', slug=organization.slug, pk=agent.account_id)
    return render(request, 'identity/form.html', {'form': form, 'title': f'Install {listing.name}; service permissions start empty'})


@organization_admin
def accounts(request, organization):
    principal = request_principal(request)
    return render(request, 'platform_services/agent_accounts.html', {'organization': organization,
        'accounts': visible_resources(principal, organization.agent_accounts.all()),
        'tasks': visible_resources(principal, organization.agent_tasks.all())})


@organization_admin
def account(request, organization, pk):
    item = get_object_or_404(AgentAccount, organization=organization, pk=pk)
    principal = request_principal(request)
    require_access(principal, item, 'agent.account_manage')
    class AccountForm(forms.ModelForm):
        class Meta:
            model = AgentAccount
            fields = ['name', 'active']
    form = AccountForm(request.POST or None, instance=item)
    if request.method == 'POST' and form.is_valid():
        form.save()
        record_decision(principal, item, 'agent.account_updated', True, 'administrator_updated', effective_tags(item))
        return redirect('agent_account', slug=organization.slug, pk=item.pk)
    bindings = visible_resources(principal, item.bindings.select_related('installation__package', 'task'))
    for binding in bindings:
        binding.visible_credentials = visible_resources(principal, binding.credentials.filter(active=True))
    return render(request, 'platform_services/agent_account.html', {'organization': organization, 'account': item,
        'form': form, 'bindings': bindings})


@organization_admin
def binding(request, organization, pk=None, account_pk=None):
    principal = request_principal(request)
    if pk:
        item = get_object_or_404(AgentBinding, pk=pk, account__organization=organization)
        require_access(principal, item, 'agent.approval_manage')
    else:
        item = AgentBinding(account=get_object_or_404(AgentAccount, pk=account_pk, organization=organization))
        require_access(principal, item.account, 'agent.approval_manage')
    class BindingForm(forms.ModelForm):
        class Meta:
            model = AgentBinding
            fields = ['installation', 'environment', 'task', 'roles', 'access_tags', 'active']
    form = BindingForm(request.POST or None, instance=item)
    for name, resources in [('installation', organization.installations.filter(enabled=True)),
            ('task', organization.agent_tasks.filter(active=True)), ('roles', Role.objects.filter(organization=organization))]:
        resources = resources.filter(pk__in=[value.pk for value in visible_resources(principal, resources)])
        form.fields[name].queryset = resources
    form.fields['access_tags'].queryset = organization.permission_tag_definitions.filter(active=True)
    if pk:
        for field in ['installation', 'environment', 'task']:
            form.fields[field].disabled = True
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            item = form.save()
            record_decision(principal, item, 'agent.approval_updated', True, 'administrator_approved', effective_tags(item))
        return redirect('agent_account', slug=organization.slug, pk=item.account_id)
    return render(request, 'identity/form.html', {'form': form,
        'title': 'Agent consumption approval: choose application, environment, and service roles'})


@organization_admin
def task(request, organization, pk=None):
    principal = request_principal(request)
    item = get_object_or_404(AgentTask, organization=organization, pk=pk) if pk else AgentTask(organization=organization, approved_by=request.user)
    if pk:
        require_access(principal, item, 'agent.task_manage')
    class TaskForm(forms.ModelForm):
        class Meta:
            model = AgentTask
            fields = ['name', 'description', 'resources', 'installation', 'environment', 'expires_at', 'active']
    form = TaskForm(request.POST or None, instance=item)
    form.fields['installation'].queryset = organization.installations.filter(pk__in=[value.pk for value in
        visible_resources(principal, organization.installations.filter(enabled=True))])
    if pk:
        for field in ['installation', 'environment']:
            form.fields[field].disabled = True
    if request.method == 'POST' and form.is_valid():
        from django.apps import apps
        for name, label in [('datasets', 'platform_services.Dataset'), ('conversations', 'core_chat.Conversation'), ('service_resources', 'identity.ProtectedResource'),
                ('fabric_contracts', 'core_fabric.FabricContract'), ('integrations', 'platform_services.ApplicationIntegration'), ('app_projects', 'platform_services.AppProject')]:
            for resource in apps.get_model(label).objects.filter(pk__in=form.cleaned_data['resources'].get(name, [])):
                require_access(principal, resource, 'agent.task_scope_approve')
        item = form.save(commit=False)
        item.approved_by = request.user
        item.save()
        record_decision(principal, item, 'agent.task_approved', True, 'administrator_approved', effective_tags(item))
        return redirect('agent_accounts', slug=organization.slug)
    return render(request, 'identity/form.html', {'form': form, 'title': 'Approved agent task and expiration'})


@organization_admin
@require_POST
@never_cache
def key(request, organization, pk):
    item = get_object_or_404(AgentBinding, pk=pk, account__organization=organization)
    credential, raw = issue_credential(item, request_principal(request))
    response = render(request, 'platform_services/agent_key.html', {'credential': credential, 'raw': raw, 'organization': organization})
    response['Cache-Control'] = 'no-store'
    return response


@organization_admin
@require_POST
def revoke(request, organization, pk):
    item = get_object_or_404(AgentCredential, pk=pk, binding__account__organization=organization)
    principal = request_principal(request)
    require_access(principal, item, 'agent.credential_revoke')
    item.active = False
    item.save(update_fields=['active'])
    record_decision(principal, item, 'agent.credential_revoked', True, 'administrator_revoked', effective_tags(item))
    return redirect('agent_account', slug=organization.slug, pk=item.binding.account_id)
