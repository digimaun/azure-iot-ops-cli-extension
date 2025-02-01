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

from ...util import get_timestamp_now_utc, should_continue_prompt
from ...util.az_client import get_registry_mgmt_client, REGISTRY_API_VERSION
from ...util.id_tools import parse_resource_id
from .common import (
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


class StateResourceKey(Enum):
    CL = "customLocation"
    INSTANCE = "instance"
    BROKER = "broker"
    LISTENER = "listener"
    AUTHN = "authn"
    AUTHZ = "authz"
    PROFILE = "profile"
    ENDPOINT = "endpoint"
    DATAFLOW = "dataflow"
    ASSET = "asset"
    ASSET_ENDPOINT_PROFILE = "assetEndpointProfile"


class TemplateParams(Enum):
    INSTANCE_NAME = "instanceName"
    CLUSTER_NAME = "clusterName"
    CUSTOM_LOCATION_NAME = "customLocationName"
    SUBSCRIPTION = "subscription"
    RESOURCEGROUP = "resourceGroup"


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
        "'/providers/Microsoft.KubernetesConfiguration/extensions/{}')]"
    ),
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
    name_parts = ""
    for arg in args:
        name_parts += f", '{arg}'"
    return f"[resourceId('{rtype}'{name_parts})]"


def get_resource_id_by_param(rtype: str, param: TemplateParams) -> str:
    return f"[resourceId('{rtype}', parameters('{param.value}'))]"


def get_deployment_by_key(key: StateResourceKey):
    return f"{key.value}Deployment"


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

        if depends_on:
            depends_on = [i.value if isinstance(i, StateResourceKey) else i for i in depends_on]
            depends_on = [i for i in depends_on if i in self.rcontainer_map]

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
        depends_on: Optional[List[str]] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        self.api_version = api_version
        self.resource_state = resource_state
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
    bundle_dir: Optional[str] = None,
    no_progress: Optional[bool] = None,
    confirm_yes: Optional[bool] = None,
    **kwargs,
):
    backup_manager = BackupManager(
        cmd=cmd,
        instance_name=instance_name,
        resource_group_name=resource_group_name,
        no_progress=no_progress,
    )

    backup_manager.analyze_cluster(**kwargs)

    should_bail = not should_continue_prompt(confirm_yes=confirm_yes, context="Backup")
    if should_bail:
        return

    backup_manager.output_template(bundle_dir=bundle_dir)


class BackupManager:
    def __init__(
        self,
        cmd,
        resource_group_name: str,
        instance_name: str,
        no_progress: Optional[bool] = None,
    ):
        self.cmd = cmd
        self.instance_name = instance_name
        self.resource_group_name = resource_group_name
        self.no_progress = no_progress
        self.instances = Instances(self.cmd)
        self.instance_record = self.instances.show(
            name=self.instance_name, resource_group_name=self.resource_group_name
        )

        self.resource_map = self.instances.get_resource_map(self.instance_record)
        self.rcontainer_map: Dict[str, ResourceContainer] = {}
        self.parameter_map: dict = {}
        self.active_deployment: Dict[StateResourceKey, str] = {}

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
            self._analyze_extensions()
            self._analyze_instance()
            self._analyze_instance_resources()
            self._analyze_assets()

    def output_template(self, bundle_dir: Optional[str] = None):
        template_gen = TemplateGen(self.rcontainer_map, self.parameter_map)
        template_gen.write(bundle_dir=bundle_dir)

    def _build_parameters(self, instance: dict):
        resource_id_parts = parse_resource_id(instance["id"])
        self.parameter_map.update(build_parameter(name=TemplateParams.CLUSTER_NAME.value))
        self.parameter_map.update(build_parameter(name=TemplateParams.CUSTOM_LOCATION_NAME.value))
        # TODO
        self.parameter_map.update(build_parameter(name=TemplateParams.INSTANCE_NAME.value))
        self.parameter_map.update(
            build_parameter(name=TemplateParams.SUBSCRIPTION.value, default=resource_id_parts["subscription"])
        )
        self.parameter_map.update(
            build_parameter(name=TemplateParams.RESOURCEGROUP.value, default=resource_id_parts["resource_group"])
        )

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
            self._add_resources(
                key=extension_moniker,
                api_version=api_version,
                data_iter=[extension_map[extension_type]],
                depends_on=depends_on,
                config={"apply_nested_name": False},
            )

    def _analyze_instance(self):
        api_version = self.instances.iotops_mgmt_client._config.api_version
        # TODO - @digimaun, in-efficient not good.
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
            cl_extension_ids.append(TEMPLATE_EXPRESSION_MAP["extensionId"].format(ext_resource.resource_state["name"]))
        custom_location["properties"]["clusterExtensionIds"] = cl_extension_ids

        self._add_resources(
            key=StateResourceKey.CL,
            api_version=CUSTOM_LOCATIONS_API_VERSION,
            data_iter=[custom_location],
            config={"apply_nested_name": False},
            depends_on=cl_monikers,
        )
        self._add_resources(
            key=StateResourceKey.INSTANCE,
            api_version=api_version,
            data_iter=[self.instance_record],
            depends_on=[StateResourceKey.CL],
        )
        self._build_parameters(self.instance_record)

    def _analyze_instance_resources(self):
        api_version = self.instances.iotops_mgmt_client._config.api_version
        brokers_iter = self.instances.iotops_mgmt_client.broker.list_by_resource_group(
            resource_group_name=self.resource_group_name, instance_name=self.instance_name
        )
        # Let us keep things simple atm
        default_broker = list(brokers_iter)[0]
        self._add_resources(
            key=StateResourceKey.BROKER,
            api_version=api_version,
            data_iter=[default_broker],
            depends_on=[StateResourceKey.INSTANCE],
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
                    get_resource_id_by_parts("Microsoft.Resources/deployments", self.active_deployment[active])
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
                    "Microsoft.Resources/deployments", self.active_deployment[StateResourceKey.PROFILE]
                ),
                parameters=nested_params,
            )

    def _analyze_assets(self):
        # Initial dependencies
        nested_params = {
            **build_parameter(name=TemplateParams.CUSTOM_LOCATION_NAME.value),
            **build_parameter(name=TemplateParams.INSTANCE_NAME.value),
        }
        registry_client = get_registry_mgmt_client(
            subscription_id=self.resource_map.subscription_id, api_version=REGISTRY_API_VERSION
        )
        instance_resource_id_expr = get_resource_id_by_param(
            "microsoft.iotoperations/instances", TemplateParams.INSTANCE_NAME
        )
        ext_loc_id = self.instance_record["extendedLocation"]["name"].lower()
        asset_endpoints = list(
            registry_client.asset_endpoint_profiles.list_by_resource_group(
                resource_group_name=self.resource_group_name
            )
        )
        asset_endpoints = [ep for ep in asset_endpoints if ep["extendedLocation"]["name"].lower() == ext_loc_id]
        self._add_deployment(
            key=StateResourceKey.ASSET_ENDPOINT_PROFILE,
            api_version=REGISTRY_API_VERSION,
            data_iter=asset_endpoints,
            depends_on=instance_resource_id_expr,
            parameters=nested_params,
        )

        assets = list(registry_client.assets.list_by_resource_group(resource_group_name=self.resource_group_name))
        assets = [asset for asset in assets if asset["extendedLocation"]["name"].lower() == ext_loc_id]
        if assets and asset_endpoints:
            self._add_deployment(
                key=StateResourceKey.ASSET,
                api_version=REGISTRY_API_VERSION,
                data_iter=assets,
                depends_on=get_resource_id_by_parts(
                    "Microsoft.Resources/deployments", self.active_deployment[StateResourceKey.ASSET_ENDPOINT_PROFILE]
                ),
                parameters=nested_params,
            )

    def _add_deployment(
        self,
        key: StateResourceKey,
        api_version: str,
        data_iter: Iterable,
        depends_on: Optional[Union[str, Iterable]] = None,
        parameters: Optional[dict] = None,
    ):
        data_iter = list(data_iter)
        if data_iter:
            deployment_name = get_deployment_by_key(key)
            self.active_deployment[key] = deployment_name
            deployment_container = DeploymentContainer(
                name=deployment_name,
                depends_on=depends_on,
                parameters=parameters,
            )
            deployment_container.add_resources(
                key=key,
                api_version=api_version,
                data_iter=data_iter,
            )
            self.rcontainer_map[deployment_name] = deployment_container

    def _add_resources(
        self,
        key: Union[StateResourceKey, str],
        api_version: str,
        data_iter: Iterable[dict],
        depends_on: Optional[List[Union[StateResourceKey, str]]] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        # TODO: re-write method
        if isinstance(key, StateResourceKey):
            key = key.value

        if depends_on:
            depends_on = [i.value if isinstance(i, StateResourceKey) else i for i in depends_on]
            depends_on = [i for i in depends_on if i in self.rcontainer_map]

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


class TemplateGen:
    def __init__(self, rcontainer_map: Dict[str, ResourceContainer], parameter_map: dict):
        self.rcontainer_map = rcontainer_map
        self.parameter_map = parameter_map

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
        template = self._prune_template_keys(template)
        return dumps(template, indent=2)

    def write(self, bundle_dir: Optional[str] = None):
        content = self._build_contents()

        bundle_path = get_bundle_path(bundle_dir=bundle_dir)
        with open(file=bundle_path, mode="w") as template_file:
            template_file.write(content)

    def get_base_format(self) -> dict:
        return {
            "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
            "languageVersion": "2.0",
            "contentVersion": "1.0.0.0",
            "apiProfile": "",
            "definitions": {},
            "parameters": {},
            "variables": {},
            "functions": [],
            "resources": {},
            "outputs": {},
        }


def get_bundle_path(bundle_dir: Optional[str] = None, system_name: str = "aio") -> PurePath:
    from ...util import normalize_dir

    bundle_dir_pure_path = normalize_dir(bundle_dir)
    bundle_pure_path = bundle_dir_pure_path.joinpath(default_bundle_name(system_name))
    return bundle_pure_path


def default_bundle_name(system_name: str) -> str:
    timestamp = get_timestamp_now_utc(format="%Y%m%dT%H%M%S")
    return f"backup_{timestamp}_{system_name}.json"


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
