# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

from typing import TYPE_CHECKING, List, NamedTuple, Optional

from azure.cli.core.azclierror import (
    AzureResponseError,
)
from knack.log import get_logger
from rich.console import Console

from ....util.az_client import get_extloc_mgmt_client, wait_for_terminal_states
from ....util.id_tools import parse_resource_id
from ....util.queryable import Queryable
from ..permissions import (
    ROLE_DEF_FORMAT_STR,
    PermissionManager,
    PrincipalType,
    get_ra_user_error_msg,
)
from ..resources import Instances

if TYPE_CHECKING:
    from ....vendor.clients.extendedlocmgmt.operations import (
        ResourceSyncRulesOperations,
    )

KUBERNETES_ARC_CONTRIBUTOR_ROLE_ID = "5d3f1697-4507-4d08-bb4a-477695db5f82"
K8_BRIDGE_APP_ID = "319f651f-7ddb-4fc6-9857-7aef9250bd05"

logger = get_logger(__name__)
console = Console()


class SyncRuleAttr(NamedTuple):
    provider: str
    priority: int
    suffix: str


SYNC_RULE_ATTRS = {
    SyncRuleAttr(provider="Microsoft.DeviceRegistry", priority=200, suffix="-adr-sync"),
    SyncRuleAttr(provider="microsoft.iotoperations", priority=400, suffix="-ops-sync"),
}


class ResourceSync(Queryable):
    def __init__(self, cmd, resource_group_name: str, instance_name: str):
        super().__init__(cmd=cmd)
        self.resource_group_name = resource_group_name
        self.instance_name = instance_name
        self.instances = Instances(self.cmd)
        self.custom_location = self.instances.get_associated_cl(
            self.instances.show(name=self.instance_name, resource_group_name=self.resource_group_name)
        )
        self.extloc_mgmt_client = get_extloc_mgmt_client(self.default_subscription_id)
        self.ops: "ResourceSyncRulesOperations" = self.extloc_mgmt_client.resource_sync_rules

    def _get_enable_params(self, provider_name: str, priority: Optional[int] = None) -> dict:
        parsed_cl_id = parse_resource_id(self.custom_location["id"])
        rg_id = "/subscriptions/{}/resourceGroups/{}".format(
            parsed_cl_id["subscription"], parsed_cl_id["resource_group"]
        )
        properties = {
            "targetResourceGroup": rg_id,
        }
        parameters = {
            "location": self.custom_location["location"],
        }
        if priority:
            properties["priority"] = priority
        properties["selector"] = {"matchLabels": {"management.azure.com/provider-name": provider_name}}
        parameters["properties"] = properties

        return parameters

    def enable(
        self,
        skip_role_assignments: Optional[bool] = None,
        custom_role_id: Optional[str] = None,
        **kwargs,
    ) -> List[dict]:
        with console.status("Working...") as c:
            pollers = []
            cl_name = self.custom_location["name"]
            for rule_attrs in SYNC_RULE_ATTRS:
                poller = self.ops.begin_create_or_update(
                    resource_group_name=self.resource_group_name,
                    resource_name=self.custom_location["name"],
                    child_resource_name=f"{cl_name}{rule_attrs.suffix}",
                    parameters=self._get_enable_params(
                        provider_name=rule_attrs.provider, priority=rule_attrs.priority
                    ),
                )
                pollers.append(poller)
            wait_for_terminal_states(**pollers, **kwargs)
            result = [p.result() for p in pollers]

            if not skip_role_assignments:
                target_role_def = custom_role_id or ROLE_DEF_FORMAT_STR.format(
                    subscription_id=self.default_subscription_id, role_id=KUBERNETES_ARC_CONTRIBUTOR_ROLE_ID
                )
                k8_bridge_sp_id = self.get_sp_id(K8_BRIDGE_APP_ID)
                if not k8_bridge_sp_id:
                    c.stop()
                    logger.warning("K8 Bridge service principal not found. Skipping role assignment.")
                    return result

                permission_manager = PermissionManager(self.default_subscription_id)
                try:
                    permission_manager.apply_role_assignment(
                        scope=self.custom_location["id"],
                        principal_id=k8_bridge_sp_id,
                        role_def_id=target_role_def,
                        principal_type=PrincipalType.SERVICE_PRINCIPAL.value,
                    )
                except Exception as e:
                    c.stop()
                    raise AzureResponseError(
                        # Add --skip-ra?
                        get_ra_user_error_msg(
                            error_str=str(e),
                            sp_name="K8 Bridge",
                            sp_id=K8_BRIDGE_APP_ID,
                            expected_role="Azure Kubernetes Service Arc Contributor Role",
                            scope=self.custom_location["id"],
                        )
                    )

            return result

    def disable(self):
        pass

    def list(self) -> List[dict]:
        return []
