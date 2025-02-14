# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

from enum import Enum
from json import dumps
from pathlib import PurePath
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union
from uuid import uuid4

from azure.cli.core.azclierror import ValidationError
from knack.log import get_logger
from packaging import version
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table, box

from ....constants import VERSION as CLI_VERSION
from ...util import chunk_list, get_timestamp_now_utc, should_continue_prompt, to_safe_filename
from ...util.az_client import (
    REGISTRY_API_VERSION,
)
from ...util.id_tools import parse_resource_id
from .common import (
    CONTRIBUTOR_ROLE_ID,
    CUSTOM_LOCATIONS_API_VERSION,
    EXTENSION_TYPE_ACS,
    EXTENSION_TYPE_OPS,
    EXTENSION_TYPE_OSM,
    EXTENSION_TYPE_PLATFORM,
    EXTENSION_TYPE_SSC,
    EXTENSION_TYPE_TO_MONIKER_MAP,
    OPS_EXTENSION_DEPS,
)
from .resources import Instances
from .resources.instances import get_fc_name

DEFAULT_CONSOLE = Console()


class SummaryMode(Enum):
    SIMPLE = "simple"
    DETAILED = "detailed"


class StateResourceKey(Enum):
    CL = "customLocation"
    INSTANCE = "instance"
    BROKER = "broker"
    LISTENER = "listener"
    AUTHN = "authn"
    AUTHZ = "authz"
    PROFILE = "dataflowProfile"
    ENDPOINT = "dataflowEndpoint"
    DATAFLOW = "dataflow"
    ASSET = "asset"
    ASSET_ENDPOINT_PROFILE = "assetEndpointProfile"
    SSC_SPC = "secretProviderClass"
    SSC_SECRETSYNC = "secretSync"
    ROLE_ASSIGNMENT = "roleAssignment"
    FEDERATE = "identityFederation"


class TemplateParams(Enum):
    INSTANCE_NAME = "instanceName"
    CLUSTER_NAME = "clusterName"
    CUSTOM_LOCATION_NAME = "customLocationName"
    SUBSCRIPTION = "subscription"
    RESOURCEGROUP = "resourceGroup"
    SCHEMA_REGISTRY_ID = "schemaRegistryId"
    RESOURCE_SLUG = "resourceSlug"


TEMPLATE_EXPRESSION_MAP = {
    "instanceName": f"[parameters('{TemplateParams.INSTANCE_NAME.value}')]",
    "instanceNestedName": (f"[concat(parameters('{TemplateParams.INSTANCE_NAME.value}'), " "'{}')]"),
    "clusterName": f"[parameters('{TemplateParams.CLUSTER_NAME.value}')]",
    "clusterId": (
        "[resourceId('Microsoft.Kubernetes/connectedClusters', " f"parameters('{TemplateParams.CLUSTER_NAME.value}'))]"
    ),
    "customLocationName": f"[parameters('{TemplateParams.CUSTOM_LOCATION_NAME.value}')]",
    "customLocationId": (
        "[resourceId('Microsoft.ExtendedLocation/customLocations', "
        f"parameters('{TemplateParams.CUSTOM_LOCATION_NAME.value}'))]"
    ),
    "extensionId": (
        "[concat(resourceId('Microsoft.Kubernetes/connectedClusters', "
        f"parameters('{TemplateParams.CLUSTER_NAME.value}')), "
        "'/providers/Microsoft.KubernetesConfiguration/extensions/{})]"
    ),
    "schemaRegistryId": f"[parameters('{TemplateParams.SCHEMA_REGISTRY_ID.value}')]",
}


def get_resource_id_expr(rtype: str, resource_id: str, for_instance: bool = True) -> str:
    id_meta = parse_resource_id(resource_id)
    initial_seg = f"parameters('{TemplateParams.INSTANCE_NAME.value}')" if for_instance else id_meta["name"]
    target_name = f"'{initial_seg}'"
    if for_instance:
        target_name = f"parameters('{TemplateParams.INSTANCE_NAME.value}')"
    last_child_num = id_meta.get("last_child_num", 0)
    if last_child_num:
        for i in range(1, last_child_num + 1):
            target_name += f", '{id_meta[f'child_name_{i}']}'"

    return f"[resourceId('{rtype}', {target_name})]"


def get_resource_id_by_parts(rtype: str, *args) -> str:
    def _rem_first_last(s: str, c: str):
        first = s.find(c)
        last = s.rfind(c)
        if first == -1 or first == last:
            return s
        return s[:first] + s[first + 1 : last] + s[last + 1 :]

    name_parts = ""
    for arg in args:
        name_parts += f", '{arg}'"
    # TODO: very hacky
    if "concat(" in name_parts:
        name_parts = _rem_first_last(name_parts, "'")
    return f"[resourceId('{rtype}'{name_parts})]"


def get_resource_id_by_param(rtype: str, param: TemplateParams) -> str:
    return f"[resourceId('{rtype}', parameters('{param.value}'))]"


class DeploymentContainer:
    def __init__(
        self,
        name: str,
        api_version: str = "2022-09-01",
        parameters: Optional[dict] = None,
        depends_on: Optional[Union[Iterable[str], str]] = None,
    ):
        self.name = name
        self.rcontainer_map: Dict[str, "ResourceContainer"] = {}
        self.api_version = api_version
        self.parameters = parameters
        self.depends_on = depends_on
        if isinstance(self.depends_on, str):
            self.depends_on = {self.depends_on}

    def add_resources(
        self,
        key: Union[StateResourceKey, str],
        api_version: str,
        data_iter: Iterable[dict],
        depends_on: Optional[List[Union[StateResourceKey, str]]] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        if isinstance(key, StateResourceKey):
            key = key.value
        depends_on = process_depends_on(depends_on)

        to_enumerate = list(data_iter)

        count = 0
        for resource in to_enumerate:
            count += 1
            suffix = "" if count <= 1 else f"_{count}"
            target_key = f"{key}{suffix}"

            self.rcontainer_map[target_key] = ResourceContainer(
                api_version=api_version,
                resource_state=resource,
                depends_on=depends_on,
                config=config,
            )

    def get(self):
        result = {
            "type": "Microsoft.Resources/deployments",
            "apiVersion": self.api_version,
            "name": self.name,
            "properties": {
                "mode": "Incremental",
                "template": {
                    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
                    "contentVersion": "1.0.0.0",
                    "resources": [r.get() for r in list(self.rcontainer_map.values())],
                },
            },
        }
        if self.parameters:
            input_param_map = {}
            for param in self.parameters:
                input_param_map[param] = {"value": f"[parameters('{param}')]"}
            result["properties"]["parameters"] = input_param_map
            result["properties"]["template"]["parameters"] = self.parameters
        if self.depends_on:
            result["dependsOn"] = list(self.depends_on)
        return result


class ResourceContainer:
    def __init__(
        self,
        api_version: str,
        resource_state: dict,
        depends_on: Optional[Iterable[str]] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        self.api_version = api_version
        self.resource_state = resource_state
        if depends_on:
            depends_on = list(depends_on)
        self.depends_on = depends_on
        if not config:
            config = {}
        self.config = config

    def _prune_resource(self):
        filter_keys = {
            "id",
            "systemData",
        }
        self.resource_state = self._prune_resource_keys(filter_keys=filter_keys, resource=self.resource_state)
        filter_keys = {
            "provisioningState",
            "currentVersion",
            "statuses",
            "status",
        }
        self.resource_state["properties"] = self._prune_resource_keys(
            filter_keys=filter_keys, resource=self.resource_state["properties"]
        )

    def _prune_identity(self):
        filter_keys = {"principalId"}
        if "identity" in self.resource_state:
            self.resource_state["identity"] = self._prune_resource_keys(
                filter_keys=filter_keys, resource=self.resource_state["identity"]
            )

    @classmethod
    def _prune_resource_keys(cls, filter_keys: set, resource: dict) -> dict:
        result = {}
        for key in resource:
            if key not in filter_keys:
                result[key] = resource[key]
        return result

    def _apply_cl_ref(self):
        if "extendedLocation" in self.resource_state:
            self.resource_state["extendedLocation"]["name"] = TEMPLATE_EXPRESSION_MAP["customLocationId"]

    def _apply_nested_name(self):
        def __extract_suffix(path: str) -> str:
            return "/" + path.partition("/")[2]

        test: Dict[str, Union[str, int]] = parse_resource_id(self.resource_state["id"])
        target_name = test["name"]
        last_child_num = test.get("last_child_num", 0)
        if last_child_num:
            for i in range(1, last_child_num + 1):
                target_name += f"/{test[f'child_name_{i}']}"
        self.resource_state["name"] = target_name
        if test["type"].lower() == "instances":
            suffix = __extract_suffix(target_name)
            if suffix == "/":
                self.resource_state["name"] = TEMPLATE_EXPRESSION_MAP["instanceName"]
            else:
                self.resource_state["name"] = TEMPLATE_EXPRESSION_MAP["instanceNestedName"].format(suffix)

    def get(self):
        apply_nested_name = self.config.get("apply_nested_name", True)
        if apply_nested_name:
            self._apply_nested_name()

        self._apply_cl_ref()
        self._prune_identity()
        self._prune_resource()

        result = {
            "apiVersion": self.api_version,
            **self.resource_state,
        }
        if self.depends_on:
            result["dependsOn"] = self.depends_on
        return result


def backup_ops_instance(
    cmd,
    resource_group_name: str,
    instance_name: str,
    summary_mode: Optional[str] = None,
    oidc_issuer: Optional[str] = None,
    bundle_dir: Optional[str] = None,
    no_progress: Optional[bool] = None,
    confirm_yes: Optional[bool] = None,
    **kwargs,
):
    backup_manager = BackupManager(
        cmd=cmd,
        instance_name=instance_name,
        oidc_issuer=oidc_issuer,
        resource_group_name=resource_group_name,
        no_progress=no_progress,
    )
    bundle_path = get_bundle_path(instance_name, bundle_dir=bundle_dir)

    backup_manager.analyze_cluster(**kwargs)

    if not no_progress:
        enumerated_resources = backup_manager.enumerate_resources()
        render_upgrade_table(
            instance_name, bundle_path, enumerated_resources, detailed=summary_mode == SummaryMode.DETAILED.value
        )

    should_bail = not should_continue_prompt(confirm_yes=confirm_yes, context="Backup")
    if should_bail:
        return

    backup_manager.output_template(bundle_path=bundle_path)


def render_upgrade_table(instance_name: str, bundle_path: str, resources: Dict[str, dict], detailed: bool = False):
    table = get_default_table(include_name=detailed)
    table.title += f" of {instance_name}"
    for rtype in resources:
        row_content = [f"{rtype}", f"{len(resources[rtype])}"]
        if detailed:
            row_content.append("\n".join([r["resource_name"] for r in resources[rtype]]))

        table.add_row(*row_content)

    DEFAULT_CONSOLE.print(table)
    DEFAULT_CONSOLE.print(f"State will be saved to:\n-> {bundle_path}\n")


def get_default_table(include_name: bool = False) -> Table:
    table = Table(
        box=box.MINIMAL,
        expand=False,
        title="Capture",
        min_width=79,
        show_footer=True,
    )
    table.add_column("Resource Type")
    table.add_column("#")
    if include_name:
        table.add_column("Name")

    return table


class BackupManager:
    def __init__(
        self,
        cmd,
        resource_group_name: str,
        instance_name: str,
        oidc_issuer: Optional[str] = None,
        no_progress: Optional[bool] = None,
    ):
        self.cmd = cmd
        self.instance_name = instance_name
        self.oidc_issuer = oidc_issuer
        self.resource_group_name = resource_group_name
        self.no_progress = no_progress
        self.instances = Instances(self.cmd)
        self.instance_record = self.instances.show(
            name=self.instance_name, resource_group_name=self.resource_group_name
        )

        self.resource_map = self.instances.get_resource_map(self.instance_record)
        self.resouce_graph = self.resource_map.connected_cluster.resource_graph
        self.rcontainer_map: Dict[str, ResourceContainer] = {}
        self.parameter_map: dict = {}
        self.variable_map: dict = {}
        self.metadata_map: dict = {}
        self.instance_identities: List[str] = []
        self.active_deployment: Dict[StateResourceKey, List[str]] = {}
        self.chunk_size = 800

    def analyze_cluster(self):
        with Progress(
            SpinnerColumn("star"),
            *Progress.get_default_columns(),
            "Elapsed:",
            TimeElapsedColumn(),
            transient=True,
            # disable=bool(self.no_progress),
            disable=True,
        ) as progress:
            _ = progress.add_task("Analyzing cluster...", total=None)
            self._build_parameters(self.instance_record)
            self._build_variables()
            self._build_metadata()

            self._analyze_extensions()
            self._analyze_instance()
            self._analyze_instance_identity()
            self._analyze_instance_resources()
            self._analyze_secretsync()
            self._analyze_assets()

    def enumerate_resources(self):
        enumerated_map: dict = {}

        def __enumerator(rcontainer_map: Dict[str, ResourceContainer]):
            for resource in rcontainer_map:
                target_rcontainer = rcontainer_map[resource]
                if isinstance(target_rcontainer, ResourceContainer):
                    if "id" not in target_rcontainer.resource_state:
                        continue
                    parsed_id = parse_resource_id(target_rcontainer.resource_state["id"])
                    # if "instances" in parsed_id["type"]:
                    #     import pdb; pdb.set_trace()
                    #     pass
                    key = f"{parsed_id['namespace']}/{parsed_id['type']}"
                    if "resource_type" in parsed_id and parsed_id["resource_type"] != parsed_id["type"]:
                        key += f"/{parsed_id['resource_type']}"

                    items: list = enumerated_map.get(key, [])
                    items.append(parsed_id)
                    enumerated_map[key] = items
                if isinstance(target_rcontainer, DeploymentContainer):
                    __enumerator(target_rcontainer.rcontainer_map)

        __enumerator(self.rcontainer_map)
        return enumerated_map

    def output_template(self, bundle_path: str):
        template_gen = TemplateGen(self.rcontainer_map, self.parameter_map, self.variable_map, self.metadata_map)
        template_gen.write(bundle_path=bundle_path)

    def _build_parameters(self, instance: dict):
        self.parameter_map.update(build_parameter(name=TemplateParams.CLUSTER_NAME.value))
        self.parameter_map.update(build_parameter(name=TemplateParams.INSTANCE_NAME.value))
        self.parameter_map.update(
            build_parameter(
                name=TemplateParams.RESOURCE_SLUG.value,
                default=(
                    "[take(uniqueString(resourceGroup().id, "
                    "parameters('clusterName'), parameters('instanceName')), 5)]"
                ),
            )
        )
        self.parameter_map.update(
            build_parameter(
                name=TemplateParams.CUSTOM_LOCATION_NAME.value,
                default="[format('location-{0}', parameters('resourceSlug'))]",
            )
        )
        self.parameter_map.update(
            build_parameter(
                name=TemplateParams.SCHEMA_REGISTRY_ID.value,
                default=instance["properties"]["schemaRegistryRef"]["resourceId"],
            )
        )

    def _build_variables(self):
        self.variable_map["aioExtName"] = "[format('azure-iot-operations-{0}', parameters('resourceSlug'))]"

    def _build_metadata(self):
        self.metadata_map["opsCliVersion"] = CLI_VERSION
        self.metadata_map["clonedInstanceId"] = self.instance_record["id"]

    def get_resources_of_type(self, resource_type: str) -> List[dict]:
        return self.resouce_graph.query_resources(
            f"""
            resources
            | where extendedLocation.name =~ '{self.instance_record["extendedLocation"]["name"]}'
            | where type =~ '{resource_type}'
            | project id, name, type, location, extendedLocation, properties
            """
        )["data"]

    def get_identities_by_client_id(self, client_ids: List[str]) -> List[dict]:
        return self.resouce_graph.query_resources(
            f"""
            resources
            | where type =~ "Microsoft.ManagedIdentity/userAssignedIdentities"
            | where properties.clientId in~ ("{'", "'.join(client_ids)}")
            | project id, name, type, properties
            """
        )["data"]

    def _analyze_extensions(self):
        depends_on_map = {
            EXTENSION_TYPE_SSC: [EXTENSION_TYPE_TO_MONIKER_MAP[EXTENSION_TYPE_PLATFORM]],
            EXTENSION_TYPE_ACS: [
                EXTENSION_TYPE_TO_MONIKER_MAP[EXTENSION_TYPE_PLATFORM],
                EXTENSION_TYPE_TO_MONIKER_MAP[EXTENSION_TYPE_OSM],
            ],
            EXTENSION_TYPE_OPS: [EXTENSION_TYPE_TO_MONIKER_MAP[ext_type] for ext_type in list(OPS_EXTENSION_DEPS)],
        }
        api_version = (
            self.resource_map.connected_cluster.clusters.extensions.clusterconfig_mgmt_client._config.api_version
        )
        extension_map = self.resource_map.connected_cluster.get_extensions_by_type(
            *list(EXTENSION_TYPE_TO_MONIKER_MAP.keys())
        )
        for extension_type in extension_map:
            extension_moniker = EXTENSION_TYPE_TO_MONIKER_MAP[extension_type]
            depends_on = depends_on_map.get(extension_type)
            extension_map[extension_type]["scope"] = TEMPLATE_EXPRESSION_MAP["clusterId"]
            if extension_moniker == EXTENSION_TYPE_TO_MONIKER_MAP[EXTENSION_TYPE_OPS]:
                extension_map[extension_type]["name"] = "[variables('aioExtName')]"

            self._add_resource(
                key=extension_moniker,
                api_version=api_version,
                data=extension_map[extension_type],
                depends_on=depends_on,
                config={"apply_nested_name": False},
            )

    def _analyze_instance(self):
        api_version = self.instances.iotops_mgmt_client._config.api_version
        # TODO - @digimaun, not efficient.
        custom_location = self.instances._get_associated_cl(self.instance_record)
        custom_location["properties"]["hostResourceId"] = TEMPLATE_EXPRESSION_MAP["clusterId"]
        # TODO
        custom_location["name"] = TEMPLATE_EXPRESSION_MAP["customLocationName"]

        cl_extension_ids = []
        cl_monikers = [
            EXTENSION_TYPE_TO_MONIKER_MAP[EXTENSION_TYPE_PLATFORM],
            EXTENSION_TYPE_TO_MONIKER_MAP[EXTENSION_TYPE_SSC],
            EXTENSION_TYPE_TO_MONIKER_MAP[EXTENSION_TYPE_OPS],
        ]
        for moniker in cl_monikers:
            ext_resource = self.rcontainer_map.get(moniker)
            if moniker == EXTENSION_TYPE_TO_MONIKER_MAP[EXTENSION_TYPE_OPS]:
                cl_extension_ids.append(TEMPLATE_EXPRESSION_MAP["extensionId"].format("', variables('aioExtName')"))
            else:
                cl_extension_ids.append(
                    TEMPLATE_EXPRESSION_MAP["extensionId"].format(f"{ext_resource.resource_state['name']}'")
                )

        custom_location["properties"]["clusterExtensionIds"] = cl_extension_ids
        custom_location["properties"]["displayName"] = "[parameters('customLocationName')]"

        self._add_resource(
            key=StateResourceKey.CL,
            api_version=CUSTOM_LOCATIONS_API_VERSION,
            data=custom_location,
            config={"apply_nested_name": False},
            depends_on=cl_monikers,
        )
        # schema_reg_id = self.instance_record["properties"]["schemaRegistryRef"]["resourceId"]
        self.instance_record["properties"]["schemaRegistryRef"]["resourceId"] = TEMPLATE_EXPRESSION_MAP[
            "schemaRegistryId"
        ]
        self._add_resource(
            key=StateResourceKey.INSTANCE,
            api_version=api_version,
            data=self.instance_record,
            depends_on=StateResourceKey.CL,
        )
        self._add_resource(
            key=StateResourceKey.ROLE_ASSIGNMENT,
            api_version="2022-04-01",
            data=get_role_assignment(),
            depends_on=EXTENSION_TYPE_TO_MONIKER_MAP[EXTENSION_TYPE_OPS],
            config={"apply_nested_name": False},
        )

    def _analyze_instance_resources(self):
        api_version = self.instances.iotops_mgmt_client._config.api_version
        brokers_iter = self.instances.iotops_mgmt_client.broker.list_by_resource_group(
            resource_group_name=self.resource_group_name, instance_name=self.instance_name
        )
        # Let us keep things simple atm
        default_broker = list(brokers_iter)[0]
        self._add_resource(
            key=StateResourceKey.BROKER,
            api_version=api_version,
            data=default_broker,
            depends_on=StateResourceKey.INSTANCE,
        )

        # Initial dependencies
        nested_params = {
            **build_parameter(name=TemplateParams.CUSTOM_LOCATION_NAME.value),
            **build_parameter(name=TemplateParams.INSTANCE_NAME.value),
        }
        broker_resource_id_expr = get_resource_id_expr(rtype=default_broker["type"], resource_id=default_broker["id"])

        # authN
        self._add_deployment(
            key=StateResourceKey.AUTHN,
            api_version=api_version,
            data_iter=self.instances.iotops_mgmt_client.broker_authentication.list_by_resource_group(
                resource_group_name=self.resource_group_name,
                instance_name=self.instance_name,
                broker_name=default_broker["name"],
            ),
            depends_on=broker_resource_id_expr,
            parameters=nested_params,
        )

        # authZ
        self._add_deployment(
            key=StateResourceKey.AUTHZ,
            api_version=api_version,
            data_iter=self.instances.iotops_mgmt_client.broker_authorization.list_by_resource_group(
                resource_group_name=self.resource_group_name,
                instance_name=self.instance_name,
                broker_name=default_broker["name"],
            ),
            depends_on=broker_resource_id_expr,
            parameters=nested_params,
        )

        # listener
        listener_depends_on = []
        for active in self.active_deployment:
            if active in [StateResourceKey.AUTHN, StateResourceKey.AUTHZ]:
                listener_depends_on.append(
                    get_resource_id_by_parts("Microsoft.Resources/deployments", self.active_deployment[active][-1])
                )

        self._add_deployment(
            key=StateResourceKey.LISTENER,
            api_version=api_version,
            data_iter=self.instances.iotops_mgmt_client.broker_listener.list_by_resource_group(
                resource_group_name=self.resource_group_name,
                instance_name=self.instance_name,
                broker_name=default_broker["name"],
            ),
            depends_on=listener_depends_on,
            parameters=nested_params,
        )

        instance_resource_id_expr = get_resource_id_by_param(
            "microsoft.iotoperations/instances", TemplateParams.INSTANCE_NAME
        )

        # endpoint
        self._add_deployment(
            key=StateResourceKey.ENDPOINT,
            api_version=api_version,
            data_iter=self.instances.iotops_mgmt_client.dataflow_endpoint.list_by_resource_group(
                resource_group_name=self.resource_group_name, instance_name=self.instance_name
            ),
            depends_on=instance_resource_id_expr,
            parameters=nested_params,
        )

        # profile
        profile_iter = list(
            self.instances.iotops_mgmt_client.dataflow_profile.list_by_resource_group(
                resource_group_name=self.resource_group_name, instance_name=self.instance_name
            )
        )
        self._add_deployment(
            key=StateResourceKey.PROFILE,
            api_version=api_version,
            data_iter=profile_iter,
            depends_on=instance_resource_id_expr,
            parameters=nested_params,
        )

        # dataflow
        if profile_iter:
            dataflows = []
            for profile in profile_iter:
                dataflows.extend(
                    self.instances.iotops_mgmt_client.dataflow.list_by_profile_resource(
                        resource_group_name=self.resource_group_name,
                        instance_name=self.instance_name,
                        dataflow_profile_name=profile["name"],
                    )
                )

            self._add_deployment(
                key=StateResourceKey.DATAFLOW,
                api_version=api_version,
                data_iter=dataflows,
                depends_on=get_resource_id_by_parts(
                    "Microsoft.Resources/deployments", self.active_deployment[StateResourceKey.PROFILE][-1]
                ),
                parameters=nested_params,
            )

    def _analyze_assets(self):
        nested_params = {
            **build_parameter(name=TemplateParams.CUSTOM_LOCATION_NAME.value),
            **build_parameter(name=TemplateParams.INSTANCE_NAME.value),
        }
        instance_resource_id_expr = get_resource_id_by_param(
            "microsoft.iotoperations/instances", TemplateParams.INSTANCE_NAME
        )

        asset_endpoints = self.get_resources_of_type(resource_type="microsoft.deviceregistry/assetendpointprofiles")
        self._add_deployment(
            key=StateResourceKey.ASSET_ENDPOINT_PROFILE,
            api_version=REGISTRY_API_VERSION,
            data_iter=asset_endpoints,
            depends_on=instance_resource_id_expr,
            parameters=nested_params,
        )

        assets = self.get_resources_of_type(resource_type="microsoft.deviceregistry/assets")
        if assets and asset_endpoints:
            self._add_deployment(
                key=StateResourceKey.ASSET,
                api_version=REGISTRY_API_VERSION,
                data_iter=assets,
                depends_on=get_resource_id_by_parts(
                    "Microsoft.Resources/deployments",
                    self.active_deployment[StateResourceKey.ASSET_ENDPOINT_PROFILE][-1],
                ),
                parameters=nested_params,
            )

    def _analyze_secretsync(self):
        nested_params = {
            **build_parameter(name=TemplateParams.CUSTOM_LOCATION_NAME.value),
            **build_parameter(name=TemplateParams.INSTANCE_NAME.value),
        }
        ssc_client = self.instances.ssc_mgmt_client
        ssc_api_version = ssc_client._config.api_version
        instance_resource_id_expr = get_resource_id_by_param(
            "microsoft.iotoperations/instances", TemplateParams.INSTANCE_NAME
        )
        ext_loc_id = self.instance_record["extendedLocation"]["name"].lower()
        ssc_spcs = list(
            ssc_client.azure_key_vault_secret_provider_classes.list_by_resource_group(
                resource_group_name=self.resource_group_name
            )
        )

        ssc_spcs = [spc for spc in ssc_spcs if spc["extendedLocation"]["name"].lower() == ext_loc_id]
        client_ids = [spc["properties"]["clientId"] for spc in ssc_spcs if "clientId" in spc["properties"]]
        self.instance_identities.extend([mid["id"] for mid in self.get_identities_by_client_id(client_ids)])

        self._add_deployment(
            key=StateResourceKey.SSC_SPC,
            api_version=ssc_api_version,
            data_iter=ssc_spcs,
            depends_on=instance_resource_id_expr,
            parameters=nested_params,
        )

        ssc_secretsyncs = list(
            ssc_client.secret_syncs.list_by_resource_group(resource_group_name=self.resource_group_name)
        )
        ssc_secretsyncs = [
            secretsync
            for secretsync in ssc_secretsyncs
            if secretsync["extendedLocation"]["name"].lower() == ext_loc_id
        ]
        if ssc_secretsyncs and ssc_spcs:
            self._add_deployment(
                key=StateResourceKey.SSC_SECRETSYNC,
                api_version=ssc_api_version,
                data_iter=ssc_secretsyncs,
                depends_on=get_resource_id_by_parts(
                    "Microsoft.Resources/deployments", self.active_deployment[StateResourceKey.SSC_SPC][-1]
                ),
                parameters=nested_params,
            )

    def _analyze_instance_identity(self):
        if not self.oidc_issuer:
            return

        uami_ids = []
        identity: dict = self.instance_record.get("identity", {}).get("userAssignedIdentities", {})
        uami_ids.extend(list(identity.keys()))

        if not uami_ids:
            return

        msi_client = self.instances.msi_mgmt_client
        for i in range(len(uami_ids)):
            resource_id = parse_resource_id(uami_ids[i])
            credentials = list(
                msi_client.federated_identity_credentials.list(
                    resource_group_name=resource_id["resource_group"], resource_name=resource_id["name"]
                )
            )
            filtered_creds = []
            for cred in credentials:
                if cred["properties"]["issuer"] != self.oidc_issuer:
                    filtered_creds.append(cred)

            for cred in filtered_creds:
                if ":aio-" not in cred["properties"]["subject"]:
                    continue

                msi_client.federated_identity_credentials.create_or_update(
                    resource_group_name=resource_id["resource_group"],
                    resource_name=resource_id["name"],
                    federated_identity_credential_resource_name=get_fc_name(
                        cluster_name=self.oidc_issuer,
                        oidc_issuer=self.oidc_issuer,
                        subject=cred["properties"]["subject"],
                    ),
                    parameters={
                        "properties": {
                            "subject": cred["properties"]["subject"],
                            "audiences": cred["properties"]["audiences"],
                            "issuer": self.oidc_issuer,
                        }
                    },
                )

    def add_deployment_by_key(self, key: StateResourceKey) -> Tuple[str, str]:
        deployments_by_key = self.active_deployment.get(key, [])
        symbolic_name = f"{key.value}s_{len(deployments_by_key)+1}"
        deployment_name = f"concat(parameters('resourceSlug'), '_{symbolic_name}')"
        deployments_by_key.append(deployment_name)
        self.active_deployment[key] = deployments_by_key
        return symbolic_name, deployment_name

    def _add_deployment(
        self,
        key: StateResourceKey,
        api_version: str,
        data_iter: Iterable,
        depends_on: Optional[Union[str, Iterable[str]]] = None,
        parameters: Optional[dict] = None,
    ):
        data_iter = list(data_iter)
        if data_iter:
            chunked_list_data = chunk_list(data_iter, self.chunk_size)
            for chunk in chunked_list_data:
                symbolic_name, deployment_name = self.add_deployment_by_key(key)
                deployment_container = DeploymentContainer(
                    name=f"[{deployment_name}]",
                    depends_on=depends_on,
                    parameters=parameters,
                )
                deployment_container.add_resources(
                    key=key,
                    api_version=api_version,
                    data_iter=chunk,
                )
                self.rcontainer_map[symbolic_name] = deployment_container

    def _add_resource(
        self,
        key: Union[StateResourceKey, str],
        api_version: str,
        data: dict,
        depends_on: Optional[Union[Iterable[str], str]] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        if isinstance(key, StateResourceKey):
            key = key.value
        depends_on = process_depends_on(depends_on)

        self.rcontainer_map[key] = ResourceContainer(
            api_version=api_version,
            resource_state=data,
            depends_on=depends_on,
            config=config,
        )


class TemplateGen:
    def __init__(
        self, rcontainer_map: Dict[str, ResourceContainer], parameter_map: dict, variable_map: dict, metadata_map: dict
    ):
        self.rcontainer_map = rcontainer_map
        self.parameter_map = parameter_map
        self.variable_map = variable_map
        self.metadata_map = metadata_map

    def _prune_template_keys(self, template: dict) -> dict:
        result = {}
        for key in template:
            if not template[key]:
                continue
            result[key] = template[key]
        return result

    def _build_contents(self) -> str:
        template = self.get_base_format()
        for template_key in self.rcontainer_map:
            template["resources"][template_key] = self.rcontainer_map[template_key].get()
        template["parameters"].update(self.parameter_map)
        template["variables"].update(self.variable_map)
        template["metadata"].update(self.metadata_map)
        template = self._prune_template_keys(template)
        return dumps(template, indent=2)

    def write(self, bundle_path: str):
        content = self._build_contents()
        with open(file=bundle_path, mode="w") as template_file:
            template_file.write(content)

    def get_base_format(self) -> dict:
        return {
            "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
            "languageVersion": "2.0",
            "contentVersion": "1.0.0.0",
            "metadata": {},
            "apiProfile": "",
            "definitions": {},
            "parameters": {},
            "variables": {},
            "functions": [],
            "resources": {},
            "outputs": {},
        }


def process_depends_on(
    depends_on: Optional[Union[Iterable[str], str, Iterable[StateResourceKey], StateResourceKey]] = None
) -> Optional[Iterable[str]]:
    if not depends_on:
        return

    result = []
    if isinstance(depends_on, StateResourceKey):
        depends_on = depends_on.value
    if isinstance(depends_on, str):
        result.append(depends_on)
        return result

    if isinstance(depends_on, Iterable):
        for d in depends_on:
            if isinstance(d, StateResourceKey):
                d = d.value
            if isinstance(d, str):
                result.append(d)

    return result


def get_bundle_path(instance_name: str, bundle_dir: Optional[str] = None) -> PurePath:
    from ...util import normalize_dir

    bundle_dir_pure_path = normalize_dir(bundle_dir)
    bundle_pure_path = bundle_dir_pure_path.joinpath(default_bundle_name(instance_name))
    return bundle_pure_path


def default_bundle_name(instance_name: str) -> str:
    timestamp = get_timestamp_now_utc(format="%Y%m%dT%H%M%S")
    return f"clone_{to_safe_filename(instance_name)}_{timestamp}_aio.json"


def build_parameter(name: str, type: str = "string", metadata: Optional[dict] = None, default: Any = None) -> dict:
    result = {
        name: {
            "type": type,
        }
    }
    if metadata:
        result[name]["metadata"] = metadata
    if default:
        result[name]["defaultValue"] = default
    return result


def get_role_assignment():
    return {
        "type": "Microsoft.Authorization/roleAssignments",
        "name": (
            f"[guid(parameters('{TemplateParams.SCHEMA_REGISTRY_ID.value}'), "
            f"parameters('{TemplateParams.CLUSTER_NAME.value}'), resourceGroup().id)]"
        ),
        "scope": f"[parameters('{TemplateParams.SCHEMA_REGISTRY_ID.value}')]",
        "properties": {
            "roleDefinitionId": (
                f"[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '{CONTRIBUTOR_ROLE_ID}')]"
            ),
            "principalId": (
                f"[reference('{EXTENSION_TYPE_TO_MONIKER_MAP[EXTENSION_TYPE_OPS]}', "
                "'2023-05-01', 'Full').identity.principalId]"
            ),
            "principalType": "ServicePrincipal",
        },
    }
