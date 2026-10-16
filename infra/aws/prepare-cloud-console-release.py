#!/usr/bin/env python3
"""Prepare existing-stack updates after exact-commit GitHub CI passes. Never calls AWS."""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from platform_services.github_ci import CIGateError, verify_repository


def prepare(stack, image, account, enable_live):
    if stack.get('StackStatus') not in {'CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE'}:
        raise ValueError('The existing Core stack must be stable before preparing this update.')
    if not stack.get('StackId', '').startswith(f'arn:aws:cloudformation:us-east-1:{account}:stack/'):
        raise ValueError('The stack must belong to the expected account in us-east-1.')
    previous = {row['ParameterKey']: row['ParameterValue'] for row in stack['Parameters']}
    if previous.get('DesiredCount') != '1' or not previous.get('CoreImageUri'):
        raise ValueError('Expected one running Core service and its existing image.')
    if not re.fullmatch(rf'{account}\.dkr\.ecr\.us-east-1\.amazonaws\.com/[^@\s]+@sha256:[a-f0-9]{{64}}', image):
        raise ValueError('Use an immutable ECR image digest in the expected account and region.')
    results = {}
    for name, uri, count, live in [('stopped', image, '0', enable_live), ('running', image, '1', enable_live),
                                  ('recovery', previous['CoreImageUri'], '1', '0')]:
        overrides = {'CoreImageUri': uri, 'DesiredCount': count, 'EnableCloudDeployments': live}
        values = [{'ParameterKey': key, 'UsePreviousValue': True} for key in previous if key not in overrides]
        values.extend({'ParameterKey': key, 'ParameterValue': value} for key, value in overrides.items())
        results[name] = values
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stack-file', type=Path, required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--commit', required=True, help='Exact source commit used to build this immutable image.')
    parser.add_argument('--account', default='015454941623')
    parser.add_argument('--enable-live', choices=['0', '1'], default='0')
    parser.add_argument('--output-directory', type=Path, required=True)
    options = parser.parse_args()
    try:
        documents = prepare(json.loads(options.stack_file.read_text())['Stacks'][0], options.image, options.account, options.enable_live)
        receipt = verify_repository('NorthCountryEngineer/NCE_Core', options.commit)
        options.output_directory.mkdir(parents=True, exist_ok=True)
        for name, values in documents.items():
            target = options.output_directory / f'core-cloud-{name}.json'
            target.write_text(json.dumps(values, indent=2) + '\n')
            target.chmod(0o600)
        proof = options.output_directory / 'core-ci.json'
        proof.write_text(json.dumps({'image': options.image, 'ci': receipt}, indent=2) + '\n')
        proof.chmod(0o600)
    except CIGateError as error:
        parser.error(str(error))
    except (ValueError, KeyError, OSError):
        parser.error('Cannot prepare release: check the stable stack metadata, account, image digest, and output directory.')
    print('Verified exact-commit GitHub CI and prepared stopped, running, and recovery updates locally. No AWS calls were made.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
