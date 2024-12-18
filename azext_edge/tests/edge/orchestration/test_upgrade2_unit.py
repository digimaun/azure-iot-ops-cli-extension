# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

import json
import re
from typing import Dict, List, Optional, Tuple, TypeVar
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
    EXTENSION_ALIAS_TO_TYPE_MAP,
    EXTENSION_MONIKER_TO_ALIAS_MAP,
    EXTENSION_TYPE_ACS,
    EXTENSION_TYPE_OPS,
    EXTENSION_TYPE_OSM,
    EXTENSION_TYPE_PLATFORM,
    EXTENSION_TYPE_SSC,
    EXTENSION_TYPE_TO_MONIKER_MAP,
)
from azext_edge.edge.providers.orchestration.targets import InitTargets
from azext_edge.edge.util import parse_kvp_nargs

from ...generators import generate_random_string
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
        properties={"connectivityStatus": "Connected"},
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


class UpgradeScenario:
    def __init__(self, description: Optional[str] = None):
        self.extensions: Dict[str, dict] = {}
        self.targets = InitTargets(cluster_name=generate_random_string(), resource_group_name=generate_random_string())
        self.init_version_map: Dict[str, dict] = {}
        self.init_version_map.update(self.targets.get_extension_versions())
        self.init_version_map.update(self.targets.get_extension_versions(False))
        self.user_kwargs: Dict[str, dict] = {}
        self.patch_record: Dict[str, dict] = {}
        self.ext_type_response_map: Dict[str, Tuple[int, Optional[dict]]] = {}
        self.expect_exception = False
        self.description = description
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

    def set_response_on_patch(self: T, ext_type: str, code: int = 200, body: Optional[dict] = None) -> T:
        if code not in (200, 202):
            self.expect_exception = True
        self.ext_type_response_map[ext_type] = (code, body)
        return self

    def set_instance_mock(self: T, mocked_responses: responses, instance_name: str, resource_group_name: str):
        mocked_responses.assert_all_requests_are_fired = False
        mock_instance_record = get_mock_instance_record(name=instance_name, resource_group_name=resource_group_name)
        mocked_responses.add(
            method=responses.GET,
            url=get_instance_endpoint(resource_group_name=resource_group_name, instance_name=instance_name),
            json=mock_instance_record,
            status=200,
            content_type="application/json",
        )

        cl_name = generate_random_string()
        mock_cl_record = get_mock_cl_record(name=cl_name, resource_group_name=resource_group_name)
        mocked_responses.add(
            method=responses.GET,
            url=f"{BASE_URL}{mock_instance_record['extendedLocation']['name']}",
            json=mock_cl_record,
            status=200,
            content_type="application/json",
        )

        mock_cluster_record = get_mock_cluster_record(resource_group_name=resource_group_name)
        mocked_responses.add(
            method=responses.GET,
            url=get_cluster_endpoint(resource_group_name=resource_group_name),
            json=mock_cluster_record,
            status=200,
            content_type="application/json",
        )

        mocked_responses.add(
            method=responses.GET,
            url=get_cluster_extensions_endpoint(resource_group_name=resource_group_name),
            json={"value": self.get_extensions()},
            status=200,
            content_type="application/json",
        )
        mocked_responses.add_callback(
            method=responses.PATCH,
            url=re.compile(CLUSTER_EXTENSIONS_URL_MATCH_RE),
            callback=self.patch_extension_response,
        )

    def patch_extension_response(self, request: requests.PreparedRequest) -> Optional[tuple]:
        ext_moniker = request.path_url.split("?")[0].split("/")[-1]
        for ext_type in EXTENSION_TYPE_TO_MONIKER_MAP:
            if EXTENSION_TYPE_TO_MONIKER_MAP[ext_type] == ext_moniker:
                status_code, response_body = self.ext_type_response_map.get(ext_type) or (
                    200,
                    json.loads(request.body),
                )
                if "properties" in response_body:
                    response_body["properties"]["extensionType"] = ext_type
                self.patch_record[ext_type] = response_body
                return (status_code, STANDARD_HEADERS, json.dumps(response_body))

        return (502, STANDARD_HEADERS, json.dumps({"error": "server error"}))

    def get_extensions(self) -> List[dict]:
        return list(self.extensions.values())


@pytest.mark.parametrize("no_progress", [True])
@pytest.mark.parametrize(
    "target_scenario,expected_patched_ext_types",
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
        (
            UpgradeScenario().set_user_kwargs(ops_version="1.2.3"),
            [EXTENSION_TYPE_OPS],
        ),
        (
            UpgradeScenario().set_user_kwargs(ops_train=f"{generate_random_string()}"),
            [EXTENSION_TYPE_OPS],
        ),
        (
            UpgradeScenario()
            .set_extension(ext_type=EXTENSION_TYPE_SSC, ext_vers="0.1.0")
            .set_extension(ext_type=EXTENSION_TYPE_OPS, ext_vers="0.1.0")
            .set_extension(ext_type=EXTENSION_TYPE_ACS, ext_vers="9.9.9")
            .set_user_kwargs(
                acs_config=[f"{generate_random_string()}={generate_random_string()}"],
                acs_version="1.1.1",
                acs_train=generate_random_string(),
            ),
            [EXTENSION_TYPE_ACS, EXTENSION_TYPE_OPS, EXTENSION_TYPE_SSC],
        ),
        (
            UpgradeScenario()
            .set_extension(ext_type=EXTENSION_TYPE_PLATFORM, ext_vers="0.5.0")
            .set_response_on_patch(ext_type=EXTENSION_TYPE_PLATFORM, code=500, body={"error": "server error"}),
            [EXTENSION_TYPE_PLATFORM],
        ),
    ],
)
def test_ops_upgrade(
    mocked_cmd: Mock,
    mocked_responses: responses,
    target_scenario: UpgradeScenario,
    expected_patched_ext_types: List[str],
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

    if target_scenario.expect_exception:
        with pytest.raises(HttpResponseError):
            upgrade_instance(**call_kwargs)
        return

    upgrade_result = upgrade_instance(**call_kwargs)

    if not expected_patched_ext_types:
        assert upgrade_result is None
        mocked_logger.warning.assert_called_once_with("Nothing to upgrade :)")
        return

    assert upgrade_result
    assert len(upgrade_result) == len(expected_patched_ext_types)

    assert_patch_order(upgrade_result, expected_patched_ext_types)
    assert_overrides(target_scenario, upgrade_result)


def assert_overrides(target_scenario: UpgradeScenario, upgrade_result: List[dict]):
    user_kwargs = target_scenario.user_kwargs
    result_type_to_payload = {k["properties"]["extensionType"]: k for k in upgrade_result}

    for moniker in EXTENSION_MONIKER_TO_ALIAS_MAP:
        alias = EXTENSION_MONIKER_TO_ALIAS_MAP[moniker]
        ext_type = EXTENSION_ALIAS_TO_TYPE_MAP[alias]
        config = user_kwargs.get(f"{alias}_config")
        if config:
            parsed_config = parse_kvp_nargs(config)
            assert result_type_to_payload[ext_type]["properties"]["configurationSettings"] == parsed_config
        version = user_kwargs.get(f"{alias}_version")
        if version:
            assert result_type_to_payload[ext_type]["properties"]["version"] == version
        release_train = user_kwargs.get(f"{alias}_train")
        if release_train:
            assert result_type_to_payload[ext_type]["properties"]["releaseTrain"] == release_train


def assert_patch_order(upgrade_result: List[dict], expected_types: List[str]):
    result_type_to_payload = {k["properties"]["extensionType"]: k for k in upgrade_result}
    for ext_type in expected_types:
        assert ext_type in result_type_to_payload

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
