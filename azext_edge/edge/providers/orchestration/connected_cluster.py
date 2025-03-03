# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

from ...util.az_client import get_resource_client

CONNECTED_CLUSTER_API_VERSION = "2022-10-01-preview"


class ConnectedCluster:
    def __init__(self, subscription_id: str, cluster_name: str, resource_group_name: str):
        self.subscription_id = subscription_id
        self.cluster_name = cluster_name
        self.resource_group_name = resource_group_name
        self.resource_client = get_resource_client(self.subscription_id)

    @property
    def resource(self) -> dict:
        return self.resource_client.resources.get_by_id(
            resource_id=f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group_name}"
            f"/providers/Microsoft.Kubernetes/connectedClusters/{self.cluster_name}",
            api_version=CONNECTED_CLUSTER_API_VERSION,
        ).as_dict()

    @property
    def location(self) -> str:
        return self.resource["location"]
