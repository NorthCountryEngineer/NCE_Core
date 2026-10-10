import json
import subprocess
import unittest
from unittest.mock import patch

from ci.chat_deployment import DeploymentTestError, deployment


class ChatDeploymentCleanupTests(unittest.TestCase):
    def test_timeout_of_owned_job_still_removes_all_owned_resources(self):
        calls = []

        def fake_docker(*args, data=None, timeout=120):
            calls.append(args)
            if args[0] == 'run' and args[-1] == 'tests':
                raise subprocess.TimeoutExpired('docker', 1)
            return ''

        with patch('ci.chat_deployment.docker', side_effect=fake_docker), \
             patch('ci.chat_deployment.wait_ready'), \
             patch('ci.chat_deployment.subprocess.run', return_value=subprocess.CompletedProcess([], 0)):
            with self.assertRaises(subprocess.TimeoutExpired):
                with deployment('local-chat:test') as instance:
                    instance['job']('tests')
        names = [args[args.index('--name') + 1] for args in calls if args[0] == 'run']
        removed = [args[-1] for args in calls if args[:2] == ('rm', '--force')]
        self.assertEqual(set(names), set(removed))

    def test_failed_backend_job_cleans_only_its_own_resources_and_keeps_secrets_out_of_arguments(self):
        calls = []

        def fake_docker(*args, data=None, timeout=120):
            calls.append((args, data))
            if args[0] == 'run' and args[-1] == 'tests':
                raise DeploymentTestError('Injected backend failure.')
            return ''

        with patch('ci.chat_deployment.docker', side_effect=fake_docker), \
             patch('ci.chat_deployment.wait_ready'), \
             patch('ci.chat_deployment.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
            with self.assertRaises(DeploymentTestError):
                with deployment('local-chat:test') as instance:
                    instance['job']('tests')

        runs = [args for args, _ in calls if args[0] == 'run']
        names = [args[args.index('--name') + 1] for args in runs]
        removed = [args[-1] for args, _ in calls if args[:2] == ('rm', '--force')]
        self.assertEqual(set(names), set(removed))
        self.assertTrue(all(name.startswith('nce-chat-test-') for name in removed))
        cleanup = [call.args[0] for call in run.call_args_list]
        self.assertEqual({args[1] for args in cleanup}, {'network', 'volume'})
        self.assertTrue(all(args[-1].startswith('nce-chat-test-') for args in cleanup))
        secret_payload = next(json.loads(data) for args, data in calls if args[0] == 'run' and args[-1] == 'secrets')
        admin_payload = next(json.loads(data) for args, data in calls if args[0] == 'run' and args[-1] == 'initialize')
        arguments = repr([args for args, _ in calls])
        for value in [secret_payload['django.key'], secret_payload['oidc.key'],
                      secret_payload['runtime.json']['DATABASE_URL'], admin_payload['password']]:
            self.assertNotIn(value, arguments)
        web = next(args for args in runs if args[-1] == 'web')
        self.assertTrue(web[web.index('--publish') + 1].startswith('127.0.0.1:'))
        self.assertIn('--read-only', web)
        self.assertIn('ALL', web)


if __name__ == '__main__':
    unittest.main()
