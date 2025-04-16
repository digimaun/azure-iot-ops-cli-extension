# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

import re
import json
from typing import Optional, TypeVar
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
from azext_edge.edge.providers.orchestration.clone import CloneManager
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
from .resources.conftest import BASE_URL, get_request_kpis, RequestKPIs
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
    get_dataflow_endpoint,
    get_mock_dataflow_endpoint_record,
)
from .resources.test_dataflow_profiles_unit import (
    get_dataflow_profile_endpoint,
    get_mock_dataflow_profile_record,
)
from .resources.test_dataflows_unit import (
    get_dataflow_endpoint as get_dataflow_ep,
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


class CloneScenario:
    def __init__(self, description: str = None):
        self.description = description
        self.resource_configs = defaultdict(dict)
        self.arg_queries = {}

    def bootstrap(
        self: C, mocked_responses: responses, instance_name: str, resource_group_name: str, cluster_name: str
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
        self.configure_instance()

    def configure_instance(self: C) -> C:
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
        payload = {"value": [mock_listener_record]}

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
        self.resource_configs["listeners"] = payload["value"]
        return self

    def add_authns(self: C) -> C:
        mock_authn_record = get_mock_broker_authn_record(
            authn_name=self.default_authn_name,
            broker_name=self.default_broker_name,
            instance_name=self.instance_name,
            resource_group_name=self.resource_group_name,
        )
        payload = {"value": [mock_authn_record]}

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
        self.resource_configs["authns"] = payload["value"]
        return self

    def add_authzs(self: C) -> C:
        payload = {"value": []}

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

    def add_dataflows(self: C) -> C:
        payload = {"value": []}

        self.responses.add(
            method=responses.GET,
            url=get_dataflow_ep(
                profile_name=self.default_dataflow_profile_name,
                resource_group_name=self.resource_group_name,
                instance_name=self.instance_name,
            ),
            json=payload,
            status=200,
            content_type="application/json",
        )
        self.resource_configs["dataflows"] = payload["value"]
        return self

    def add_dataflow_profiles(self: C) -> C:
        mock_dataflow_profile_record = get_mock_dataflow_profile_record(
            profile_name=self.default_dataflow_endpoint_name,
            instance_name=self.instance_name,
            resource_group_name=self.resource_group_name,
        )
        payload = {"value": [mock_dataflow_profile_record]}

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
        self.resource_configs["profiles"] = payload["value"]
        return self

    def add_dataflow_endpoints(self: C) -> C:
        mock_dataflow_endpoint_record = get_mock_dataflow_endpoint_record(
            dataflow_endpoint_name=self.default_dataflow_endpoint_name,
            instance_name=self.instance_name,
            resource_group_name=self.resource_group_name,
        )
        payload = {"value": [mock_dataflow_endpoint_record]}

        self.responses.add(
            method=responses.GET,
            url=get_dataflow_endpoint(
                resource_group_name=self.resource_group_name,
                instance_name=self.instance_name,
            ),
            json=payload,
            status=200,
            content_type="application/json",
        )
        self.resource_configs["endpoints"] = payload["value"]
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


@pytest.mark.parametrize("clone_scenario", [CloneScenario()])
def test_clone_manager(
    mocked_cmd: Mock,
    mocked_responses: responses,
    clone_scenario: CloneScenario,
):
    cluster_name = generate_random_string()
    instance_name = generate_random_string()
    resource_group_name = generate_random_string()

    clone_scenario.bootstrap(
        mocked_responses,
        resource_group_name=resource_group_name,
        instance_name=instance_name,
        cluster_name=cluster_name,
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
    # restore_client = clone_state.get_restore_client()
    import pdb

    pdb.set_trace()
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


def __replace_cl(resource_configs: dict):
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
    "roleAssignments_1": {},
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
    "authns_1": {},
    "listeners_1": {},
    "dataflowEndpoints_1": {},
    "dataflowProfiles_1": {},
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
        expected_resource_keys = list(EXPECTED_ORD_MIN_RESOURCE_MAP.keys())

        for i in range(len(expected_resource_keys)):
            assert (
                resource_keys[i] == expected_resource_keys[i]
            ), f"Expected resource key: {expected_resource_keys[i]} at position {i}"

        resources = content["resources"]
        self._assert_extensions(resources)
        self._assert_root_components(resources)

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
            component_meta: dict = EXPECTED_ORD_MIN_RESOURCE_MAP[key]
            component_replacements = component_meta.get("replacements")
            if component_replacements:
                if callable(component_replacements):
                    component_replacements = component_replacements(self.resource_configs)
                component_config.update(component_replacements)
            self._prune_resource(component_config)
            assert component_config == resources[key], f"Root resource mismatch for {key}"

    def _prune_resource(self, resource: dict):
        resource.pop("id", None)
        resource.pop("systemData", None)
        if "properties" in resource:
            resource["properties"].pop("provisioningState", None)
