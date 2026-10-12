#!/usr/bin/env python3
"""Future user-approved ECS release task. Refuses without an explicit cost gate."""
import argparse
import json
import os
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--stack", required=True)
parser.add_argument("--region", required=True)
parser.add_argument("--bootstrap", action="store_true", help="Run first-admin setup using its dedicated secret-injected task")
parser.add_argument("--registration", action="store_true", help="Register an external app and write its resource credential to its exact SSM parameter")
parser.add_argument("command", nargs=argparse.REMAINDER)
args = parser.parse_args()
if os.environ.get("CORE_ENABLE_AWS_DEPLOYMENT") != "true":
    parser.error("This starts BILLABLE Fargate compute. Set CORE_ENABLE_AWS_DEPLOYMENT=true only after reviewing AWS costs.")
command = args.command[1:] if args.command[:1] == ["--"] else args.command
if not command:
    parser.error("Supply a management command after --")
if args.bootstrap and args.registration:
    parser.error("Choose bootstrap or registration")


def aws(*arguments):
    result = subprocess.run(["aws", *arguments, "--region", args.region, "--output", "json"],
                            check=True, text=True, capture_output=True)
    return json.loads(result.stdout)


stack = aws("cloudformation", "describe-stacks", "--stack-name", args.stack)["Stacks"][0]
outputs = {item["OutputKey"]: item["OutputValue"] for item in stack["Outputs"]}
containers = [{"name": "web", "command": ["bootstrap" if args.bootstrap else "manage", *command]}]
if not args.bootstrap and not args.registration:
    containers.append({"name": "worker", "command": ["idle"]})
definition = "BootstrapTaskDefinition" if args.bootstrap else "RegistrationTaskDefinition" if args.registration else "TaskDefinition"
result = aws("ecs", "run-task", "--cluster", outputs["Cluster"], "--launch-type", "FARGATE",
    "--platform-version", "1.4.0", "--task-definition", outputs[definition],
    "--network-configuration", json.dumps({"awsvpcConfiguration": {"subnets": [outputs["PublicSubnet1"], outputs["PublicSubnet2"]],
        "securityGroups": [outputs["TaskSecurityGroup"]], "assignPublicIp": "ENABLED"}}),
    "--overrides", json.dumps({"containerOverrides": containers}))
if result.get("failures") or not result.get("tasks"):
    raise SystemExit("ECS did not start the release task; inspect the ECS console.")
task = result["tasks"][0]["taskArn"]
print(f"Started release task {task}; waiting for completion. Logs: {outputs['LogGroup']}")
subprocess.run(["aws", "ecs", "wait", "tasks-stopped", "--cluster", outputs["Cluster"], "--tasks", task,
                "--region", args.region], check=True)
finished = aws("ecs", "describe-tasks", "--cluster", outputs["Cluster"], "--tasks", task)["tasks"][0]
web = next(item for item in finished["containers"] if item["name"] == "web")
if web.get("exitCode") != 0:
    raise SystemExit("Release command failed. Inspect the web CloudWatch stream before starting the service.")
print("Release command completed successfully.")
