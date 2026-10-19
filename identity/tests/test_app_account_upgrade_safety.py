"""The reviewed identity upgrade is bounded, additive and actor checked."""
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import tempfile

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from identity.models import ApplicationAccountAccess, Invitation, User
from platform_services.models import Installation, MarketplacePackage
from platform_services.tests import test_external_apps as external_tests


class ApplicationIdentityUpgradeSafetyTests(TestCase):
    setUpTestData = classmethod(external_tests.ExternalApplicationTests.setUpTestData.__func__)

    def setUp(self):
        self.installation.version = '1.0.0'
        old = deepcopy(self.installation.manifest)
        old.pop('account_roles', None)
        old['permissions'] = [row for row in old['permissions']
            if row['codename'] not in {'accounts-manage', 'agents-manage'}]
        self.installation.manifest = old
        self.installation.save(update_fields=['version', 'manifest'])
        self.options = dict(organization=self.organization.slug,
            service_id=str(self.installation.service_id),
            package=str(Path(settings.BASE_DIR) / 'examples/axolotl-news-package.json'),
            expected_version='1.0.0', actor_email=self.admin.email)

    def snapshot(self):
        return (list(Installation.objects.order_by('pk').values('id', 'version', 'manifest')),
            list(MarketplacePackage.objects.order_by('pk').values('id', 'version', 'manifest')),
            User.objects.count(), Invitation.objects.count(), ApplicationAccountAccess.objects.count())

    def rejected_without_changes(self, options):
        before = self.snapshot()
        with self.assertRaises(CommandError):
            call_command('upgrade_external_application', **options, stdout=StringIO())
        self.assertEqual(self.snapshot(), before)

    def test_non_admin_and_inactive_actor_cannot_upgrade(self):
        self.rejected_without_changes({**self.options, 'actor_email': self.member.email})
        User.objects.filter(pk=self.admin.pk).update(is_active=False)
        self.rejected_without_changes(self.options)

    def test_stale_expected_version_and_wrong_organization_are_rejected(self):
        self.rejected_without_changes({**self.options, 'expected_version': '0.0.0'})
        self.rejected_without_changes({**self.options, 'organization': self.other.slug})

    def test_additive_upgrade_cannot_remove_an_existing_permission(self):
        package = json.loads(Path(self.options['package']).read_text())
        package['manifest']['permissions'] = [row for row in package['manifest']['permissions']
            if row['codename'] != 'posts-write']
        for role in package['manifest']['account_roles']:
            role['permissions'] = [name for name in role['permissions'] if name != 'posts-write']
        with tempfile.TemporaryDirectory(prefix='core-identity-upgrade-test-') as directory:
            path = Path(directory) / 'reviewed-package.json'
            path.write_text(json.dumps(package))
            self.rejected_without_changes({**self.options, 'package': str(path)})
