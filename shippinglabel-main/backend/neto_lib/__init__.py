"""neto_lib — 共享的 Neto AddOrder 推送逻辑。

Sheet 驱动的字段 mapping(C=静态/D=CSV 列/E=客户特化),
按 PurchaseOrderNumber 分组合并 OrderLine,POST AddOrder + GetOrder 复核。
"""

from backend.neto_lib.sheet import load_mapping
from backend.neto_lib.payload import (
    resolve,
    build_order_fields,
    build_orderline,
    group_by_po,
    build_payload,
)
from backend.neto_lib.client import (
    post_addorder,
    get_order,
    upload_with_verify,
    extract_errors,
    extract_warnings,
    extract_created_order_id,
)

__all__ = [
    "load_mapping",
    "resolve",
    "build_order_fields",
    "build_orderline",
    "group_by_po",
    "build_payload",
    "post_addorder",
    "get_order",
    "upload_with_verify",
    "extract_errors",
    "extract_warnings",
    "extract_created_order_id",
]
