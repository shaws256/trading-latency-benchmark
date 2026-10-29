#!/usr/bin/env python3
import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from stacks.benchmark_stack import BenchmarkStack, DEFAULT_NATIVE_REF, DEFAULT_NATIVE_REPO

app = cdk.App()

# Read CDK context parameters
num_subscribers = int(app.node.try_get_context("num_subscribers") or 3)
instance_type = app.node.try_get_context("instance_type") or "m7i.large"
multicast_group = app.node.try_get_context("multicast_group") or "239.1.1.1"
multicast_port = int(app.node.try_get_context("multicast_port") or 5001)
s3_bucket_name = app.node.try_get_context("s3_bucket_name")  # None if not provided
placement_strategy = app.node.try_get_context("placement_strategy") or "single-az-cpg"
base_ami = app.node.try_get_context("base_ami")  # None if not provided
# Native sender/receiver: built on each instance from this repo and ref (a branch, tag or
# full commit SHA). It must be reachable from the instances, so a local branch is not enough.
native_repo = app.node.try_get_context("native_repo") or DEFAULT_NATIVE_REPO
native_ref = app.node.try_get_context("native_ref") or DEFAULT_NATIVE_REF
build_native = str(app.node.try_get_context("build_native") or "true").lower() != "false"

stack = BenchmarkStack(
    app,
    "TgwMulticastBenchmark",
    num_subscribers=num_subscribers,
    instance_type=instance_type,
    multicast_group=multicast_group,
    multicast_port=multicast_port,
    s3_bucket_name=s3_bucket_name,
    placement_strategy=placement_strategy,
    base_ami=base_ami,
    native_repo=native_repo,
    native_ref=native_ref,
    build_native=build_native,
)

cdk.Aspects.of(app).add(AwsSolutionsChecks())

app.synth()
