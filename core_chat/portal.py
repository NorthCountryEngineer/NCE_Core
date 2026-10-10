import uuid
from functools import wraps

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from identity.access import request_principal, require_access, visible_resources
from identity.models import Membership, Organization, User
from identity.views import organization_admin
from platform_services.data import IdempotencyConflict
from platform_services.models import AgentAccount, Installation
from .federation import FederationUnavailable
from .models import Conversation, FederationConnection, Participant
from .services import authorize, can_manage, capability, create_conversation, message_page, send_message


def member(view):
    @login_required
    @never_cache
    @wraps(view)
    def wrapped(request, slug, *args, **kwargs):
        organization = get_object_or_404(Organization, slug=slug, active=True)
        if not Membership.objects.filter(organization=organization, user=request.user, active=True).exists():
            raise PermissionDenied
        request.organization = organization
        require_access(request_principal(request), organization, 'chat.portal_access')
        return view(request, organization, *args, **kwargs)
    return wrapped


@member
def index(request, organization):
    principal = request_principal(request)
    capability(principal, organization, 'chat.read')
    conversations = []
    for room in organization.chat_conversations.filter(active=True):
        try:
            authorize(principal, room)
            conversations.append(room)
        except PermissionDenied:
            continue
    return render(request, 'core_chat/index.html', {'organization': organization, 'conversations': conversations})


class ConversationForm(forms.ModelForm):
    class Meta:
        model = Conversation
        fields = ['title', 'ai_enabled', 'active', 'connection', 'remote_conversation_id']

    def __init__(self, *args, organization, principal, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['connection'].queryset = FederationConnection.objects.filter(organization=organization,
            pk__in=[item.pk for item in visible_resources(principal, FederationConnection.objects.filter(organization=organization, active=True))])
        if self.instance.pk and not self.instance._state.adding:
            # Authority and routing are immutable for an established conversation.
            self.fields['connection'].disabled = True
            self.fields['remote_conversation_id'].disabled = True


@member
def create(request, organization):
    principal = request_principal(request)
    capability(principal, organization, 'chat.manage')
    form = ConversationForm(request.POST or None, organization=organization, principal=principal,
                            instance=Conversation(organization=organization))
    if request.method == 'POST' and form.is_valid():
        room = create_conversation(principal, organization, form.cleaned_data['title'], form.cleaned_data['ai_enabled'],
                                   form.cleaned_data['connection'], form.cleaned_data['remote_conversation_id'])
        return redirect('chat_room', slug=organization.slug, pk=room.pk)
    return render(request, 'identity/form.html', {'title': 'Create conversation', 'form': form})


@member
def detail(request, organization, pk):
    room = get_object_or_404(Conversation, organization=organization, pk=pk)
    principal = request_principal(request)
    participant = authorize(principal, room)
    class SendForm(forms.Form):
        text = forms.CharField(widget=forms.Textarea, max_length=16384, label='Message')
        request_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    form = SendForm(request.POST or None)
    unavailable = False
    if request.method == 'POST' and form.is_valid():
        try:
            send_message(principal, room, str(form.cleaned_data['request_id']), form.cleaned_data['text'])
        except (ValidationError, IdempotencyConflict, FederationUnavailable) as error:
            form.add_error(None, 'The shared instance is unavailable; retry with the same message ID.' if isinstance(error, FederationUnavailable) else 'This message could not be accepted.')
        else:
            return redirect('chat_room', slug=organization.slug, pk=room.pk)
    try:
        page = message_page(principal, room)
    except FederationUnavailable:
        page = {'messages': []}
        unavailable = True
    can_edit = False
    try:
        can_manage(principal, room)
        can_edit = True
    except PermissionDenied:
        pass
    return render(request, 'core_chat/room.html', {'organization': organization, 'conversation': room,
        'chat_messages': page['messages'], 'form': form, 'can_send': participant.role != 'reader', 'can_edit': can_edit,
        'unavailable': unavailable, 'chat_state': {'cursor': page.get('next_cursor', 0)}})


@member
@require_GET
def feed(request, organization, pk):
    from platform_services.api import response
    room = get_object_or_404(Conversation, organization=organization, pk=pk)
    try:
        return response(message_page(request_principal(request), room, after=int(request.GET.get('after', 0))))
    except (ValueError, ValidationError):
        return response({'error': 'invalid_cursor'}, 400)
    except FederationUnavailable:
        return response({'error': 'chat_authority_unavailable'}, 503)


class ParticipantForm(forms.ModelForm):
    class Meta:
        model = Participant
        fields = ['user', 'installation', 'agent', 'role', 'allow_ai', 'federated_issuer', 'active']

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['user'].queryset = User.objects.filter(memberships__organization=organization, memberships__active=True, is_active=True)
        self.fields['agent'].queryset = AgentAccount.objects.filter(organization=organization, active=True)
        self.fields['installation'].queryset = Installation.objects.filter(organization=organization, enabled=True)


@member
def settings(request, organization, pk):
    room = get_object_or_404(Conversation, organization=organization, pk=pk)
    principal = request_principal(request)
    can_manage(principal, room)
    instance = Participant(conversation=room)
    if request.GET.get('participant'):
        instance = get_object_or_404(room.participants, pk=request.GET['participant'])
        require_access(principal, instance, 'chat.participant_manage')
    form = ParticipantForm(request.POST or None, instance=instance, organization=organization)
    if request.method == 'POST' and form.is_valid():
        item = form.save()
        from identity.access import record_decision
        record_decision(principal, item, 'chat.participant_updated', True, 'owner_approved', [])
        return redirect('chat_settings', slug=organization.slug, pk=room.pk)
    return render(request, 'core_chat/settings.html', {'organization': organization, 'conversation': room, 'form': form,
        'participants': visible_resources(principal, room.participants.select_related('user', 'installation__package'))})


@member
def edit(request, organization, pk):
    room = get_object_or_404(Conversation, organization=organization, pk=pk)
    principal = request_principal(request)
    can_manage(principal, room)
    form = ConversationForm(request.POST or None, instance=room, organization=organization, principal=principal)
    if request.method == 'POST' and form.is_valid():
        form.save()
        from identity.access import record_decision
        record_decision(principal, room, 'chat.settings_updated', True, 'owner_approved', [])
        return redirect('chat_settings', slug=organization.slug, pk=room.pk)
    return render(request, 'identity/form.html', {'title': 'Conversation settings', 'form': form})


@organization_admin
def administration(request, organization):
    principal = request_principal(request)
    return render(request, 'core_chat/admin.html', {'organization': organization,
        'conversations': visible_resources(principal, organization.chat_conversations.all()),
        'connections': visible_resources(principal, FederationConnection.objects.filter(organization=organization))})


@organization_admin
def connection_edit(request, organization, pk=None):
    instance = get_object_or_404(FederationConnection, organization=organization, pk=pk) if pk else FederationConnection(organization=organization)
    require_access(request_principal(request), instance if pk else organization, 'chat.connection_manage')
    class ConnectionForm(forms.ModelForm):
        class Meta:
            model = FederationConnection
            fields = ['name', 'issuer', 'remote_organization_id', 'credential_file', 'active']
    form = ConnectionForm(request.POST or None, instance=instance)
    if request.method == 'POST' and form.is_valid():
        form.save()
        from identity.views import audit
        audit(request, 'chat.connection_updated', instance.pk)
        return redirect('chat_admin', slug=organization.slug)
    return render(request, 'identity/form.html', {'title': 'Trusted chat connection', 'form': form})
