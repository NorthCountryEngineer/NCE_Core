"""App presentation and invitation entry; existing Core policy remains authoritative."""
import re
from urllib.parse import parse_qs, urlencode, urlparse

from django import forms
from django.conf import settings
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .models import Application, Invitation


def authorization_context(request, value=None):
    """Resolve branding only from an active public client and exact registered callback."""
    value = value if value is not None else request.POST.get("next", request.GET.get("next", ""))
    if not isinstance(value, str) or len(value) > 8192:
        return None
    # Login may resume invitation acceptance, which in turn resumes authorization.
    for _ in range(2):
        parsed = urlparse(value)
        if parsed.scheme or parsed.netloc or parsed.fragment or "\\" in value:
            return None
        params = parse_qs(parsed.query, keep_blank_values=True)
        if any(len(values) != 1 for values in params.values()):
            return None
        if parsed.path == reverse("oauth2_provider:authorize"):
            break
        if re.fullmatch(r"/invitations/[A-Za-z0-9_-]{24,128}/", parsed.path):
            value = params.get("next", [""])[0]
            continue
        return None
    else:
        return None
    params = {key: values[0] for key, values in params.items()}
    if (params.get("response_type") != "code" or params.get("code_challenge_method") != "S256" or
            not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.get("code_challenge", "")) or
            not 1 <= len(params.get("state", "")) <= 1024 or
            "openid" not in params.get("scope", "").split()):
        return None
    app = Application.objects.select_related("service__organization").filter(
        client_id=params.get("client_id"), client_type=Application.CLIENT_PUBLIC).first()
    if not app or not app.is_usable(request) or params.get("redirect_uri") not in app.redirect_uris.split():
        return None
    origin = urlparse(params["redirect_uri"])
    if (not origin.netloc or not origin.hostname or origin.username is not None or origin.fragment or
            origin.scheme not in {"https", "http"} or
            origin.scheme == "http" and not (settings.DEBUG and origin.hostname in {"localhost", "127.0.0.1", "::1"})):
        return None
    organization = getattr(request, "organization", None)
    if organization and organization.pk != app.service.organization_id:
        return None
    theme = app.service.auth_theme
    if theme == "automatic":
        theme = "axolotl" if app.service.slug == "axolotl-news" else "core"
    return {"application": app, "next": value, "name": app.service.auth_name or app.service.name,
            "theme": theme,
            "home": f"{origin.scheme}://{origin.netloc}",
            "signup_url": reverse("app_signup") + "?" + urlencode({"next": value}),
            "login_url": reverse("login") + "?" + urlencode({"next": value})}


def request_auth_context(request):
    if request.path == reverse("oauth2_provider:authorize"):
        if request.method == "POST":
            value = reverse("oauth2_provider:authorize") + "?" + request.POST.urlencode()
        else:
            value = request.get_full_path()
        return authorization_context(request, value)
    if request.path in {reverse("login"), reverse("app_signup")} or request.path.startswith("/invitations/"):
        return authorization_context(request)
    return None


class InvitationEntryForm(forms.Form):
    invitation = forms.CharField(label="Invitation link or code", max_length=8192,
                                 widget=forms.TextInput(attrs={"autocomplete": "off"}))

    def clean_invitation(self):
        value = self.cleaned_data["invitation"].strip()
        if "://" in value or value.startswith("/"):
            parsed = urlparse(value)
            issuer = urlparse(settings.ISSUER)
            if parsed.netloc and (parsed.scheme, parsed.netloc) != (issuer.scheme, issuer.netloc):
                raise forms.ValidationError("Use the invitation link from your app's administrator.")
            match = re.fullmatch(r"/invitations/([A-Za-z0-9_-]{24,128})/", parsed.path)
            if not match or parsed.fragment:
                raise forms.ValidationError("Use a valid invitation link or code.")
            value = match[1]
        if not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", value):
            raise forms.ValidationError("Use a valid invitation link or code.")
        return value


@never_cache
@require_http_methods(["GET", "POST"])
def signup(request):
    context = authorization_context(request)
    if not context:
        raise Http404("Start account creation from the application.")
    form = InvitationEntryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        invitation = Invitation.objects.filter(token_hash=Invitation.digest(form.cleaned_data["invitation"]),
            organization=context["application"].service.organization).first()
        if invitation and invitation.usable:
            return redirect(reverse("accept_invitation", args=[form.cleaned_data["invitation"]]) +
                            "?" + urlencode({"next": context["next"]}))
        form.add_error("invitation", "This invitation is unavailable for this app. Ask its administrator for a new one.")
    response = render(request, "identity/app_signup.html", {"form": form, "next": context["next"]})
    response["Referrer-Policy"] = "same-origin"
    return response
