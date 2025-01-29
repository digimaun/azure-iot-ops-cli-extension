# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

from enum import Enum
from json import dumps
from pathlib import PurePath
from typing import Dict, Iterable, List, Optional, Tuple, Union
from uuid import uuid4

from azure.cli.core.azclierror import ValidationError
from knack.log import get_logger
from packaging import version
from rich.console import Console
from rich.json import JSON
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table, box

from ...util import get_timestamp_now_utc, parse_kvp_nargs, should_continue_prompt
from ...util.tools import parse_resource_id
from .common import (
    CUSTOM_LOCATIONS_API_VERSION,
    EXTENSION_MONIKER_TO_ALIAS_MAP,
    EXTENSION_TYPE_OPS,
    EXTENSION_TYPE_TO_MONIKER_MAP,
    ConfigSyncModeType,
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


class ResourceContainer:
    def __init__(
        self,
        api_version: str,
        resource_state: dict,
        depends_on: Optional[List[str]] = None,
    ):
        self.api_version = api_version
        self.resource_state = resource_state
        self.depends_on = depends_on

    # delete properties.ProvisioningState
    @classmethod
    def _prune_resource_keys(cls, resource: dict) -> dict:
        filter_keys = {"id", "systemData", "provisioningState"}
        result = {}
        for key in resource:
            if key not in filter_keys:
                result[key] = resource[key]
        if "properties" in result:
            result["properties"] = cls._prune_resource_keys(result["properties"])
        return result

    def _apply_cl_ref(self):
        if "extendedLocation" in self.resource_state:
            self.resource_state["extendedLocation"][
                "name"
            ] = "[resourceId('Microsoft.ExtendedLocation/customLocations', parameters('customLocationName'))]"

    def _apply_nested_name(self):
        test: Dict[str, Union[str, int]] = parse_resource_id(self.resource_state["id"])
        target_name = test["name"]
        last_child_num = test.get("last_child_num", 0)
        if last_child_num:
            for i in range(1, last_child_num + 1):
                target_name += f"/{test[f'child_name_{i}']}"
        self.resource_state["name"] = target_name

    def get(self):
        #self._apply_cl_ref()
        self._apply_nested_name()

        result = {
            "apiVersion": self.api_version,
            **self._prune_resource_keys(self.resource_state),
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

    def analyze_cluster(self, **override_kwargs: dict):
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
            # self._analyze_extensions()
            self._analyze_instance()
            self._analyze_instance_container()

    def output_template(self, bundle_dir: Optional[str] = None):
        template_gen = TemplateGen(self.rcontainer_map)
        template_gen.write(bundle_dir=bundle_dir)

    def _analyze_instance(self):
        aio_api_version = self.instances.iotops_mgmt_client._config.api_version
        # TODO - @digimaun, in-efficient not good.
        custom_location = self.instances._get_associated_cl(self.instance_record)
        self._add_resources(
            key=StateResourceKey.CL, api_version=CUSTOM_LOCATIONS_API_VERSION, data_iter=[custom_location]
        )
        self._add_resources(
            key=StateResourceKey.INSTANCE,
            api_version=aio_api_version,
            data_iter=[self.instance_record],
            depends_on=[StateResourceKey.CL],
        )
        # depends_on custom location

    def _analyze_instance_container(self):
        api_version = self.instances.iotops_mgmt_client._config.api_version
        brokers_iter = self.instances.iotops_mgmt_client.broker.list_by_resource_group(
            resource_group_name=self.resource_group_name, instance_name=self.instance_name
        )

        brokers = list(brokers_iter)
        self._add_resources(
            key=StateResourceKey.BROKER,
            api_version=api_version,
            data_iter=brokers,
            depends_on=[StateResourceKey.INSTANCE],
        )
        for broker in brokers:
            listeners_iter = self.instances.iotops_mgmt_client.broker_listener.list_by_resource_group(
                resource_group_name=self.resource_group_name,
                instance_name=self.instance_name,
                broker_name=broker["name"],
            )
            self._add_resources(
                key=StateResourceKey.LISTENER,
                api_version=api_version,
                data_iter=listeners_iter,
            )
            authns_iter = self.instances.iotops_mgmt_client.broker_authentication.list_by_resource_group(
                resource_group_name=self.resource_group_name,
                instance_name=self.instance_name,
                broker_name=broker["name"],
            )
            self._add_resources(
                key=StateResourceKey.AUTHN,
                api_version=api_version,
                data_iter=authns_iter,
            )
            authzs_iter = self.instances.iotops_mgmt_client.broker_authorization.list_by_resource_group(
                resource_group_name=self.resource_group_name,
                instance_name=self.instance_name,
                broker_name=broker["name"],
            )
            self._add_resources(
                key=StateResourceKey.AUTHZ,
                api_version=api_version,
                data_iter=authzs_iter,
            )

        profiles_iter = self.instances.iotops_mgmt_client.dataflow_profile.list_by_resource_group(
            resource_group_name=self.resource_group_name, instance_name=self.instance_name
        )
        profiles = list(profiles_iter)
        self._add_resources(
            key=StateResourceKey.PROFILE,
            api_version=api_version,
            data_iter=profiles,
        )
        endpoints_iter = self.instances.iotops_mgmt_client.dataflow_endpoint.list_by_resource_group(
            resource_group_name=self.resource_group_name, instance_name=self.instance_name
        )
        self._add_resources(
            key=StateResourceKey.ENDPOINT,
            api_version=api_version,
            data_iter=endpoints_iter,
        )
        for profile in profiles:
            dataflows_iter = self.instances.iotops_mgmt_client.dataflow.list_by_profile_resource(
                resource_group_name=self.resource_group_name,
                instance_name=self.instance_name,
                dataflow_profile_name=profile["name"],
            )
            self._add_resources(
                key=StateResourceKey.DATAFLOW,
                api_version=api_version,
                data_iter=dataflows_iter,
            )

    def _add_resources(
        self,
        key: StateResourceKey,
        api_version: str,
        data_iter: Iterable[dict],
        depends_on: Optional[List[StateResourceKey]] = None,
    ):
        key = key.value
        if depends_on:
            depends_on = [i.value for i in depends_on]
        to_enumerate = list(data_iter)

        count = 0
        for resource in to_enumerate:
            count += 1
            suffix = "" if count <= 1 else f"_{count}"
            target_key = f"{key}{suffix}"

            self.rcontainer_map[target_key] = ResourceContainer(
                api_version=api_version, resource_state=resource, depends_on=depends_on
            )


class TemplateGen:
    def __init__(self, rcontainer_map: Dict[str, ResourceContainer]):
        self.rcontainer_map = rcontainer_map

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
