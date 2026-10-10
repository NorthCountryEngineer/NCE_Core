import re
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import RuntimeApplication


@transaction.atomic
def update_inventory(node, values):
    if not isinstance(values, list) or len(values) > 100:
        raise ValidationError('Runtime inventory supports at most 100 Deployments per namespace.')
    sanitized, seen = [], set()
    def name(value):
        return isinstance(value, str) and len(value) <= 63 and re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', value)
    for item in values:
        if not isinstance(item, dict) or set(item) != {'name', 'uid', 'containers'} or not name(item['name']) or item['name'] in seen:
            raise ValidationError('Invalid runtime inventory.')
        try:
            uid = str(UUID(item['uid']))
        except (ValueError, TypeError, AttributeError):
            raise ValidationError('Use the Kubernetes resource UID.')
        containers = item['containers']
        if not isinstance(containers, list) or not 1 <= len(containers) <= 20:
            raise ValidationError('Invalid container inventory.')
        container_names = set()
        for container in containers:
            if (not isinstance(container, dict) or set(container) != {'name', 'image'} or not name(container['name']) or
                    container['name'] in container_names or not isinstance(container['image'], str) or
                    not 1 <= len(container['image']) <= 512 or not re.fullmatch(r'[a-zA-Z0-9./:@_-]+', container['image'])):
                raise ValidationError('Only container names and image references are accepted in inventory.')
            container_names.add(container['name'])
        sanitized.append((item['name'], uid, containers))
        seen.add(item['name'])
    for resource_name, uid, containers in sanitized:
        RuntimeApplication.objects.update_or_create(node=node, name=resource_name,
            defaults={'resource_uid': uid, 'containers': containers, 'observed_at': timezone.now()})
    node.applications.exclude(name__in=seen).delete()
