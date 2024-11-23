# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------


import json
import re
from enum import Enum
from pathlib import Path
from random import randint
from typing import Dict, FrozenSet, List, Optional, Tuple, NamedTuple
from unittest.mock import Mock

import pytest
import requests
import responses

from azext_edge.edge.providers.base import DEFAULT_NAMESPACE
from azext_edge.edge.providers.orchestration.common import (
    ARM_ENDPOINT,
    EXTENSION_TYPE_PLATFORM,
    EXTENSION_TYPE_SSC,
    OPS_EXTENSION_DEPS,
    EXTENSION_TYPE_OPS,
    KubernetesDistroType,
    MqMemoryProfile,
    MqServiceType,
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
from .test_template_unit import EXPECTED_EXTENSION_RESOURCE_KEYS, EXPECTED_INSTANCE_RESOURCE_KEYS

ZEROED_SUBSCRIPTION = get_zeroed_subscription()


path_pattern_base = r"^/subscriptions/[0-9a-fA-F-]+/resourcegroups/[a-zA-Z0-9]+"
STANDARD_HEADERS = {"content-type": "application/json"}


class CallKey(Enum):
    CONNECT_RESOURCE_MANAGER = "connectResourceManager"
    GET_CLUSTER = "getCluster"
    GET_RESOURCE_PROVIDERS = "getResourceProviders"
    DEPLOY_INIT_WHATIF = "deployInitWhatIf"
    DEPLOY_INIT = "deployInit"
    GET_SCHEMA_REGISTRY = "getSchemaRegistry"
    GET_CLUSTER_EXTENSIONS = "getClusterExtensions"
    GET_SCHEMA_REGISTRY_RA = "getSchemaRegistryRoleAssignments"
    PUT_SCHEMA_REGISTRY_RA = "putSchemaRegistryRoleAssignment"
    DEPLOY_CREATE_WHATIF = "deployCreateWhatIf"
    DEPLOY_CREATE = "deployCreate"


class RequestKPIs(NamedTuple):
    method: str
    url: str
    params: dict
    path_url: str
    body_str: str


class ServiceGenerator:
    def __init__(self, scenario: dict, mocked_responses: responses):
        self.scenario = scenario
        self.mocked_responses = mocked_responses
        self.call_map: Dict[CallKey, List[RequestKPIs]] = {}
        self._bootstrap()

    def _bootstrap(self):
        for method in [
            responses.GET,
            responses.HEAD,
            responses.POST,
            responses.PUT,
        ]:
            self.mocked_responses.add_callback(method=method, url=re.compile(r".*"), callback=self._handle_requests)
        self._reset_call_map()

    def _reset_call_map(self):
        self.call_map = {}
        for key in CallKey:
            self.call_map[key] = []

    def _handle_requests(self, request: requests.PreparedRequest) -> Optional[tuple]:
        request_kpis = get_request_kpis(request)
        for handler in [self._handle_common, self._handle_init, self._handle_create]:
            handler_response = handler(request_kpis)
            if handler_response:
                return handler_response

        raise RuntimeError(f"No match for {request_kpis.method} {request_kpis.url}.")

    def _handle_common(self, request_kpis: RequestKPIs) -> Optional[tuple]:
        # return (status_code, headers, body)
        if request_kpis.method == responses.HEAD:
            if request_kpis.url == ARM_ENDPOINT:
                self.call_map[CallKey.CONNECT_RESOURCE_MANAGER].append(request_kpis)
                return (200, {}, None)

        if request_kpis.method == responses.GET:
            if request_kpis.path_url == f"/subscriptions/{ZEROED_SUBSCRIPTION}/providers":
                assert request_kpis.params["api-version"] == "2024-03-01"
                self.call_map[CallKey.GET_RESOURCE_PROVIDERS].append(request_kpis)
                return (200, STANDARD_HEADERS, json.dumps(self.scenario["providerNamespace"]))

            if request_kpis.path_url == (
                f"/subscriptions/{ZEROED_SUBSCRIPTION}/resourcegroups/{self.scenario['resourceGroup']}"
                f"/providers/Microsoft.Kubernetes/connectedClusters/{self.scenario['cluster']['name']}"
            ):
                assert request_kpis.params["api-version"] == "2024-07-15-preview"
                self.call_map[CallKey.GET_CLUSTER].append(request_kpis)
                return (200, STANDARD_HEADERS, json.dumps(self.scenario["cluster"]))

    def _handle_init(self, request_kpis: RequestKPIs):
        url_deployment_seg = r"/providers/Microsoft\.Resources/deployments/aziotops\.enablement\.[a-zA-Z0-9\.-]+"
        if request_kpis.method == responses.POST:
            if re.match(
                path_pattern_base + url_deployment_seg + r"/whatIf$",
                request_kpis.path_url,
            ):
                assert request_kpis.params["api-version"] == "2024-03-01"
                assert f"/resourcegroups/{self.scenario['resourceGroup']}/" in request_kpis.path_url
                assert_init_deployment_body(body_str=request_kpis.body_str, target_scenario=self.scenario)
                self.call_map[CallKey.DEPLOY_INIT_WHATIF].append(request_kpis)
                return (200, STANDARD_HEADERS, json.dumps(self.scenario["whatIf"]))

        if request_kpis.method == responses.PUT:
            if re.match(
                path_pattern_base + url_deployment_seg,
                request_kpis.path_url,
            ):
                assert request_kpis.params["api-version"] == "2024-03-01"
                assert f"/resourcegroups/{self.scenario['resourceGroup']}/" in request_kpis.path_url
                assert_init_deployment_body(body_str=request_kpis.body_str, target_scenario=self.scenario)
                self.call_map[CallKey.DEPLOY_INIT].append(request_kpis)
                return (200, STANDARD_HEADERS, json.dumps({}))

    def _handle_create(self, request_kpis: RequestKPIs):
        if request_kpis.method == responses.GET:
            if request_kpis.path_url == (
                f"/subscriptions/{ZEROED_SUBSCRIPTION}/resourceGroups/{self.scenario['resourceGroup']}"
                f"/providers/microsoft.deviceregistry/schemaRegistries/{self.scenario['schemaRegistry']['name']}"
            ):
                assert request_kpis.params["api-version"] == "2024-09-01-preview"
                self.call_map[CallKey.GET_SCHEMA_REGISTRY].append(request_kpis)
                return (200, STANDARD_HEADERS, json.dumps(self.scenario["schemaRegistry"]))

            if request_kpis.path_url == (
                f"/subscriptions/{ZEROED_SUBSCRIPTION}/resourceGroups/{self.scenario['resourceGroup']}"
                f"/providers/microsoft.deviceregistry/schemaRegistries/{self.scenario['schemaRegistry']['name']}"
                f"/providers/Microsoft.Authorization/roleAssignments"
            ):
                ops_ext_identity = self._get_extension_identity()
                assert request_kpis.params["api-version"] == "2022-04-01"
                assert request_kpis.params["$filter"] == f"principalId eq '{ops_ext_identity['principalId']}'"
                self.call_map[CallKey.GET_SCHEMA_REGISTRY_RA].append(request_kpis)
                return (200, STANDARD_HEADERS, json.dumps(self.scenario["schemaRegistry"]["roleAssignments"]))

            if request_kpis.path_url == (
                f"/subscriptions/{ZEROED_SUBSCRIPTION}/resourceGroups/{self.scenario['resourceGroup']}"
                f"/providers/Microsoft.Kubernetes/connectedClusters/{self.scenario['cluster']['name']}"
                f"/providers/Microsoft.KubernetesConfiguration/extensions"
            ):
                assert request_kpis.params["api-version"] == "2023-05-01"
                self.call_map[CallKey.GET_CLUSTER_EXTENSIONS].append(request_kpis)
                return (200, STANDARD_HEADERS, json.dumps(self.scenario["cluster"]["extensions"]))

        url_deployment_seg = r"/providers/Microsoft\.Resources/deployments/aziotops\.instance\.[a-zA-Z0-9\.-]+"
        if request_kpis.method == responses.POST:
            if re.match(
                path_pattern_base + url_deployment_seg + r"/whatIf$",
                request_kpis.path_url,
            ):
                assert request_kpis.params["api-version"] == "2024-03-01"
                assert f"/resourcegroups/{self.scenario['resourceGroup']}/" in request_kpis.path_url
                assert_instance_deployment_body(body_str=request_kpis.body_str, target_scenario=self.scenario)
                self.call_map[CallKey.DEPLOY_CREATE_WHATIF].append(request_kpis)
                return (200, STANDARD_HEADERS, json.dumps(self.scenario["whatIf"]))

        if request_kpis.method == responses.PUT:
            if re.match(
                path_pattern_base + url_deployment_seg,
                request_kpis.path_url,
            ):
                assert request_kpis.params["api-version"] == "2024-03-01"
                assert f"/resourcegroups/{self.scenario['resourceGroup']}/" in request_kpis.path_url
                assert_instance_deployment_body(body_str=request_kpis.body_str, target_scenario=self.scenario)
                self.call_map[CallKey.DEPLOY_CREATE].append(request_kpis)
                return (200, STANDARD_HEADERS, json.dumps({}))

            if request_kpis.path_url.startswith(
                f"/subscriptions/{ZEROED_SUBSCRIPTION}/resourceGroups/{self.scenario['resourceGroup']}"
                f"/providers/microsoft.deviceregistry/schemaRegistries/{self.scenario['schemaRegistry']['name']}"
                f"/providers/Microsoft.Authorization/roleAssignments/"
            ):
                ops_ext_identity = self._get_extension_identity()
                assert request_kpis.params["api-version"] == "2022-04-01"
                self.call_map[CallKey.PUT_SCHEMA_REGISTRY_RA].append(request_kpis)
                return (200, STANDARD_HEADERS, json.dumps({}))

    def _get_extension_identity(self, extension_type: str = EXTENSION_TYPE_OPS) -> Optional[dict]:
        for ext in self.scenario["cluster"]["extensions"]["value"]:
            if ext["properties"]["extensionType"] == extension_type:
                return ext.get("identity")


def get_request_kpis(request: requests.PreparedRequest):
    return RequestKPIs(
        method=request.method,
        url=request.url,
        params=request.params,
        path_url=request.path_url.split("?")[0],
        body_str=request.body,
    )


def build_target_scenario(
    cluster_name: str,
    resource_group_name: str,
    extension_config_settings: Optional[dict] = None,
    omit_ops_ext: bool = False,
    **kwargs,
) -> dict:
    schema_registry_name: str = generate_random_string()

    expected_extension_types = list(OPS_EXTENSION_DEPS)
    if not omit_ops_ext:
        expected_extension_types.append(EXTENSION_TYPE_OPS)
    default_extensions_config = {
        ext_type: {
            "id": generate_random_string(),
            "properties": {
                "extensionType": ext_type,
                "provisioningState": PROVISIONING_STATE_SUCCESS,
                "configurationSettings": {},
            },
        }
        for ext_type in expected_extension_types
    }
    default_extensions_config[EXTENSION_TYPE_PLATFORM]["properties"]["configurationSettings"][
        "installCertManager"
    ] = "true"
    if not omit_ops_ext:
        default_extensions_config[EXTENSION_TYPE_OPS]["identity"] = {"principalId": generate_random_string()}

    if extension_config_settings:
        default_extensions_config.update(extension_config_settings)
    extensions_list = list(default_extensions_config.values())

    payload = {
        "instance": {"name": generate_random_string(), "description": None, "namespace": None},
        "customLocationName": None,
        "enableRsyncRules": None,
        "location": None,
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
            "roleAssignments": {"value": []},
        },
        "dataflow": {"profileInstances": None},
        "kubernetesDistro": None,
        "noProgress": True,
    }
    if "cluster_properties" in kwargs:
        payload["cluster"]["properties"].update(kwargs["cluster_properties"])
        kwargs.pop("cluster_properties")

    payload.update(**kwargs)
    return payload


def assert_call_map(expected_call_count_map: dict, call_map: dict):
    for key in call_map:
        expected_count = 0
        if key in expected_call_count_map:
            expected_count = expected_call_count_map[key]
        assert len(call_map[key]) == expected_count, f"{key} has unexpected call(s)."


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
    servgen = ServiceGenerator(scenario=target_scenario, mocked_responses=mocked_responses)
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
    expected_call_count_map = {
        CallKey.CONNECT_RESOURCE_MANAGER: 1,
        CallKey.GET_RESOURCE_PROVIDERS: 1,  # TODO: Register RPs
        CallKey.GET_CLUSTER: 1,
        CallKey.DEPLOY_INIT_WHATIF: 1,
        CallKey.DEPLOY_INIT: 1,
    }
    assert_call_map(expected_call_count_map, servgen.call_map)

    if target_scenario["noProgress"]:
        assert init_result is not None  # TODO - @digimaun


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
    servgen = ServiceGenerator(scenario=target_scenario, mocked_responses=mocked_responses)
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
    if target_scenario["instance"]["description"]:
        create_call_kwargs["instance_description"] = target_scenario["instance"]["description"]
    if target_scenario["dataflow"]["profileInstances"]:
        create_call_kwargs["dataflow_profile_instances"] = target_scenario["dataflow"]["profileInstances"]

    create_result = create_instance(**create_call_kwargs)

    expected_call_count_map = {
        CallKey.CONNECT_RESOURCE_MANAGER: 1,
        CallKey.GET_RESOURCE_PROVIDERS: 1,  # TODO: Register RPs
        CallKey.GET_CLUSTER: 1,
        CallKey.GET_SCHEMA_REGISTRY: 1,
        CallKey.GET_CLUSTER_EXTENSIONS: 2,
        CallKey.GET_SCHEMA_REGISTRY_RA: 1,
        CallKey.PUT_SCHEMA_REGISTRY_RA: 1,
        CallKey.DEPLOY_CREATE_WHATIF: 1,
        CallKey.DEPLOY_CREATE: 1,
    }
    assert_call_map(expected_call_count_map, servgen.call_map)

    if target_scenario["noProgress"]:
        assert create_result is not None  # @digimaun


def assert_instance_deployment_body(body_str: str, target_scenario: dict):
    assert body_str
    body = json.loads(body_str)
    parameters = body["properties"]["parameters"]
    assert parameters["clusterName"]["value"] == target_scenario["cluster"]["name"]

    assert parameters["clusterNamespace"]["value"] == target_scenario["instance"]["namespace"] or DEFAULT_NAMESPACE
    assert (
        parameters["clusterLocation"]["value"] == target_scenario["location"] or target_scenario["cluster"]["location"]
    )

    cl_extension_ids = set(
        [
            ext["id"]
            for ext in target_scenario["cluster"]["extensions"]["value"]
            if ext["properties"]["extensionType"] in [EXTENSION_TYPE_PLATFORM, EXTENSION_TYPE_SSC]
        ]
    )
    assert set(parameters["clExtentionIds"]["value"]) == cl_extension_ids
    assert parameters["schemaRegistryId"]["value"] == target_scenario["schemaRegistry"]["id"]
    assert parameters["deployResourceSyncRules"]["value"] == bool(target_scenario["enableRsyncRules"])

    # Optionals
    assert parameters["defaultDataflowinstanceCount"] == target_scenario["dataflow"]["profileInstances"] or 1
    assert (
        parameters["kubernetesDistro"]["value"] == target_scenario["kubernetesDistro"]
        or KubernetesDistroType.k8s.value
    )
    # TODO - @digimaun
    assert parameters["brokerConfig"] == {
        "value": {
            "frontendReplicas": 2,
            "frontendWorkers": 2,
            "backendRedundancyFactor": 2,
            "backendWorkers": 2,
            "backendPartitions": 2,
            "memoryProfile": "Medium",
            "serviceType": "ClusterIp",
        }
    }
    expected_trust_config = {"source": "SelfSigned"}
    if target_scenario["trust"]["userTrust"]:
        # TODO - @digimaun - trust setting key validation should be handled in target unit tests
        expected_trust_config = {"source": "CustomerManaged", "settings": target_scenario["trust"]["settings"]}
    assert parameters["trustConfig"]["value"] == expected_trust_config

    mode = body["properties"]["mode"]
    assert mode == "Incremental"

    template = body["properties"]["template"]
    for key in EXPECTED_INSTANCE_RESOURCE_KEYS:
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
