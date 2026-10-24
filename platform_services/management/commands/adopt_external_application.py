"""Plan first; apply only a reviewed exact fingerprint. No cloud operations."""
import json
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from platform_services.application_adoption import apply_adoption, plan_adoption


class Command(BaseCommand):
    help = 'Plan a logical external-application organization move. Apply requires its exact reviewed plan hash; never changes hosting or DNS.'

    def add_arguments(self, parser):
        parser.add_argument('--application', required=True, help='Existing CloudApplication UUID')
        parser.add_argument('--target-organization', required=True, help='Existing target Organization UUID')
        parser.add_argument('--target-environment', required=True, help='Existing target DeploymentEnvironment UUID')
        parser.add_argument('--actor', required=True, help='Existing active operator User UUID')
        parser.add_argument('--member-user', action='append', default=[], help='Explicit source user UUID to retain, repeatable. Other members are inferred only from app-scoped access.')
        parser.add_argument('--apply-plan-hash', help='Apply the exact reviewed plan. Omit for a read-only plan.')

    def handle(self, **options):
        args = [options['application'], options['target_organization'], options['target_environment'], options['actor']]
        try:
            if options['apply_plan_hash']:
                value = apply_adoption(*args, member_user_ids=options['member_user'],
                    expected_plan_hash=options['apply_plan_hash'])
            else:
                value = plan_adoption(*args, member_user_ids=options['member_user'])
        except (ObjectDoesNotExist, ValueError, PermissionDenied, ValidationError) as error:
            raise CommandError('Application adoption refused: ' + str(error)) from None
        self.stdout.write(json.dumps(value, indent=2, sort_keys=True))
