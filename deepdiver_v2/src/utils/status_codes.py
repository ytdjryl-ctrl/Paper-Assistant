# Copyright (c) 2025 Huawei Technologies Co., Ltd. All rights reserved.
#!/usr/bin/env python3
from enum import IntEnum

class JsonRpcErr(IntEnum):
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    REQUEST_TIMEOUT = -32000

