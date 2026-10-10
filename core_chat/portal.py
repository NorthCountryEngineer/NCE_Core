import uuid
from functools import wraps

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from identity.access import request_principal, require_access, visible_resources
from identity.models import Membership, Organization, User
from identity.views import organization_admin
from platform_services.data import IdempotencyConflict
from platform_services.models import AgentAccount, Installation
from .federation import FederationUnavailable
from .models import Conversation, FederationConnection, Participant
from .services import authorize, can_manage, capability, create_conversation, message_page, send_message, serialize
from .workspace import workspace_state


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
    can_create = permits(lambda: capability(principal, organization, 'chat.manage'))
    state = workspace_state(principal, organization)
    return render(request, 'core_chat/index.html', {'organization': organization, 'workspace': state,
        'can_create': can_create})


def permits(check):
    try:
        check()
    except PermissionDenied:
        return False
    return True


class ConversationForm(forms.ModelForm):
    members = forms.ModelMultipleChoiceField(queryset=User.objects.none(), required=False,
        label='Invite people', help_text='Choose one person for a direct message, or two or more for a group. Existing Chat permissions still apply.')
    class Meta:
        model = Conversation
        fields = ['title', 'kind', 'topic', 'ai_enabled', 'active', 'connection', 'remote_conversation_id']

    def __init__(self, *args, organization, principal, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['kind'].required = False
        members = visible_resources(principal, Membership.objects.filter(organization=organization, active=True, user__is_active=True).select_related('user'))
        self.fields['members'].queryset = User.objects.filter(pk__in=[m.user_id for m in members]).exclude(pk=principal.user.pk)
        self.fields['connection'].queryset = FederationConnection.objects.filter(organization=organization,
            pk__in=[item.pk for item in visible_resources(principal, FederationConnection.objects.filter(organization=organization, active=True))])
        if self.instance.pk and not self.instance._state.adding:
            # Authority and routing are immutable for an established conversation.
            self.fields['connection'].disabled = True
            self.fields['remote_conversation_id'].disabled = True
            self.fields.pop('members')
            self.fields['kind'].disabled = True

    def clean_kind(self):
        return self.cleaned_data['kind'] or self.instance.kind or 'channel'

    def clean(self):
        values = super().clean()
        if 'members' in self.fields:
            count = len(values.get('members', []))
            if count > 50:
                self.add_error('members', 'Invite at most 50 people at once.')
            if values.get('kind') == 'direct' and count != 1:
                self.add_error('members', 'Choose exactly one person for a direct message.')
            if values.get('kind') == 'group' and count < 2:
                self.add_error('members', 'Choose at least two people for a group message.')
        return values


class SendForm(forms.Form):
    text = forms.CharField(widget=forms.Textarea(attrs={'rows': 2, 'placeholder': 'Write a message…'}), max_length=16384, label='Message')
    request_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    reply_to = forms.UUIDField(widget=forms.HiddenInput, required=False)


@member
def create(request, organization):
    principal = request_principal(request)
    capability(principal, organization, 'chat.manage')
    form = ConversationForm(request.POST or None, organization=organization, principal=principal,
                            instance=Conversation(organization=organization))
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            room = create_conversation(principal, organization, form.cleaned_data['title'], form.cleaned_data['ai_enabled'],
                                       form.cleaned_data['connection'], form.cleaned_data['remote_conversation_id'],
                                       kind=form.cleaned_data['kind'], topic=form.cleaned_data['topic'])
            for user in form.cleaned_data['members']:
                item = Participant(conversation=room, user=user)
                item.full_clean()
                item.save()
        return redirect('chat_room', slug=organization.slug, pk=room.pk)
    return render(request, 'identity/form.html', {'title': 'Create conversation', 'form': form})


@member
def detail(request, organization, pk):
    room = get_object_or_404(Conversation, organization=organization, pk=pk)
    principal = request_principal(request)
    authorize(principal, room)
    form = SendForm(request.POST or None)
    unavailable = False
    if request.method == 'POST' and form.is_valid():
        try:
            send_message(principal, room, str(form.cleaned_data['request_id']), form.cleaned_data['text'],
                         reply_to=form.cleaned_data['reply_to'])
        except (ValidationError, IdempotencyConflict, FederationUnavailable) as error:
            form.add_error(None, 'The shared instance is unavailable; retry with the same message ID.' if isinstance(error, FederationUnavailable) else 'This message could not be accepted.')
        else:
            return redirect('chat_room', slug=organization.slug, pk=room.pk)
    query = request.GET.get('q', '').strip()[:160]
    try:
        # Load the most recent local history; incremental feed supplies subsequent
        # pages. A message permalink opens the window around that authorized row.
        after = 0
        if not room.connection_id:
            latest = list(room.messages.order_by('-pk').values_list('pk', flat=True)[:100])
            after = max(0, min(latest) - 1) if latest else 0
            if request.GET.get('message'):
                try:
                    target_id = uuid.UUID(request.GET['message'])
                except ValueError:
                    from platform_services.api import response
                    return response({'error': 'invalid_message'}, 400)
                target = get_object_or_404(room.messages, public_id=target_id)
                require_access(principal, target, 'chat.message_read')
                after = max(0, target.pk - 1)
        page = message_page(principal, room, after=after)
        if query:
            if room.connection_id:
                page['messages'] = [m for m in page['messages'] if query.casefold() in m['text'].casefold()]
            else:
                from identity.access import decision
                page['messages'] = [serialize(m, principal) for m in room.messages.filter(text__icontains=query).select_related('conversation', 'reply_to__conversation').order_by('-pk')[:200] if decision(principal, m)[0]]
                page['messages'].reverse()
    except FederationUnavailable:
        page = {'messages': []}
        unavailable = True
    can_edit = False
    try:
        can_manage(principal, room)
        can_edit = True
    except PermissionDenied:
        pass
    state = workspace_state(principal, organization)
    participant = authorize(principal, room)
    roster = visible_resources(principal, room.participants.filter(active=True, user__is_active=True,
        user__memberships__organization=organization, user__memberships__active=True).select_related('user'))
    return render(request, 'core_chat/room.html', {'organization': organization, 'conversation': room, 'workspace': state,
        'chat_messages': page['messages'], 'form': form,
        'can_send': permits(lambda: authorize(principal, room, 'send')), 'can_edit': can_edit,
        'can_create': permits(lambda: capability(principal, organization, 'chat.manage')),
        'unavailable': unavailable, 'query': query, 'notification_mode': participant.notifications,
        'mention_people': [p.user for p in roster],
        'has_older': not room.connection_id and after > 0 and not query,
        'chat_state': {'cursor': page.get('next_cursor', 0), 'query': query, 'before': after + 1}})


@member
@require_POST
def post_message(request, organization, pk):
    from platform_services.api import response
    room = get_object_or_404(Conversation, organization=organization, pk=pk)
    form = SendForm(request.POST)
    if set(request.POST) - {'text', 'request_id', 'reply_to', 'csrfmiddlewaretoken', 'purpose_code'} or not form.is_valid():
        return response({'error': 'invalid_message'}, 400)
    try:
        result = send_message(request_principal(request), room, str(form.cleaned_data['request_id']),
                              form.cleaned_data['text'], reply_to=form.cleaned_data['reply_to'])
        return response(result, 201 if result['created'] else 200)
    except ValidationError:
        return response({'error': 'invalid_message'}, 400)
    except IdempotencyConflict:
        return response({'error': 'idempotency_conflict'}, 409)
    except FederationUnavailable:
        return response({'error': 'chat_authority_unavailable'}, 503)


@member
@require_GET
def feed(request, organization, pk):
    from platform_services.api import response
    room = get_object_or_404(Conversation, organization=organization, pk=pk)
    try:
        principal = request_principal(request)
        if 'before' in request.GET:
            from identity.access import decision
            authorize(principal, room)
            before = int(request.GET['before'])
            if before < 1 or room.connection_id:
                raise ValidationError('Use a positive local history cursor.')
            rows = list(room.messages.filter(pk__lt=before).select_related('conversation', 'reply_to__conversation').order_by('-pk')[:100])
            messages = []
            for row in reversed(rows):
                if decision(principal, row)[0]:
                    require_access(principal, row, 'chat.message_read')
                    messages.append(serialize(row, principal))
            cursor = rows[-1].pk if rows else 0
            return response({'messages': messages, 'before': cursor, 'has_more': bool(cursor and room.messages.filter(pk__lt=cursor).exists())})
        page = message_page(principal, room, after=int(request.GET.get('after', 0)))
        page['can_send'] = permits(lambda: authorize(principal, room, 'send'))
        return response(page)
    except (ValueError, ValidationError):
        return response({'error': 'invalid_cursor'}, 400)
    except FederationUnavailable:
        return response({'error': 'chat_authority_unavailable'}, 503)


@member
@require_GET
def activity(request, organization):
    from platform_services.api import response
    principal = request_principal(request)
    capability(principal, organization, 'chat.read')
    return response(workspace_state(principal, organization))


@member
@require_POST
def preferences(request, organization, pk):
    from platform_services.api import response
    room = get_object_or_404(Conversation, organization=organization, pk=pk)
    participant = authorize(request_principal(request), room)
    class PreferencesForm(forms.Form):
        cursor = forms.IntegerField(required=False, min_value=0, max_value=2**63 - 1)
        notifications = forms.ChoiceField(required=False, choices=Participant._meta.get_field('notifications').choices)
    form = PreferencesForm(request.POST)
    if set(request.POST) - {'csrfmiddlewaretoken', 'cursor', 'notifications', 'purpose_code'} or not form.is_valid():
        return response({'error': 'invalid_preferences'}, 400)
    cursor = form.cleaned_data['cursor']
    if cursor is not None:
        # Concurrent tabs can never move a reader backwards. GET requests and
        # prefetches do not acknowledge messages on the user's behalf.
        Participant.objects.filter(pk=participant.pk, last_read_cursor__lt=cursor).update(last_read_cursor=cursor)
    if form.cleaned_data['notifications']:
        Participant.objects.filter(pk=participant.pk).update(notifications=form.cleaned_data['notifications'])
    return response({'saved': True})


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
