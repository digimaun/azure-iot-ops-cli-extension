# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

import json
from pathlib import Path

import pytest

from ...generators import generate_random_string

MOCK_BROKER_CONFIG_PATH = Path(__file__).parent.joinpath("./broker_config.json")


@pytest.fixture
def mocked_resource_graph(mocker):
    patched = mocker.patch("azext_edge.edge.providers.orchestration.connected_cluster.ResourceGraph", autospec=True)
    yield patched


@pytest.fixture(scope="module")
def mock_broker_config():
    custom_config = {generate_random_string(): generate_random_string()}
    MOCK_BROKER_CONFIG_PATH.write_text(json.dumps(custom_config), encoding="utf-8")
    yield custom_config
    MOCK_BROKER_CONFIG_PATH.unlink()


@pytest.fixture
def mocked_verify_cli_client_connections(mocker):
    patched = mocker.patch("azext_edge.edge.providers.orchestration.host.verify_cli_client_connections", autospec=True)
    yield patched
