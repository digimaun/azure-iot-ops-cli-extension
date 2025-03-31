# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

from typing import Iterable, Optional, List

from knack.log import get_logger

from .providers.orchestration.resources import Brokers
from .common import DEFAULT_BROKER

logger = get_logger(__name__)


def show_broker(cmd, broker_name: str, instance_name: str, resource_group_name: str) -> dict:
    return Brokers(cmd).show(name=broker_name, instance_name=instance_name, resource_group_name=resource_group_name)


def list_brokers(cmd, instance_name: str, resource_group_name: str) -> Iterable[dict]:
    return Brokers(cmd).list(instance_name=instance_name, resource_group_name=resource_group_name)


def delete_broker(
    cmd, broker_name: str, instance_name: str, resource_group_name: str, confirm_yes: Optional[bool] = None, **kwargs
):
    return Brokers(cmd).delete(
        name=broker_name,
        instance_name=instance_name,
        resource_group_name=resource_group_name,
        confirm_yes=confirm_yes,
        **kwargs,
    )


def create_broker_listener(
    cmd,
    listener_name: str,
    instance_name: str,
    resource_group_name: str,
    config_file: str,
    broker_name: str = DEFAULT_BROKER,
    **kwargs,
) -> dict:
    return Brokers(cmd).listeners.create(
        name=listener_name,
        broker_name=broker_name,
        instance_name=instance_name,
        resource_group_name=resource_group_name,
        config_file=config_file,
        **kwargs,
    )


def add_broker_listener_port(
    cmd,
    listener_name: str,
    instance_name: str,
    resource_group_name: str,
    port: int,
    broker_name: str = DEFAULT_BROKER,
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
    **kwargs,
) -> dict:
    return Brokers(cmd).listeners.add_port(
        listener_name=listener_name,
        broker_name=broker_name,
        instance_name=instance_name,
        resource_group_name=resource_group_name,
        port=port,
        service_name=service_name,
        service_type=service_type,
        authn=authn,
        authz=authz,
        protocol=protocol,
        tls_auto_issuer_name=tls_auto_issuer_name,
        tls_auto_issuer_kind=tls_auto_issuer_kind,
        tls_auto_issuer_group=tls_auto_issuer_group,
        tls_auto_duration=tls_auto_duration,
        tls_auto_private_key_algorithm=tls_auto_private_key_algorithm,
        tls_auto_private_key_rotation_policy=tls_auto_private_key_rotation_policy,
        tls_auto_san_dns=tls_auto_san_dns,
        tls_auto_san_ip=tls_auto_san_ip,
        tls_auto_secret_name=tls_auto_secret_name,
        tls_manual_secret_ref=tls_manual_secret_ref,
        **kwargs,
    )


def show_broker_listener(
    cmd, listener_name: str, instance_name: str, resource_group_name: str, broker_name: str = DEFAULT_BROKER
) -> dict:
    return Brokers(cmd).listeners.show(
        name=listener_name,
        broker_name=broker_name,
        instance_name=instance_name,
        resource_group_name=resource_group_name,
    )


def list_broker_listeners(
    cmd, instance_name: str, resource_group_name: str, broker_name: str = DEFAULT_BROKER
) -> Iterable[dict]:
    return Brokers(cmd).listeners.list(
        broker_name=broker_name, instance_name=instance_name, resource_group_name=resource_group_name
    )


def delete_broker_listener(
    cmd,
    listener_name: str,
    instance_name: str,
    resource_group_name: str,
    broker_name: str = DEFAULT_BROKER,
    confirm_yes: Optional[bool] = None,
    **kwargs,
):
    return Brokers(cmd).listeners.delete(
        name=listener_name,
        broker_name=broker_name,
        instance_name=instance_name,
        resource_group_name=resource_group_name,
        confirm_yes=confirm_yes,
        **kwargs,
    )


def create_broker_authn(
    cmd,
    authn_name: str,
    instance_name: str,
    resource_group_name: str,
    config_file: str,
    broker_name: str = DEFAULT_BROKER,
    **kwargs,
) -> dict:
    return Brokers(cmd).authns.create(
        name=authn_name,
        broker_name=broker_name,
        instance_name=instance_name,
        resource_group_name=resource_group_name,
        config_file=config_file,
        **kwargs,
    )


def show_broker_authn(
    cmd, authn_name: str, instance_name: str, resource_group_name: str, broker_name: str = DEFAULT_BROKER
) -> dict:
    return Brokers(cmd).authns.show(
        name=authn_name,
        broker_name=broker_name,
        instance_name=instance_name,
        resource_group_name=resource_group_name,
    )


def list_broker_authns(
    cmd, instance_name: str, resource_group_name: str, broker_name: str = DEFAULT_BROKER
) -> Iterable[dict]:
    return Brokers(cmd).authns.list(
        broker_name=broker_name, instance_name=instance_name, resource_group_name=resource_group_name
    )


def delete_broker_authn(
    cmd,
    authn_name: str,
    instance_name: str,
    resource_group_name: str,
    broker_name: str = DEFAULT_BROKER,
    confirm_yes: Optional[bool] = None,
    **kwargs,
):
    return Brokers(cmd).authns.delete(
        name=authn_name,
        broker_name=broker_name,
        instance_name=instance_name,
        resource_group_name=resource_group_name,
        confirm_yes=confirm_yes,
        **kwargs,
    )


def create_broker_authz(
    cmd,
    authz_name: str,
    instance_name: str,
    resource_group_name: str,
    config_file: str,
    broker_name: str = DEFAULT_BROKER,
    **kwargs,
) -> dict:
    return Brokers(cmd).authzs.create(
        name=authz_name,
        broker_name=broker_name,
        instance_name=instance_name,
        resource_group_name=resource_group_name,
        config_file=config_file,
        **kwargs,
    )


def show_broker_authz(
    cmd, authz_name: str, instance_name: str, resource_group_name: str, broker_name: str = DEFAULT_BROKER
) -> dict:
    return Brokers(cmd).authzs.show(
        name=authz_name,
        broker_name=broker_name,
        instance_name=instance_name,
        resource_group_name=resource_group_name,
    )


def list_broker_authzs(
    cmd, instance_name: str, resource_group_name: str, broker_name: str = DEFAULT_BROKER
) -> Iterable[dict]:
    return Brokers(cmd).authzs.list(
        broker_name=broker_name, instance_name=instance_name, resource_group_name=resource_group_name
    )


def delete_broker_authz(
    cmd,
    authz_name: str,
    instance_name: str,
    resource_group_name: str,
    broker_name: str = DEFAULT_BROKER,
    confirm_yes: Optional[bool] = None,
    **kwargs,
):
    return Brokers(cmd).authzs.delete(
        name=authz_name,
        broker_name=broker_name,
        instance_name=instance_name,
        resource_group_name=resource_group_name,
        confirm_yes=confirm_yes,
        **kwargs,
    )
