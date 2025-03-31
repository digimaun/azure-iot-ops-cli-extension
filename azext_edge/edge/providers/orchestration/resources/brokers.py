# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

from typing import TYPE_CHECKING, Iterable, Optional, List

from knack.log import get_logger
from rich.console import Console

from ....util.az_client import wait_for_terminal_state
from ....util.common import should_continue_prompt
from ....util.queryable import Queryable
from .instances import Instances
from .reskit import GetInstanceExtLoc, get_file_config

logger = get_logger(__name__)


if TYPE_CHECKING:
    from ....vendor.clients.iotopsmgmt.operations import (
        BrokerAuthenticationOperations,
        BrokerAuthorizationOperations,
        BrokerListenerOperations,
        BrokerOperations,
    )

console = Console()


class Brokers(Queryable):
    def __init__(self, cmd):
        super().__init__(cmd=cmd)
        self.instances = Instances(cmd=cmd)
        self.iotops_mgmt_client = self.instances.iotops_mgmt_client

        self.ops: "BrokerOperations" = self.iotops_mgmt_client.broker
        self.listeners = BrokerListeners(self.iotops_mgmt_client.broker_listener, self.instances.get_ext_loc)
        self.authns = BrokerAuthn(self.iotops_mgmt_client.broker_authentication, self.instances.get_ext_loc)
        self.authzs = BrokerAuthz(self.iotops_mgmt_client.broker_authorization, self.instances.get_ext_loc)

    def show(self, name: str, instance_name: str, resource_group_name: str) -> dict:
        return self.ops.get(resource_group_name=resource_group_name, instance_name=instance_name, broker_name=name)

    def list(self, instance_name: str, resource_group_name: str) -> Iterable[dict]:
        return self.ops.list_by_resource_group(resource_group_name=resource_group_name, instance_name=instance_name)

    def delete(
        self, name: str, instance_name: str, resource_group_name: str, confirm_yes: Optional[bool] = None, **kwargs
    ):
        should_bail = not should_continue_prompt(confirm_yes=confirm_yes)
        if should_bail:
            return

        with console.status("Working..."):
            poller = self.ops.begin_delete(
                resource_group_name=resource_group_name,
                instance_name=instance_name,
                broker_name=name,
            )
            return wait_for_terminal_state(poller, **kwargs)


class BrokerListeners:
    def __init__(self, ops: "BrokerListenerOperations", get_ext_loc: GetInstanceExtLoc):
        self.ops = ops
        self.get_ext_loc = get_ext_loc

    @classmethod
    def build_config(
        safe_params: List[str],
        service_name: Optional[str] = None,
        service_type: str = "LoadBalancer",
        ports: Optional[List[int]] = None,
        authn: Optional[List[str]] = None,
        authz: Optional[List[str]] = None,
        protocol: Optional[List[str]] = None,
        tls_auto_issuer_name: Optional[List[str]] = None,
        tls_auto_issuer_kind: Optional[List[str]] = None,
        tls_auto_issuer_group: Optional[List[str]] = None,
        tls_manual_x509_secret: Optional[List[str]] = None,
        san_ip: Optional[List[str]] = None,
        san_dns: Optional[List[str]] = None,
    ) -> dict:
        config = {}
        config["ports"] = []
        for port in ports:
            port_config = {"port": port}
            config["ports"].append(port_config)

        return config

    def create(
        self,
        name: str,
        broker_name: str,
        instance_name: str,
        resource_group_name: str,
        config_file: Optional[str] = None,
        config: Optional[dict] = None,
        **kwargs
    ) -> dict:
        if not any([config, config_file]):
            logger.warning("Please provide listener config via parameters or --config-file.")
            return

        listener_config = config or get_file_config(config_file)

        resource = {}
        resource["extendedLocation"] = self.get_ext_loc(name=instance_name, resource_group_name=resource_group_name)
        resource["properties"] = listener_config

        with console.status("Working..."):
            poller = self.ops.begin_create_or_update(
                resource_group_name=resource_group_name,
                instance_name=instance_name,
                broker_name=broker_name,
                listener_name=name,
                resource=resource,
            )
            return wait_for_terminal_state(poller, **kwargs)

    def add_port(
        self,
        listener_name: str,
        broker_name: str,
        instance_name: str,
        resource_group_name: str,
        port: int,
        service_name: Optional[str] = None,
        service_type: Optional[str] = None,
        authn: Optional[str] = None,
        authz: Optional[str] = None,
        protocol: Optional[str] = None,
        tls_auto_issuer_name: Optional[str] = None,
        tls_auto_issuer_kind: Optional[str] = None,
        tls_auto_issuer_group: Optional[str] = None,
        tls_auto_duration: Optional[str] = None,
        tls_auto_private_key_algorithm: Optional[str] = None,
        tls_auto_private_key_rotation_policy: Optional[str] = None,
        tls_auto_san_dns: Optional[List[str]] = None,
        tls_auto_san_ip: Optional[List[str]] = None,
        tls_auto_secret_name: Optional[str] = None,
        tls_manual_secret_ref: Optional[str] = None,
    ) -> dict:
        listener = self.show(name=listener_name, broker_name=broker_name, instance_name=instance_name, resource_group_name=resource_group_name)
        #li
        import pdb; pdb.set_trace()
        pass


    def show(self, name: str, broker_name: str, instance_name: str, resource_group_name: str) -> dict:
        return self.ops.get(
            listener_name=name,
            broker_name=broker_name,
            instance_name=instance_name,
            resource_group_name=resource_group_name,
        )

    def list(self, broker_name: str, instance_name: str, resource_group_name: str) -> Iterable[dict]:
        return self.ops.list_by_resource_group(
            resource_group_name=resource_group_name, instance_name=instance_name, broker_name=broker_name
        )

    def delete(
        self,
        name: str,
        broker_name: str,
        instance_name: str,
        resource_group_name: str,
        confirm_yes: Optional[bool] = None,
        **kwargs
    ):
        should_bail = not should_continue_prompt(confirm_yes=confirm_yes)
        if should_bail:
            return

        with console.status("Working..."):
            poller = self.ops.begin_delete(
                listener_name=name,
                broker_name=broker_name,
                instance_name=instance_name,
                resource_group_name=resource_group_name,
            )
            return wait_for_terminal_state(poller, **kwargs)


class BrokerAuthn:
    def __init__(self, ops: "BrokerAuthenticationOperations", get_ext_loc: GetInstanceExtLoc):
        self.ops = ops
        self.get_ext_loc = get_ext_loc

    def create(
        self, name: str, broker_name: str, instance_name: str, resource_group_name: str, config_file: str, **kwargs
    ):
        authn_config = get_file_config(config_file)
        resource = {}
        resource["extendedLocation"] = self.get_ext_loc(name=instance_name, resource_group_name=resource_group_name)
        resource["properties"] = authn_config

        with console.status("Working..."):
            poller = self.ops.begin_create_or_update(
                resource_group_name=resource_group_name,
                instance_name=instance_name,
                broker_name=broker_name,
                authentication_name=name,
                resource=resource,
            )
            return wait_for_terminal_state(poller, **kwargs)

    def show(self, name: str, broker_name: str, instance_name: str, resource_group_name: str) -> dict:
        return self.ops.get(
            authentication_name=name,
            broker_name=broker_name,
            instance_name=instance_name,
            resource_group_name=resource_group_name,
        )

    def list(self, broker_name: str, instance_name: str, resource_group_name: str) -> Iterable[dict]:
        return self.ops.list_by_resource_group(
            resource_group_name=resource_group_name, instance_name=instance_name, broker_name=broker_name
        )

    def delete(
        self,
        name: str,
        broker_name: str,
        instance_name: str,
        resource_group_name: str,
        confirm_yes: Optional[bool] = None,
        **kwargs
    ):
        should_bail = not should_continue_prompt(confirm_yes=confirm_yes)
        if should_bail:
            return

        with console.status("Working..."):
            poller = self.ops.begin_delete(
                authentication_name=name,
                broker_name=broker_name,
                instance_name=instance_name,
                resource_group_name=resource_group_name,
            )
            return wait_for_terminal_state(poller, **kwargs)


class BrokerAuthz:
    def __init__(self, ops: "BrokerAuthorizationOperations", get_ext_loc: GetInstanceExtLoc):
        self.ops = ops
        self.get_ext_loc = get_ext_loc

    def create(
        self, name: str, broker_name: str, instance_name: str, resource_group_name: str, config_file: str, **kwargs
    ):
        authz_config = get_file_config(config_file)
        resource = {}
        resource["extendedLocation"] = self.get_ext_loc(name=instance_name, resource_group_name=resource_group_name)
        resource["properties"] = authz_config

        with console.status("Working..."):
            poller = self.ops.begin_create_or_update(
                resource_group_name=resource_group_name,
                instance_name=instance_name,
                broker_name=broker_name,
                authorization_name=name,
                resource=resource,
            )
            return wait_for_terminal_state(poller, **kwargs)

    def show(self, name: str, broker_name: str, instance_name: str, resource_group_name: str) -> dict:
        return self.ops.get(
            authorization_name=name,
            broker_name=broker_name,
            instance_name=instance_name,
            resource_group_name=resource_group_name,
        )

    def list(self, broker_name: str, instance_name: str, resource_group_name: str) -> Iterable[dict]:
        return self.ops.list_by_resource_group(
            resource_group_name=resource_group_name, instance_name=instance_name, broker_name=broker_name
        )

    def delete(
        self,
        name: str,
        broker_name: str,
        instance_name: str,
        resource_group_name: str,
        confirm_yes: Optional[bool] = None,
        **kwargs
    ):
        should_bail = not should_continue_prompt(confirm_yes=confirm_yes)
        if should_bail:
            return

        with console.status("Working..."):
            poller = self.ops.begin_delete(
                authorization_name=name,
                broker_name=broker_name,
                instance_name=instance_name,
                resource_group_name=resource_group_name,
            )
            return wait_for_terminal_state(poller, **kwargs)
