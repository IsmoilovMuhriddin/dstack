import json
import sys
import time
import uuid
from functools import cmp_to_key, reduce
from typing import List, Optional, Tuple

import yaml
from botocore.client import BaseClient

from dstack import version
from dstack.backend.aws import jobs, logs
from dstack.core.instance import InstanceType
from dstack.core.job import Job, JobStatus, Requirements
from dstack.core.repo import RepoAddress
from dstack.core.request import RequestHead, RequestStatus
from dstack.core.runners import Gpu, Resources, Runner

CREATE_INSTANCE_RETRY_RATE_SECS = 3


def _serialize_runner(runner: Runner) -> dict:
    resources = {
        "cpus": runner.resources.cpus,
        "memory_mib": runner.resources.memory_mib,
        "gpus": [
            {
                "name": gpu.name,
                "memory_mib": gpu.memory_mib,
            }
            for gpu in (runner.resources.gpus or [])
        ],
        "interruptible": runner.resources.interruptible is True,
        "local": runner.resources.local is True,
    }
    data = {
        "runner_id": runner.runner_id,
        "request_id": runner.request_id,
        "resources": resources,
        "job": runner.job.serialize(),
    }
    return data


def _unserialize_runner(data: dict) -> Runner:
    return Runner(
        data["runner_id"],
        data.get("request_id"),
        Resources(
            data["resources"]["cpus"],
            data["resources"]["memory_mib"],
            [Gpu(g["name"], g["memory_mib"]) for g in data["resources"]["gpus"]],
            data["resources"]["interruptible"] is True,
            data["resources"].get("local") is True,
        ),
        Job.unserialize(data["job"]),
    )


def _get_instance_types(ec2_client: BaseClient) -> List[InstanceType]:
    response = None
    instance_types = []
    while not response or response.get("NextToken"):
        kwargs = {}
        if response and "NextToken" in response:
            kwargs["NextToken"] = response["NextToken"]
        response = ec2_client.describe_instance_types(
            Filters=[
                {
                    "Name": "instance-type",
                    "Values": ["c5.*", "m5.*", "p2.*", "p3.*", "p4d.*", "p4de.*"],
                },
            ],
            **kwargs,
        )
        for instance_type in response["InstanceTypes"]:
            gpus = (
                [
                    [Gpu(gpu["Name"], gpu["MemoryInfo"]["SizeInMiB"])] * gpu["Count"]
                    for gpu in instance_type["GpuInfo"]["Gpus"]
                ]
                if instance_type.get("GpuInfo") and instance_type["GpuInfo"].get("Gpus")
                else []
            )
            instance_types.append(
                InstanceType(
                    instance_type["InstanceType"],
                    Resources(
                        instance_type["VCpuInfo"]["DefaultVCpus"],
                        instance_type["MemoryInfo"]["SizeInMiB"],
                        reduce(list.__add__, gpus) if gpus else [],
                        "spot" in instance_type["SupportedUsageClasses"],
                        False,
                    ),
                )
            )

    def compare(i1, i2):
        r1_gpu_total_memory_mib = sum(map(lambda g: g.memory_mib, i1.resources.gpus or []))
        r2_gpu_total_memory_mib = sum(map(lambda g: g.memory_mib, i2.resources.gpus or []))
        if r1_gpu_total_memory_mib < r2_gpu_total_memory_mib:
            return -1
        elif r1_gpu_total_memory_mib > r2_gpu_total_memory_mib:
            return 1
        if i1.resources.cpus < i2.resources.cpus:
            return -1
        elif i1.resources.cpus > i2.resources.cpus:
            return 1
        if i1.resources.memory_mib < i2.resources.memory_mib:
            return -1
        elif i1.resources.memory_mib > i2.resources.memory_mib:
            return 1
        return 0

    return sorted(instance_types, key=cmp_to_key(compare))


def _matches(resources: Resources, requirements: Optional[Requirements]) -> bool:
    if not requirements:
        return True
    if requirements.cpus and requirements.cpus > resources.cpus:
        return False
    if requirements.memory_mib and requirements.memory_mib > resources.memory_mib:
        return False
    if requirements.gpus:
        gpu_count = requirements.gpus.count or 1
        if gpu_count > len(resources.gpus or []):
            return False
        if requirements.gpus.name and gpu_count > len(
            list(filter(lambda gpu: gpu.name == requirements.gpus.name, resources.gpus or []))
        ):
            return False
        if requirements.gpus.memory_mib and gpu_count > len(
            list(
                filter(
                    lambda gpu: gpu.memory_mib >= requirements.gpus.memory_mib,
                    resources.gpus or [],
                )
            )
        ):
            return False
        if requirements.interruptible and not resources.interruptible:
            return False
    return True


def _get_instance_type(
    ec2_client: BaseClient, requirements: Optional[Requirements]
) -> Optional[InstanceType]:
    instance_types = _get_instance_types(ec2_client)

    instance_type = next(
        (
            instance_type
            for instance_type in instance_types
            if _matches(instance_type.resources, requirements)
        ),
        None,
    )
    return (
        InstanceType(
            instance_type.instance_name,
            Resources(
                instance_type.resources.cpus,
                instance_type.resources.memory_mib,
                instance_type.resources.gpus,
                requirements and requirements.interruptible,
                False,
            ),
        )
        if instance_type
        else None
    )


def _create_runner(
    logs_client: BaseClient, s3_client: BaseClient, bucket_name: str, runner: Runner
):
    key = f"runners/{runner.runner_id}.yaml"
    metadata = {}
    if runner.job.status == JobStatus.STOPPING:
        metadata["status"] = "stopping"
    s3_client.put_object(
        Body=yaml.dump(_serialize_runner(runner)),
        Bucket=bucket_name,
        Key=key,
        Metadata=metadata,
    )
    log_group_name = f"/dstack/runners/{bucket_name}"
    logs.create_log_group_if_not_exists(logs_client, bucket_name, log_group_name)


def _update_runner(s3_client: BaseClient, bucket_name: str, runner: Runner):
    key = f"runners/{runner.runner_id}.yaml"
    metadata = {}
    if runner.job.status == JobStatus.STOPPING:
        metadata["status"] = "stopping"
    s3_client.put_object(
        Body=yaml.dump(_serialize_runner(runner)),
        Bucket=bucket_name,
        Key=key,
        Metadata=metadata,
    )


def get_security_group_id(ec2_client: BaseClient, bucket_name: str, subnet_id: Optional[str]):
    _subnet_postfix = (subnet_id.replace("-", "_") + "_") if subnet_id else ""
    security_group_name = (
        "dstack_security_group_" + _subnet_postfix + bucket_name.replace("-", "_").lower()
    )
    if not version.__is_release__:
        security_group_name += "_stgn"
    response = ec2_client.describe_security_groups(
        Filters=[
            {
                "Name": "group-name",
                "Values": [
                    security_group_name,
                ],
            },
        ],
    )
    if response.get("SecurityGroups"):
        security_group_id = response["SecurityGroups"][0]["GroupId"]
    else:
        group_specification = {}
        if subnet_id:
            subnets_response = ec2_client.describe_subnets(SubnetIds=[subnet_id])
            group_specification["VpcId"] = subnets_response["Subnets"][0]["VpcId"]
        security_group = ec2_client.create_security_group(
            Description="Generated by dstack",
            GroupName=security_group_name,
            TagSpecifications=[
                {
                    "ResourceType": "security-group",
                    "Tags": [
                        {"Key": "owner", "Value": "dstack"},
                        {"Key": "dstack_bucket", "Value": bucket_name},
                    ],
                },
            ],
            **group_specification,
        )
        security_group_id = security_group["GroupId"]
        ip_permissions = [
            {
                "FromPort": 3000,
                "ToPort": 4000,
                "IpProtocol": "tcp",
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }
        ]
        if not version.__is_release__:
            ip_permissions.append(
                {
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpProtocol": "tcp",
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }
            )
        ec2_client.authorize_security_group_ingress(
            GroupId=security_group_id, IpPermissions=ip_permissions
        )
        ec2_client.authorize_security_group_egress(
            GroupId=security_group_id,
            IpPermissions=[
                {
                    "IpProtocol": "-1",
                }
            ],
        )
    return security_group_id


def _serialize_config_yaml(bucket_name: str, region_name: str):
    return f"backend: aws\\n" f"bucket: {bucket_name}\\n" f"region: {region_name}"


def _serialize_runner_yaml(
    runner_id: str,
    resources: Resources,
    runner_port_range_from: int,
    runner_port_range_to: int,
):
    s = (
        f"id: {runner_id}\\n"
        f"expose_ports: {runner_port_range_from}-{runner_port_range_to}\\n"
        f"resources:\\n"
    )
    s += f"  cpus: {resources.cpus}\\n"
    if resources.gpus:
        s += "  gpus:\\n"
        for gpu in resources.gpus:
            s += f"    - name: {gpu.name}\\n      memory_mib: {gpu.memory_mib}\\n"
    if resources.interruptible:
        s += "  interruptible: true\\n"
    if resources.local:
        s += "  local: true\\n"
    return s[:-2]


def _user_data(
    bucket_name,
    region_name,
    runner_id: str,
    resources: Resources,
    port_range_from: int = 3000,
    port_range_to: int = 4000,
) -> str:
    sysctl_port_range_from = int((port_range_to - port_range_from) / 2) + port_range_from
    sysctl_port_range_to = port_range_to - 1
    runner_port_range_from = port_range_from
    runner_port_range_to = sysctl_port_range_from - 1
    user_data = f"""#!/bin/bash
if [ -e "/etc/fuse.conf" ]
then
sudo sed "s/# *user_allow_other/user_allow_other/" /etc/fuse.conf > t
sudo mv t /etc/fuse.conf
else
echo "user_allow_other" | sudo tee -a /etc/fuse.conf > /dev/null
fi
sudo sysctl -w net.ipv4.ip_local_port_range="{sysctl_port_range_from} ${sysctl_port_range_to}"
mkdir -p /root/.dstack/
echo $'{_serialize_config_yaml(bucket_name, region_name)}' > /root/.dstack/config.yaml
echo $'{_serialize_runner_yaml(runner_id, resources, runner_port_range_from, runner_port_range_to)}' > /root/.dstack/runner.yaml
die() {{ status=$1; shift; echo "FATAL: $*"; exit $status; }}
EC2_PUBLIC_HOSTNAME="`wget -q -O - http://169.254.169.254/latest/meta-data/public-hostname || die \"wget public-hostname has failed: $?\"`"
echo "hostname: $EC2_PUBLIC_HOSTNAME" >> /root/.dstack/runner.yaml
HOME=/root nohup dstack-runner start --http-port 4000 &
"""
    return user_data


def role_name(iam_client: BaseClient, bucket_name: str) -> str:
    policy_name = "dstack_policy_" + bucket_name.replace("-", "_").lower()
    _role_name = "dstack_role_" + bucket_name.replace("-", "_").lower()
    try:
        iam_client.get_role(RoleName=_role_name)
    except Exception as e:
        if (
            hasattr(e, "response")
            and e.response.get("Error")
            and e.response["Error"].get("Code") == "NoSuchEntity"
        ):
            response = iam_client.create_policy(
                PolicyName=policy_name,
                Description="Generated by dstack",
                PolicyDocument=json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": "s3:*",
                                "Resource": [
                                    f"arn:aws:s3:::{bucket_name}",
                                    f"arn:aws:s3:::{bucket_name}/*",
                                ],
                            },
                            {
                                "Effect": "Allow",
                                "Action": "logs:*",
                                "Resource": [
                                    f"arn:aws:logs:*:*:log-group:/dstack/jobs/{bucket_name}*:*",
                                    f"arn:aws:logs:*:*:log-group:/dstack/runners/{bucket_name}*:*",
                                ],
                            },
                            {
                                "Effect": "Allow",
                                "Action": "ec2:*",
                                "Resource": "*",
                                "Condition": {
                                    "StringEquals": {
                                        "aws:ResourceTag/dstack_bucket": bucket_name,
                                    }
                                },
                            },
                        ],
                    }
                ),
                Tags=[
                    {"Key": "owner", "Value": "dstack"},
                    {"Key": "dstack_bucket", "Value": bucket_name},
                ],
            )
            policy_arn = response["Policy"]["Arn"]
            iam_client.create_role(
                RoleName=_role_name,
                AssumeRolePolicyDocument=json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Action": "sts:AssumeRole",
                                "Effect": "Allow",
                                "Principal": {"Service": "ec2.amazonaws.com"},
                            }
                        ],
                    }
                ),
                Description="Generated by dstack",
                MaxSessionDuration=3600,
                Tags=[
                    {"Key": "owner", "Value": "dstack"},
                    {"Key": "dstack_bucket", "Value": bucket_name},
                ],
            )
            iam_client.attach_role_policy(RoleName=_role_name, PolicyArn=policy_arn)
        else:
            raise e
    return _role_name


def instance_profile_arn(iam_client: BaseClient, bucket_name: str) -> str:
    _role_name = role_name(iam_client, bucket_name)
    try:
        response = iam_client.get_instance_profile(InstanceProfileName=_role_name)
        return response["InstanceProfile"]["Arn"]
    except Exception as e:
        if (
            hasattr(e, "response")
            and e.response.get("Error")
            and e.response["Error"].get("Code") == "NoSuchEntity"
        ):
            response = iam_client.create_instance_profile(
                InstanceProfileName=_role_name,
                Tags=[
                    {"Key": "owner", "Value": "dstack"},
                    {"Key": "dstack_bucket", "Value": bucket_name},
                ],
            )
            _instance_profile_arn = response["InstanceProfile"]["Arn"]
            iam_client.add_role_to_instance_profile(
                InstanceProfileName=_role_name,
                RoleName=_role_name,
            )
            return _instance_profile_arn
        else:
            raise e


def _get_default_ami_image_version() -> Optional[str]:
    if version.__is_release__:
        return version.__version__
    else:
        return None


def _get_ami_image(
    ec2_client: BaseClient,
    cuda: bool,
    _version: Optional[str] = _get_default_ami_image_version(),
) -> Tuple[str, str]:
    ami_name = "dstack"
    if cuda:
        ami_name = ami_name + "-cuda-11.1"
    if not version.__is_release__:
        ami_name = "[stgn] " + ami_name
    ami_name = ami_name + f"-{_version or '*'}"
    response = ec2_client.describe_images(
        Filters=[
            {"Name": "name", "Values": [ami_name]},
        ],
    )
    images = list(
        filter(
            lambda i: cuda == ("cuda" in i["Name"]) and i["State"] == "available",
            response["Images"],
        )
    )
    if images:
        ami = next(iter(sorted(images, key=lambda i: i["CreationDate"], reverse=True)))
        return ami["ImageId"], ami["Name"]
    else:
        if _version:
            return _get_ami_image(ec2_client, cuda, _version=None)
        else:
            raise Exception(f"Can't find an AMI image prefix='{ami_name}")


def _run_instance(
    ec2_client: BaseClient,
    iam_client: BaseClient,
    bucket_name: str,
    region_name: str,
    subnet_id: Optional[str],
    runner_id: str,
    instance_type: InstanceType,
    local_repo_user_name: Optional[str],
    local_repo_user_email: Optional[str],
    repo_address: RepoAddress,
) -> str:
    launch_specification = {}
    if not version.__is_release__:
        launch_specification["KeyName"] = "stgn_dstack"
    if instance_type.resources.interruptible:
        launch_specification["InstanceMarketOptions"] = {
            "MarketType": "spot",
            "SpotOptions": {
                "SpotInstanceType": "persistent",
                "InstanceInterruptionBehavior": "stop",
            },
        }
    if subnet_id:
        launch_specification["NetworkInterfaces"] = [
            {
                "AssociatePublicIpAddress": True,
                "DeviceIndex": 0,
                "SubnetId": subnet_id,
                "Groups": [get_security_group_id(ec2_client, bucket_name, subnet_id)],
            },
        ]
    else:
        launch_specification["SecurityGroupIds"] = [
            get_security_group_id(ec2_client, bucket_name, subnet_id)
        ]
    tags = [
        {"Key": "owner", "Value": "dstack"},
        {"Key": "dstack_bucket", "Value": bucket_name},
        {"Key": "dstack_repo", "Value": repo_address.path()},
    ]
    if local_repo_user_name:
        tags.append({"Key": "dstack_user_name", "Value": local_repo_user_name})
    if local_repo_user_email:
        tags.append({"Key": "dstack_user_email", "Value": local_repo_user_email})
    response = ec2_client.run_instances(
        BlockDeviceMappings=[
            {
                "DeviceName": "/dev/sda1",
                "Ebs": {
                    "VolumeSize": 100,
                    "VolumeType": "gp2",
                },
            }
        ],
        ImageId=_get_ami_image(ec2_client, len(instance_type.resources.gpus) > 0)[0],
        InstanceType=instance_type.instance_name,
        MinCount=1,
        MaxCount=1,
        IamInstanceProfile={
            "Arn": instance_profile_arn(iam_client, bucket_name),
        },
        UserData=_user_data(bucket_name, region_name, runner_id, instance_type.resources),
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": tags,
            },
        ],
        **launch_specification,
    )
    if instance_type.resources.interruptible:
        request_id = response["Instances"][0]["SpotInstanceRequestId"]
        ec2_client.create_tags(Resources=[request_id], Tags=tags)
    else:
        request_id = response["Instances"][0]["InstanceId"]
    return request_id


def _run_instance_retry(
    ec2_client: BaseClient,
    iam_client: BaseClient,
    bucket_name: str,
    region_name: str,
    subnet_id: Optional[str],
    runner_id: str,
    instance_type: InstanceType,
    local_repo_user_name: Optional[str],
    local_repo_user_email: Optional[str],
    repo_address: RepoAddress,
    attempts: int = 3,
) -> str:
    try:
        return _run_instance(
            ec2_client,
            iam_client,
            bucket_name,
            region_name,
            subnet_id,
            runner_id,
            instance_type,
            local_repo_user_name,
            local_repo_user_email,
            repo_address,
        )
    except Exception as e:
        if (
            hasattr(e, "response")
            and e.response.get("Error")
            and e.response["Error"].get("Code") == "InvalidParameterValue"
        ):
            if attempts > 0:
                time.sleep(CREATE_INSTANCE_RETRY_RATE_SECS)
                return _run_instance_retry(
                    ec2_client,
                    iam_client,
                    bucket_name,
                    region_name,
                    subnet_id,
                    runner_id,
                    instance_type,
                    local_repo_user_name,
                    local_repo_user_email,
                    repo_address,
                    attempts - 1,
                )
            else:
                raise Exception("Failed to retry", e)
        else:
            raise e


def run_job(
    logs_client: BaseClient,
    ec2_client: BaseClient,
    iam_client: BaseClient,
    s3_client: BaseClient,
    bucket_name: str,
    region_name,
    subnet_id: Optional[str],
    job: Job,
):
    if job.status != JobStatus.SUBMITTED:
        raise Exception("Can't create a request for a job which status is not SUBMITTED")

    runner = None
    try:
        job.runner_id = uuid.uuid4().hex
        jobs.update_job(s3_client, bucket_name, job)
        instance_type = _get_instance_type(ec2_client, job.requirements)
        if instance_type is None:
            job.status = JobStatus.FAILED
            jobs.update_job(s3_client, bucket_name, job)
            sys.exit(f"No instance type matching requirements.")

        runner = Runner(job.runner_id, None, instance_type.resources, job)
        _create_runner(logs_client, s3_client, bucket_name, runner)
        runner.request_id = _run_instance_retry(
            ec2_client,
            iam_client,
            bucket_name,
            region_name,
            subnet_id,
            job.runner_id,
            instance_type,
            job.local_repo_user_name,
            job.local_repo_user_email,
            job.repo_address,
        )
        _update_runner(s3_client, bucket_name, runner)
    except Exception as e:
        job.status = JobStatus.FAILED
        job.request_id = runner.request_id if runner else None
        jobs.update_job(s3_client, bucket_name, job)
        raise e


def _delete_runner(s3_client: BaseClient, bucket_name: str, runner: Runner):
    key = f"runners/{runner.runner_id}.yaml"
    s3_client.delete_object(Bucket=bucket_name, Key=key)


def _get_runner(s3_client: BaseClient, bucket_name: str, runner_id: str) -> Optional[Runner]:
    key = f"runners/{runner_id}.yaml"
    try:
        obj = s3_client.get_object(Bucket=bucket_name, Key=key)
        return _unserialize_runner(yaml.load(obj["Body"].read().decode("utf-8"), yaml.FullLoader))
    except Exception as e:
        if (
            hasattr(e, "response")
            and e.response.get("Error")
            and e.response["Error"].get("Code") == "NoSuchKey"
        ):
            return None
        else:
            raise e


def _cancel_spot_request(ec2_client: BaseClient, request_id: str):
    ec2_client.cancel_spot_instance_requests(SpotInstanceRequestIds=[request_id])
    response = ec2_client.describe_instances(
        Filters=[
            {"Name": "spot-instance-request-id", "Values": [request_id]},
        ],
    )
    if response.get("Reservations") and response["Reservations"][0].get("Instances"):
        ec2_client.terminate_instances(
            InstanceIds=[response["Reservations"][0]["Instances"][0]["InstanceId"]]
        )


def _terminate_instance(ec2_client: BaseClient, request_id: str):
    try:
        ec2_client.terminate_instances(InstanceIds=[request_id])
    except Exception as e:
        if (
            hasattr(e, "response")
            and e.response.get("Error")
            and e.response["Error"].get("Code") == "InvalidInstanceID.NotFound"
        ):
            pass
        else:
            raise e


def get_request_head(
    ec2_client: BaseClient,
    s3_client: BaseClient,
    bucket_name: str,
    job: Job,
    runner: Optional[Runner] = None,
) -> RequestHead:
    _local = job.requirements and job.requirements.local
    interruptible = job.requirements and job.requirements.interruptible
    request_id = None
    if job.request_id:
        request_id = job.request_id
    elif runner and runner.request_id:
        request_id = runner.request_id
    elif not runner:
        runner = _get_runner(s3_client, bucket_name, job.runner_id)
        if runner:
            request_id = runner.request_id
    if request_id:
        """
        if _local:
            _running = local.is_running(request_id)
            return RequestHead(job.job_id, RequestStatus.RUNNING if _running else RequestStatus.TERMINATED, None)
        el ivan
        """
        if interruptible:
            try:
                response = ec2_client.describe_spot_instance_requests(
                    SpotInstanceRequestIds=[request_id]
                )
                if response.get("SpotInstanceRequests"):
                    status = response["SpotInstanceRequests"][0]["Status"]
                    if status["Code"] in [
                        "fulfilled",
                        "request-canceled-and-instance-running",
                    ]:
                        request_status = RequestStatus.RUNNING
                    elif status["Code"] in [
                        "not-scheduled-yet",
                        "pending-evaluation",
                        "pending-fulfillment",
                    ]:
                        request_status = RequestStatus.PENDING
                    elif status["Code"] in [
                        "capacity-not-available",
                        "instance-stopped-no-capacity",
                        "instance-terminated-by-price",
                        "instance-stopped-by-price",
                        "instance-terminated-no-capacity",
                        "limit-exceeded",
                        "price-too-low",
                    ]:
                        request_status = RequestStatus.NO_CAPACITY
                    elif status["Code"] in [
                        "instance-terminated-by-user",
                        "instance-stopped-by-user",
                        "canceled-before-fulfillment",
                        "instance-terminated-by-schedule",
                        "instance-terminated-by-service",
                        "spot-instance-terminated-by-user",
                        "marked-for-stop",
                        "marked-for-termination",
                    ]:
                        request_status = RequestStatus.TERMINATED
                    else:
                        raise Exception(
                            f"Unsupported EC2 spot instance request status code: {status['Code']}"
                        )
                    return RequestHead(job.job_id, request_status, status.get("Message"))
                else:
                    return RequestHead(job.job_id, RequestStatus.TERMINATED, None)
            except Exception as e:
                if (
                    hasattr(e, "response")
                    and e.response.get("Error")
                    and e.response["Error"].get("Code") == "InvalidSpotInstanceRequestID.NotFound"
                ):
                    return RequestHead(
                        job.job_id,
                        RequestStatus.TERMINATED,
                        e.response["Error"].get("Message"),
                    )
                else:
                    raise e
        else:
            try:
                response = ec2_client.describe_instances(InstanceIds=[request_id])
                if response.get("Reservations") and response["Reservations"][0].get("Instances"):
                    state = response["Reservations"][0]["Instances"][0]["State"]
                    if state["Name"] in ["running"]:
                        request_status = RequestStatus.RUNNING
                    elif state["Name"] in ["pending"]:
                        request_status = RequestStatus.PENDING
                    elif state["Name"] in [
                        "shutting-down",
                        "terminated",
                        "stopping",
                        "stopped",
                    ]:
                        request_status = RequestStatus.TERMINATED
                    else:
                        raise Exception(f"Unsupported EC2 instance state name: {state['Name']}")
                    return RequestHead(job.job_id, request_status, None)
                else:
                    return RequestHead(job.job_id, RequestStatus.TERMINATED, None)
            except Exception as e:
                if (
                    hasattr(e, "response")
                    and e.response.get("Error")
                    and e.response["Error"].get("Code") == "InvalidInstanceID.NotFound"
                ):
                    return RequestHead(
                        job.job_id,
                        RequestStatus.TERMINATED,
                        e.response["Error"].get("Message"),
                    )
                else:
                    raise e
    else:
        message = (
            "The spot instance request ID is not specified"
            if interruptible
            else "The instance ID is not specified"
        )
        return RequestHead(job.job_id, RequestStatus.TERMINATED, message)


def _stop_runner(ec2_client: BaseClient, s3_client: BaseClient, bucket_name: str, runner: Runner):
    if runner.request_id:
        if runner.resources.local:
            pass
            # local.stop_process(runner.request_id) IVAN
        elif runner.resources.interruptible:
            _cancel_spot_request(ec2_client, runner.request_id)
        else:
            _terminate_instance(ec2_client, runner.request_id)
    _delete_runner(s3_client, bucket_name, runner)


def stop_job(
    ec2_client: BaseClient,
    s3_client: BaseClient,
    bucket_name: str,
    repo_address: RepoAddress,
    job_id: str,
    abort: bool,
):
    job_head = jobs.list_job_head(s3_client, bucket_name, repo_address, job_id)
    job = jobs.get_job(s3_client, bucket_name, repo_address, job_id)
    runner = _get_runner(s3_client, bucket_name, job.runner_id) if job else None
    request_status = (
        get_request_head(ec2_client, s3_client, bucket_name, job, runner).status
        if job
        else RequestStatus.TERMINATED
    )
    if (
        job_head
        and job_head.status.is_unfinished()
        or job
        and job.status.is_unfinished()
        or runner
        and runner.job.status.is_unfinished()
        or request_status != RequestStatus.TERMINATED
    ):
        if abort:
            new_status = JobStatus.ABORTED
        elif (
            not job_head
            or job_head.status in [JobStatus.SUBMITTED, JobStatus.DOWNLOADING]
            or not job
            or job.status in [JobStatus.SUBMITTED, JobStatus.DOWNLOADING]
            or request_status == RequestStatus.TERMINATED
            or not runner
        ):
            new_status = JobStatus.STOPPED
        elif (
            job_head
            and job_head.status != JobStatus.UPLOADING
            or job
            and job.status != JobStatus.UPLOADING
        ):
            new_status = JobStatus.STOPPING
        else:
            new_status = None
        if new_status:
            if runner and runner.job.status.is_unfinished() and runner.job.status != new_status:
                if new_status.is_finished():
                    _stop_runner(ec2_client, s3_client, bucket_name, runner)
                else:
                    runner.job.status = new_status
                    _update_runner(s3_client, bucket_name, runner)
            if (
                job_head
                and job_head.status.is_unfinished()
                and job_head.status != new_status
                or job
                and job.status.is_unfinished()
                and job.status != new_status
            ):
                job.status = new_status
                jobs.update_job(s3_client, bucket_name, job)
