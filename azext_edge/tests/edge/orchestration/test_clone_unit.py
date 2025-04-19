# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

import re
import json
from typing import Optional, TypeVar, List, Tuple
import math
from unittest.mock import Mock
from copy import deepcopy

import pytest
import responses
import requests

from azext_edge.edge.common import (
    DEFAULT_BROKER,
    DEFAULT_BROKER_AUTHN,
    DEFAULT_BROKER_LISTENER,
    DEFAULT_DATAFLOW_ENDPOINT,
    DEFAULT_DATAFLOW_PROFILE,
)
from azext_edge.edge.providers.orchestration.clone import (
    CloneManager,
    DEPLOYMENT_CHUNK_SIZE,
    InstanceRestore,
    default_bundle_name,
)
from azext_edge.edge.providers.orchestration.common import (
    EXTENSION_TYPE_ACS,
    EXTENSION_TYPE_OPS,
    EXTENSION_TYPE_PLATFORM,
    EXTENSION_TYPE_SSC,
)
from collections import defaultdict
from azext_edge.constants import VERSION as CLI_VERSION
from azext_edge.edge.util.id_tools import parse_resource_id
from ...generators import generate_random_string, get_zeroed_subscription
from .resources.conftest import BASE_URL, get_request_kpis
from .resources.test_broker_authns_unit import (
    get_broker_authn_endpoint,
    get_mock_broker_authn_record,
)
from .resources.test_broker_authzs_unit import (
    get_broker_authz_endpoint,
    get_mock_broker_authz_record,
)
from .resources.test_broker_listeners_unit import (
    get_broker_listener_endpoint,
    get_mock_broker_listener_record,
)
from .resources.test_brokers_unit import (
    get_broker_endpoint,
    get_mock_broker_record,
)
from .resources.test_custom_locations_unit import (
    get_custom_location_endpoint,
    get_mock_custom_location_record,
)
from .resources.test_dataflow_endpoints_unit import (
    get_dataflow_endpoint_endpoint,
    get_mock_dataflow_endpoint_record,
)
from .resources.test_dataflow_profiles_unit import (
    get_dataflow_profile_endpoint,
    get_mock_dataflow_profile_record,
)
from .resources.test_dataflows_unit import (
    get_dataflow_endpoint,
)
from .resources.test_dataflows_unit import (
    get_mock_dataflow_record,
)
from .resources.test_instances_unit import (
    get_instance_endpoint,
    get_mock_instance_record,
)
from .resources.test_secretsync_spcs_unit import get_spc_endpoint
from .resources.test_secretsyncs_unit import get_secretsync_endpoint

ZEROED_SUBSCRIPTION = get_zeroed_subscription()


C = TypeVar("C", bound="CloneScenario")

EXT_NAME_PLAT = "azure-iot-operations-platform"
EXT_NAME_SSC = "azure-secrets-store"
EXT_NAME_OPS = "azure-iot-operations"

EXTENSIONS_TYPE_TO_NAME = [
    (EXTENSION_TYPE_PLATFORM, EXT_NAME_PLAT),
    (EXTENSION_TYPE_ACS, "azure-arc-containerstorage"),
    (EXTENSION_TYPE_SSC, EXT_NAME_SSC),
    (EXTENSION_TYPE_OPS, EXT_NAME_OPS),
]

PLURALS = ["authns", "authzs", "listeners", "dataflowEndpoints", "dataflowProfiles", "dataflows"]
SINGLETONS = ["customLocation", "instance", "roleAssignments_1", "broker"]


def get_deploy_url(cluster_sub_id: str, cluster_rg: str, deployment_name: str, page_num: int = 1) -> str:
    return (
        f"{BASE_URL}/subscriptions/{cluster_sub_id}/resourcegroups/{cluster_rg}/providers"
        f"/Microsoft.Resources/deployments/{deployment_name}_{page_num}?api-version=2024-03-01"
    )


class CloneScenario:
    def __init__(self, description: str = None):
        self.description = description
        self.resource_configs = defaultdict(list)
        self.arg_queries = {}
        self.deploy_responses = []

    def bootstrap(
        self: C,
        mocked_responses: responses,
        instance_name: str,
        resource_group_name: str,
        cluster_name: str,
        add_resources_map: Optional[dict] = None,
    ):
        self.responses = mocked_responses
        # self.responses.assert_all_requests_are_fired = False
        self.instance_name = instance_name
        self.resource_group_name = resource_group_name
        self.cluster_name = cluster_name
        self.cl_name = generate_random_string()
        self.sr_name = generate_random_string()
        self.default_broker_name = DEFAULT_BROKER
        self.default_authn_name = DEFAULT_BROKER_AUTHN
        self.default_listener_name = DEFAULT_BROKER_LISTENER
        self.default_dataflow_profile_name = DEFAULT_DATAFLOW_PROFILE
        self.default_dataflow_endpoint_name = DEFAULT_DATAFLOW_ENDPOINT
        self.add_resources_map = add_resources_map or {}
        self._configure_instance()

    def _configure_instance(self: C) -> C:
        self.add_extensions()
        self.add_custom_location()
        self.add_instance()
        self.add_broker()
        self.add_listeners()
        self.add_authns()
        self.add_authzs()
        self.add_dataflow_profiles()
        self.add_dataflow_endpoints()
        self.add_dataflows()
        self.add_arg_handler()
        self.add_secretsync_spcs()
        self.add_secretsyncs()
        return self

    def wrap_cluster_deploy(self: C, split_content: List[dict], to_cluster_id: Optional[str] = None) -> C:
        if not to_cluster_id:
            return

        cluster_sub_id = parse_resource_id(to_cluster_id)["subscription"]
        cluster_rg = parse_resource_id(to_cluster_id)["resource_group"]

        deployment_name = default_bundle_name(self.instance_name)
        for i in range(len(split_content)):
            r = self.responses.add(
                method=responses.PUT,
                url=get_deploy_url(
                    cluster_sub_id=cluster_sub_id,
                    cluster_rg=cluster_rg,
                    deployment_name=deployment_name,
                    page_num=i + 1,
                ),
                json={},
                status=200,
                content_type="application/json",
            )
            self.deploy_responses.append(r)
        return self

    def add_extensions(self: C) -> C:
        extensions_endpoint = (
            f"{BASE_URL}/subscriptions/{ZEROED_SUBSCRIPTION}/resourceGroups/{self.resource_group_name}"
            f"/providers/Microsoft.Kubernetes/connectedClusters/{self.cluster_name}/providers"
            "/Microsoft.KubernetesConfiguration/extensions?api-version=2023-05-01"
        )

        extensions = []
        for ext_type, ext_name in EXTENSIONS_TYPE_TO_NAME:
            extensions.append(
                self._create_extension(ext_type, ext_name, "1.0.0", "stable"),
            )

        self.responses.add(
            method=responses.GET,
            url=extensions_endpoint,
            json={"value": extensions},
            status=200,
            content_type="application/json",
        )
        self.resource_configs["extensions"] = extensions
        return self

    def _create_extension(self, ext_type: str, ext_name: str, version: str, train: str) -> dict:
        ext = {
            "id": (
                f"/subscriptions/{ZEROED_SUBSCRIPTION}/resourceGroups/{self.resource_group_name}"
                f"/providers/Microsoft.Kubernetes/connectedClusters/{self.cluster_name}/providers"
                f"/Microsoft.KubernetesConfiguration/extensions/{ext_name}"
            ),
            "name": ext_name,
            "type": "Microsoft.KubernetesConfiguration/extensions",
            "properties": {
                "extensionType": ext_type,
                "version": version,
                "releaseTrain": train,
                "provisioningState": "Succeeded",
                "configurationSettings": {},
            },
        }

        if ext_type == EXTENSION_TYPE_OPS:
            identity_id = generate_random_string()
            ext["identity"] = {
                "type": "SystemAssigned",
                "principalId": identity_id,
            }

        return ext

    def add_custom_location(self: C) -> C:
        mock_cl_record = get_mock_custom_location_record(
            name=self.cl_name, resource_group_name=self.resource_group_name, cluster_name=self.cluster_name
        )
        self.responses.add(
            method=responses.GET,
            url=get_custom_location_endpoint(
                resource_group_name=self.resource_group_name, custom_location_name=self.cl_name
            ),
            json=mock_cl_record,
            status=200,
            content_type="application/json",
        )
        self.resource_configs["customLocation"] = mock_cl_record
        return self

    def add_instance(self: C) -> C:
        mock_instance_record = get_mock_instance_record(
            name=self.instance_name,
            resource_group_name=self.resource_group_name,
            cl_name=self.cl_name,
            schema_registry_name=self.sr_name,
        )
        self.resource_configs["schemaRegistryId"] = mock_instance_record["properties"]["schemaRegistryRef"][
            "resourceId"
        ]
        self.responses.add(
            method=responses.GET,
            url=get_instance_endpoint(resource_group_name=self.resource_group_name, instance_name=self.instance_name),
            json=mock_instance_record,
            status=200,
            content_type="application/json",
        )
        self.resource_configs["instance"] = mock_instance_record
        return self

    def add_broker(self: C) -> C:
        mock_broker_record = get_mock_broker_record(
            broker_name=self.default_broker_name,
            instance_name=self.instance_name,
            resource_group_name=self.resource_group_name,
        )
        self.responses.add(
            method=responses.GET,
            url=get_broker_endpoint(resource_group_name=self.resource_group_name, instance_name=self.instance_name),
            json={"value": [mock_broker_record]},
            status=200,
            content_type="application/json",
        )
        self.resource_configs["broker"] = mock_broker_record
        return self

    def add_listeners(self: C) -> C:
        mock_listener_record = get_mock_broker_listener_record(
            listener_name=self.default_listener_name,
            broker_name=self.default_broker_name,
            instance_name=self.instance_name,
            resource_group_name=self.resource_group_name,
        )
        listeners = [mock_listener_record]
        for i in range(1, self.add_resources_map.get("listeners", 0)):
            listeners.append(
                get_mock_broker_listener_record(
                    listener_name=generate_random_string(),
                    broker_name=self.default_broker_name,
                    instance_name=self.instance_name,
                    resource_group_name=self.resource_group_name,
                )
            )
        payload = {"value": listeners}

        self.responses.add(
            method=responses.GET,
            url=get_broker_listener_endpoint(
                resource_group_name=self.resource_group_name,
                instance_name=self.instance_name,
                broker_name=self.default_broker_name,
            ),
            json=payload,
            status=200,
            content_type="application/json",
        )
        self.resource_configs["listeners"] = listeners
        return self

    def add_authns(self: C) -> C:
        mock_authn_record = get_mock_broker_authn_record(
            authn_name=self.default_authn_name,
            broker_name=self.default_broker_name,
            instance_name=self.instance_name,
            resource_group_name=self.resource_group_name,
        )
        authns = [mock_authn_record]
        for i in range(1, self.add_resources_map.get("authns", 0)):
            authns.append(
                get_mock_broker_authn_record(
                    authn_name=generate_random_string(),
                    broker_name=self.default_broker_name,
                    instance_name=self.instance_name,
                    resource_group_name=self.resource_group_name,
                )
            )
        payload = {"value": authns}

        self.responses.add(
            method=responses.GET,
            url=get_broker_authn_endpoint(
                resource_group_name=self.resource_group_name,
                instance_name=self.instance_name,
                broker_name=self.default_broker_name,
            ),
            json=payload,
            status=200,
            content_type="application/json",
        )
        self.resource_configs["authns"] = authns
        return self

    def add_authzs(self: C) -> C:
        authzs = []
        for i in range(self.add_resources_map.get("authzs", 0)):
            authzs.append(
                get_mock_broker_authz_record(
                    authz_name=generate_random_string(),
                    broker_name=self.default_broker_name,
                    instance_name=self.instance_name,
                    resource_group_name=self.resource_group_name,
                )
            )
        payload = {"value": authzs}

        self.responses.add(
            method=responses.GET,
            url=get_broker_authz_endpoint(
                resource_group_name=self.resource_group_name,
                instance_name=self.instance_name,
                broker_name=self.default_broker_name,
            ),
            json=payload,
            status=200,
            content_type="application/json",
        )
        self.resource_configs["authzs"] = payload["value"]
        return self

    def add_dataflow_profiles(self: C) -> C:
        mock_dataflow_profile_record = get_mock_dataflow_profile_record(
            profile_name=self.default_dataflow_profile_name,
            instance_name=self.instance_name,
            resource_group_name=self.resource_group_name,
        )
        profiles = [mock_dataflow_profile_record]
        for i in range(self.add_resources_map.get("dataflowProfiles", 0)):
            profiles.append(
                get_mock_dataflow_profile_record(
                    profile_name=generate_random_string(),
                    instance_name=self.instance_name,
                    resource_group_name=self.resource_group_name,
                )
            )
        payload = {"value": profiles}

        self.responses.add(
            method=responses.GET,
            url=get_dataflow_profile_endpoint(
                resource_group_name=self.resource_group_name,
                instance_name=self.instance_name,
            ),
            json=payload,
            status=200,
            content_type="application/json",
        )
        self.resource_configs["dataflowProfiles"] = profiles
        return self

    def add_dataflow_endpoints(self: C) -> C:
        mock_dataflow_endpoint_record = get_mock_dataflow_endpoint_record(
            dataflow_endpoint_name=self.default_dataflow_endpoint_name,
            instance_name=self.instance_name,
            resource_group_name=self.resource_group_name,
        )
        endpoints = [mock_dataflow_endpoint_record]
        for i in range(self.add_resources_map.get("dataflowEndpoints", 0)):
            endpoints.append(
                get_mock_dataflow_endpoint_record(
                    dataflow_endpoint_name=generate_random_string(),
                    instance_name=self.instance_name,
                    resource_group_name=self.resource_group_name,
                )
            )
        payload = {"value": endpoints}

        self.responses.add(
            method=responses.GET,
            url=get_dataflow_endpoint_endpoint(
                resource_group_name=self.resource_group_name,
                instance_name=self.instance_name,
            ),
            json=payload,
            status=200,
            content_type="application/json",
        )
        self.resource_configs["dataflowEndpoints"] = endpoints
        return self

    def add_dataflows(self: C) -> C:
        dataflows = []
        for profile in self.resource_configs["dataflowProfiles"]:
            per_profile = []
            for _ in range(self.add_resources_map.get("dataflows", 0)):
                per_profile.append(
                    get_mock_dataflow_record(
                        dataflow_name=generate_random_string(),
                        profile_name=profile["name"],
                        instance_name=self.instance_name,
                        resource_group_name=self.resource_group_name,
                    )
                )
            payload = {"value": per_profile}
            self.responses.add(
                method=responses.GET,
                url=get_dataflow_endpoint(
                    profile_name=profile["name"],
                    instance_name=self.instance_name,
                    resource_group_name=self.resource_group_name,
                ),
                json=payload,
                status=200,
                content_type="application/json",
            )
            dataflows.extend(per_profile)

        self.resource_configs["dataflows"] = dataflows
        return self

    def add_secretsync_spcs(self: C) -> C:
        payload = {"value": []}

        self.responses.add(
            method=responses.GET,
            url=get_spc_endpoint(
                resource_group_name=self.resource_group_name,
            ),
            json=payload,
            status=200,
            content_type="application/json",
        )
        self.resource_configs["spcs"] = payload["value"]
        return self

    def add_secretsyncs(self: C) -> C:
        payload = {"value": []}

        self.responses.add(
            method=responses.GET,
            url=get_secretsync_endpoint(
                resource_group_name=self.resource_group_name,
            ),
            json=payload,
            status=200,
            content_type="application/json",
        )
        self.resource_configs["secretsyncs"] = payload["value"]
        return self

    def add_arg_handler(self: C) -> C:
        def _handle_requests(request: requests.PreparedRequest) -> Optional[tuple]:
            request_kpis = get_request_kpis(request)
            if request_kpis.body_str:
                request_payload = json.loads(request_kpis.body_str)
                query = request_payload["query"]
                if '| where type =~ "Microsoft.ManagedIdentity/userAssignedIdentities"' in query:
                    self.arg_queries["uami"] = 1
                    return request_kpis.respond_with(200, response_body={"data": []})
                if "| where type =~ 'microsoft.deviceregistry/assetendpointprofiles'" in query:
                    self.arg_queries["assetendpointprofiles"] = 1
                    return request_kpis.respond_with(200, response_body={"data": []})
                if "| where type =~ 'microsoft.deviceregistry/assets'" in query:
                    self.arg_queries["assets"] = 1
                    return request_kpis.respond_with(200, response_body={"data": []})
            raise RuntimeError("Unexpected query: " + query)

        self.responses.add_callback(
            method="POST",
            url=re.compile(
                r"https://management.azure.com/providers/Microsoft.ResourceGraph/resources\?api-version=2022-10-01"
            ),
            callback=_handle_requests,
        )
        return self


# @pytest.mark.parametrize("add_dataflows", [0, 1])
# @pytest.mark.parametrize("add_dataflow_endpoints", [0, 1])
# @pytest.mark.parametrize("add_dataflow_profiles", [0, 1])
#@pytest.mark.parametrize("add_authzs", [0])
@pytest.mark.parametrize("add_authns", [0, 1, 100])
@pytest.mark.parametrize("add_listeners", [0, 1, 100])
@pytest.mark.parametrize("clone_scenario", [CloneScenario()])
def test_clone_manager(
    mocked_cmd: Mock,
    mocked_responses: responses,
    clone_scenario: CloneScenario,
    add_listeners: int,
    add_authns: int,
    #add_authzs: int,
    #add_dataflow_profiles: int,
    #add_dataflow_endpoints: int,
    #add_dataflows: int,
):
    cluster_name = generate_random_string()
    instance_name = generate_random_string()
    resource_group_name = generate_random_string()

    add_resources_map = {
        "listeners": add_listeners,
        "authns": add_authns,
        #"authzs": add_authzs,
        #"dataflowProfiles": add_dataflow_profiles,
        #"dataflowEndpoints": add_dataflow_endpoints,
        #"dataflows": add_dataflows,
    }

    clone_scenario.bootstrap(
        mocked_responses,
        resource_group_name=resource_group_name,
        instance_name=instance_name,
        cluster_name=cluster_name,
        add_resources_map=add_resources_map,
    )

    clone_manager = CloneManager(
        cmd=mocked_cmd, resource_group_name=resource_group_name, instance_name=instance_name, no_progress=True
    )
    clone_state = clone_manager.analyze_cluster()
    template_content = clone_state.get_content()
    content = template_content.content

    CloneAssertor(clone_scenario).assert_content(content)

    split_content = template_content.get_split_content()

    # template_content.write()
    # template_content._get_deployments()

    cluster_sub_id = generate_random_string()
    cluster_rg = generate_random_string()
    cluster_name = generate_random_string()
    to_instance_name = generate_random_string()
    to_cluster_id = (
        f"/subscriptions/{cluster_sub_id}/resourceGroups/{cluster_rg}"
        f"/providers/Microsoft.Kubernetes/connectedClusters/{cluster_name}"
    )
    clone_scenario.wrap_cluster_deploy(split_content, to_cluster_id=to_cluster_id)

    restore_client: InstanceRestore = clone_state.get_restore_client(to_cluster_id=to_cluster_id, template_mode=None)
    restore_client.deploy(instance_name=to_instance_name)
    # deploy_responses[0].calls[0].request.body
    # import pdb
    # pdb.set_trace()
    pass


EXPECTED_TEMPLATE_KEYS = {
    "$schema",
    "languageVersion",
    "contentVersion",
    "metadata",
    "parameters",
    "variables",
    "resources",
}
EXPECTED_METADATA_KEYS = {"opsCliVersion", "clonedInstanceId"}
EXPECTED_PARAMETER_KEYS = {"clusterName", "instanceName", "resourceSlug", "customLocationName"}
EXPECTED_VARIABLE_KEYS = {"aioExtName"}

EXPECTED_ORD_EXT_RESOURCE_MAP = {
    "platform": {
        "replacements": {
            "scope": "[resourceId('Microsoft.Kubernetes/connectedClusters', parameters('clusterName'))]",
            "apiVersion": "2023-05-01",
        },
    },
    "containerStorage": {
        "replacements": {
            "scope": "[resourceId('Microsoft.Kubernetes/connectedClusters', parameters('clusterName'))]",
            "apiVersion": "2023-05-01",
            "dependsOn": ["platform"],
        },
    },
    "secretStore": {
        "replacements": {
            "scope": "[resourceId('Microsoft.Kubernetes/connectedClusters', parameters('clusterName'))]",
            "apiVersion": "2023-05-01",
            "dependsOn": ["platform"],
        },
    },
    "iotOperations": {
        "replacements": {
            "name": "[variables('aioExtName')]",
            "scope": "[resourceId('Microsoft.Kubernetes/connectedClusters', parameters('clusterName'))]",
            "apiVersion": "2023-05-01",
            "dependsOn": ["platform", "containerStorage", "secretStore"],
            "identity": {
                "type": "SystemAssigned",
            },
        },
    },
}


def __replace_cl(context: dict) -> dict:
    resource_configs = context["resource_configs"]
    custom_location = resource_configs["customLocation"]

    ext_map = {
        v["name"]: v
        for v in resource_configs["extensions"]
        if v["name"] in [EXT_NAME_PLAT, EXT_NAME_SSC, EXT_NAME_OPS]
    }
    extension_ids = []
    for ext_name in ext_map:
        if ext_name == EXT_NAME_OPS:
            extension_ids.append(
                (
                    "[concat(resourceId('Microsoft.Kubernetes/connectedClusters', parameters('clusterName')), "
                    "'/providers/Microsoft.KubernetesConfiguration/extensions/', variables('aioExtName'))]"
                )
            )
        else:
            extension_ids.append(
                (
                    "[concat(resourceId('Microsoft.Kubernetes/connectedClusters', parameters('clusterName')), "
                    f"'/providers/Microsoft.KubernetesConfiguration/extensions/{ext_name}')]"
                )
            )

    return {
        "apiVersion": "2021-08-31-preview",
        "name": "[parameters('customLocationName')]",
        "properties": {
            "hostResourceId": "[resourceId('Microsoft.Kubernetes/connectedClusters', parameters('clusterName'))]",
            "namespace": custom_location["properties"]["namespace"],
            "displayName": "[parameters('customLocationName')]",
            "clusterExtensionIds": extension_ids,
            "authentication": {},
        },
        "dependsOn": ["platform", "secretStore", "iotOperations"],
    }


def __replace_instance_resource(context: dict) -> dict:
    config = context["config"]
    config_type: str = config["type"]
    config_name: str = config["name"]
    type_segment = config_type.split("/")[-1].lower()

    if type_segment in ["authentications", "authorizations", "listeners"]:
        config_name = f"/default/{config_name}"
    if type_segment in ["dataflowprofiles", "dataflowendpoints", "dataflows"]:
        config_name = f"/{config_name}"

    return {
        "apiVersion": "2025-04-01",
        "name": f"[concat(parameters('instanceName'), '{config_name}')]",
        "extendedLocation": {
            "name": "[resourceId('Microsoft.ExtendedLocation/customLocations', parameters('customLocationName'))]",
            "type": "CustomLocation",
        },
    }


EXPECTED_ORD_MIN_RESOURCE_MAP = {
    **EXPECTED_ORD_EXT_RESOURCE_MAP,
    "customLocation": {"replacements": __replace_cl},
    "instance": {
        "replacements": {
            "apiVersion": "2025-04-01",
            "name": "[parameters('instanceName')]",
            "extendedLocation": {
                "name": "[resourceId('Microsoft.ExtendedLocation/customLocations', parameters('customLocationName'))]",
                "type": "CustomLocation",
            },
            "dependsOn": ["customLocation"],
        }
    },
    "roleAssignments": {},
    "broker": {
        "replacements": {
            "apiVersion": "2025-04-01",
            "name": "[concat(parameters('instanceName'), '/default')]",
            "extendedLocation": {
                "name": "[resourceId('Microsoft.ExtendedLocation/customLocations', parameters('customLocationName'))]",
                "type": "CustomLocation",
            },
            "dependsOn": ["instance"],
        }
    },
    "listeners": {"replacements": __replace_instance_resource},
    "authns": {"replacements": __replace_instance_resource},
    "authzs": {"replacements": __replace_instance_resource},
    "dataflowEndpoints": {"replacements": __replace_instance_resource},
    "dataflowProfiles": {"replacements": __replace_instance_resource},
}


class CloneAssertor:
    def __init__(self, clone_scenario: CloneScenario):
        self.clone_scenario = clone_scenario
        self.resource_configs = clone_scenario.resource_configs
        self.extension_name_map = {}

    def assert_content(self, content: dict):
        assert isinstance(content, dict), "content should be a dictionary"
        assert set(content.keys()) == EXPECTED_TEMPLATE_KEYS, "Unexpected keys in template content"

        assert (
            content["$schema"] == "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#"
        ), "Schema mismatch"
        assert content["languageVersion"] == "2.0", "Language version mismatch"
        assert content["contentVersion"] == "1.0.0.0", "Content version mismatch"

        assert isinstance(content["metadata"], dict), "Metadata key should be a dictionary"
        assert set(content["metadata"].keys()) == EXPECTED_METADATA_KEYS, "Unexpected keys in metadata content"
        assert content["metadata"]["opsCliVersion"] == CLI_VERSION, "Ops CLI version mismatch"
        assert (
            content["metadata"]["clonedInstanceId"] == self.clone_scenario.resource_configs["instance"]["id"]
        ), "Cloned instance ID mismatch"

        assert isinstance(content["parameters"], dict), "Parameters key should be a dictionary"
        assert set(content["parameters"].keys()) == EXPECTED_PARAMETER_KEYS, "Unexpected keys in parameters content"
        assert content["parameters"]["clusterName"] == {"type": "string"}
        assert content["parameters"]["instanceName"] == {
            "type": "string",
            "defaultValue": self.clone_scenario.instance_name,
        }
        assert content["parameters"]["resourceSlug"] == {
            "type": "string",
            "defaultValue": (
                "[take(uniqueString(resourceGroup().id, parameters('clusterName'), parameters('instanceName')), 5)]"
            ),
        }
        assert content["parameters"]["customLocationName"] == {
            "type": "string",
            "defaultValue": "[format('location-{0}', parameters('resourceSlug'))]",
        }

        assert isinstance(content["variables"], dict), "Variables key should be a dictionary"
        assert set(content["variables"].keys()) == EXPECTED_VARIABLE_KEYS, "Unexpected keys in variables content"
        assert content["variables"]["aioExtName"] == "[format('azure-iot-operations-{0}', parameters('resourceSlug'))]"

        self._assert_resources(content)

    def _assert_resources(self, content: dict):
        assert isinstance(content["resources"], dict), "Resources key should be a dictionary"
        assert content["resources"], "Resources dict should not be empty"
        resource_keys = list(content["resources"].keys())
        expected_resource_keys = self._get_expected_resource_keys()

        for i in range(len(expected_resource_keys)):
            assert (
                resource_keys[i] == expected_resource_keys[i]
            ), f"Expected resource key: {expected_resource_keys[i]} at position {i}"

        resources = content["resources"]
        self._assert_extensions(resources)
        self._assert_root_components(resources)
        self._assert_role_assignments(resources)
        self._assert_deployments(resources)

    def _assert_extensions(self, resources: dict):
        expected_ext_keys = list(EXPECTED_ORD_EXT_RESOURCE_MAP.keys())
        for i in range(len(expected_ext_keys)):
            key_name = expected_ext_keys[i]
            extension_config: dict = deepcopy(self.resource_configs["extensions"][i])
            expected_ext_meta: dict = EXPECTED_ORD_EXT_RESOURCE_MAP[key_name]
            clone_replacements = expected_ext_meta.get("replacements")
            if clone_replacements:
                extension_config.update(clone_replacements)
            self._prune_resource(extension_config)
            assert extension_config == resources[key_name], f"Extension resource mismatch for {key_name}"

    def _assert_root_components(self, resources: dict):
        keys = ["customLocation", "instance", "broker"]
        for key in keys:
            component_config = deepcopy(self.resource_configs[key])
            self._handle_component_conversion(component_config, key)
            assert component_config == resources[key], f"Root resource mismatch for {key}"

    def _handle_component_conversion(self, component_config: dict, conversion_map_key: str) -> dict:
        component_meta: dict = EXPECTED_ORD_MIN_RESOURCE_MAP[conversion_map_key]
        component_replacements = component_meta.get("replacements")
        if component_replacements:
            if callable(component_replacements):
                context = {"config": component_config, "resource_configs": self.resource_configs}
                component_replacements = component_replacements(context)
            component_config.update(component_replacements)
        self._prune_resource(component_config)
        return component_config

    def _assert_role_assignments(self, resources: dict):
        key = "roleAssignments_1"
        deployment = resources[key]
        parsed_sr_id = parse_resource_id(self.resource_configs["schemaRegistryId"])
        self._assert_deployment_generic(
            deployment, key, resource_group=parsed_sr_id["resource_group"], depends_on=["iotOperations"]
        )
        if True:  # TODO If template mode is default
            template = deployment["properties"]["template"]
            assert (
                template["$schema"]
                == "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#"
            )
            assert template["contentVersion"] == "1.0.0.0"
            assert template["parameters"] == {
                "clusterName": {"type": "string"},
                "instanceName": {"type": "string"},
                "principalId": {"type": "string"},
                "schemaRegistryId": {"type": "string"},
            }
            assert isinstance(template["resources"], list), "Deployment resources key should be a list"
            assert len(template["resources"]) == 1
            sr_ra_def = template["resources"][0]
            assert sr_ra_def["type"] == "Microsoft.Authorization/roleAssignments"
            assert sr_ra_def["apiVersion"] == "2022-04-01"
            assert (
                sr_ra_def["name"]
                == "[guid(parameters('instanceName'), parameters('clusterName'), resourceGroup().id)]"
            )
            assert sr_ra_def["scope"] == "[parameters('schemaRegistryId')]"
            assert (
                sr_ra_def["properties"]["roleDefinitionId"]
                == "[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b24988ac-6180-42a0-ab88-20f7382dd24c')]"
            )
            assert sr_ra_def["properties"]["principalId"] == "[parameters('principalId')]"
            assert sr_ra_def["properties"]["principalType"] == "ServicePrincipal"

    def _assert_deployments(self, resources: dict):
        template_fetched_keys = defaultdict(list)
        for deployment_key, resource_config_key, depends_on in self._get_deployment_key_pairs():
            deployment = resources[deployment_key]
            self._assert_deployment_generic(
                deployment,
                deployment_key,
                depends_on=depends_on,
            )
            if True:  # TODO If template mode is default
                template = deployment["properties"]["template"]
                assert (
                    template["$schema"]
                    == "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#"
                )
                assert template["contentVersion"] == "1.0.0.0"
                assert template["parameters"] == {
                    "customLocationName": {"type": "string"},
                    "instanceName": {"type": "string"},
                }
                deployment_resources = template["resources"]
                deployment_resources_len = len(deployment_resources)

                continue_from = len(template_fetched_keys[resource_config_key])

                for i in range(deployment_resources_len):
                    template_fetched_keys[resource_config_key].append(deployment_resources[i])
                    model_config = deepcopy(self.resource_configs[resource_config_key][continue_from + i])
                    self._handle_component_conversion(model_config, resource_config_key)
                    assert model_config == deployment_resources[i]

        for key in template_fetched_keys:
            assert len(template_fetched_keys[key]) == len(
                self.resource_configs[key]
            ), f"Mismatch in resource count for {key}"

    def _get_deployment_key_pairs(self) -> List[Tuple[str, str, List[str]]]:
        payload = []
        dep_map = {"listeners": ["authns", "authzs"], "dataflows": ["dataflowProfiles", "dataflowEndpoints"]}
        chunks_map = defaultdict(dict)

        broker_related = {"listeners", "authns", "authzs"}

        for plural in PLURALS:
            kind_len = len(self.resource_configs[plural])
            chunks = math.ceil(kind_len / DEPLOYMENT_CHUNK_SIZE)
            chunks_map[plural] = chunks

        for plural in PLURALS:
            depends_on = []
            if plural in dep_map:
                for dep in dep_map[plural]:
                    if dep in chunks_map:
                        depends_on.append(
                            f"[resourceId('Microsoft.Resources/deployments', concat(parameters('resourceSlug'), '_{dep}_{chunks_map[dep]}'))]"
                        )
            elif plural in broker_related:
                depends_on.append(
                    "[resourceId('microsoft.iotoperations/instances/brokers', parameters('instanceName'), 'default')]"
                )
            else:
                depends_on.append("[resourceId('microsoft.iotoperations/instances', parameters('instanceName'))]")

            for i in range(chunks):
                paged_key = f"{plural}_{i + 1}"
                payload.append((paged_key, plural, depends_on))

        return payload

    def _get_expected_resource_keys(self):
        resource_keys = []
        enumerate_through = [*PLURALS]

        for r in enumerate_through:
            kind_len = len(self.resource_configs[r])
            if not kind_len:
                continue
            chunks = math.ceil(kind_len / DEPLOYMENT_CHUNK_SIZE)

            for i in range(chunks):
                paged_key = f"{r}_{i + 1}"
                resource_keys.append(paged_key)

        resource_keys = [*list(EXPECTED_ORD_EXT_RESOURCE_MAP.keys()), *SINGLETONS] + resource_keys

        return resource_keys

    def _assert_deployment_generic(
        self,
        deployment: dict,
        expected_name: str,
        resource_group: Optional[str] = None,
        depends_on: Optional[List[str]] = None,
    ):
        assert deployment["type"] == "Microsoft.Resources/deployments"
        assert deployment["apiVersion"] == "2022-09-01"
        assert deployment["name"] == f"[concat(parameters('resourceSlug'), '_{expected_name}')]"
        assert deployment["properties"]["mode"] == "Incremental"
        # assert deployment["properties"]["template"]
        if resource_group:
            assert deployment["resourceGroup"] == resource_group
        if depends_on:
            assert deployment["dependsOn"] == depends_on

    def _prune_resource(self, resource: dict):
        resource.pop("id", None)
        resource.pop("systemData", None)
        if "properties" in resource:
            resource["properties"].pop("provisioningState", None)
