"""构造 Neto AddOrder payload。"""

import re

_EXCEL_SKU_WRAPPER = re.compile(r'^="(.+)"$')


def resolve(parts, csv_row):
    """按 C → D → E 优先级从 (c, d, e) tuple 列表中取值,拼接成字符串。"""
    if not parts:
        return None
    out = []
    for c, d, e in parts:
        if c:
            out.append(c)
        elif d:
            out.append(str(csv_row.get(d, "")))
        elif e:
            out.append(e)
    return "".join(out)


def build_order_fields(mapping, csv_row):
    """Order 级字段(非 OrderLine.*),用组里第一行 CSV 生成。"""
    order = {}
    for field, parts in mapping.items():
        if field.startswith("OrderLine."):
            continue
        val = resolve(parts, csv_row)
        if val is None:
            continue
        order[field] = val
    return order


def build_orderline(mapping, csv_row):
    """单个 OrderLine,从 mapping 中 OrderLine.* 字段构造。"""
    line = {}
    for field, parts in mapping.items():
        if not field.startswith("OrderLine."):
            continue
        val = resolve(parts, csv_row)
        if val is None:
            continue
        line[field.split(".", 1)[1]] = val
    if "Quantity" in line:
        try:
            line["Quantity"] = int(line["Quantity"])
        except ValueError:
            pass
    sku = line.get("SKU")
    if isinstance(sku, str):
        m = _EXCEL_SKU_WRAPPER.match(sku)
        if m:
            line["SKU"] = m.group(1)
    return line


def group_by_po(mapping, csv_rows):
    """按 PurchaseOrderNumber 分组;同 PO 的多行合并为一单。
    PO 为空的行各自独立,不与其他空 PO 行合并。返回 [(po_or_key, [rows])]。"""
    po_parts = mapping.get("PurchaseOrderNumber")
    groups = {}
    order_keys = []
    for i, row in enumerate(csv_rows):
        po = resolve(po_parts, row) if po_parts else None
        key = po if po else f"__no_po_{i}"
        if key not in groups:
            groups[key] = []
            order_keys.append(key)
        groups[key].append(row)
    return [(k, groups[k]) for k in order_keys]


def build_payload(mapping, csv_rows):
    """一站式:CSV rows → AddOrder payload。返回 [{order, lines, payload}]。"""
    groups = group_by_po(mapping, csv_rows)
    results = []
    for key, group_rows in groups:
        order = build_order_fields(mapping, group_rows[0])
        lines = [build_orderline(mapping, r) for r in group_rows]
        lines = [l for l in lines if l]
        if lines:
            order["OrderLine"] = lines
        po = key if not str(key).startswith("__no_po_") else None
        results.append({
            "po": po,
            "lines": lines,
            "payload": {"Order": [order]},
        })
    return results
