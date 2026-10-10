"""Human administration and policy-filtered central metrics."""
import json
from copy import deepcopy
from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import render, redirect
from django.views.decorators.cache import never_cache
from identity.access import request_principal, decision, require_access
from identity.views import organization_admin
from .models import AIRulePolicy, AIUsageMeter, AIUsageReservation
from .ai_rules import DEFAULTS, METRICS, save_policy, require_rules, today, RuleConflict, acknowledge


class PrettyJSONEncoder(json.JSONEncoder):
    def __init__(self, *args, **kwargs):
        kwargs['indent'] = 2
        super().__init__(*args, **kwargs)


class RuleForm(forms.Form):
    revision = forms.IntegerField(widget=forms.HiddenInput)
    baseline = forms.JSONField(required=False, encoder=PrettyJSONEncoder, widget=forms.Textarea(attrs={'rows': 20, 'cols': 80}))
    overrides = forms.JSONField(required=False, encoder=PrettyJSONEncoder, widget=forms.Textarea(attrs={'rows': 12, 'cols': 80}))


@organization_admin
@never_cache
def administration(request, organization):
    organization, principal = request.organization, request_principal(request)
    require_rules(principal, organization, 'rules.manage')
    policy, _ = AIRulePolicy.objects.get_or_create(organization=organization, defaults={'baseline': deepcopy(DEFAULTS)})
    require_access(principal, policy, 'ai_rules.administration')
    error = None
    form = RuleForm(request.POST or None, initial={'revision': policy.revision,
        'baseline': policy.baseline or DEFAULTS, 'overrides': policy.overrides})
    if request.method == 'POST' and form.is_valid():
        try:
            save_policy(organization, principal, form.cleaned_data)
            return redirect('ai_rules_admin', slug=organization.slug)
        except RuleConflict:
            error = 'Rules changed while you were editing. Reload and review the current revision.'
        except ValidationError as exc:
            form.add_error(None, exc)
    require_rules(principal, organization, 'usage.read')
    meter = AIUsageMeter.objects.filter(organization=organization, subject='*', day=today()).first()
    totals = [{'metric': metric, 'used': meter.used.get(metric, 0) if meter else 0,
        'held': meter.held.get(metric, 0) if meter else 0} for metric in METRICS]
    rows = [row for row in AIUsageReservation.objects.filter(organization=organization).order_by('-created')[:100]
        if decision(principal, row)[0]]
    return render(request, 'platform_services/ai_rules.html', {'organization': organization, 'policy': policy,
        'form': form, 'error': error, 'totals': totals, 'rows': rows, 'day': today()})


@organization_admin
@never_cache
def reconcile(request, organization, pk):
    if request.method != 'POST':
        raise PermissionDenied('Use the protected administrator action.')
    acknowledge(request.organization, request_principal(request), pk)
    return redirect('ai_rules_admin', slug=organization.slug)
