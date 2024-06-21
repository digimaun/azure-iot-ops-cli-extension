# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

import logging
from typing import List, Optional, Union
from azure.cli.core.azclierror import ResourceNotFoundError
from rich import print

from ...util.az_client import get_resource_client
from ...util.queryable import Queryable


def get_instance_query(name: Optional[str] = None, resource_group_name: Optional[str] = None):
    query = """
        resources
        | where type =~ 'Private.IoTOperations/instances'
        """

    if resource_group_name:
        query += f"| where resourceGroup =~ '{resource_group_name}'"
    if name:
        query += f"| where name =~ '{name}'"

    query += "| project extendedLocation, id, location, name, properties, systemData, tags, type"
    return query


QUERIES = {
    "get_cl_from_instance": """
        resources
        | where type =~ 'microsoft.extendedlocation/customlocations'
        | where id =~ '{resource_id}'
        | project id, name, properties
        """
}

INSTANCES_API_VERSION = "2021-10-01-privatepreview"
# TODO temporary
BASE_URL = "https://eastus2euap.management.azure.com"


class Instances(Queryable):
    def __init__(self, cmd):
        super().__init__(cmd=cmd)
        self.resource_client = get_resource_client(self.default_subscription_id, base_url=BASE_URL)

        logger = logging.getLogger("azure.mgmt.resource")
        logger.setLevel(logging.ERROR)


    def show2(self, name: str, resource_group_name: str, show_tree: Optional[bool] = None) -> Optional[dict]:
        result = self.resource_client.resources.get_by_id(
            resource_id=f"subscriptions/{self.default_subscription_id}/resourceGroups/{resource_group_name}/providers/Private.IoTOperations/instances/{name}",
            api_version=INSTANCES_API_VERSION,
        ).as_dict()

        if not result:
            raise ResourceNotFoundError(
                f"Unable to find instance '{name}' in resource group '{resource_group_name}' "
                f"using {self.subscriptions_label}."
            )

        if show_tree:
            self._show_tree(result)
            return

        return result

    def show(self, name: str, resource_group_name: str, show_tree: Optional[bool] = None) -> Optional[dict]:
        instance_query = get_instance_query(name=name, resource_group_name=resource_group_name)
        result = self.query(instance_query, resource_group_name=resource_group_name, first=True)
        if not result:
            raise ResourceNotFoundError(
                f"Unable to find instance '{name}' in resource group '{resource_group_name}' "
                f"using {self.subscriptions_label}."
            )

        if show_tree:
            self._show_tree(result)
            return

        return result

    def list(self, resource_group_name: Optional[str] = None) -> List[dict]:
        instance_query = get_instance_query(resource_group_name=resource_group_name)
        return self.query(instance_query, resource_group_name=resource_group_name)

    def _show_tree(self, instance: dict):
        custom_location = self._get_associated_cl(instance)
        _, resource_group_name, resource_name = extract_info(custom_location["properties"]["hostResourceId"])

        # Currently resource map will query cluster state upon init
        # therefore we only use it when necessary to save cycles.
        from .resource_map import IoTOperationsResourceMap

        resource_map = IoTOperationsResourceMap(
            cmd=self.cmd, cluster_name=resource_name, resource_group_name=resource_group_name
        )
        print(resource_map.build_tree(category_color="cyan"))

    def _get_associated_cl(self, instance: dict) -> dict:
        return self.query(
            QUERIES["get_cl_from_instance"].format(resource_id=instance["extended_location"]["name"]), first=True
        )


def extract_info(resource_string):
    # Split the string by "/"
    parts = resource_string.split("/")

    # Extract the subscription, resource group, and resource name
    subscription = parts[2]
    resource_group = parts[4]
    resource_name = parts[8]

    # Return the extracted information
    return subscription, resource_group, resource_name
