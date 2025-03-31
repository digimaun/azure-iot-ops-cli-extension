# coding=utf-8
# ----------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License file in the project root for license information.
# ----------------------------------------------------------------------------------------------

import json
from typing import Protocol, Dict, List

from ....util import read_file_content


def get_file_config(file_path: str) -> dict:
    config = json.loads(read_file_content(file_path=file_path))
    if "properties" in config:
        config = config["properties"]
    return config


class GetInstanceExtLoc(Protocol):
    def __call__(self, name: str, resource_group_name: str) -> Dict[str, str]:
        ...


def _sanitize_safe_params(safe_params: list, keep: list) -> list:
    """
    Intended to filter un-related params,
    leaving only related params with inherent positional indexing
    to be used by the _associate_related function.
    """
    result: List[str] = []
    if not safe_params:
        return result
    for param in safe_params:
        if param in keep:
            result.append(param)
    return result

def _associate_related(sanitized_params: list, key: str) -> dict:
    """
    Intended to associate related param indexes. For example
    associate --file with the nearest --step or associate --related-file
    with the nearest --file.
    """
    result: Dict[int, list] = {}
    if not sanitized_params:
        return result
    params_len = len(sanitized_params)
    key_index = 0
    related_key_index = 0
    for i in range(params_len):
        if sanitized_params[i] == key:
            result[key_index] = []
            for j in range(i + 1, params_len):
                if sanitized_params[j] == key:
                    break
                result[key_index].append(related_key_index)
                related_key_index = related_key_index + 1
            key_index = key_index + 1
    return result
