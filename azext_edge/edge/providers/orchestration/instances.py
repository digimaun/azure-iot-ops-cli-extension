# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

from typing import List, Optional, Union
from azure.cli.core.azclierror import ResourceNotFoundError

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


class Instances(Queryable):
    def __init__(self, cmd):
        super().__init__(cmd=cmd)

    def show(self, name: str, resource_group_name: str):
        instance_query = get_instance_query(name=name, resource_group_name=resource_group_name)
        result = self.query(instance_query, resource_group_name=resource_group_name, first=True)
        if not result:
            raise ResourceNotFoundError(
                f"Unable to find instance '{name}' in resource group '{resource_group_name}' "
                f"using {self.subscriptions_label}."
            )

        return result

    def list(self, resource_group_name: Optional[str] = None):
        instance_query = get_instance_query(resource_group_name=resource_group_name)
        return self.query(instance_query, resource_group_name=resource_group_name)
