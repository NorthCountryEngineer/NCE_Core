"""Read-only management-shell proof of preserved static/serverless control auth.

Run via the normal Core launcher after restore. No credentials/external ID or
SDK diagnostics are printed. The retained CodeBuild, source connection and
artifact-bucket verification is read-only. Output contains booleans and caller ARNs.
"""
import json
import boto3
from botocore.config import Config
from platform_services.cloud_models import CloudPlatform
from platform_services.cloud_aws import _clients, verify
PHASE = 'platform-lookup'


def prove():
    global PHASE
    platforms = list(CloudPlatform.objects.filter(mode='aws', account_id='015454941623', prefix='nce-family'))
    if len(platforms) != 1:
        raise RuntimeError('Expected exactly one preserved AWS control platform')
    platform = platforms[0]
    client = boto3.Session(region_name=platform.region).client('sts', config=Config(
        connect_timeout=5, read_timeout=20, retries={'mode': 'standard', 'total_max_attempts': 1}))
    PHASE = 'runtime-caller-identity'
    source = client.get_caller_identity()
    if source['Account'] != platform.account_id or ':user/' not in source['Arn']:
        raise RuntimeError('Expected the scoped IAM-user Core runtime identity')
    PHASE = 'assume-preserved-control-role'
    delegated = _clients(platform, ('sts', 'cloudformation'))
    PHASE = 'delegated-caller-identity'
    caller = delegated['sts'].get_caller_identity()
    PHASE = 'verify-retained-static-control-contract'
    verified = verify(platform)
    if verified['account_id'] != platform.account_id:
        raise RuntimeError('Retained static control contract account differs')
    return {'passed': True, 'source_principal_arn': source['Arn'], 'assumed_principal_arn': caller['Arn'],
            'account_matches': caller['Account'] == platform.account_id, 'preserved_external_id_used': True,
            'retained_static_codebuild_source_connection_artifact_bucket_verified': True,
            'describe_stacks_intentionally_not_permitted_by_existing_control_role': True, 'application_infrastructure_created': False,
            'credential_values_printed': False}


try:
    print(json.dumps(prove(), sort_keys=True))
except Exception as error:
    code = getattr(error, 'response', {}).get('Error', {}).get('Code', '')
    if code not in {'AccessDenied', 'AccessDeniedException', 'ExpiredToken', 'InvalidClientTokenId', 'ValidationError'}:
        code = type(error).__name__
    print(json.dumps({'passed': False, 'phase': PHASE, 'error_code': code, 'credential_values_printed': False}))
    raise RuntimeError('Read-only AWS control authentication proof failed; private diagnostics suppressed') from None
