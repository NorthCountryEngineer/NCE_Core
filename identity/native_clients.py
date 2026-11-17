"""One explicitly registered local-testing callback, not a generic scheme exception."""

ROBOTICS_DEVELOPMENT_REDIRECT = 'ai.nnytech.ncerobotics.dev:/oauth/callback'
ROBOTICS_DEVELOPMENT_CLIENT_NAME = 'NCE Robotics iOS Personal Team Development'


def robotics_development_callback(application, uri):
    """Permit only a marked public client bound to this live robotics service."""
    if (not getattr(application, 'native_development', False) or
            uri != ROBOTICS_DEVELOPMENT_REDIRECT or
            application.redirect_uris != ROBOTICS_DEVELOPMENT_REDIRECT or
            application.client_type != application.CLIENT_PUBLIC or
            application.authorization_grant_type != application.GRANT_AUTHORIZATION_CODE or
            application.allowed_origins or not application.service_id):
        return False
    from platform_services.models import Installation
    return Installation.objects.filter(service_id=application.service_id,
        organization_id=application.service.organization_id, package__slug='nce-robotics',
        manifest__external_runtime='edge-device', enabled=True, organization__active=True,
        service__active=True, package__active=True).exists()
