from django.shortcuts import redirect


class DeveloperOnboardingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (request.user.is_authenticated and request.user.is_developer and
            request.method == "GET" and not request.path.startswith((
                "/onboarding/", "/accounts/", "/platform-admin/", "/api/", "/health/", "/.well-known/", "/static/",
            )) and not getattr(request.user, "onboarding", None)):
            request.session["after_onboarding"] = request.get_full_path()
            return redirect("onboarding")
        if (request.user.is_authenticated and request.user.is_developer and request.method == "GET" and
            request.path == "/" and getattr(request.user, "onboarding", None) and not request.user.onboarding.complete):
            return redirect("onboarding")
        return self.get_response(request)
