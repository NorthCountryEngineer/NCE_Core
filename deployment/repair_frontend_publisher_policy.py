"""Plan/apply one fixed existing Proposal frontend publisher permission repair.

Default planning is read-only. Apply requires a retained exact plan and writes a
durable exclusive intent before its single policy update; unknown outcomes are
reconciled by an operator rather than retried.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess

ACCOUNT = '015454941623'
USER = 'nce-lightsail-frontend-publisher'
POLICY = 'fixed-production-frontends'
SID = 'ProposalProductionManualDeployments'
RESOURCE = 'arn:aws:amplify:us-east-1:015454941623:apps/d1f14xk20wy3vy/branches/production/deployments/*'
ACTIONS = ['amplify:CreateDeployment', 'amplify:StartDeployment']


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def make_plan(before):
    require(isinstance(before, dict) and before.get('Version') == '2012-10-17'
            and isinstance(before.get('Statement'), list), 'Read the current existing identity policy first')
    require(not any(s.get('Sid') == SID for s in before['Statement']), 'Proposal publisher repair already exists; review current policy')
    after = copy.deepcopy(before)
    after['Statement'].append({'Sid': SID, 'Effect': 'Allow', 'Action': ACTIONS.copy(), 'Resource': RESOURCE})
    return {'version': 1, 'account_id': ACCOUNT, 'user_name': USER, 'policy_name': POLICY,
            'before_sha256': digest(before), 'after_sha256': digest(after), 'before': before, 'after': after}


class AWS:
    def __init__(self, run=subprocess.run): self.run = run

    def call(self, service, operation, *arguments):
        require((service, operation) in {('sts', 'get-caller-identity'), ('iam', 'get-user-policy'),
                                        ('iam', 'put-user-policy')}, 'Operation outside fixed policy repair')
        # The operator explicitly selected this profile. Inherited credentials,
        # credential-file paths and endpoint overrides must not replace it.
        environment = {key: value for key, value in os.environ.items() if not key.startswith('AWS_')}
        environment.update(AWS_MAX_ATTEMPTS='1', AWS_PAGER='', AWS_EC2_METADATA_DISABLED='true')
        command = ['aws', '--profile', 'axolotl-deploy', '--region', 'us-east-1', '--output', 'json', '--no-cli-pager', service, operation]
        if service == 'iam': command += ['--user-name', USER, '--policy-name', POLICY]
        result = self.run([*command, *arguments], check=True, capture_output=True, text=True, env=environment, timeout=45)
        require(len(result.stdout.encode()) <= 131072, 'Policy API response exceeded fixed bound')
        return json.loads(result.stdout) if result.stdout else {}

    def identity(self): return self.call('sts', 'get-caller-identity')
    def policy(self): return self.call('iam', 'get-user-policy')['PolicyDocument']
    def put(self, policy): return self.call('iam', 'put-user-policy', '--policy-document', json.dumps(policy, separators=(',', ':')))


def record(path, value, *, initial=False):
    require(not path.is_symlink(), 'Receipt must not be a symlink')
    target = path if initial else path.with_name(path.name + '.tmp-' + str(os.getpid()))
    with target.open('x') as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.flush(); os.fsync(handle.fileno())
    if not initial: os.replace(target, path)


def read_plan(api):
    require(api.identity().get('Account') == ACCOUNT, 'Operator AWS account changed')
    return make_plan(api.policy())


def apply_plan(plan, api, receipt):
    require(plan == make_plan(plan['before']), 'Reviewed policy plan was altered')
    require(not receipt.exists() and not receipt.is_symlink(), 'Existing policy-write intent requires reconciliation')
    identity = api.identity()
    require(identity.get('Account') == ACCOUNT, 'Operator AWS account changed')
    require(digest(api.policy()) == plan['before_sha256'], 'Publisher policy changed after review')
    state = {key: plan[key] for key in ('account_id', 'user_name', 'policy_name', 'before_sha256', 'after_sha256')}
    state.update(state='updating', operator_arn=identity.get('Arn'))
    record(receipt, state, initial=True)
    api.put(plan['after'])
    require(digest(api.policy()) == plan['after_sha256'], 'Policy update must be reconciled against the exact after hash')
    state['state'] = 'succeeded'; record(receipt, state)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('plan', 'apply'))
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args()
    if args.operation == 'plan':
        plan = read_plan(AWS()); record(args.plan, plan, initial=True)
        print(json.dumps({key: plan[key] for key in ('account_id', 'user_name', 'policy_name', 'before_sha256', 'after_sha256')}, indent=2))
    else:
        require(args.receipt is not None, 'Apply requires a new durable receipt path')
        print(json.dumps(apply_plan(json.loads(args.plan.read_text()), AWS(), args.receipt), indent=2))


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print(json.dumps({'status': 'STOPPED', 'error_type': type(error).__name__,
                          'reason': str(error) if isinstance(error, ValueError) else 'Private API diagnostics suppressed; reconcile any durable write intent'}))
        raise SystemExit(1)
