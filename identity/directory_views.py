"""Server-rendered people directory; identity and access stay separately managed."""
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from . import directory
from .access import request_principal, visible_resources
from .directory_forms import (DirectoryCreateForm, DirectoryMembershipForm, EmployeeUpdateForm,
    ExternalPersonReferenceForm, ManagerAssignmentForm, RevisionForm, manager_available)
from .models import AuditEvent, EmployeeProfile, Membership
from .views import organization_admin


def _memberships(principal, organization):
    allowed = visible_resources(principal, organization.memberships.filter(active=True, user__is_active=True).select_related("user").order_by("user__email"))
    return Membership.objects.filter(pk__in=[member.pk for member in allowed]).select_related("user")


def _error(form, error):
    # Service validation messages are fixed descriptions; never interpolate a submitted value.
    form.add_error(None, " ".join(error.messages) if isinstance(error, ValidationError) else "This person changed. Reload the page before saving again.")


@organization_admin
@require_http_methods(["GET", "POST"])
def directory_people(request, organization):
    principal = request_principal(request)
    response_status = 200
    form = DirectoryCreateForm(request.POST if request.method == "POST" else None,
        memberships=_memberships(principal, organization))
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                values = {name: form.cleaned_data[name] for name in EmployeeProfileFormFields}
                person = directory.create_profile(principal, organization, values)
                if form.cleaned_data["membership"]:
                    person = directory.attach_membership(principal, person, form.cleaned_data["membership"], person.revision)
        except (ValidationError, directory.DirectoryVersionConflict) as error:
            _error(form, error)
            if isinstance(error, directory.DirectoryVersionConflict):
                response_status = 409
        else:
            messages.success(request, "Person created.")
            return redirect("directory_person", slug=organization.slug, pk=person.pk)
    search = request.GET.get("q", "")[:160].strip()
    status = request.GET.get("status", "")
    role = request.GET.get("role", "")
    if status not in EmployeeProfile.Status.values:
        status = ""
    people = directory.visible_profiles(principal, organization, search=search, limit=201, status=status,
        is_manager=True if role == "manager" else False if role == "employee" else None)
    return render(request, "identity/directory.html", {
        "organization": organization, "title": "People", "form": form, "people": people[:200],
        "search": search, "status": status, "role": role, "status_choices": EmployeeProfile.Status.choices,
        "truncated": people.truncated or len(people) > 200,
    }, status=response_status)


EmployeeProfileFormFields = ("first_name", "last_name", "work_email", "title", "department", "employee_code", "status", "is_manager")


@organization_admin
@require_http_methods(["GET", "POST"])
def directory_person(request, organization, pk):
    principal = request_principal(request)
    response_status = 200
    person = directory.profile_detail(principal, organization, pk)
    all_assignments = directory.visible_assignments(principal, organization, employee=person, active_only=False)
    direct_reports = directory.visible_assignments(principal, organization, manager=person)
    assignments = [edge for edge in all_assignments if edge.active]
    current_managers = [assignment.manager_id for assignment in assignments]
    primary = next((assignment.manager_id for assignment in assignments if assignment.is_primary), None)
    manager_query = (request.POST.get("manager_q", "") if request.method == "POST" else request.GET.get("manager_q", ""))[:160].strip()
    choices = directory.visible_profiles(principal, organization, search=manager_query, limit=1000,
        status=EmployeeProfile.Status.ACTIVE, is_manager=True)
    manager_choices_complete = not choices.truncated
    # Keep every authorized existing assignment across a search or result cap.
    # Permission-hidden relationships are separately protected by the service.
    manager_ids = {candidate.pk for candidate in choices if candidate.pk != person.pk and manager_available(candidate)}
    manager_ids.update(current_managers)
    managers = EmployeeProfile.objects.filter(pk__in=manager_ids).select_related("membership__user")
    action = request.POST.get("action", "") if request.method == "POST" else ""
    data = lambda name: request.POST if action == name else None
    forms = {
        "profile": EmployeeUpdateForm(data("profile"), instance=person, initial={"expected_revision": person.revision}),
        "membership": DirectoryMembershipForm(data("membership"), memberships=_memberships(principal, organization),
            initial={"expected_revision": person.revision, "membership": person.membership_id}),
        "managers": ManagerAssignmentForm(data("managers"), managers=managers,
            initial={"expected_revision": person.revision, "managers": current_managers, "primary_manager": primary}),
        "external": ExternalPersonReferenceForm(data("external"), initial={"expected_revision": person.revision}),
        "invite": RevisionForm(data("invite"), initial={"expected_revision": person.revision}),
    }
    if request.method == "POST":
        form = forms.get(action)
        if form is None:
            forms["profile"].add_error(None, "Choose a supported people action.")
        elif action == "managers" and not manager_choices_complete:
            form.add_error(None, "Narrow the manager search before saving. The matching list is incomplete; existing assignments have been preserved.")
        elif form.is_valid():
            values = form.cleaned_data
            revision = values["expected_revision"]
            try:
                if action == "profile":
                    directory.update_profile(principal, person, {name: values[name] for name in EmployeeProfileFormFields}, revision)
                elif action == "membership":
                    directory.attach_membership(principal, person, values["membership"], revision)
                elif action == "managers":
                    directory.replace_manager_assignments(principal, person,
                        [{"manager_id": manager.pk, "is_primary": manager == values["primary_manager"]} for manager in values["managers"]], revision)
                elif action == "external":
                    directory.add_external_reference(principal, person,
                        {name: values[name] for name in ("source_namespace", "object_type", "external_id", "source_revision")}, revision)
                elif action == "invite":
                    invitation, raw = directory.invite_profile(principal, person, revision)
                    if raw:
                        response = render(request, "identity/credential.html", {"organization": organization,
                            "title": "Invitation created", "invitation_link": settings.ISSUER + reverse("accept_invitation", args=[raw]),
                            "description": "Share this single-use setup link securely. It expires in 48 hours. The invited person chooses their own password."})
                        response["Referrer-Policy"] = "same-origin"
                        return response
                    messages.info(request, "A current invitation already exists. Its setup link cannot be retrieved again. Revoke it from organization administration before inviting again if needed.")
            except (ValidationError, directory.DirectoryVersionConflict) as error:
                _error(form, error)
                if isinstance(error, directory.DirectoryVersionConflict):
                    response_status = 409
            else:
                messages.success(request, "People record saved.")
                return redirect("directory_person", slug=organization.slug, pk=person.pk)
    # Linked identities and integration references are separately tag-protected.
    membership = person.membership if person.membership_id and visible_resources(principal, [person.membership]) else None
    references = visible_resources(principal, person.external_links.all())
    historical_assignments = [edge for edge in all_assignments if not edge.active]
    history = AuditEvent.objects.filter(organization=organization, target__startswith=f"{person.pk}:", action__startswith="directory.")[:30]
    return render(request, "identity/directory_person.html", {
        "organization": organization, "title": "Person", "person": person, "forms": forms,
        "assignments": assignments, "direct_reports": direct_reports,
        "linked_membership": membership, "external_references": references,
        "historical_assignments": historical_assignments, "history": history,
        "manager_query": manager_query, "manager_choices_complete": manager_choices_complete,
    }, status=response_status)
