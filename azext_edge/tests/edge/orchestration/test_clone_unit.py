# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

import re
from typing import Optional, TypeVar
from unittest.mock import Mock

import pytest
import responses
from .resources.test_instances_unit import (
    get_instance_endpoint,
    get_mock_instance_record,
)
from .resources.test_custom_locations_unit import (
    get_custom_location_endpoint,
    get_mock_custom_location_record,
)
from azext_edge.edge.util.id_tools import parse_resource_id


from azext_edge.edge.providers.orchestration.clone import CloneManager

from ...generators import generate_random_string


C = TypeVar("C", bound="CloneScenario")


class CloneScenario:
    def __init__(self, description: str = None):
        self.description = description

    def bootstrap(self: C, mocked_responses: responses, instance_name: str, resource_group_name: str):
        self.responses = mocked_responses
        # self.responses.assert_all_requests_are_fired = False
        self.instance_name = instance_name
        self.resource_group_name = resource_group_name
        self.cl_name = generate_random_string()

        mock_instance_record = get_mock_instance_record(
            name=self.instance_name, resource_group_name=resource_group_name, cl_name=self.cl_name
        )
        mocked_responses.add(
            method=responses.GET,
            url=get_instance_endpoint(resource_group_name=resource_group_name, instance_name=self.instance_name),
            json=mock_instance_record,
            status=200,
            content_type="application/json",
        )
        mock_cl_record = get_mock_custom_location_record(name=self.cl_name, resource_group_name=resource_group_name)
        mocked_responses.add(
            method=responses.GET,
            url=get_custom_location_endpoint(
                resource_group_name=resource_group_name, custom_location_name=self.cl_name
            ),
            json=mock_cl_record,
            status=200,
            content_type="application/json",
        )

        # mocked_responses.add_callback(
        #     method=responses.PATCH,
        #     url=re.compile(CLUSTER_EXTENSIONS_URL_MATCH_RE),
        #     callback=self.patch_extension_response,
        # )

    def add_instance_min(self: C) -> C:
        self.add_cluster()
        self.add_extension()
        self.add_custom_location()
        self.add_instance()
        self.add_broker()
        self.add_listener()
        self.add_authn()
        self.add_dataflow_profile()
        self.add_dataflow_endpoint()
        return self

    def add_cluster(self: C) -> C:
        pass

    def add_extension(self: C) -> C:
        pass

    def add_custom_location(self: C) -> C:
        pass

    def add_instance(self: C) -> C:
        pass

    def add_broker(self: C) -> C:
        pass

    def add_listener(self: C) -> C:
        pass

    def add_authn(self: C) -> C:
        pass

    def add_authz(self: C) -> C:
        pass

    def add_dataflow(self: C) -> C:
        pass

    def add_dataflow_profile(self: C) -> C:
        pass

    def add_dataflow_endpoint(self: C) -> C:
        pass

    def add_asset(self: C) -> C:
        pass

    def add_aep(self: C) -> C:
        pass


@pytest.mark.parametrize("clone_scenario", [CloneScenario().add_instance_min()])
def test_clone_manager(
    mocked_cmd: Mock,
    mocked_responses: responses,
    clone_scenario: CloneScenario,
):
    instance_name = generate_random_string()
    resource_group_name = generate_random_string()

    clone_scenario.bootstrap(mocked_responses, resource_group_name=resource_group_name, instance_name=instance_name)

    clone_manager = CloneManager(
        cmd=mocked_cmd, resource_group_name=resource_group_name, instance_name=instance_name, no_progress=True
    )
    import pdb

    pdb.set_trace()
    pass
