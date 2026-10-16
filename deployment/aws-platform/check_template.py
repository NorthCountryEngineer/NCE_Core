#!/usr/bin/env python3
"""Offline security invariants for platform onboarding; no SDK or network calls.

This supplements, rather than replaces, CloudFormation schema validation and an
account-aware first deployment. Run it whenever editing the onboarding template.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / 'platform.template.json'


def require(value, message):
    if not value:
        raise SystemExit(f'FAIL: {message}')


def statements(resource):
    value = resources[resource]['Properties']
    return [item for entry in value.get('Policies', []) for item in entry['PolicyDocument']['Statement']]


def actions(resource):
    return {item for statement in statements(resource) if statement['Effect'] == 'Allow'
            for item in ([statement['Action']] if isinstance(statement['Action'], str) else statement['Action'])}


def start_statement(resource):
    return next(item for item in statements(resource) if 'codebuild:StartBuild' in item['Action'])


body = TEMPLATE.read_bytes()
template = json.loads(body)
resources = template['Resources']
require(len(body) <= 51200, 'template must fit CloudFormation inline limit')
require(template.get('Metadata', {}).get('AWSToolsMetrics', {}).get('AWSAgentToolkit') == 'aws-cloudformation@3', 'tool/context marker missing')

bucket = resources['Artifacts']
require(bucket['DeletionPolicy'] == bucket['UpdateReplacePolicy'] == 'Retain', 'artifact retention changed')
require(all(bucket['Properties']['PublicAccessBlockConfiguration'].values()), 'artifact public access must stay blocked')
require(bucket['Properties']['VersioningConfiguration']['Status'] == 'Enabled', 'artifact versioning must stay enabled')
require(bucket['Properties']['BucketEncryption']['ServerSideEncryptionConfiguration'][0]['ServerSideEncryptionByDefault']['SSEAlgorithm'] == 'AES256', 'artifact encryption changed')
tls = resources['ArtifactsTLS']['Properties']['PolicyDocument']['Statement'][0]
require(tls['Effect'] == 'Deny' and tls['Condition']['Bool']['aws:SecureTransport'] == 'false', 'TLS enforcement missing')

for name, entry in [('Runner', 'runner.py'), ('SourceBuilder', 'build_source.py')]:
    project = resources[name]['Properties']
    spec = project['Source']['BuildSpec']['Fn::Sub']
    require(spec.index('git rev-parse HEAD') < spec.index('pip install') < spec.index(f'python3 -I deployment/aws-apps/{entry}'), f'{name} must check trusted source before executing it')
    require('${RunnerCommit}' in spec and project['SourceVersion'] == {'Ref': 'RunnerCommit'}, f'{name} must pin trusted source')
    require(project['Source']['Auth'] == {'Type': 'CODECONNECTIONS', 'Resource': {'Ref': 'CodeConnectionArn'}}, f'{name} connection changed')
    require(project['Artifacts']['Type'] == 'NO_ARTIFACTS' and project['Environment']['PrivilegedMode'] is False, f'{name} cannot use privileged Docker or automatic artifacts')
    require('Triggers' not in project and project['ConcurrentBuildLimit'] == 1, f'{name} must not auto-build')

require(resources['Runner']['Properties']['ServiceRole'] != resources['SourceBuilder']['Properties']['ServiceRole'], 'source and deployment credentials must stay separate')
require(resources['Runner']['Properties']['Environment']['ComputeType'] == 'BUILD_GENERAL1_SMALL' and resources['SourceBuilder']['Properties']['Environment']['ComputeType'] == 'BUILD_GENERAL1_MEDIUM', 'nested builder must use a separate concurrency quota from its waiting runner')
source_actions = actions('SourceRole')
require(not any(action.startswith(('iam:', 'ssm:', 'cloudformation:', 'amplify:', 'lambda:', 'sts:')) for action in source_actions), 'source builder received deployment/secret permissions')
require(not any(action.startswith(('ecs:', 'ecr:', 'rds:', 'secretsmanager:', 'ec2:', 'servicediscovery:')) for action in source_actions), 'source builder received container or runtime-secret permissions')
require(not any(action in source_actions for action in ['s3:GetObject', 's3:ListBucket']), 'source builder cannot read other app archives')
source_write = next(item for item in statements('SourceRole') if 's3:PutObject' in item['Action'])
require(source_write['Resource'] == {'Fn::Sub': '${Artifacts.Arn}/builds/*'}, 'source archive writes escaped build prefix')

frontend_app = template['Parameters']['FrontendAppId']
frontend_branch = template['Parameters']['FrontendBranch']
require(frontend_app['Default'] == '' and frontend_app['AllowedPattern'] == '^$|^d[a-z0-9]{1,40}$', 'frontend app binding must default disabled and reject ARN/path/wildcard injection')
require(frontend_branch['Default'] == 'production' and frontend_branch['AllowedPattern'] == '^[A-Za-z0-9._-]{1,255}$', 'frontend branch binding must select one bounded literal branch')
require(re.fullmatch(frontend_app['AllowedPattern'], '') is not None, 'empty frontend app must remain a valid disabled setting')
require(template['Conditions']['HasBoundFrontendApp'] == {'Fn::Not': [{'Fn::Equals': [{'Ref': 'FrontendAppId'}, '']}]}, 'frontend publishing must be explicitly bound by the platform owner')
frontend_policy = resources['BoundFrontendPublishPolicy']
require(frontend_policy['Type'] == 'AWS::IAM::ManagedPolicy' and frontend_policy['Condition'] == 'HasBoundFrontendApp', 'optional frontend grant must be a conditional managed policy')
require(frontend_policy['Properties']['Roles'] == [{'Ref': 'RunnerRole'}], 'optional frontend publishing escaped the trusted runner role')
require(frontend_policy['Properties']['PolicyDocument']['Statement'] == [{
    'Effect': 'Allow', 'Action': ['amplify:CreateDeployment', 'amplify:StartDeployment'],
    'Resource': [
        {'Fn::Sub': 'arn:${AWS::Partition}:amplify:${AWS::Region}:${AWS::AccountId}:apps/${FrontendAppId}/branches/${FrontendBranch}'},
        {'Fn::Sub': 'arn:${AWS::Partition}:amplify:${AWS::Region}:${AWS::AccountId}:apps/${FrontendAppId}/branches/${FrontendBranch}/*'},
    ],
    'Condition': {'StringEquals': {'aws:RequestedRegion': {'Ref': 'AWS::Region'}}},
}], 'optional frontend publishing must allow only two actions on one exact account/region/app/branch and its descendants')
normal_publish = next(item for item in statements('RunnerRole') if item.get('Sid') == 'PublishOnlyTaggedFrontends')
require(normal_publish['Action'] == ['amplify:CreateDeployment', 'amplify:StartDeployment', 'amplify:GetApp', 'amplify:GetBranch'] and normal_publish['Condition'] == {'StringEquals': {'aws:ResourceTag/nce:platform-prefix': {'Ref': 'Prefix'}}}, 'normal frontend publish/read tag authority must remain intact')

for name, permitted in [('ControlRole', ['NCE_DEPLOYMENT', 'NCE_RESULT_KEY', 'NCE_RUN_TOKEN']), ('RunnerRole', ['NCE_BUILD_CONFIG', 'NCE_RUN_TOKEN'])]:
    condition = start_statement(name)['Condition']
    keys = condition['Null']
    for key in ['codebuild:source.buildspec', 'codebuild:source.location', 'codebuild:serviceRole', 'codebuild:environment.image', 'codebuild:environment.privilegedMode', 'codebuild:artifacts', 'codebuild:logsConfig']:
        require(keys.get(key) == 'true', f'{name} permits unsafe override {key}')
    require(condition['ForAllValues:StringEquals']['codebuild:environment.environmentVariables.name'] == permitted, f'{name} permits protected environment variable overrides')

control_trust = resources['ControlRole']['Properties']['AssumeRolePolicyDocument']['Statement'][0]
require(control_trust['Principal'] == {'AWS': {'Ref': 'CoreRuntimeRoleArn'}}, 'control trust must name exact runtime principal')
require(control_trust['Condition']['StringEquals']['sts:ExternalId'] == {'Ref': 'CoreExternalId'}, 'control external ID condition missing')
for policy, action in [(resources['AppBoundary']['Properties']['PolicyDocument'], 'kms:Decrypt'), (resources['ControlRole']['Properties']['Policies'][0]['PolicyDocument'], 'kms:Encrypt')]:
    grant = next(item for item in policy['Statement'] if action in item['Action'])
    require(grant['Resource'] == {'Fn::Sub': 'arn:${AWS::Partition}:kms:${AWS::Region}:${AWS::AccountId}:key/*'}, 'SSM KMS grant escaped target account/region')
    require(grant['Condition']['StringEquals']['kms:ViaService'] == {'Fn::Sub': 'ssm.${AWS::Region}.amazonaws.com'}, 'SSM KMS grant permits direct key use')
    require(grant['Condition']['StringLike']['kms:EncryptionContext:PARAMETER_ARN'] == {'Fn::Sub': 'arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter/${Prefix}/apps/*/core-resource-client'}, 'SSM KMS grant escaped application credential path')
    require(grant['Condition']['ForAnyValue:StringEquals']['kms:ResourceAliases'] == ['alias/aws/ssm'] and grant['Condition']['Null']['kms:ResourceAliases'] == 'false', 'SSM KMS grant permits a customer-managed or unaliased key')
cf_statements = statements('CloudFormationRole')
bucket_permissions = next(item for item in cf_statements if item['Sid'] == 'AppBuckets')
require(bucket_permissions['Resource'] == {'Fn::Sub': 'arn:${AWS::Partition}:s3:::${Prefix}-${AWS::AccountId}-*'}, 'CloudFormation bucket reads must remain within app names')
require({'s3:ListBucket', 's3:GetBucketAcl', 's3:GetBucketLogging', 's3:GetBucketWebsite', 's3:GetReplicationConfiguration', 's3:GetBucketObjectLockConfiguration', 's3:GetBucketAbac', 's3:GetBucketMetadataTableConfiguration', 's3:ListTagsForResource'} <= set(bucket_permissions['Action']), 'CloudFormation S3 stabilization metadata permissions missing')
require('iam:ListAttachedRolePolicies' in actions('CloudFormationRole'), 'CloudFormation IAM role read permission missing')
creation = next(item for item in cf_statements if 'iam:CreateRole' in item['Action'])
require(creation['Condition']['ArnEquals']['iam:PermissionsBoundary'] == {'Ref': 'AppBoundary'}, 'new app role boundary must be mandatory')
require(not actions('CloudFormationRole') & {'iam:AttachRolePolicy', 'iam:CreatePolicy', 'iam:DeleteRolePermissionsBoundary'}, 'app execution role permits privilege escalation')
for name, service in [('RunnerRole', 'cloudformation.amazonaws.com'), ('CloudFormationRole', 'lambda.amazonaws.com')]:
    pass_role = next(item for item in statements(name) if 'iam:PassRole' in item['Action'])
    require(pass_role['Resource'] != '*' and pass_role['Condition']['StringEquals']['iam:PassedToService'] == service, f'{name} pass-role must stay bounded')
fargate = resources['FargateCloudFormationRole']
require(fargate['Condition'] == 'EnableFargate', 'container provisioning must be explicitly enabled at onboarding')
fargate_statements = statements('FargateCloudFormationRole')
new_roles = next(item for item in fargate_statements if 'iam:CreateRole' in item['Action'])
require(new_roles['Condition']['ArnEquals']['iam:PermissionsBoundary'] == {'Ref': 'AppBoundary'}, 'container role boundary missing')
require(not actions('FargateCloudFormationRole') & {'iam:AttachRolePolicy', 'iam:CreatePolicy', 'iam:DeleteRolePermissionsBoundary'}, 'container role permits privilege escalation')
require(not any(action.startswith('route53:') for action in actions('FargateCloudFormationRole')), 'container recipe cannot manage public DNS')
namespace_policy = resources['FargatePrivateNamespacePolicy']
require(namespace_policy['Condition'] == 'EnableFargate', 'private DNS support must follow the container recipe gate')
require(namespace_policy['Properties']['Roles'] == [{'Ref': 'FargateCloudFormationRole'}], 'private DNS support escaped its exact execution role')
namespace_statements = namespace_policy['Properties']['PolicyDocument']['Statement']
namespace_actions = {action for entry in namespace_statements for action in entry['Action']}
require(namespace_actions == {'route53:CreateHostedZone', 'route53:DeleteHostedZone', 'route53:GetHostedZone', 'route53:ListHostedZones', 'route53:ListHostedZonesByName', 'servicediscovery:ListServices'}, 'private namespace support gained unrelated operations')
provider_policy = resources['FargateProviderMetadataPolicy']
require(provider_policy['Condition'] == 'EnableFargate' and provider_policy['Properties']['Roles'] == [{'Ref': 'FargateCloudFormationRole'}], 'provider metadata support escaped the gated container execution role')
provider_statements = provider_policy['Properties']['PolicyDocument']['Statement']
require(len(provider_statements) == 1, 'provider metadata gained unrelated grants')
provider_read = provider_statements[0]
require(set(provider_read['Action']) == {'ecs:DescribeTaskDefinition', 'logs:DescribeIndexPolicies', 'rds:DescribeEngineDefaultParameters', 'rds:DescribeEvents', 'servicediscovery:ListTagsForResource'}, 'provider metadata gained unrelated operations')
require(provider_read['Effect'] == 'Allow' and provider_read['Resource'] == '*' and provider_read['Condition'] == {'StringEquals': {'aws:RequestedRegion': {'Ref': 'AWS::Region'}}}, 'provider metadata must use supported unscoped resources and require this region')
service_listing = next(item for item in fargate_statements if 'ecs:ListServiceDeployments' in item['Action'])
require({'Fn::Sub': 'arn:${AWS::Partition}:ecs:${AWS::Region}:${AWS::AccountId}:service/${Prefix}-*/*'} in service_listing['Resource'] and '*' not in service_listing['Resource'], 'service deployment listing escaped named container resources')
zone_create = next(entry for entry in namespace_statements if 'route53:CreateHostedZone' in entry['Action'])
require(zone_create['Condition'] == {'Bool': {'aws:ViaAWSService': 'true'}, 'StringEquals': {'aws:CalledViaLast': 'servicediscovery.amazonaws.com'}, 'Null': {'aws:CalledViaLast': 'false'}}, 'hosted zone creation must require Cloud Map as the last FAS service; direct and missing-context calls deny')
require('servicediscovery:CreatePublicDnsNamespace' not in actions('FargateCloudFormationRole'), 'private namespace role must not create a public namespace')
zone_delete = next(entry for entry in namespace_statements if 'route53:DeleteHostedZone' in entry['Action'])
require(zone_delete['Condition'] == zone_create['Condition'] and zone_delete['Resource'] == {'Fn::Sub': 'arn:${AWS::Partition}:route53:::hostedzone/*'}, 'namespace zone deletion must remain Cloud Map FAS gated and zone-ARN scoped')
cleanup_policy = resources['FargateTaskDefinitionCleanupPolicy']
require(cleanup_policy['Condition'] == 'EnableFargate' and cleanup_policy['Properties']['Roles'] == [{'Ref': 'FargateCloudFormationRole'}], 'task cleanup escaped its gated fixed execution role')
require(cleanup_policy['Properties']['PolicyDocument']['Statement'] == [{'Effect': 'Allow', 'Action': ['ecs:DeregisterTaskDefinition'], 'Resource': '*', 'Condition': {'StringEquals': {'aws:RequestedRegion': {'Ref': 'AWS::Region'}}}}], 'task cleanup gained unrelated actions or unsupported ARN scoping')
require('ecs:DeregisterTaskDefinition' not in actions('FargateCloudFormationRole'), 'unsupported deregistration ARN scope must not remain in the named container grant')
app_bucket = next(entry for entry in fargate_statements if entry.get('Sid') == 'AppBuckets')
require('s3:DeleteBucketPolicy' in app_bucket['Action'] and app_bucket['Resource'] == {'Fn::Sub': 'arn:${AWS::Partition}:s3:::${Prefix}-${AWS::AccountId}-*'}, 'bucket-policy cleanup escaped named platform buckets')
new_discovery = next(entry for entry in fargate_statements if entry.get('Sid') == 'CreateTaggedServiceDiscovery')
require('servicediscovery:TagResource' in new_discovery['Action'] and new_discovery['Condition']['StringEquals']['aws:RequestTag/nce:platform-prefix'] == {'Ref': 'Prefix'}, 'new namespace tags need request-tag authorization')
migrations = next(item for item in statements('RunnerRole') if 'ecs:RunTask' in item['Action'])
require(migrations['Resource'] == {'Fn::Sub': 'arn:${AWS::Partition}:ecs:${AWS::Region}:${AWS::AccountId}:task-definition/${Prefix}-*-migration:*'}, 'runner can execute an arbitrary container task')
require(migrations['Condition']['ArnLike']['ecs:cluster'] == {'Fn::Sub': 'arn:${AWS::Partition}:ecs:${AWS::Region}:${AWS::AccountId}:cluster/${Prefix}-*'}, 'migration cluster scope missing')
for name, resource in resources.items():
    if resource['Type'] == 'AWS::IAM::Role':
        length = sum(len(json.dumps(entry['PolicyDocument'], separators=(',', ':'))) for entry in resource['Properties'].get('Policies', []))
        require(length <= 10240, f'{name} inline policies exceed IAM role limit')
    elif resource['Type'] == 'AWS::IAM::ManagedPolicy':
        length = len(json.dumps(resource['Properties']['PolicyDocument'], separators=(',', ':')))
        require(length <= 6144, f'{name} exceeds managed-policy size limit')
print(f'PASS: onboarding invariants; {len(resources)} resources; {len(body)} bytes. No AWS calls made.')
