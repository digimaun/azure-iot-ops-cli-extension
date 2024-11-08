# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

from json import dumps
from typing import Dict, List, Optional, Tuple

from azure.cli.core.azclierror import (
    ValidationError,
)
from knack.log import get_logger
from packaging import version
from rich.console import Console
from rich.json import JSON
from rich.progress import Progress, SpinnerColumn, TimeElapsedColumn
from rich.table import Table, box

from ...util import assemble_nargs_to_dict
from ...util.common import should_continue_prompt
from .resources import Instances
from .template import M3_ENABLEMENT_TEMPLATE, M3_INSTANCE_TEMPLATE

logger = get_logger(__name__)


OPS_EXTENSION_TYPES_MAP = {
    "microsoft.iotoperations.platform": "platform",
    "microsoft.openservicemesh": "openServiceMesh",
    "microsoft.azure.secretstore": "secretStore",
    "microsoft.arc.containerstorage": "containerStorage",
    "microsoft.iotoperations": "iotOperations",
}

INSTANCE_TEMPLATE = M3_INSTANCE_TEMPLATE.copy()
ENABLEMENT_TEMPLATE = M3_ENABLEMENT_TEMPLATE.copy()
MAX_DISPLAY_WIDTH = 100

DEFAULT_CONSOLE = Console(width=MAX_DISPLAY_WIDTH)


def upgrade_ops_resources(
    cmd,
    resource_group_name: str,
    instance_name: str,
    no_progress: Optional[bool] = None,
    confirm_yes: Optional[bool] = None,
    ops_config: Optional[List[str]] = None,
    ops_version: Optional[str] = None,
    ops_train: Optional[str] = None,
    acs_config: Optional[List[str]] = None,
    acs_version: Optional[str] = None,
    acs_train: Optional[str] = None,
    osm_config: Optional[List[str]] = None,
    osm_version: Optional[str] = None,
    osm_train: Optional[str] = None,
    ssc_config: Optional[List[str]] = None,
    ssc_version: Optional[str] = None,
    ssc_train: Optional[str] = None,
    plt_config: Optional[List[str]] = None,
    plt_version: Optional[str] = None,
    plt_train: Optional[str] = None,
    **kwargs,
):
    upgrade_manager = UpgradeManager(
        cmd=cmd,
        instance_name=instance_name,
        resource_group_name=resource_group_name,
        no_progress=no_progress,
    )

    with Progress(
        SpinnerColumn("star"), *Progress.get_default_columns(), "Elapsed:", TimeElapsedColumn(), transient=True
    ) as progress:
        _ = progress.add_task("Analyzing cluster...", total=None)
        upgradable_extensions = upgrade_manager.analyze_cluster(
            ops_config=ops_config,
            ops_version=ops_version,
            ops_train=ops_train,
            acs_config=acs_config,
            acs_version=acs_version,
            acs_train=acs_train,
            osm_config=osm_config,
            osm_version=osm_version,
            osm_train=osm_train,
            ssc_config=ssc_config,
            ssc_version=ssc_version,
            ssc_train=ssc_train,
            plt_config=plt_config,
            plt_version=plt_version,
            plt_train=plt_train,
        )

    if not upgradable_extensions:
        logger.warning("Nothing to upgrade :)")
        return

    return upgrade_manager.apply_upgrades(upgradable_extensions, confirm_yes)


class UpgradeManager:
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
        self.instances = Instances(self.cmd)
        self.resource_map = self.instances.get_resource_map(
            self.instances.show(name=self.instance_name, resource_group_name=self.resource_group_name)
        )
        if not self.resource_map.connected_cluster.connected:
            raise ValidationError(f"Cluster {self.resource_map.connected_cluster.cluster_name} is not connected.")

    def analyze_cluster(self, **override_kwargs: dict) -> List["ExtensionUpgradeState"]:
        cluster_state = ClusterUpgradeState(
            extensions_map=self.resource_map.connected_cluster.get_extensions_by_type(
                *list(OPS_EXTENSION_TYPES_MAP.keys())
            ),
            override_map=build_override_map(**override_kwargs),
        )
        upgradable_extensions: List["ExtensionUpgradeState"] = []
        for ext in cluster_state.extension_upgrade_states:
            if ext.can_upgrade():
                upgradable_extensions.append(ext)
        return upgradable_extensions

    def apply_upgrades(
        self, upgradable_extensions: List["ExtensionUpgradeState"], confirm_yes: Optional[bool] = None
    ) -> Optional[List[dict]]:

        table = get_default_table()
        for ext in upgradable_extensions:
            table.add_row(
                f"{ext.moniker}",
                f"{ext.current_version[0]} {{{ext.current_version[1]}}}",
                JSON(dumps(ext.get_patch())),
            )
            table.add_section()

        DEFAULT_CONSOLE.print(table)
        should_bail = not should_continue_prompt(confirm_yes=confirm_yes, context="Upgrade")
        if should_bail:
            return

        return_payload = []
        for ext in upgradable_extensions:
            print(f"Start {ext.moniker}")
            updated = self.resource_map.connected_cluster.clusters.extensions.update_cluster_extension(
                resource_group_name=self.resource_group_name,
                cluster_name=self.resource_map.connected_cluster.cluster_name,
                extension_name=ext.extension["name"],
                update_payload=ext.get_patch(),
            )
            print(f"Finish {ext.moniker}")
            return_payload.append(updated)

        return return_payload


def build_override_map(**override_kwargs) -> Dict[str, "ConfigOverride"]:
    result_map = {}
    for pair in [
        ("platform", "plt"),
        ("openServiceMesh", "osm"),
        ("secretStore", "ssc"),
        ("containerStorage", "acs"),
        ("iotOperations", "ops"),
    ]:
        config_override = ConfigOverride(
            config=override_kwargs.get(f"{pair[1]}_config"),
            version=override_kwargs.get(f"{pair[1]}_version"),
            train=override_kwargs.get(f"{pair[1]}_train"),
        )
        if not config_override.is_empty:
            result_map[pair[0]] = config_override

    return result_map


class ConfigOverride:
    def __init__(
        self,
        config: Optional[dict] = None,
        version: Optional[str] = None,
        train: Optional[str] = None,
    ):
        self.config = assemble_nargs_to_dict(config, True)
        self.version = version
        self.train = train

    @property
    def is_empty(self):
        return not any([self.config, self.version, self.train])


class ClusterUpgradeState:
    def __init__(self, extensions_map: Dict[str, dict], override_map: Dict[str, "ConfigOverride"]):
        self.extensions_map = extensions_map
        self.override_map = override_map
        self.extension_upgrade_states = self.refresh_upgrade_state()

    def refresh_upgrade_state(self) -> List["ExtensionUpgradeState"]:
        ext_queue: List["ExtensionUpgradeState"] = []

        for ext_type in OPS_EXTENSION_TYPES_MAP:
            if ext_type in self.extensions_map:
                ext_queue.append(
                    ExtensionUpgradeState(
                        extension=self.extensions_map[ext_type],
                        moniker=OPS_EXTENSION_TYPES_MAP[ext_type],
                        override=self.override_map.get(OPS_EXTENSION_TYPES_MAP[ext_type]),
                    )
                )
        return ext_queue


class ExtensionUpgradeState:
    def __init__(self, extension: dict, moniker: str, override: Optional[ConfigOverride] = None):
        self.extension = extension
        self.moniker = moniker
        self.override = override
        self.template = M3_INSTANCE_TEMPLATE if moniker == "iotOperations" else M3_ENABLEMENT_TEMPLATE

    @property
    def current_version(self) -> Tuple[str, str]:
        return (self.extension["properties"]["version"], self.extension["properties"]["releaseTrain"])

    @property
    def template_version(self) -> Tuple[str, str]:
        return (
            self.template.content["variables"]["VERSIONS"][self.moniker],
            self.template.content["variables"]["TRAINS"][self.moniker],
        )

    def can_upgrade(self) -> bool:
        return any(
            [
                version.parse(self.template_version[0]) > version.parse(self.current_version[0])
                or self.template_version[1].lower() != self.current_version[1].lower(),
                self.override,
            ]
        )

    def get_patch(self) -> dict:
        payload = {
            "properties": {"releaseTrain": self.template_version[1], "version": self.template_version[0]},
        }
        if self.override:
            if self.override.config:
                payload["properties"]["configurationSettings"] = self.override.config
            if self.override.train:
                payload["properties"]["releaseTrain"] = self.override.train
            if self.override.version:
                payload["properties"]["version"] = self.override.version

        return payload


def get_default_table() -> Table:
    table = Table(
        box=box.ROUNDED, highlight=True, expand=False, min_width=MAX_DISPLAY_WIDTH, title="The Upgrade Story"
    )
    table.add_column("Extension")
    table.add_column("Version {Train}")
    table.add_column("Patch Payload")

    return table
