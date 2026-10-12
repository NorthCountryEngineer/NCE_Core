#!/usr/bin/env python3
"""Render local CloudFormation JSON only. No AWS calls or provisioning."""
import json
from pathlib import Path

ref = lambda value: {"Ref": value}
get = lambda resource, attribute: {"Fn::GetAtt": [resource, attribute]}
sub = lambda value: {"Fn::Sub": value}
resources = {}


def resource(name, kind, **properties):
    resources[name] = {"Type": kind, "Properties": properties}
    return ref(name)


resource("Vpc", "AWS::EC2::VPC", CidrBlock="10.64.0.0/16", EnableDnsSupport=True, EnableDnsHostnames=True)
resource("InternetGateway", "AWS::EC2::InternetGateway")
resource("GatewayAttachment", "AWS::EC2::VPCGatewayAttachment", VpcId=ref("Vpc"), InternetGatewayId=ref("InternetGateway"))
resource("PublicRoutes", "AWS::EC2::RouteTable", VpcId=ref("Vpc"))
resource("PublicRoute", "AWS::EC2::Route", RouteTableId=ref("PublicRoutes"), DestinationCidrBlock="0.0.0.0/0", GatewayId=ref("InternetGateway"))
resources["PublicRoute"]["DependsOn"] = "GatewayAttachment"
for index in range(2):
    suffix = str(index + 1)
    az = {"Fn::Select": [index, {"Fn::GetAZs": ""}]}
    resource("PublicSubnet" + suffix, "AWS::EC2::Subnet", VpcId=ref("Vpc"), AvailabilityZone=az,
             CidrBlock=f"10.64.{index}.0/24", MapPublicIpOnLaunch=False)
    resource("PublicAssociation" + suffix, "AWS::EC2::SubnetRouteTableAssociation",
             SubnetId=ref("PublicSubnet" + suffix), RouteTableId=ref("PublicRoutes"))
    # RDS subnets have no internet route and no public IPs.
    resource("DatabaseSubnet" + suffix, "AWS::EC2::Subnet", VpcId=ref("Vpc"), AvailabilityZone=az,
             CidrBlock=f"10.64.{index + 10}.0/24", MapPublicIpOnLaunch=False)
resource("AlbSecurityGroup", "AWS::EC2::SecurityGroup", GroupDescription="HTTPS origin ingress",
         VpcId=ref("Vpc"), SecurityGroupIngress=[{"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "CidrIp": "0.0.0.0/0"}],
         SecurityGroupEgress=[{"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000, "CidrIp": "10.64.0.0/16"}])
resource("TaskSecurityGroup", "AWS::EC2::SecurityGroup", GroupDescription="Core tasks; ALB only inbound",
         VpcId=ref("Vpc"), SecurityGroupIngress=[{"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000, "SourceSecurityGroupId": ref("AlbSecurityGroup")}],
         SecurityGroupEgress=[{"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "CidrIp": "0.0.0.0/0"}])
resource("DatabaseSecurityGroup", "AWS::EC2::SecurityGroup", GroupDescription="PostgreSQL; Core tasks only",
         VpcId=ref("Vpc"), SecurityGroupIngress=[{"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432, "SourceSecurityGroupId": ref("TaskSecurityGroup")}])
resource("TaskDatabaseEgress", "AWS::EC2::SecurityGroupEgress", GroupId=ref("TaskSecurityGroup"),
         IpProtocol="tcp", FromPort=5432, ToPort=5432, DestinationSecurityGroupId=ref("DatabaseSecurityGroup"))
resource("DatabaseSecret", "AWS::SecretsManager::Secret", Description="Core PostgreSQL administrator credential",
         GenerateSecretString={"SecretStringTemplate": '{"username":"ncecore"}', "GenerateStringKey": "password", "PasswordLength": 40, "ExcludePunctuation": True})
resources["DatabaseSecret"].update(DeletionPolicy="Retain", UpdateReplacePolicy="Retain")
resource("OriginSecret", "AWS::SecretsManager::Secret", Description="Core proxy origin header; not Django signing keys",
         GenerateSecretString={"PasswordLength": 64, "ExcludePunctuation": True})
resources["OriginSecret"].update(DeletionPolicy="Retain", UpdateReplacePolicy="Retain")
resource("DatabaseSubnets", "AWS::RDS::DBSubnetGroup", DBSubnetGroupDescription="Private Core database subnets",
         SubnetIds=[ref("DatabaseSubnet1"), ref("DatabaseSubnet2")])
resource("Database", "AWS::RDS::DBInstance", Engine="postgres", DBInstanceClass="db.t4g.micro", DBName="ncecore",
         AllocatedStorage="20", StorageType="gp3", StorageEncrypted=True, PubliclyAccessible=False, MultiAZ=False,
         MasterUsername="ncecore", MasterUserPassword=sub("{{resolve:secretsmanager:${DatabaseSecret}:SecretString:password}}"),
         DBSubnetGroupName=ref("DatabaseSubnets"), VPCSecurityGroups=[ref("DatabaseSecurityGroup")],
         BackupRetentionPeriod=7, DeletionProtection=True, CopyTagsToSnapshot=True, AutoMinorVersionUpgrade=True)
resources["Database"].update(DeletionPolicy="Snapshot", UpdateReplacePolicy="Snapshot")
resource("DatabaseSecretAttachment", "AWS::SecretsManager::SecretTargetAttachment", SecretId=ref("DatabaseSecret"),
         TargetId=ref("Database"), TargetType="AWS::RDS::DBInstance")
resource("Logs", "AWS::Logs::LogGroup", RetentionInDays=14)
resources["Logs"].update(DeletionPolicy="Retain", UpdateReplacePolicy="Retain")
resource("Cluster", "AWS::ECS::Cluster")
ecs_trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
resource("ExecutionRole", "AWS::IAM::Role", AssumeRolePolicyDocument=ecs_trust,
         ManagedPolicyArns=[sub("arn:${AWS::Partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy")],
         Policies=[{"PolicyName": "CoreSecrets", "PolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": [ref("CoreRuntimeSecretArn"), ref("DatabaseSecret"), ref("OriginSecret")]}]}}])
resource("TaskRole", "AWS::IAM::Role", AssumeRolePolicyDocument=ecs_trust)
resource("AmplifyComputeRole", "AWS::IAM::Role", AssumeRolePolicyDocument={"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "amplify.amazonaws.com"}, "Action": "sts:AssumeRole", "Condition": {"StringEquals": {"aws:SourceAccount": ref("AWS::AccountId")}, "ArnLike": {"aws:SourceArn": sub("arn:${AWS::Partition}:amplify:${AWS::Region}:${AWS::AccountId}:apps/*")}}}]},
         Policies=[{"PolicyName": "ReadOriginHeaderOnly", "PolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": ref("OriginSecret")}]}}])
environment = [{"Name": key, "Value": value} for key, value in {
    "CORE_ISSUER": ref("CorePublicOrigin"), "CORE_TRUST_PROXY": "1", "CORE_AWS_ORIGIN_BOUNDARY": "1",
    "CORE_DB_HOST": get("Database", "Endpoint.Address"), "CORE_DB_NAME": "ncecore", "CORE_DB_USER": "ncecore",
}.items()]
secrets = [{"Name": key, "ValueFrom": value} for key, value in {
    "CORE_DJANGO_KEY": sub("${CoreRuntimeSecretArn}:django_key::"),
    "CORE_OIDC_PRIVATE_KEY": sub("${CoreRuntimeSecretArn}:oidc_key::"),
    "CORE_ORIGIN_SECRET": ref("OriginSecret"), "CORE_DB_PASSWORD": sub("${DatabaseSecret}:password::"),
}.items()]
containers = []
for name in ["web", "worker"]:
    item = {"Name": name, "Image": ref("CoreImageUri"), "Essential": True, "Command": [name],
            "Environment": environment, "Secrets": secrets, "MemoryReservation": 256,
            "LogConfiguration": {"LogDriver": "awslogs", "Options": {"awslogs-group": ref("Logs"), "awslogs-region": ref("AWS::Region"), "awslogs-stream-prefix": name}}}
    if name == "web":
        item["PortMappings"] = [{"ContainerPort": 8000, "Protocol": "tcp"}]
    containers.append(item)
resource("TaskDefinition", "AWS::ECS::TaskDefinition", Cpu="256", Memory="1024", NetworkMode="awsvpc",
         RequiresCompatibilities=["FARGATE"], RuntimePlatform={"CpuArchitecture": "ARM64", "OperatingSystemFamily": "LINUX"},
         ExecutionRoleArn=get("ExecutionRole", "Arn"), TaskRoleArn=get("TaskRole", "Arn"), ContainerDefinitions=containers)
# A separate one-off bootstrap task is the only task that receives the initial
# administrator password. It is never part of the long-running service.
bootstrap_container = dict(containers[0])
bootstrap_container["Command"] = ["bootstrap"]
bootstrap_container["Secrets"] = secrets + [{"Name": "CORE_BOOTSTRAP_PASSWORD", "ValueFrom": sub("${CoreRuntimeSecretArn}:bootstrap_password::")}]
resource("BootstrapTaskDefinition", "AWS::ECS::TaskDefinition", Cpu="256", Memory="1024", NetworkMode="awsvpc",
         RequiresCompatibilities=["FARGATE"], RuntimePlatform={"CpuArchitecture": "ARM64", "OperatingSystemFamily": "LINUX"},
         ExecutionRoleArn=get("ExecutionRole", "Arn"), TaskRoleArn=get("TaskRole", "Arn"), ContainerDefinitions=[bootstrap_container])
parameter_arn = sub("arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter${CoreResourceCredentialsParameter}")
resource("RegistrationTaskRole", "AWS::IAM::Role", AssumeRolePolicyDocument=ecs_trust,
         Policies=[{"PolicyName": "WriteAxolotlResourceCredentialOnly", "PolicyDocument": {"Version": "2012-10-17", "Statement": [
             {"Effect": "Allow", "Action": "ssm:PutParameter", "Resource": parameter_arn},
             {"Effect": "Allow", "Action": "kms:Encrypt", "Resource": sub("arn:${AWS::Partition}:kms:${AWS::Region}:${AWS::AccountId}:key/*"),
              "Condition": {"StringEquals": {"kms:ViaService": sub("ssm.${AWS::Region}.amazonaws.com"), "kms:EncryptionContext:PARAMETER_ARN": parameter_arn}}},
         ]}}])
registration_container = dict(containers[0])
registration_container["Command"] = ["manage", "register_external_application"]
registration_container["Environment"] = environment + [
    {"Name": "CORE_REGISTRATION_SSM_PARAMETER", "Value": ref("CoreResourceCredentialsParameter")},
    {"Name": "AWS_REGION", "Value": ref("AWS::Region")},
]
resource("RegistrationTaskDefinition", "AWS::ECS::TaskDefinition", Cpu="256", Memory="1024", NetworkMode="awsvpc",
         RequiresCompatibilities=["FARGATE"], RuntimePlatform={"CpuArchitecture": "ARM64", "OperatingSystemFamily": "LINUX"},
         ExecutionRoleArn=get("ExecutionRole", "Arn"), TaskRoleArn=get("RegistrationTaskRole", "Arn"), ContainerDefinitions=[registration_container])
resource("LoadBalancer", "AWS::ElasticLoadBalancingV2::LoadBalancer", Scheme="internet-facing", Type="application",
         SecurityGroups=[ref("AlbSecurityGroup")], Subnets=[ref("PublicSubnet1"), ref("PublicSubnet2")],
         LoadBalancerAttributes=[{"Key": "routing.http.drop_invalid_header_fields.enabled", "Value": "true"}])
resource("TargetGroup", "AWS::ElasticLoadBalancingV2::TargetGroup", VpcId=ref("Vpc"), TargetType="ip", Port=8000, Protocol="HTTP",
         HealthCheckPath="/health/", HealthCheckIntervalSeconds=30, Matcher={"HttpCode": "200"})
resource("Listener", "AWS::ElasticLoadBalancingV2::Listener", LoadBalancerArn=ref("LoadBalancer"), Port=443, Protocol="HTTPS",
         Certificates=[{"CertificateArn": ref("CertificateArn")}], SslPolicy="ELBSecurityPolicy-TLS13-1-2-2021-06",
         DefaultActions=[{"Type": "forward", "TargetGroupArn": ref("TargetGroup")}])
resource("Service", "AWS::ECS::Service", Cluster=ref("Cluster"), TaskDefinition=ref("TaskDefinition"), LaunchType="FARGATE", PlatformVersion="1.4.0",
         DesiredCount=ref("DesiredCount"), HealthCheckGracePeriodSeconds=120,
         DeploymentConfiguration={"MinimumHealthyPercent": 0, "MaximumPercent": 100,
                                  "DeploymentCircuitBreaker": {"Enable": True, "Rollback": True}},
         NetworkConfiguration={"AwsvpcConfiguration": {"AssignPublicIp": "ENABLED", "SecurityGroups": [ref("TaskSecurityGroup")], "Subnets": [ref("PublicSubnet1"), ref("PublicSubnet2")]}},
         LoadBalancers=[{"ContainerName": "web", "ContainerPort": 8000, "TargetGroupArn": ref("TargetGroup")}])
resources["Service"]["DependsOn"] = ["Listener", "DatabaseSecretAttachment", "PublicRoute"]
template = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Description": "Core Django + worker on ARM64 Fargate, RDS and HTTPS ALB. BILLABLE when deployed. No NAT gateway. Amplify SSR frontend is configured separately.",
    "Parameters": {
        "CoreImageUri": {"Type": "String", "AllowedPattern": ".+@sha256:[a-f0-9]{64}", "Description": "Immutable ARM64 image digest in ECR"},
        "CorePublicOrigin": {"Type": "String", "AllowedPattern": "https://[A-Za-z0-9.-]+", "Description": "Permanent public Core issuer: Amplify frontend origin"},
        "CoreOriginHost": {"Type": "String", "Default": "core-origin.axolotl.news", "AllowedPattern": "[A-Za-z0-9.-]+", "Description": "Cloudflare DNS-only CNAME host; covered by the ACM certificate"},
        "CertificateArn": {"Type": "String", "AllowedPattern": "arn:aws:acm:.*", "Description": "Validated ACM certificate in THIS region for CoreOriginHost"},
        "CoreRuntimeSecretArn": {"Type": "String", "AllowedPattern": "arn:aws:secretsmanager:.*", "Description": "Existing JSON SecretString keys django_key, oidc_key (RSA PEM) and bootstrap_password; preserve signing keys"},
        "CoreResourceCredentialsParameter": {"Type": "String", "Default": "/axolotl-news/core-resource-client", "AllowedPattern": "/[A-Za-z0-9_./-]+", "Description": "Exactly one standard SecureString parameter writable by the dedicated app-registration task"},
        "DesiredCount": {"Type": "Number", "Default": 0, "AllowedValues": [0, 1], "Description": "Keep zero until one-off migrations and first administrator bootstrap succeed"},
    },
    "Resources": resources,
    "Outputs": {key: {"Value": value} for key, value in {
        "CorePublicOrigin": ref("CorePublicOrigin"), "CoreBackendOrigin": sub("https://${CoreOriginHost}"),
        "LoadBalancerDnsName": get("LoadBalancer", "DNSName"), "CoreOriginSecretArn": ref("OriginSecret"),
        "AmplifyComputeRoleArn": get("AmplifyComputeRole", "Arn"), "Cluster": ref("Cluster"),
        "Service": get("Service", "Name"), "TaskDefinition": ref("TaskDefinition"),
        "BootstrapTaskDefinition": ref("BootstrapTaskDefinition"),
        "RegistrationTaskDefinition": ref("RegistrationTaskDefinition"), "CoreResourceCredentialsParameter": ref("CoreResourceCredentialsParameter"),
        "TaskSecurityGroup": ref("TaskSecurityGroup"), "PublicSubnet1": ref("PublicSubnet1"), "PublicSubnet2": ref("PublicSubnet2"),
        "DatabaseIdentifier": ref("Database"), "DatabaseSecretArn": ref("DatabaseSecret"), "LogGroup": ref("Logs"),
    }.items()},
}
path = Path(__file__).with_name("core-fargate.template.json")
path.write_text(json.dumps(template, indent=2) + "\n")
print(f"Rendered {path.name} locally; no AWS calls.")
registry = {"AWSTemplateFormatVersion": "2010-09-09", "Description": "Core ECR image registry; storage/scanning may incur AWS charges after use.",
    "Resources": {"Repository": {"Type": "AWS::ECR::Repository", "DeletionPolicy": "Retain", "UpdateReplacePolicy": "Retain",
        "Properties": {"ImageTagMutability": "IMMUTABLE", "ImageScanningConfiguration": {"ScanOnPush": True},
                       "LifecyclePolicy": {"LifecyclePolicyText": json.dumps({"rules": [{"rulePriority": 1, "description": "Remove untagged images after 7 days", "selection": {"tagStatus": "untagged", "countType": "sinceImagePushed", "countUnit": "days", "countNumber": 7}, "action": {"type": "expire"}}]})}}}},
    "Outputs": {"RepositoryUri": {"Value": get("Repository", "RepositoryUri")}}}
Path(__file__).with_name("core-registry.template.json").write_text(json.dumps(registry, indent=2) + "\n")
