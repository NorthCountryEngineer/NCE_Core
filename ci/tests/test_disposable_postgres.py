import os
import secrets
import unittest
from unittest.mock import Mock, patch

from ci import verify_with_postgres as v


class DisposablePostgresTests(unittest.TestCase):
    def test_password_is_generated_passed_on_stdin_and_cleaned_up(self):
        password = secrets.token_urlsafe(48)
        with patch.object(v.secrets, 'token_urlsafe', return_value=password), \
             patch.object(v, 'docker', side_effect=['', '55439', '', '']) as docker, \
             patch.object(v.subprocess, 'run', return_value=Mock(returncode=0)) as run, \
             patch.dict(os.environ, {'REGISTRY_PASSWORD': secrets.token_urlsafe(32), 'KUBECONFIG': '/production/kubeconfig'}):
            self.assertEqual(v.verify(), 0)
        start = docker.call_args_list[0]
        self.assertNotIn(password, start.args)
        self.assertIn('127.0.0.1::5432', start.args)
        self.assertIn(password, start.kwargs['data'])
        environment = run.call_args.kwargs['env']
        self.assertIn(password, environment['DATABASE_URL'])
        self.assertEqual(environment['CORE_CI_MODE'], 'verify')
        self.assertNotIn('REGISTRY_PASSWORD', environment)
        self.assertNotIn('KUBECONFIG', environment)
        cleanup = docker.call_args_list[-1]
        self.assertEqual(cleanup.args[:3], ('rm', '--force', '--volumes'))
        self.assertEqual(cleanup.args[3], start.args[3])

    def test_failure_still_removes_own_database_and_volume(self):
        with patch.object(v, 'docker', side_effect=['', '55439', '', '']) as docker, \
             patch.object(v.subprocess, 'run', return_value=Mock(returncode=1)):
            self.assertEqual(v.verify(), 1)
        self.assertEqual(docker.call_args_list[-1].args[:3], ('rm', '--force', '--volumes'))

    def test_docker_diagnostics_never_expose_credentials(self):
        password = secrets.token_urlsafe(32)
        with patch.object(v.subprocess, 'run', return_value=Mock(returncode=1, stdout=password, stderr=password)):
            with self.assertRaises(RuntimeError) as error:
                v.docker('run')
        self.assertNotIn(password, str(error.exception))
