# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

import pytest
from ..generators import generate_random_string

AZ_CLIENT_PATH = "azext_edge.edge.util.az_client"


@pytest.mark.parametrize("done", [True, False])
def test_wait_for_terminal_state(mocker, done):
    # could be fixture with param
    sleep_patch = mocker.patch(f"{AZ_CLIENT_PATH}.sleep")
    poll_num = 10
    mocker.patch(f"{AZ_CLIENT_PATH}.POLL_RETRIES", poll_num)

    poller = mocker.Mock()
    poller.done.return_value = done
    poller.result.return_value = generate_random_string()

    from azext_edge.edge.providers.orchestration.base import wait_for_terminal_state

    result = wait_for_terminal_state(poller)
    assert result == poller.result.return_value
    assert sleep_patch.call_count == (1 if done else poll_num)
