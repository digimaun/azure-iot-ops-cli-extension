# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

from typing import Dict, List, Optional, TypeVar
from unittest.mock import Mock

import pytest
import responses
from azure.cli.core.azclierror import (
    ArgumentUsageError,
    AzureResponseError,
    RequiredArgumentMissingError,
)
from azure.core.exceptions import HttpResponseError

from azext_edge.edge.providers.orchestration.common import (
    EXTENSION_TYPE_ACS,
    EXTENSION_TYPE_OPS,
    EXTENSION_TYPE_OSM,
    EXTENSION_TYPE_PLATFORM,
    EXTENSION_TYPE_SSC,
    EXTENSION_TYPE_TO_MONIKER_MAP,
)
from azext_edge.edge.providers.orchestration.targets import InitTargets

from ...generators import generate_random_string, get_zeroed_subscription
from .resources.conftest import (
    BASE_URL,
    CONNECTED_CLUSTER_API_VERSION,
    CLUSTER_EXTENSIONS_API_VERSION,
    get_base_endpoint,
    get_mock_resource,
)
from .resources.test_instances_unit import (
    get_instance_endpoint,
    get_mock_cl_record,
    get_mock_instance_record,
)

T = TypeVar("T", bound="UpgradeScenario")


def get_mock_cluster_record(resource_group_name: str, name: str = "mycluster") -> dict:
    return get_mock_resource(
        name=name,
        properties={},
        resource_group_name=resource_group_name,
    )


def get_cluster_endpoint(resource_group_name: str, name: str = "mycluster") -> dict:
    resource_path = "/connectedClusters"
    if name:
        resource_path += f"/{name}"
    endpoint = get_base_endpoint(
        resource_group_name=resource_group_name,
        resource_path=resource_path,
        resource_provider="Microsoft.Kubernetes",
        api_version=CONNECTED_CLUSTER_API_VERSION,
    )
    endpoint = endpoint.replace("/resourceGroups/", "/resourcegroups/", 1)
    return endpoint


def get_cluster_extensions_endpoint(resource_group_name: str, cluster_name: str = "mycluster") -> dict:
    resource_path = f"/connectedClusters/{cluster_name}/providers/Microsoft.KubernetesConfiguration/extensions"
    return get_base_endpoint(
        resource_group_name=resource_group_name,
        resource_path=resource_path,
        resource_provider="Microsoft.Kubernetes",
        api_version=CLUSTER_EXTENSIONS_API_VERSION,
    )


class UpgradeScenario:
    def __init__(self):
        self.extensions: Dict[str, dict] = {}
        self.targets = InitTargets(cluster_name=generate_random_string(), resource_group_name=generate_random_string())
        self.init_version_map: Dict[str, dict] = {}
        self.init_version_map.update(self.targets.get_extension_versions())
        self.init_version_map.update(self.targets.get_extension_versions(False))
        self._build_defaults()

    def _build_defaults(self):
        for ext_type in EXTENSION_TYPE_TO_MONIKER_MAP:
            self.extensions[ext_type] = {
                "properties": {
                    "extensionType": ext_type,
                    "version": self.init_version_map[EXTENSION_TYPE_TO_MONIKER_MAP[ext_type]]["version"],
                    "releaseTrain": self.init_version_map[EXTENSION_TYPE_TO_MONIKER_MAP[ext_type]]["train"],
                    "configurationSettings": {},
                },
                "name": EXTENSION_TYPE_TO_MONIKER_MAP[ext_type],
            }

    def set_extension(self: T, ext_type: str, ext_vers: Optional[str] = None, ext_train: Optional[str] = None) -> T:
        if ext_vers:
            self.extensions[ext_type]["properties"]["version"] = ext_vers
        if ext_train:
            self.extensions[ext_type]["properties"]["releaseTrain"] = ext_train
        return self

    def set_get_instance_mock(
        self: T, mocked_responses: responses, instance_name: str, resource_group_name: str, status_code: int = 200
    ):
        mock_instance_record = get_mock_instance_record(name=instance_name, resource_group_name=resource_group_name)
        mocked_responses.add(
            method=responses.GET,
            url=get_instance_endpoint(resource_group_name=resource_group_name, instance_name=instance_name),
            json=mock_instance_record,
            status=status_code,
            content_type="application/json",
        )

        cl_name = generate_random_string()
        mock_cl_record = get_mock_cl_record(name=cl_name, resource_group_name=resource_group_name)
        mocked_responses.add(
            method=responses.GET,
            url=f"{BASE_URL}{mock_instance_record['extendedLocation']['name']}",
            json=mock_cl_record,
            status=status_code,
            content_type="application/json",
        )

        mock_cluster_record = get_mock_cluster_record(resource_group_name=resource_group_name)
        mocked_responses.add(
            method=responses.GET,
            url=get_cluster_endpoint(resource_group_name=resource_group_name),
            json=mock_cluster_record,
            status=status_code,
            content_type="application/json",
        )

    def get_extensions_response(self) -> List[dict]:
        return list(self.extensions.values())


def build_target_scenario(**kwargs) -> dict:
    scenario_payload = {}

    if "ops_config" in kwargs:
        scenario_payload["ops_config"] = kwargs["ops_config"]

    # ops_config=ops_config,
    # ops_version=ops_version,
    # ops_train=ops_train,
    # acs_config=acs_config,
    # acs_version=acs_version,
    # acs_train=acs_train,
    # osm_config=osm_config,
    # osm_version=osm_version,
    # osm_train=osm_train,
    # ssc_config=ssc_config,
    # ssc_version=ssc_version,
    # ssc_train=ssc_train,
    # plat_config=plat_config,
    # plat_version=plat_version,
    # plat_train=plat_train,


@pytest.mark.parametrize("no_progress", [True])
@pytest.mark.parametrize(
    "target_scenario",
    [UpgradeScenario()],
)
def test_ops_upgrade(
    mocked_cmd: Mock,
    mocked_responses: responses,
    target_scenario: UpgradeScenario,
    no_progress: bool,
):
    from azext_edge.edge.commands_edge import upgrade_instance

    resource_group_name = generate_random_string()
    instance_name = generate_random_string()

    target_scenario.set_get_instance_mock(
        mocked_responses=mocked_responses, instance_name=instance_name, resource_group_name=resource_group_name
    )

    call_kwargs = {
        "cmd": mocked_cmd,
        "resource_group_name": resource_group_name,
        "instance_name": instance_name,
        "confirm_yes": True,
    }
    import pdb

    pdb.set_trace()
    upgrade_result = upgrade_instance(**call_kwargs)
    import pdb

    pdb.set_trace()

    # cmd=cmd,
    # resource_group_name=resource_group_name,
    # instance_name=instance_name,
    # no_progress=no_progress,
    # confirm_yes=confirm_yes,
    # ops_config=ops_config,
    # ops_version=ops_version,
    # ops_train=ops_train,
    # acs_config=acs_config,
    # acs_version=acs_version,
    # acs_train=acs_train,
    # osm_config=osm_config,
    # osm_version=osm_version,
    # osm_train=osm_train,
    # ssc_config=ssc_config,
    # ssc_version=ssc_version,
    # ssc_train=ssc_train,
    # plat_config=plat_config,
    # plat_version=plat_version,
    # plat_train=plat_train,
