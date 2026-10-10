"""Human-friendly editor for the declarative organization header."""
from django import forms
from django.forms import formset_factory

from identity.models import PermissionTag
from .models import Deployment
from . import header_configuration as config


class HeaderForm(forms.Form):
    unified_header_enabled = forms.BooleanField(required=False, label='Show the shared header by default')
    identity_name = forms.CharField(max_length=160, required=False, label='Platform name', help_text='Blank uses the Core platform’s default name. This also updates the organization’s identity branding.')
    logo_url = forms.CharField(max_length=2048, required=False, label='Logo URL', help_text='Public HTTPS image URL. No credentials or signed query strings. Leave blank for a text logo.')
    show_organization = forms.BooleanField(required=False, label='Show the organization name')
    preset = forms.ChoiceField(choices=[(name, name.title()) for name in config.PRESETS] + [('custom', 'Custom colors')], label='Color scheme')
    background = forms.RegexField(regex=r'^#[0-9a-fA-F]{6}$', label='Header background', widget=forms.TextInput(attrs={'type': 'color'}))
    foreground = forms.RegexField(regex=r'^#[0-9a-fA-F]{6}$', label='Header text', widget=forms.TextInput(attrs={'type': 'color'}))
    accent = forms.RegexField(regex=r'^#[0-9a-fA-F]{6}$', label='Accent color', widget=forms.TextInput(attrs={'type': 'color'}))
    density = forms.ChoiceField(choices=[('comfortable', 'Comfortable'), ('compact', 'Compact')], label='Header spacing')
    shortcuts = forms.ModelMultipleChoiceField(queryset=Deployment.objects.none(), required=False, label='Shared application shortcuts',
        help_text='Choose up to twelve deployed apps. Only users with access will see them. Users can also keep their own personal pins.')

    def __init__(self, *args, organization, **kwargs):
        self.organization = organization
        current = config.configuration(organization)
        initial = {**current, 'unified_header_enabled': organization.unified_header_enabled, 'identity_name': organization.identity_name}
        for index, (key, label) in enumerate(config.WIDGETS.items()):
            initial['widget_' + key] = key in current['widgets']
            initial['order_' + key] = current['widgets'].index(key) + 1 if key in current['widgets'] else len(config.WIDGETS)
        super().__init__(*args, initial=initial, **kwargs)
        self.fields['shortcuts'].queryset = Deployment.objects.filter(installation__organization=organization,
            installation__package__kind='application').select_related('installation__package', 'environment').order_by('installation__package__name', 'name', 'pk')
        self.fields['shortcuts'].label_from_instance = lambda d: f'{d.installation.package.name} · {d.name} · {d.environment.name if d.environment_id else "unassigned"}'
        for key, label in config.WIDGETS.items():
            self.fields['widget_' + key] = forms.BooleanField(required=False, label=label)
            self.fields['order_' + key] = forms.IntegerField(min_value=1, max_value=len(config.WIDGETS), label=label + ' position', initial=initial['order_' + key])

    def clean_logo_url(self):
        value = self.cleaned_data['logo_url']
        return config.safe_url(value, logo=True) if value else ''

    def configuration(self, links):
        value = {key: self.cleaned_data[key] for key in config.DEFAULT if key not in {'widgets', 'shortcuts', 'links'}}
        if value['preset'] in config.PRESETS:
            value['background'], value['foreground'], value['accent'] = config.PRESETS[value['preset']]
        value['widgets'] = sorted((key for key in config.WIDGETS if self.cleaned_data['widget_' + key]), key=lambda key: self.cleaned_data['order_' + key])
        value['shortcuts'] = [str(d.pk) for d in self.cleaned_data['shortcuts']]
        value['links'] = links
        return config.validate(value, self.organization)


class LinkForm(forms.Form):
    label = forms.CharField(max_length=60, label='Link label')
    url = forms.CharField(max_length=2048, label='Link URL')
    new_tab = forms.BooleanField(required=False, label='Open in a new tab', initial=True)
    permission_tags = forms.ModelMultipleChoiceField(queryset=PermissionTag.objects.none(), required=False, label='Required permission tags',
        help_text='All selected tags are required. Untagged links are visible to organization members.')

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['permission_tags'].queryset = PermissionTag.objects.filter(organization=organization, active=True)

    def clean_url(self):
        return config.safe_url(self.cleaned_data['url'])


LinkFormSet = formset_factory(LinkForm, extra=1, can_delete=True, can_order=True, max_num=8, validate_max=True, absolute_max=16)
