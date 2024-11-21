# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------


import json
import re
from os import environ
from pathlib import Path
from random import randint
from typing import Dict, FrozenSet, List, Optional
from unittest.mock import Mock

import pytest
import requests
import responses

from azext_edge.edge.common import INIT_NO_PREFLIGHT_ENV_KEY
from azext_edge.edge.providers.base import DEFAULT_NAMESPACE
from azext_edge.edge.providers.orchestration.common import (
    ARM_ENDPOINT,
    KubernetesDistroType,
    MqMemoryProfile,
    MqServiceType,
    OPS_EXTENSION_DEPS,
    EXTENSION_TYPE_PLATFORM,
)
from azext_edge.edge.providers.orchestration.rp_namespace import RP_NAMESPACE_SET
from azext_edge.edge.providers.orchestration.work import (
    CONNECTIVITY_STATUS_CONNECTED,
    PROVISIONING_STATE_SUCCESS,
    WorkCategoryKey,
    WorkManager,
    WorkStepKey,
)

from ...generators import generate_random_string, get_zeroed_subscription
from .test_template_unit import EXPECTED_EXTENSION_RESOURCE_KEYS

ZEROED_SUBSCRIPTION = get_zeroed_subscription()


path_pattern_base = r"^/subscriptions/[0-9a-fA-F-]+/resourcegroups/[a-zA-Z0-9]+"
STANDARD_HEADERS = {"content-type": "application/json"}


def build_target_scenario(
    cluster_name: str, resource_group_name: str, extension_config_settings: Optional[dict] = None, **kwargs
) -> dict:
    schema_registry_name: str = generate_random_string()
    default_extensions_config = {
        ext_type: {
            "id": generate_random_string(),
            "properties": {
                "extensionType": ext_type,
                "provisioningState": PROVISIONING_STATE_SUCCESS,
                "configurationSettings": {},
            },
        }
        for ext_type in OPS_EXTENSION_DEPS
    }
    default_extensions_config[EXTENSION_TYPE_PLATFORM]["properties"]["configurationSettings"][
        "installCertManager"
    ] = "true"

    if extension_config_settings:
        default_extensions_config.update(extension_config_settings)
    extensions_list = list(default_extensions_config.values())

    payload = {
        "instance": {"name": generate_random_string(), "description": None, "namespace": None},
        "customLocationName": None,
        "enableRsyncRules": None,
        "resourceGroup": resource_group_name,
        "cluster": {
            "name": cluster_name,
            "location": generate_random_string(),
            "properties": {
                "provisioningState": PROVISIONING_STATE_SUCCESS,
                "connectivityStatus": CONNECTIVITY_STATUS_CONNECTED,
                "totalNodeCount": 1,
            },
            "extensions": {"value": extensions_list},
        },
        "providerNamespace": {
            "value": [{"namespace": namespace, "registrationState": "Registered"} for namespace in RP_NAMESPACE_SET]
        },
        "whatIf": {"status": PROVISIONING_STATE_SUCCESS},
        "trust": {"userTrust": None, "settings": None},
        "enableFaultTolerance": None,
        "ensureLatest": None,
        "schemaRegistry": {
            "id": (
                f"/subscriptions/{ZEROED_SUBSCRIPTION}/resourceGroups/{resource_group_name}"
                f"/providers/microsoft.deviceregistry/schemaRegistries/{schema_registry_name}"
            ),
            "name": schema_registry_name,
        },
        "dataflow": {"profileInstances": 1},
        "noProgress": True,
    }
    if "cluster_properties" in kwargs:
        payload["cluster"]["properties"].update(kwargs["cluster_properties"])
        kwargs.pop("cluster_properties")

    payload.update(**kwargs)
    return payload


@pytest.mark.parametrize(
    "target_scenario",
    [
        build_target_scenario(cluster_name=generate_random_string(), resource_group_name=generate_random_string()),
        build_target_scenario(
            cluster_name=generate_random_string(),
            resource_group_name=generate_random_string(),
            cluster_properties={"totalNodeCount": 3},
            enableFaultTolerance=True,
            trust={"userTrust": True},
        ),
    ],
)
def test_iot_ops_init(
    mocked_cmd: Mock,
    mocked_responses: responses,
    mocked_sleep: Mock,
    target_scenario: dict,
):
    call_map: Dict[str, list] = {
        "connectivityCheck": [],
        "getCluster": [],
        "registerProviders": [],
        "whatIf": [],
        "deploy": [],
    }

    def init_service_generator(request: requests.PreparedRequest) -> tuple:
        method: str = request.method
        url: str = request.url
        params: dict = request.params
        path_url: str = request.path_url
        path_url = path_url.split("?")[0]
        body_str: str = request.body

        # return (status_code, headers, body)
        if method == responses.HEAD and url == ARM_ENDPOINT:
            call_map["connectivityCheck"].append(request)
            return (200, {}, None)

        if method == responses.GET:
            if path_url == (
                f"/subscriptions/{ZEROED_SUBSCRIPTION}/resourcegroups/{target_scenario['resourceGroup']}"
                f"/providers/Microsoft.Kubernetes/connectedClusters/{target_scenario['cluster']['name']}"
            ):
                call_map["getCluster"].append(request)
                return (200, STANDARD_HEADERS, json.dumps(target_scenario["cluster"]))

            if path_url == f"/subscriptions/{ZEROED_SUBSCRIPTION}/providers":
                assert params["api-version"] == "2024-03-01"
                call_map["registerProviders"].append(request)
                return (200, STANDARD_HEADERS, json.dumps(target_scenario["providerNamespace"]))

        url_deployment_seg = r"/providers/Microsoft\.Resources/deployments/aziotops\.enablement\.[a-zA-Z0-9\.-]+"
        if method == responses.POST:
            if re.match(
                path_pattern_base + url_deployment_seg + r"/whatIf$",
                path_url,
            ):
                assert params["api-version"] == "2024-03-01"
                assert f"/resourcegroups/{target_scenario['resourceGroup']}/" in path_url
                assert_init_deployment_body(body_str=body_str, target_scenario=target_scenario)
                call_map["whatIf"].append(request)
                return (200, STANDARD_HEADERS, json.dumps(target_scenario["whatIf"]))

        if method == responses.PUT:
            if re.match(
                path_pattern_base + url_deployment_seg,
                path_url,
            ):
                assert params["api-version"] == "2024-03-01"
                assert f"/resourcegroups/{target_scenario['resourceGroup']}/" in path_url
                assert_init_deployment_body(body_str=body_str, target_scenario=target_scenario)
                call_map["deploy"].append(request)
                return (200, STANDARD_HEADERS, json.dumps({}))

        raise RuntimeError(f"No match for {method} {url}.")

    for method in [
        responses.GET,
        responses.HEAD,
        responses.POST,
        responses.PUT,
    ]:
        mocked_responses.add_callback(method=method, url=re.compile(r".*"), callback=init_service_generator)

    from azext_edge.edge.commands_edge import init

    init_call_kwargs = {
        "cmd": mocked_cmd,
        "cluster_name": target_scenario["cluster"]["name"],
        "resource_group_name": target_scenario["resourceGroup"],
        "enable_fault_tolerance": target_scenario["enableFaultTolerance"],
        "user_trust": target_scenario["trust"]["userTrust"],
        "no_progress": target_scenario["noProgress"],
        "ensure_latest": target_scenario["ensureLatest"],
    }

    init_result = init(**init_call_kwargs)

    for call in call_map:
        assert len(call_map[call]) == 1

    if target_scenario["noProgress"]:
        assert init_result is not None  # @digimaun


def assert_init_deployment_body(body_str: str, target_scenario: dict):
    assert body_str
    body = json.loads(body_str)
    parameters = body["properties"]["parameters"]
    assert parameters["clusterName"]["value"] == target_scenario["cluster"]["name"]

    expected_trust_config = {"source": "SelfSigned"}
    if target_scenario["trust"]["userTrust"]:
        expected_trust_config = {"source": "CustomerManaged"}
    assert parameters["trustConfig"]["value"] == expected_trust_config

    expected_advanced_config = {}
    if target_scenario["enableFaultTolerance"]:
        expected_advanced_config["edgeStorageAccelerator"] = {"faultToleranceEnabled": True}
    assert parameters["advancedConfig"]["value"] == expected_advanced_config

    mode = body["properties"]["mode"]
    assert mode == "Incremental"

    template = body["properties"]["template"]
    for key in EXPECTED_EXTENSION_RESOURCE_KEYS:
        assert template["resources"][key]


@pytest.mark.parametrize(
    "target_scenario",
    [
        build_target_scenario(cluster_name=generate_random_string(), resource_group_name=generate_random_string()),
    ],
)
def test_iot_ops_create(
    mocked_cmd: Mock,
    mocked_responses: responses,
    mocked_sleep: Mock,
    target_scenario: dict,
):
    call_map: Dict[str, list] = {
        "connectivityCheck": [],
        "getCluster": [],
        "registerProviders": [],
        "getSchemaRegistry": [],
        "getClusterExtensions": [],
        "whatIf": [],
        "deploy": [],
    }

    def instance_service_generator(request: requests.PreparedRequest) -> tuple:
        method: str = request.method
        url: str = request.url
        params: dict = request.params
        path_url: str = request.path_url
        path_url = path_url.split("?")[0]
        body_str: str = request.body

        # return (status_code, headers, body)
        if method == responses.HEAD and url == ARM_ENDPOINT:
            call_map["connectivityCheck"].append(request)
            return (200, {}, None)

        if method == responses.GET:
            if path_url == (
                f"/subscriptions/{ZEROED_SUBSCRIPTION}/resourcegroups/{target_scenario['resourceGroup']}"
                f"/providers/Microsoft.Kubernetes/connectedClusters/{target_scenario['cluster']['name']}"
            ):
                call_map["getCluster"].append(request)
                return (200, STANDARD_HEADERS, json.dumps(target_scenario["cluster"]))

            if path_url == f"/subscriptions/{ZEROED_SUBSCRIPTION}/providers":
                assert params["api-version"] == "2024-03-01"
                call_map["registerProviders"].append(request)
                return (200, STANDARD_HEADERS, json.dumps(target_scenario["providerNamespace"]))

            if path_url == (
                f"/subscriptions/{ZEROED_SUBSCRIPTION}/resourceGroups/{target_scenario['resourceGroup']}"
                f"/providers/microsoft.deviceregistry/schemaRegistries/{target_scenario['schemaRegistry']['name']}"
            ):
                assert params["api-version"] == "2024-09-01-preview"
                call_map["getSchemaRegistry"].append(request)
                return (200, STANDARD_HEADERS, json.dumps(target_scenario["schemaRegistry"]))

            if path_url == (
                f"/subscriptions/{ZEROED_SUBSCRIPTION}/resourceGroups/{target_scenario['resourceGroup']}"
                f"/providers/Microsoft.Kubernetes/connectedClusters/{target_scenario['cluster']['name']}"
                f"/providers/Microsoft.KubernetesConfiguration/extensions"
            ):
                assert params["api-version"] == "2023-05-01"
                call_map["getClusterExtensions"].append(request)
                return (200, STANDARD_HEADERS, json.dumps(target_scenario["cluster"]["extensions"]))

        url_deployment_seg = r"/providers/Microsoft\.Resources/deployments/aziotops\.instance\.[a-zA-Z0-9\.-]+"
        if method == responses.POST:
            if re.match(
                path_pattern_base + url_deployment_seg + r"/whatIf$",
                path_url,
            ):
                assert params["api-version"] == "2024-03-01"
                assert f"/resourcegroups/{target_scenario['resourceGroup']}/" in path_url
                assert_instance_deployment_body(body_str=body_str, target_scenario=target_scenario)
                call_map["whatIf"].append(request)
                return (200, STANDARD_HEADERS, json.dumps(target_scenario["whatIf"]))

        if method == responses.PUT:
            if re.match(
                path_pattern_base + url_deployment_seg,
                path_url,
            ):
                assert params["api-version"] == "2024-03-01"
                assert f"/resourcegroups/{target_scenario['resourceGroup']}/" in path_url
                assert_instance_deployment_body(body_str=body_str, target_scenario=target_scenario)
                call_map["deploy"].append(request)
                return (200, STANDARD_HEADERS, json.dumps({}))

        raise RuntimeError(f"No match for {method} {url}.")

    for method in [
        responses.GET,
        responses.HEAD,
        responses.POST,
        responses.PUT,
    ]:
        mocked_responses.add_callback(method=method, url=re.compile(r".*"), callback=instance_service_generator)

    from azext_edge.edge.commands_edge import create_instance

    create_call_kwargs = {
        "cmd": mocked_cmd,
        "cluster_name": target_scenario["cluster"]["name"],
        "resource_group_name": target_scenario["resourceGroup"],
        "instance_name": target_scenario["instance"]["name"],
        "schema_registry_resource_id": target_scenario["schemaRegistry"]["id"],
        "location": target_scenario["cluster"]["location"],
        "custom_location_name": target_scenario["customLocationName"],
        "enable_rsync_rules": target_scenario["enableRsyncRules"],
        "instance_description": target_scenario["instance"]["description"],
        "dataflow_profile_instances": target_scenario["dataflow"]["profileInstances"],
        "trust_settings": target_scenario["trust"]["settings"],
        # "container_runtime_socket": None,
        # "kubernetes_distro": None,
        # "ops_config": None,
        # "ops_version": None,
        # "ops_train": None,
        # "custom_broker_config_file": None,
        # "broker_memory_profile": str = MqMemoryProfile.medium.value,
        # "broker_service_type": str = MqServiceType.cluster_ip.value,
        # "broker_backend_partitions": int = 2,
        # "broker_backend_workers": int = 2,
        # "broker_backend_redundancy_factor": int = 2,
        # "broker_frontend_workers": int = 2,
        # "broker_frontend_replicas": int = 2,
        # "add_insecure_listener": Optional[bool] = None,
        # "tags": Optional[dict] = None,
        "no_progress": target_scenario["noProgress"],
    }
    if target_scenario["instance"]["namespace"]:
        create_call_kwargs["cluster_namespace"] = target_scenario["instance"]["namespace"]

    create_result = create_instance(**create_call_kwargs)

    for call in call_map:
        assert len(call_map[call]) == 1

    if target_scenario["noProgress"]:
        assert create_result is not None  # @digimaun


def assert_instance_deployment_body(body_str: str, target_scenario: dict):
    # TODO @digimaun
    assert body_str
    body = json.loads(body_str)
    parameters = body["properties"]["parameters"]
    assert parameters["clusterName"]["value"] == target_scenario["cluster"]["name"]

    expected_trust_config = {"source": "SelfSigned"}
    if target_scenario["trust"]["userTrust"]:
        expected_trust_config = {"source": "CustomerManaged"}
    assert parameters["trustConfig"]["value"] == expected_trust_config

    expected_advanced_config = {}
    if target_scenario["enableFaultTolerance"]:
        expected_advanced_config["edgeStorageAccelerator"] = {"faultToleranceEnabled": True}
    assert parameters["advancedConfig"]["value"] == expected_advanced_config

    mode = body["properties"]["mode"]
    assert mode == "Incremental"

    template = body["properties"]["template"]
    for key in EXPECTED_EXTENSION_RESOURCE_KEYS:
        assert template["resources"][key]


# def _assert_displays_for(work_category_set: FrozenSet[WorkCategoryKey], display_spys: Dict[str, Mock]):
#     render_display = display_spys["render_display"]
#     render_display_call_kwargs = [m.kwargs for m in render_display.mock_calls]

#     index = 0
#     if WorkCategoryKey.PRE_FLIGHT in work_category_set:
#         assert render_display_call_kwargs[index] == {
#             "category": WorkCategoryKey.PRE_FLIGHT,
#             "active_step": WorkStepKey.REG_RP,
#         }
#         index += 1
#         assert render_display_call_kwargs[index] == {"active_step": WorkStepKey.ENUMERATE_PRE_FLIGHT}
#         index += 1
#         assert render_display_call_kwargs[index] == {"active_step": WorkStepKey.WHAT_IF}
#         index += 1
#         assert render_display_call_kwargs[index] == {"active_step": -1}
#         index += 1

#     if WorkCategoryKey.DEPLOY_AIO in work_category_set:
#         assert render_display_call_kwargs[index] == {"category": WorkCategoryKey.DEPLOY_AIO}
#         index += 1
#         # DEPLOY_AIO gets rendered twice to dynamically expose deployment link
#         assert render_display_call_kwargs[index] == {"category": WorkCategoryKey.DEPLOY_AIO}
