# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

from .work import deploy
from .deletion import delete_ops_resources
from .host import run_host_verify
from .instances import Instances


__all__ = [
    "deploy",
    "delete_ops_resources",
    "run_host_verify",
    "Instances",
]
