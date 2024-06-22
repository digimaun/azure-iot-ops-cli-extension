# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

import json
from time import sleep
from typing import TYPE_CHECKING, List, Tuple

from knack.log import get_logger

from ...constants import USER_AGENT
from .common import ensure_azure_namespace_path

ensure_azure_namespace_path()

from azure.core.pipeline.policies import HttpLoggingPolicy, UserAgentPolicy
from azure.identity import AzureCliCredential, ClientSecretCredential
from azure.mgmt.authorization import AuthorizationManagementClient
from azure.mgmt.resource import ResourceManagementClient

AZURE_CLI_CREDENTIAL = AzureCliCredential()

POLL_RETRIES = 240
POLL_WAIT_SEC = 15

logger = get_logger(__name__)
# logger.setLevel(logging.INFO)


if TYPE_CHECKING:
    from azure.core.polling import LROPoller
    from azure.mgmt.resource.resources.models import GenericResource


def get_resource_client(subscription_id: str, **kwargs) -> ResourceManagementClient:
    if "http_logging_policy" not in kwargs:
        kwargs["http_logging_policy"] = get_default_logging_policy()

    return ResourceManagementClient(
        credential=AZURE_CLI_CREDENTIAL,
        subscription_id=subscription_id,
        user_agent_policy=UserAgentPolicy(user_agent=USER_AGENT),
        **kwargs,
    )


def get_authz_client(subscription_id: str) -> AuthorizationManagementClient:
    return AuthorizationManagementClient(
        credential=AZURE_CLI_CREDENTIAL,
        subscription_id=subscription_id,
        user_agent_policy=UserAgentPolicy(user_agent=USER_AGENT),
    )


def get_token_from_sp_credential(tenant_id: str, client_id: str, client_secret: str, scope: str) -> str:
    client_secret_cred = ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
    return client_secret_cred.get_token(scope).token


def wait_for_terminal_state(poller: "LROPoller") -> "GenericResource":
    # resource client does not handle sigint well
    counter = 0
    while counter < POLL_RETRIES:
        sleep(POLL_WAIT_SEC)
        counter = counter + 1
        if poller.done():
            break
    return poller.result()


def wait_for_terminal_states(
    *pollers: "LROPoller", retries: int = POLL_RETRIES, wait_sec: int = POLL_WAIT_SEC
) -> Tuple["LROPoller"]:
    counter = 0
    while counter < retries:
        sleep(wait_sec)
        counter = counter + 1
        batch_done = all(poller.done() for poller in pollers)
        if batch_done:
            break

    return pollers


def get_tenant_id() -> str:
    from azure.cli.core._profile import Profile

    profile = Profile()
    sub = profile.get_subscription()
    return sub["tenantId"]


def get_default_logging_policy() -> HttpLoggingPolicy:
    http_logging_policy = HttpLoggingPolicy(logger=logger)
    http_logging_policy.allowed_query_params.add("api-version")
    http_logging_policy.allowed_query_params.add("$filter")
    http_logging_policy.allowed_query_params.add("$expand")

    return http_logging_policy


class AzMicroMgmtClient:
    def __init__(self, subscription_id: str, **kwargs):
        self.resource_client = get_resource_client(subscription_id, **kwargs)

    @classmethod
    def _get_response_body_text(cls, response, _, *args, **kwargs) -> str:
        """
        Apply when requiring the raw response body, to avoid set model deserialization.
        """
        return response.http_response.text()

    @classmethod
    def _enumerate_models(cls, models: list, *args, **kwargs) -> List[dict]:
        unpacked = []
        for model in models:
            m = model.as_dict()
            m.update(model.additional_properties)
            unpacked.append(m)

        return unpacked

    def get_resource_by_id(self, resource_id: str, api_version: str) -> dict:
        text = self.resource_client.resources.get_by_id(
            resource_id=resource_id,
            api_version=api_version,
            cls=AzMicroMgmtClient._get_response_body_text,
        )
        return json.loads(text)

    def list_resources(self, resource_type: str) -> List[dict]:
        model_iterator = self.resource_client.resources.list(
            filter=f"resourceType eq '{resource_type}'",
            expand="properties,etag",
            cls=AzMicroMgmtClient._enumerate_models,
        )
        # Enumerate models
        return list(model_iterator)
