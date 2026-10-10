"""Optional Core configuration routing for apps with their own auth and vaults.

No Django imports, local persistence, provider I/O, or paid calls. The host fixes
the mode, authenticated actor, local role policy, and selected inherited names.
"""
from copy import deepcopy
import os
import re


class IntegrationDenied(Exception):
    pass


class ApplicationIntegrations:
    def __init__(self, *, mode='standalone', core=None, inherited=(), local_configurations=None,
                 authorize_local=None, local_credential=None, configure_local=None):
        if mode not in {'standalone', 'platform', 'hybrid'}:
            raise ValueError('Choose standalone, platform, or hybrid integration configuration.')
        if mode != 'standalone' and core is None or mode == 'standalone' and inherited:
            raise ValueError('Platform configuration requires a configured Core client and explicit routing.')
        self.mode, self.core = mode, core
        self.inherited = frozenset(inherited)
        self.local_configurations = local_configurations if callable(local_configurations) else deepcopy(local_configurations or {})
        self.authorize_local = authorize_local
        self.local_credential = local_credential
        self.configure_local = configure_local

    @classmethod
    def from_environment(cls, *, mode=None, inherited=(), **local_options):
        selected = mode or ('platform' if os.environ.get('CORE_SERVICES_URL') else 'standalone')
        core = None
        if selected != 'standalone':
            from core_client import CoreClient
            core = CoreClient.from_environment()
        return cls(mode=selected, core=core, inherited=inherited, **local_options)

    def centralized(self, slug):
        if not isinstance(slug, str) or not re.fullmatch(r'[-a-zA-Z0-9_]+', slug):
            raise ValueError('Use an integration slug.')
        return self.mode == 'platform' or self.mode == 'hybrid' and slug in self.inherited

    def local_access(self, slug, actor, action):
        if not self.authorize_local or self.authorize_local(slug, actor, action) is not True:
            raise IntegrationDenied('The current application role does not permit this integration operation.')

    def local_configuration(self, slug, actor):
        return self.local_configurations(slug, actor) if callable(self.local_configurations) else self.local_configurations.get(slug)

    def resolve(self, slug, *, actor=None, user_access_token=None):
        if self.centralized(slug):
            # Failure remains a failure. Never fall back to a local credential.
            return self.core.resolve_integration(slug, user_access_token=user_access_token)
        self.local_access(slug, actor, 'use')
        value = self.local_configuration(slug, actor)
        if value is None:
            raise IntegrationDenied('This application has no configured integration.')
        return {'slug': slug, 'mode': 'standalone', 'configuration': deepcopy(value)}

    def credential(self, slug, *, integration=None, actor=None, user_access_token=None):
        if self.centralized(slug):
            if integration is None:
                raise ValueError('Pin the resolved integration configuration before retrieving its credential.')
            return self.core.integration_credential(slug, integration=integration, user_access_token=user_access_token)
        self.local_access(slug, actor, 'use')
        self.local_access(slug, actor, 'credential')
        if self.local_configuration(slug, actor) is None or not self.local_credential:
            raise IntegrationDenied('The application has no credential provider for this integration.')
        return self.local_credential(slug, actor)

    def configure(self, slug, values, *, actor=None, user_access_token=None):
        if self.centralized(slug):
            if not user_access_token:
                raise IntegrationDenied('Application administration requires the current administrator access token.')
            return self.core.configure_integration(slug, values, user_access_token=user_access_token)
        self.local_access(slug, actor, 'configure')
        if not self.configure_local:
            raise IntegrationDenied('Configure this integration in the application settings.')
        # The host retains its schema, vault, optimistic versions, and CSRF policy.
        return self.configure_local(slug, values, actor)
