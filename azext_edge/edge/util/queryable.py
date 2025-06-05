# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

from typing import List, Optional, Union

from .az_client import get_resource_client
from .resource_graph import ResourceGraph


GRAPH_ENDPOINT = "https://graph.microsoft.com/"
GRAPH_V1_ENDPOINT = f"{GRAPH_ENDPOINT}v1.0"
GRAPH_V1_SP_ENDPOINT = f"{GRAPH_V1_ENDPOINT}/servicePrincipals"


class Queryable:
    def __init__(self, cmd, subscriptions: Optional[List] = None):
        from azure.cli.core.commands.client_factory import get_subscription_id

        self.cmd = cmd
        self.default_subscription_id: str = get_subscription_id(cli_ctx=cmd.cli_ctx)

        if not subscriptions:
            subscriptions = [self.default_subscription_id]

        self.subscriptions = subscriptions
        self.resource_graph = ResourceGraph(cmd=cmd, subscriptions=self.subscriptions)
        self.resource_client = get_resource_client(subscription_id=self.default_subscription_id)

    def _process_query_result(self, result: dict, first: bool = False) -> Optional[Union[dict, List[dict]]]:
        if "data" in result:
            if result["data"] and first:
                return result["data"][0]
            return result["data"]

    def query(self, query: str, first: bool = False) -> Optional[Union[dict, List[dict]]]:
        return self._process_query_result(result=self.resource_graph.query_resources(query=query), first=first)

    def get_resource_group(self, name: str) -> dict:
        return self.resource_client.resource_groups.get(resource_group_name=name)

    def get_sp_id(self, app_id: str) -> Optional[str]:
        """
        Attempts to fetch the service principal Id by app Id from the Microsoft Graph API.
        """
        from azure.cli.core.util import send_raw_request

        # See if we can fetch the RP OID.
        try:
            sp_response = send_raw_request(
                cli_ctx=self.cmd.cli_ctx,
                method="GET",
                url=f"{GRAPH_V1_SP_ENDPOINT}(appId='{app_id}')",
            ).json()
            return sp_response.get("id", "").lower()
        except Exception:
            # If not, bail without throwing.
            pass
