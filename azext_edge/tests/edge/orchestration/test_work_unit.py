# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------


import json
import re
from os import environ
from pathlib import Path
from random import randint
from typing import Dict, FrozenSet, List
from unittest.mock import Mock

import pytest
import requests
import responses

from azext_edge.edge.common import INIT_NO_PREFLIGHT_ENV_KEY
from azext_edge.edge.providers.base import DEFAULT_NAMESPACE
from azext_edge.edge.providers.orchestration.common import (
    ARM_ENDPOINT,
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
from .test_template_unit import EXPECTED_EXTENSION_RESOURCE_KEYS

ZEROED_SUBSCRIPTION = get_zeroed_subscription()


path_pattern_base = r"^/subscriptions/[0-9a-fA-F-]+/resourcegroups/[a-zA-Z0-9]+"
STANDARD_HEADERS = {"content-type": "application/json"}


def build_target_scenario(cluster_name: str, resource_group_name: str, **kwargs) -> dict:
    payload = {
        "clusterName": cluster_name,
        "resourceGroup": resource_group_name,
        "cluster": {
            "name": cluster_name,
            "location": generate_random_string(),
            "properties": {
                "provisioningState": PROVISIONING_STATE_SUCCESS,
                "connectivityStatus": CONNECTIVITY_STATUS_CONNECTED,
                "totalNodeCount": 1,
            },
        },
        "namespace": {
            "value": [{"namespace": namespace, "registrationState": "Registered"} for namespace in RP_NAMESPACE_SET]
        },
        "whatIf": {"status": PROVISIONING_STATE_SUCCESS},
        "trust": {"userTrust": None},
        "enableFaultTolerance": None,
        "ensureLatest": None,
    }
    if "cluster_properties" in kwargs:
        payload["cluster"]["properties"].update(kwargs["cluster_properties"])
        kwargs.pop("cluster_properties")

    payload.update(**kwargs)
    return payload


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
    call_map: Dict[str, list] = {
        "connectivityCheck": [],
        "getCluster": [],
        "registerProviders": [],
        "whatIf": [],
        "deploy": [],
    }

    def service_generator(request: requests.PreparedRequest) -> tuple:
        method: str = request.method
        url: str = request.url
        params: dict = request.params
        path_url: str = request.path_url
        path_url = path_url.split("?")[0]
        body_str: str = request.body

        # return (status_code, headers, body)
        if method == responses.HEAD and url == ARM_ENDPOINT:
            call_map["connectivityCheck"].append(request)
            return (200, {}, None)

        if method == responses.GET:
            if path_url == (
                f"/subscriptions/{ZEROED_SUBSCRIPTION}/resourcegroups/{target_scenario['resourceGroup']}"
                f"/providers/Microsoft.Kubernetes/connectedClusters/{target_scenario['clusterName']}"
            ):
                call_map["getCluster"].append(request)
                return (200, STANDARD_HEADERS, json.dumps(target_scenario["cluster"]))

            if path_url == f"/subscriptions/{ZEROED_SUBSCRIPTION}/providers":
                call_map["registerProviders"].append(request)
                return (200, STANDARD_HEADERS, json.dumps(target_scenario["namespace"]))

        url_deployment_seg = r"/providers/Microsoft\.Resources/deployments/aziotops\.enablement\.[a-zA-Z0-9\.-]+"
        if method == responses.POST:
            if re.match(
                path_pattern_base + url_deployment_seg + r"/whatIf$",
                path_url,
            ):
                assert params["api-version"] == "2024-03-01"
                assert f"/resourcegroups/{target_scenario['resourceGroup']}/" in path_url
                assert_init_deployment_body(body_str=body_str, target_scenario=target_scenario)
                call_map["whatIf"].append(request)
                return (200, STANDARD_HEADERS, json.dumps(target_scenario["whatIf"]))

        if method == responses.PUT:
            if re.match(
                path_pattern_base + url_deployment_seg,
                path_url,
            ):
                assert params["api-version"] == "2024-03-01"
                assert f"/resourcegroups/{target_scenario['resourceGroup']}/" in path_url
                assert_init_deployment_body(body_str=body_str, target_scenario=target_scenario)
                call_map["deploy"].append(request)
                return (200, STANDARD_HEADERS, json.dumps({}))

    for method in [
        responses.GET,
        responses.HEAD,
        responses.POST,
        responses.PUT,
    ]:
        mocked_responses.add_callback(method=method, url=re.compile(r".*"), callback=service_generator)

    from azext_edge.edge.commands_edge import init

    init_call_kwargs = {
        "cmd": mocked_cmd,
        "cluster_name": target_scenario["clusterName"],
        "resource_group_name": target_scenario["resourceGroup"],
        "enable_fault_tolerance": target_scenario["enableFaultTolerance"],
        "user_trust": target_scenario["trust"]["userTrust"],
        "no_progress": True,
        "ensure_latest": target_scenario["ensureLatest"],
    }

    init_result = init(**init_call_kwargs)

    for call in call_map:
        assert len(call_map[call]) == 1


def assert_init_deployment_body(body_str: str, target_scenario: dict):
    assert body_str
    body = json.loads(body_str)
    parameters = body["properties"]["parameters"]
    assert parameters["clusterName"]["value"] == target_scenario["clusterName"]

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


# @pytest.mark.parametrize(
#     """
#     cluster_name,
#     cluster_namespace,
#     resource_group_name,
#     no_deploy,
#     no_preflight,
#     disable_rsync_rules,
#     """,
#     [
#         pytest.param(
#             generate_random_string(),  # cluster_name
#             None,  # cluster_namespace
#             generate_random_string(),  # resource_group_name
#             None,  # no_deploy
#             None,  # no_preflight
#             None,  # disable_rsync_rules
#         ),
#         pytest.param(
#             generate_random_string(),  # cluster_name
#             None,  # cluster_namespace
#             generate_random_string(),  # resource_group_name
#             None,  # no_deploy
#             None,  # no_preflight
#             None,  # disable_rsync_rules
#         ),
#         pytest.param(
#             generate_random_string(),  # cluster_name
#             generate_random_string(),  # cluster_namespace
#             generate_random_string(),  # resource_group_name
#             None,  # no_deploy
#             None,  # no_preflight
#             None,  # disable_rsync_rules
#         ),
#         pytest.param(
#             generate_random_string(),  # cluster_name
#             None,  # cluster_namespace
#             generate_random_string(),  # resource_group_name
#             None,  # no_deploy
#             None,  # no_preflight
#             None,  # disable_rsync_rules
#         ),
#         pytest.param(
#             generate_random_string(),  # cluster_name
#             None,  # cluster_namespace
#             generate_random_string(),  # resource_group_name
#             True,  # no_deploy
#             None,  # no_preflight
#             None,  # disable_rsync_rules
#         ),
#         pytest.param(
#             generate_random_string(),  # cluster_name
#             None,  # cluster_namespace
#             generate_random_string(),  # resource_group_name
#             True,  # no_deploy
#             None,  # no_preflight
#             None,  # disable_rsync_rules
#         ),
#         pytest.param(
#             generate_random_string(),  # cluster_name
#             None,  # cluster_namespace
#             generate_random_string(),  # resource_group_name
#             True,  # no_deploy
#             True,  # no_preflight
#             True,  # disable_rsync_rules
#         ),
#     ],
# )
# def test_work_order(
#     mocked_cmd: Mock,
#     mocked_config: Mock,
#     mocked_deploy_template: Mock,
#     mocked_register_providers: Mock,
#     mocked_verify_cli_client_connections: Mock,
#     mocked_edge_api_keyvault_api_v1: Mock,
#     mocked_verify_write_permission_against_rg: Mock,
#     mocked_wait_for_terminal_state: Mock,
#     mocked_connected_cluster_location: Mock,
#     mocked_connected_cluster_extensions: Mock,
#     mocked_verify_custom_locations_enabled: Mock,
#     mocked_verify_arc_cluster_config: Mock,
#     mocked_verify_custom_location_namespace: Mock,
#     spy_get_current_template_copy: Mock,
#     cluster_name,
#     cluster_namespace,
#     resource_group_name,
#     no_deploy,
#     no_preflight,
#     disable_rsync_rules,
#     spy_work_displays,
# ):
#     # TODO: Refactor for simplification

#     call_kwargs = {
#         "cmd": mocked_cmd,
#         "cluster_name": cluster_name,
#         "resource_group_name": resource_group_name,
#         "no_deploy": no_deploy,
#         "no_progress": True,
#         "disable_rsync_rules": disable_rsync_rules,
#         "wait_sec": 0.25,
#     }

#     if no_preflight:
#         environ[INIT_NO_PREFLIGHT_ENV_KEY] = "true"

#     for param_with_default in [
#         (cluster_namespace, "cluster_namespace"),
#     ]:
#         if param_with_default[0]:
#             call_kwargs[param_with_default[1]] = param_with_default[0]

#     result = init(**call_kwargs)
#     expected_template_copies = 0

#     # TODO - @digimaun
#     # nothing_to_do = all([not keyvault_resource_id, no_tls, no_deploy, no_preflight])
#     # if nothing_to_do:
#     #     assert not result
#     #     mocked_verify_cli_client_connections.assert_not_called()
#     #     mocked_edge_api_keyvault_api_v1.is_deployed.assert_not_called()
#     #     return

#     # if any([not no_preflight, not no_deploy, keyvault_resource_id]):
#     #     mocked_verify_cli_client_connections.assert_called_once()
#     #     mocked_connected_cluster_location.assert_called_once()

#     expected_cluster_namespace = cluster_namespace.lower() if cluster_namespace else DEFAULT_NAMESPACE

#     displays_to_eval = []
#     for category_tuple in [
#         (not no_preflight, WorkCategoryKey.PRE_FLIGHT),
#         # (keyvault_resource_id, WorkCategoryKey.CSI_DRIVER),
#         (not no_deploy, WorkCategoryKey.DEPLOY_AIO),
#     ]:
#         if category_tuple[0]:
#             displays_to_eval.append(category_tuple[1])
#     _assert_displays_for(set(displays_to_eval), spy_work_displays)

#     if not no_preflight:
#         expected_template_copies += 1
#         mocked_register_providers.assert_called_once()
#         mocked_verify_custom_locations_enabled.assert_called_once()
#         mocked_connected_cluster_extensions.assert_called_once()
#         mocked_verify_arc_cluster_config.assert_called_once()
#         mocked_verify_custom_location_namespace.assert_called_once()

#         if not disable_rsync_rules:
#             mocked_verify_write_permission_against_rg.assert_called_once()
#             mocked_verify_write_permission_against_rg.call_args.kwargs["subscription_id"]
#             mocked_verify_write_permission_against_rg.call_args.kwargs["resource_group_name"] == resource_group_name
#         else:
#             mocked_verify_write_permission_against_rg.assert_not_called()
#     else:
#         mocked_register_providers.assert_not_called()
#         mocked_verify_custom_locations_enabled.assert_not_called()
#         mocked_connected_cluster_extensions.assert_not_called()
#         mocked_verify_arc_cluster_config.assert_not_called()
#         mocked_verify_custom_location_namespace.assert_not_called()

#     if not no_deploy:
#         expected_template_copies += 1
#         assert result["deploymentName"]
#         assert result["resourceGroup"] == resource_group_name
#         assert result["clusterName"] == cluster_name
#         assert result["clusterNamespace"]
#         assert result["deploymentLink"]
#         assert result["deploymentState"]
#         assert result["deploymentState"]["status"]
#         assert result["deploymentState"]["correlationId"]
#         assert result["deploymentState"]["opsVersion"] == CURRENT_TEMPLATE.get_component_vers()
#         assert result["deploymentState"]["timestampUtc"]
#         assert result["deploymentState"]["timestampUtc"]["started"]
#         assert result["deploymentState"]["timestampUtc"]["ended"]
#         assert "resources" in result["deploymentState"]

#         assert mocked_deploy_template.call_count == 2
#         assert mocked_deploy_template.call_args.kwargs["template"]
#         assert mocked_deploy_template.call_args.kwargs["parameters"]
#         assert mocked_deploy_template.call_args.kwargs["subscription_id"]
#         assert mocked_deploy_template.call_args.kwargs["resource_group_name"] == resource_group_name
#         assert mocked_deploy_template.call_args.kwargs["deployment_name"]
#         assert mocked_deploy_template.call_args.kwargs["cluster_name"] == cluster_name
#         assert mocked_deploy_template.call_args.kwargs["cluster_namespace"] == expected_cluster_namespace
#     else:
#         pass
# if not nothing_to_do and result:
#     assert "deploymentName" not in result
#     assert "resourceGroup" not in result
#     assert "clusterName" not in result
#     assert "clusterNamespace" not in result
#     assert "deploymentLink" not in result
#     assert "deploymentState" not in result
# TODO
# mocked_deploy_template.assert_not_called()

# assert spy_get_current_template_copy.call_count == expected_template_copies


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
