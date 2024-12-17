# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

import json
import re
from typing import Dict, List, NamedTuple, Optional, Tuple, TypeVar
from unittest.mock import Mock

import pytest
import requests
import responses
from azure.cli.core.azclierror import (
    ArgumentUsageError,
    AzureResponseError,
    RequiredArgumentMissingError,
)
from azure.core.exceptions import HttpResponseError

from azext_edge.edge.providers.orchestration.common import (
    EXTENSION_MONIKER_TO_ALIAS_MAP,
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
    CLUSTER_EXTENSIONS_API_VERSION,
    CLUSTER_EXTENSIONS_URL_MATCH_RE,
    CONNECTED_CLUSTER_API_VERSION,
    get_base_endpoint,
    get_mock_resource,
)
from .resources.test_instances_unit import (
    get_instance_endpoint,
    get_mock_cl_record,
    get_mock_instance_record,
)

T = TypeVar("T", bound="UpgradeScenario")
STANDARD_HEADERS = {"content-type": "application/json"}


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


@pytest.fixture
def mocked_logger(mocker):
    yield mocker.patch(
        "azext_edge.edge.providers.orchestration.upgrade2.logger",
    )


class OverrideRequest(NamedTuple):
    extension_type: str
    extension_version: str
    extension_train: str


class ExpectedExtPatch(NamedTuple):
    extension_type: str
    extension_version: str
    extension_train: str
    settings: Optional[Dict[str, str]] = None


class UpgradeScenario:
    def __init__(self):
        self.extensions: Dict[str, dict] = {}
        self.targets = InitTargets(cluster_name=generate_random_string(), resource_group_name=generate_random_string())
        self.init_version_map: Dict[str, dict] = {}
        self.init_version_map.update(self.targets.get_extension_versions())
        self.init_version_map.update(self.targets.get_extension_versions(False))
        self.user_kwargs: Dict[str, dict] = {}
        self.patch_record: Dict[str, dict] = {}
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

    def set_user_kwargs(self: T, **kwargs):
        self.user_kwargs.update(kwargs)
        return self

    def set_extension(self: T, ext_type: str, ext_vers: Optional[str] = None, ext_train: Optional[str] = None) -> T:
        if ext_vers:
            self.extensions[ext_type]["properties"]["version"] = ext_vers
        if ext_train:
            self.extensions[ext_type]["properties"]["releaseTrain"] = ext_train
        return self

    def set_expected_ext_kpis(self: T, *ext_kpis: Tuple[str, Optional[str], Optional[str], Optional[Dict[str, str]]]):
        for ext_kpi in ext_kpis:
            self.expected_kpis[ext_kpi[0]] = (ext_kpi[1], ext_kpi[2], ext_kpi[3])

    def set_instance_mock(
        self: T, mocked_responses: responses, instance_name: str, resource_group_name: str, status_code: int = 200
    ):
        mocked_responses.assert_all_requests_are_fired = False
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

        mocked_responses.add(
            method=responses.GET,
            url=get_cluster_extensions_endpoint(resource_group_name=resource_group_name),
            json={"value": self.get_extensions()},
            status=status_code,
            content_type="application/json",
        )
        mocked_responses.add_callback(
            method=responses.PATCH,
            url=re.compile(CLUSTER_EXTENSIONS_URL_MATCH_RE),
            callback=self.patch_extension_response,
        )

    def patch_extension_response(self, request: requests.PreparedRequest) -> Optional[tuple]:
        body = json.loads(request.body)
        ext_moniker = request.path_url.split("?")[0].split("/")[-1]
        for ext_type in EXTENSION_TYPE_TO_MONIKER_MAP:
            if EXTENSION_TYPE_TO_MONIKER_MAP[ext_type] == ext_moniker:
                body["properties"]["extensionType"] = ext_type
                self.patch_record[ext_type] = body

        return (200, STANDARD_HEADERS, json.dumps(body))

    def get_extensions(self) -> List[dict]:
        return list(self.extensions.values())


@pytest.mark.parametrize("no_progress", [True])
@pytest.mark.parametrize(
    "target_scenario,patched_ext_types",
    [
        (UpgradeScenario(), []),
        (
            UpgradeScenario().set_extension(ext_type=EXTENSION_TYPE_PLATFORM, ext_vers="1.0.0"),
            [],
        ),
        (
            UpgradeScenario().set_extension(ext_type=EXTENSION_TYPE_PLATFORM, ext_vers="0.5.0"),
            [EXTENSION_TYPE_PLATFORM],
        ),
        (
            UpgradeScenario()
            .set_extension(ext_type=EXTENSION_TYPE_PLATFORM, ext_vers="0.5.0")
            .set_extension(ext_type=EXTENSION_TYPE_OPS, ext_vers="0.2.0")
            .set_extension(ext_type=EXTENSION_TYPE_OSM, ext_vers="0.3.0"),
            [EXTENSION_TYPE_PLATFORM, EXTENSION_TYPE_OPS, EXTENSION_TYPE_OSM],
        ),
        (
            UpgradeScenario().set_user_kwargs(ops_config=[f"{generate_random_string()}={generate_random_string()}"]),
            [EXTENSION_TYPE_OPS],
        ),
    ],
)
def test_ops_upgrade(
    mocked_cmd: Mock,
    mocked_responses: responses,
    target_scenario: UpgradeScenario,
    patched_ext_types: List[str],
    no_progress: bool,
    mocked_logger: Mock,
    mocked_sleep: Mock,
):
    from azext_edge.edge.commands_edge import upgrade_instance

    resource_group_name = generate_random_string()
    instance_name = generate_random_string()

    target_scenario.set_instance_mock(
        mocked_responses=mocked_responses, instance_name=instance_name, resource_group_name=resource_group_name
    )
    call_kwargs = {
        "cmd": mocked_cmd,
        "resource_group_name": resource_group_name,
        "instance_name": instance_name,
        "no_progress": no_progress,
        "confirm_yes": True,
    }
    call_kwargs.update(target_scenario.user_kwargs)

    upgrade_result = upgrade_instance(**call_kwargs)

    if not patched_ext_types:
        assert upgrade_result is None
        mocked_logger.warning.assert_called_once_with("Nothing to upgrade :)")
        return

    assert_patch_order(upgrade_result)


def assert_patch_order(upgrade_result: List[dict]):
    order_map = {}
    index = 0
    for key in EXTENSION_TYPE_TO_MONIKER_MAP:
        order_map[key] = index
        index = index + 1

    last_index = -1
    for patched_ext in upgrade_result:
        current_index = order_map[patched_ext["properties"]["extensionType"]]
        assert current_index > last_index
        last_index = current_index

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
