"""Explicit administrative approvals for test agents; never used by production code."""
from identity.models import Role, ServicePermission
from platform_services.models import AgentBinding


def approve_agent(agent, consumers=None, tags=()):
    role, _ = Role.objects.get_or_create(organization=agent.organization, slug='test-agent-tools', defaults={'name': 'Approved test agent tools'})
    role.permissions.set(ServicePermission.objects.filter(service__organization=agent.organization,
        codename__in=['agents-run', 'data-read', 'data-write', 'chat-send', 'chat-read', 'chat-ai', 'workflows-run', 'workflows-read']))
    bindings = []
    for consumer in consumers or agent.organization.installations.all():
        binding, _ = AgentBinding.objects.get_or_create(account=agent.account, installation=consumer, environment='development')
        binding.roles.add(role)
        binding.access_tags.add(*tags)
        bindings.append(binding)
    return bindings
