import json
import warnings
from collections import namedtuple
from contextlib import contextmanager

import boto3
import moto
import pytest
from dagster import ExperimentalWarning
from dagster._core.test_utils import in_process_test_workspace, instance_for_test
from dagster._core.types.loadable_target_origin import LoadableTargetOrigin

from . import repo

Secret = namedtuple("Secret", ["name", "arn"])


@pytest.fixture(autouse=True)
def ignore_experimental_warning():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=ExperimentalWarning)
        yield


@pytest.fixture
def cloudwatch_client(region):
    with moto.mock_logs():
        yield boto3.client("logs", region_name=region)


@pytest.fixture
def log_group(cloudwatch_client):
    name = "/dagster-test/test-cloudwatch-logging"
    cloudwatch_client.create_log_group(logGroupName=name)
    return name


@pytest.fixture
def image():
    return "dagster:first"


@pytest.fixture
def other_image():
    return "dagster:second"


@pytest.fixture
def environment():
    return [{"name": "FOO", "value": "bar"}]


@pytest.fixture
def task_definition(ecs, image, environment):
    return ecs.register_task_definition(
        family="dagster",
        containerDefinitions=[
            {
                "name": "dagster",
                "image": image,
                "environment": environment,
                "entryPoint": ["ls"],
                "dependsOn": [
                    {
                        "containerName": "other",
                        "condition": "SUCCESS",
                    },
                ],
            },
            {
                "name": "other",
                "image": image,
                "entryPoint": ["ls"],
            },
        ],
        networkMode="awsvpc",
        memory="512",
        cpu="256",
    )["taskDefinition"]


@pytest.fixture
def assign_public_ip():
    return True


@pytest.fixture
def task(ecs, subnet, security_group, task_definition, assign_public_ip):
    return ecs.run_task(
        taskDefinition=task_definition["family"],
        networkConfiguration={
            "awsvpcConfiguration": {
                "assignPublicIp": "ENABLED" if assign_public_ip else "DISABLED",
                "subnets": [subnet.id],
                "securityGroups": [security_group.id],
            },
        },
    )["tasks"][0]


@pytest.fixture
def stub_aws(ecs, ec2, secrets_manager, cloudwatch_client, monkeypatch):
    def mock_client(*args, **kwargs):
        if "ecs" in args:
            return ecs
        if "secretsmanager" in args:
            return secrets_manager
        if "logs" in args:
            return cloudwatch_client
        else:
            raise Exception("Unexpected args")

    monkeypatch.setattr(boto3, "client", mock_client)
    monkeypatch.setattr(boto3, "resource", lambda *args, **kwargs: ec2)


@pytest.fixture
def stub_ecs_metadata(task, monkeypatch, requests_mock):
    container_uri = "http://metadata_host"
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", container_uri)
    container = task["containers"][0]["name"]
    requests_mock.get(container_uri, json={"Name": container})

    task_uri = container_uri + "/task"
    requests_mock.get(
        task_uri,
        json={
            "Cluster": task["clusterArn"],
            "TaskARN": task["taskArn"],
        },
    )


@pytest.fixture
def instance_cm(stub_aws, stub_ecs_metadata):
    @contextmanager
    def cm(config=None):
        overrides = {
            "run_launcher": {
                "module": "dagster_aws.ecs",
                "class": "EcsRunLauncher",
                "config": {**(config or {})},
            }
        }
        with instance_for_test(overrides) as dagster_instance:
            yield dagster_instance

    return cm


@pytest.fixture
def instance(instance_cm):
    with instance_cm() as dagster_instance:
        yield dagster_instance


@pytest.fixture
def instance_with_log_group(instance_cm, log_group):
    with instance_cm(config={"task_definition": {"log_group": log_group}}) as dagster_instance:
        yield dagster_instance


@pytest.fixture
def instance_with_resources(instance_cm):
    with instance_cm(
        config={
            "run_resources": {
                "cpu": "1024",
                "memory": "2048",
                "ephemeral_storage": 50,
            }
        }
    ) as dagster_instance:
        yield dagster_instance


@pytest.fixture
def instance_dont_use_current_task(instance_cm, subnet, monkeypatch):
    with instance_cm(
        config={
            "use_current_ecs_task_config": False,
            "run_task_kwargs": {
                "cluster": "my_cluster",
                "networkConfiguration": {
                    "awsvpcConfiguration": {
                        "subnets": [subnet.id],
                        "assignPublicIp": "ENABLED",
                    },
                },
            },
        }
    ) as dagster_instance:
        # Not running in an ECS task
        monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", None)
        yield dagster_instance


@pytest.fixture
def instance_fargate_spot(instance_cm):
    with instance_cm(
        config={
            "run_task_kwargs": {
                "capacityProviderStrategy": [
                    {
                        "capacityProvider": "FARGATE_SPOT",
                    }
                ],
            },
        }
    ) as dagster_instance:
        yield dagster_instance


@pytest.fixture
def workspace(instance, image):
    with in_process_test_workspace(
        instance,
        loadable_target_origin=LoadableTargetOrigin(
            python_file=repo.__file__,
            attribute=repo.repository.__name__,
        ),
        container_image=image,
    ) as workspace:
        yield workspace


@pytest.fixture
def other_workspace(instance, other_image):
    with in_process_test_workspace(
        instance,
        loadable_target_origin=LoadableTargetOrigin(
            python_file=repo.__file__,
            attribute=repo.repository.__name__,
        ),
        container_image=other_image,
    ) as workspace:
        yield workspace


@pytest.fixture
def pipeline():
    return repo.pipeline


@pytest.fixture
def external_pipeline(workspace):
    location = workspace.get_code_location(workspace.code_location_names[0])
    return location.get_repository(repo.repository.__name__).get_full_external_job(
        repo.pipeline.__name__
    )


@pytest.fixture
def other_external_pipeline(other_workspace):
    location = other_workspace.get_code_location(other_workspace.code_location_names[0])
    return location.get_repository(repo.repository.__name__).get_full_external_job(
        repo.pipeline.__name__
    )


@pytest.fixture
def run(instance, pipeline, external_pipeline):
    return instance.create_run_for_pipeline(
        pipeline,
        external_pipeline_origin=external_pipeline.get_external_origin(),
        pipeline_code_origin=external_pipeline.get_python_origin(),
    )


@pytest.fixture
def other_run(instance, pipeline, other_external_pipeline):
    return instance.create_run_for_pipeline(
        pipeline,
        external_pipeline_origin=other_external_pipeline.get_external_origin(),
        pipeline_code_origin=other_external_pipeline.get_python_origin(),
    )


@pytest.fixture
def launch_run(pipeline, external_pipeline, workspace):
    def _launch_run(instance):
        run = instance.create_run_for_pipeline(
            pipeline,
            external_pipeline_origin=external_pipeline.get_external_origin(),
            pipeline_code_origin=external_pipeline.get_python_origin(),
        )
        instance.launch_run(run.run_id, workspace)

    return _launch_run


@pytest.fixture
def custom_instance_cm(stub_aws, stub_ecs_metadata):
    @contextmanager
    def cm(config=None):
        overrides = {
            "run_launcher": {
                "module": "dagster_aws.ecs.test_utils",
                "class": "CustomECSRunLauncher",
                "config": {**(config or {})},
            }
        }
        with instance_for_test(overrides) as dagster_instance:
            yield dagster_instance

    return cm


@pytest.fixture
def custom_instance(custom_instance_cm):
    with custom_instance_cm() as dagster_instance:
        yield dagster_instance


@pytest.fixture
def custom_workspace(custom_instance, image):
    with in_process_test_workspace(
        custom_instance,
        loadable_target_origin=LoadableTargetOrigin(
            python_file=repo.__file__,
            attribute=repo.repository.__name__,
        ),
        container_image=image,
    ) as workspace:
        yield workspace


@pytest.fixture
def custom_run(custom_instance, pipeline, external_pipeline):
    return custom_instance.create_run_for_pipeline(
        pipeline,
        external_pipeline_origin=external_pipeline.get_external_origin(),
        pipeline_code_origin=external_pipeline.get_python_origin(),
    )


@pytest.fixture
def tagged_secret(secrets_manager):
    # A secret tagged with "dagster"
    name = "tagged_secret"
    arn = secrets_manager.create_secret(
        Name=name,
        SecretString="hello",
        Tags=[{"Key": "dagster", "Value": "true"}],
    )["ARN"]

    yield Secret(name, arn)


@pytest.fixture
def other_secret(secrets_manager):
    # A secret without a tag
    name = "other_secret"
    arn = secrets_manager.create_secret(
        Name=name,
        SecretString="hello",
    )["ARN"]

    yield Secret(name, arn)


@pytest.fixture
def configured_secret(secrets_manager):
    name = "configured_secret"
    arn = secrets_manager.create_secret(
        Name=name,
        SecretString=json.dumps({"hello": "world"}),
    )["ARN"]

    yield Secret(name, arn)


@pytest.fixture
def other_configured_secret(secrets_manager):
    name = "other_configured_secret"
    arn = secrets_manager.create_secret(
        Name=name,
        SecretString=json.dumps({"goodnight": "moon"}),
    )["ARN"]

    yield Secret(name, arn)


@pytest.fixture
def container_context_config(configured_secret):
    return {
        "env_vars": ["SHARED_KEY=SHARED_VAL"],
        "ecs": {
            "secrets": [
                {
                    "name": "HELLO",
                    "valueFrom": configured_secret.arn + "/hello",
                }
            ],
            "secrets_tags": ["dagster"],
            "env_vars": ["FOO_ENV_VAR=BAR_VALUE"],
            "container_name": "foo",
            "run_resources": {
                "cpu": "4096",
                "memory": "8192",
                "ephemeral_storage": 100,
            },
            "server_resources": {
                "cpu": "1024",
                "memory": "2048",
                "ephemeral_storage": 25,
            },
            "task_role_arn": "fake-task-role",
            "execution_role_arn": "fake-execution-role",
            "runtime_platform": {
                "operatingSystemFamily": "WINDOWS_SERVER_2019_FULL",
            },
        },
    }


@pytest.fixture
def other_container_context_config(other_configured_secret):
    return {
        "env_vars": ["SHARED_OTHER_KEY=SHARED_OTHER_VAL"],
        "ecs": {
            "secrets": [
                {
                    "name": "GOODBYE",
                    "valueFrom": other_configured_secret.arn + "/goodbye",
                }
            ],
            "secrets_tags": ["other_secret_tag"],
            "env_vars": ["OTHER_FOO_ENV_VAR"],
            "container_name": "bar",
            "run_resources": {
                "cpu": "256",
            },
            "server_resources": {
                "cpu": "2048",
                "memory": "4096",
            },
            "task_role_arn": "other-task-role",
            "execution_role_arn": "other-fake-execution-role",
        },
    }


@pytest.fixture
def launch_run_with_container_context(
    pipeline, external_pipeline, workspace, container_context_config
):
    def _launch_run(instance):
        python_origin = external_pipeline.get_python_origin()
        python_origin = python_origin._replace(
            repository_origin=python_origin.repository_origin._replace(
                container_context=container_context_config,
            )
        )

        run = instance.create_run_for_pipeline(
            pipeline,
            external_pipeline_origin=external_pipeline.get_external_origin(),
            pipeline_code_origin=python_origin,
        )
        instance.launch_run(run.run_id, workspace)

    return _launch_run
