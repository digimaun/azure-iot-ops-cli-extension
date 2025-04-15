# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

import re
import json
from typing import Optional, TypeVar
from unittest.mock import Mock

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


class CloneScenario:
    def __init__(self, description: str = None):
        self.description = description
        self.resource_configs = {}
        self.ext_identities = {}
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
        self.add_brokers()
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
        extensions = {
            "value": [
                self._create_extension(EXTENSION_TYPE_PLATFORM, "azure-iot-operations-platform", "1.0.0", "stable"),
                self._create_extension(EXTENSION_TYPE_SSC, "azure-secrets-store", "1.0.0", "stable"),
                self._create_extension(EXTENSION_TYPE_ACS, "azure-arc-containerstorage", "1.0.0", "stable"),
                self._create_extension(EXTENSION_TYPE_OPS, "azure-iot-operations", "1.0.0", "stable"),
            ]
        }
        self.responses.add(
            method=responses.GET,
            url=extensions_endpoint,
            json=extensions,
            status=200,
            content_type="application/json",
        )
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
                "tenantId": ZEROED_SUBSCRIPTION,
            }
            self.ext_identities[ext_type] = identity_id

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
        self.resource_configs["custom_location"] = mock_cl_record
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

    def add_brokers(self: C) -> C:
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
        self.resource_configs["brokers"] = [mock_broker_record]
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
    import pdb

    pdb.set_trace()
    pass
