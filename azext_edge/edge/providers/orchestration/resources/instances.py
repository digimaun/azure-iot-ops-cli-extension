# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

from typing import Iterable, Optional, NamedTuple

from knack.log import get_logger
from rich import print

from ....util.az_client import (
    get_iotops_mgmt_client,
    get_custom_locations_mgmt_client,
    parse_resource_id,
    wait_for_terminal_state,
)
from ....util.queryable import Queryable
from ..common import CUSTOM_LOCATIONS_API_VERSION
from ..resource_map import IoTOperationsResourceMap
from ..connected_cluster import ConnectedCluster

logger = get_logger(__name__)


class ResourceSyncRuleConfig(NamedTuple):
    priority: int
    match_label: str
    name: str


class Instances(Queryable):
    def __init__(self, cmd):
        super().__init__(cmd=cmd)
        self.iotops_mgmt_client = get_iotops_mgmt_client(
            subscription_id=self.default_subscription_id,
        )
        self.cl_mgmt_client = get_custom_locations_mgmt_client(
            subscription_id=self.default_subscription_id,
        )

    def show(self, name: str, resource_group_name: str, show_tree: Optional[bool] = None) -> Optional[dict]:
        result = self.iotops_mgmt_client.instance.get(instance_name=name, resource_group_name=resource_group_name)

        if show_tree:
            self._show_tree(result)
            return

        return result

    def list(self, resource_group_name: Optional[str] = None) -> Iterable[dict]:
        if resource_group_name:
            return self.iotops_mgmt_client.instance.list_by_resource_group(resource_group_name=resource_group_name)

        return self.iotops_mgmt_client.instance.list_by_subscription()

    def _show_tree(self, instance: dict):
        resource_map = self.get_resource_map(instance)
        with self.console.status("Working..."):
            resource_map.refresh_resource_state()
        print(resource_map.build_tree(category_color="cyan"))

    def _get_associated_cl(self, instance: dict) -> dict:
        return self.resource_client.resources.get_by_id(
            resource_id=instance["extendedLocation"]["name"], api_version=CUSTOM_LOCATIONS_API_VERSION
        ).as_dict()

    def get_resource_map(self, instance: dict) -> IoTOperationsResourceMap:
        custom_location = self._get_associated_cl(instance)
        resource_id_container = parse_resource_id(custom_location["properties"]["hostResourceId"])

        return IoTOperationsResourceMap(
            cmd=self.cmd,
            cluster_name=resource_id_container.resource_name,
            resource_group_name=resource_id_container.resource_group_name,
            defer_refresh=True,
        )

    def update(
        self,
        name: str,
        resource_group_name: str,
        tags: Optional[dict] = None,
        description: Optional[str] = None,
        **kwargs: dict,
    ) -> dict:
        instance = self.show(name=name, resource_group_name=resource_group_name)

        if description:
            instance["properties"]["description"] = description

        if tags or tags == {}:
            instance["tags"] = tags

        with self.console.status("Working..."):
            poller = self.iotops_mgmt_client.instance.begin_create_or_update(
                instance_name=name,
                resource_group_name=resource_group_name,
                resource=instance,
            )
            return wait_for_terminal_state(poller, **kwargs)

    def create_ops_baseline(
        self,
        instance_name: str,
        cluster_name: str,
        resource_group_name: str,
        custom_location_name: str,
        extension_ids: frozenset,
        include_resource_sync_rules: bool = True,
        instance_desc: Optional[str] = None,
        instance_tags: Optional[dict] = None,
        **kwargs,
    ):

        connected_cluster = ConnectedCluster(
            cmd=self.cmd,
            subscription_id=self.default_subscription_id,
            cluster_name=cluster_name,
            resource_group_name=resource_group_name,
        )
        cluster_resource = connected_cluster.resource
        cluster_resource_extensions = connected_cluster.extensions
        import pdb; pdb.set_trace()
        custom_location_poller = self.cl_mgmt_client.custom_locations.begin_create_or_update(
            resource_group_name=resource_group_name, resource_name=custom_location_name, parameters={}
        )
        custom_location: dict = wait_for_terminal_state(custom_location_poller, **kwargs)

        rsr_configs = [
            ResourceSyncRuleConfig(
                priority=200, match_label="microsoft.iotoperationsmq", name=f"{custom_location['name']}-mq-sync"
            ),
            ResourceSyncRuleConfig(
                priority=400, match_label="Microsoft.DeviceRegistry", name=f"{custom_location['name']}-adr-sync"
            ),
        ]

        if include_resource_sync_rules:
            for rsr_config in rsr_configs:
                rsr_poller = self.cl_mgmt_client.resource_sync_rules.begin_create_or_update(
                    resource_group_name=resource_group_name,
                    resource_name=custom_location["name"],
                    child_resource_name=rsr_config.name,
                    parameters={
                        "location": cluster_resource["location"],
                        "properties": {
                            "priority": rsr_config.priority,
                            "selector": {"management.azure.com/provider-name": rsr_config.match_label},
                        },
                    },
                )
                wait_for_terminal_state(rsr_poller, **kwargs)

        instance_create_payload = {
            "extendedLocation": {"name": "customLocationId"},
            "location": cluster_resource["location"],
            "name": instance_name,
            "properties": {"description": instance_desc},
        }
        if instance_tags:
            instance_create_payload["tags"] = instance_tags
        instance_poller = self.iotops_mgmt_client.instance.begin_create_or_update(
            resource_group_name=resource_group_name,
            instance_name=instance_name,
            resource=instance_create_payload,
        )
        instance = wait_for_terminal_state(instance_poller, **kwargs)
