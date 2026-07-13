"""OMS Service — 将 merge 后的数据通过 AddOrder API 推送到 Neto/Maropost"""

import requests
import pandas as pd

from backend.neto_lib import load_mapping, build_payload, upload_with_verify



def push_orders(merged_df: pd.DataFrame, oms_cfg: dict, platform: str = "amazon", platform_cfg: dict = None) -> dict:
    """
    将 merge 后的 DataFrame 转为 AddOrder 请求并推送到 OMS。
    任何 platform_cfg 里配了 sheet_id 的平台都走 sheet 驱动的 neto_lib 路径,
    否则回退到老的 inline mapping。
    """
    if platform_cfg and platform_cfg.get("sheet_id"):
        return _push_via_sheet(merged_df, oms_cfg, platform_cfg)

    endpoint = oms_cfg.get("endpoint", "")
    username = oms_cfg.get("username", "")
    api_key = oms_cfg.get("api_key", "")

    if not endpoint or not api_key:
        return {"success": 0, "failed": 0, "errors": ["OMS endpoint or API key not configured"]}

    order_mapping = oms_cfg.get("order_mapping", {})
    line_mapping = oms_cfg.get("order_line_mapping", {})
    order_defaults = oms_cfg.get("order_defaults", {})

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "NETOAPI_ACTION": "AddOrder",
        "NETOAPI_USERNAME": username,
        "NETOAPI_KEY": api_key,
    }

    # 按 PO # 分组，每个 PO 是一个 Order，多行是 OrderLines
    merge_key = "PO #"
    if merge_key not in merged_df.columns:
        return {"success": 0, "failed": 0, "errors": [f"Column '{merge_key}' not found in data"]}

    grouped = merged_df.groupby(merge_key)
    orders = []

    for po, group in grouped:
        if not po or pd.isna(po):
            continue
        first_row = group.iloc[0]

        # Order 级别：先填默认值，再用 CSV 字段覆盖
        order = dict(order_defaults)
        # InternalOrderNotes 用 PO# 填充
        order["InternalOrderNotes"] = str(po)
        # Ship Full Name = Postcode+State+OrderID (按 Neto 导入模板格式)
        postcode = first_row.get("Ship To ZIP Code", "") or ""
        state = first_row.get("Ship To State", "") or ""
        if pd.notna(postcode): postcode = str(postcode).strip()
        if pd.notna(state): state = str(state).strip()
        order["ShipFirstName"] = f"{postcode}{state} {po}"
        for csv_col, api_field in order_mapping.items():
            if csv_col in first_row.index:
                val = first_row[csv_col]
                if pd.notna(val) and str(val).strip():
                    order[api_field] = str(val).strip()

        # Neto 必填: Bill 地址默认复制 Ship 地址（除非已有）
        bill_ship_map = {
            "BillFirstName": "ShipFirstName",
            "BillLastName": "ShipLastName",
            "BillStreet1": "ShipStreet1",
            "BillStreet2": "ShipStreet2",
            "BillCity": "ShipCity",
            "BillState": "ShipState",
            "BillPostCode": "ShipPostCode",
            "BillCountry": "ShipCountry",
        }
        for bill_field, ship_field in bill_ship_map.items():
            if bill_field not in order and ship_field in order:
                order[bill_field] = order[ship_field]

        # Neto 必填: Username — 用已有字段或默认 guest 账户
        if "Username" not in order:
            order["Username"] = order.get("Email", oms_cfg.get("default_username", "test@pdf2csv.lo"))

        # OrderLine 级别字段映射
        order_lines = []
        for _, row in group.iterrows():
            line = {}
            for csv_col, api_field in line_mapping.items():
                if csv_col in row.index:
                    val = row[csv_col]
                    if pd.notna(val) and str(val).strip():
                        if api_field in ("Quantity",):
                            try:
                                line[api_field] = int(float(val))
                            except (ValueError, TypeError):
                                line[api_field] = str(val).strip()
                        elif api_field == "UnitPrice":
                            try:
                                price = float(str(val).strip().replace("$", "").replace(",", ""))
                                line[api_field] = f"{price * 1.1:.2f}"
                            except (ValueError, TypeError):
                                line[api_field] = str(val).strip()
                        else:
                            line[api_field] = str(val).strip()
            if line:
                order_lines.append(line)

        if order_lines:
            order["OrderLine"] = order_lines

        if order:
            orders.append(order)

    if not orders:
        return {"success": 0, "failed": 0, "errors": ["No valid orders to push"]}

    # 发送请求
    payload = {"Order": orders}

    try:
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=30)

        # HTTP 级别检查
        if resp.status_code != 200:
            return {
                "status": "error",
                "message": f"OMS returned HTTP {resp.status_code}",
                "total": len(orders),
                "success": 0,
                "failed": len(orders),
                "errors": [resp.text[:500]],
            }

        resp_data = resp.json()

        # 解析 Neto 返回的 Messages（可能是 dict 或 list）
        messages = resp_data.get("Messages", [])
        errors = []
        warnings = []

        def _extract_text(obj):
            """从嵌套结构提取可读文本"""
            if isinstance(obj, str):
                return [obj]
            if isinstance(obj, dict):
                return [obj.get("Description") or obj.get("Message") or str(obj)]
            if isinstance(obj, list):
                texts = []
                for item in obj:
                    texts.extend(_extract_text(item))
                return texts
            return [str(obj)]

        def _parse_messages(msgs):
            if isinstance(msgs, dict):
                if msgs.get("Error"):
                    errors.extend(_extract_text(msgs["Error"]))
                if msgs.get("Warning"):
                    warnings.extend(_extract_text(msgs["Warning"]))
            elif isinstance(msgs, list):
                for msg in msgs:
                    if isinstance(msg, dict):
                        if msg.get("Error"):
                            errors.extend(_extract_text(msg["Error"]))
                        if msg.get("Warning"):
                            warnings.extend(_extract_text(msg["Warning"]))
                    elif isinstance(msg, str):
                        errors.append(msg)

        _parse_messages(messages)

        # 提取成功的 OrderID
        ack = resp_data.get("Order", [])
        success_ids = []
        if isinstance(ack, list):
            for o in ack:
                if isinstance(o, dict) and o.get("OrderID"):
                    success_ids.append(o["OrderID"])

        success_count = len(success_ids) if success_ids else (len(orders) - len(errors))

        if errors:
            status = "partial" if success_count > 0 else "error"
            message = f"{success_count}/{len(orders)} orders pushed, {len(errors)} error(s)"
        elif warnings:
            status = "warning"
            message = f"All {len(orders)} orders pushed with {len(warnings)} warning(s)"
        else:
            status = "success"
            message = f"All {len(orders)} orders pushed successfully"

        return {
            "status": status,
            "message": message,
            "total": len(orders),
            "success": success_count,
            "failed": len(errors),
            "success_ids": success_ids,
            "errors": errors,
            "warnings": warnings,
        }
    except requests.exceptions.RequestException as e:
        return {
            "status": "error",
            "message": f"Network error: {str(e)}",
            "total": len(orders),
            "success": 0,
            "failed": len(orders),
            "errors": [str(e)],
        }


# Neto 的"软警告":生产订单也是同样空着、不影响发货,过滤掉避免 dashboard 误报。
_SUPPRESSED_WARNINGS = (
    "BillLastName is a required field",
    "ShippingMethod",  # 覆盖 "not specified" 和 "Small Parcel does not exist" 等
)


def _is_suppressed(msg: str) -> bool:
    return any(s in msg for s in _SUPPRESSED_WARNINGS)


def _push_via_sheet(merged_df: pd.DataFrame, oms_cfg: dict, platform_cfg: dict) -> dict:
    """sheet 驱动路径:从 Google Sheet 读 mapping,逐单 upload + GetOrder 复核。"""
    creds = {
        "endpoint": oms_cfg.get("endpoint", ""),
        "api_key": oms_cfg.get("api_key", ""),
        "username": oms_cfg.get("username", ""),
    }
    if not creds["endpoint"] or not creds["api_key"]:
        return {"status": "error", "message": "OMS endpoint or API key not configured", "total": 0, "success": 0, "failed": 0, "errors": ["OMS endpoint or API key not configured"], "warnings": [], "success_ids": []}

    sheet_id = platform_cfg["sheet_id"]
    sheet_tab = platform_cfg.get("sheet_tab", "Sheet1")
    mapping = load_mapping(sheet_id, sheet_tab)

    csv_rows = merged_df.fillna("").astype(str).to_dict("records")

    # merge 阶段把原始 CSV 列改了名(如 amazon 的 "Order ID" → "PO #"),
    # 但 sheet mapping 仍按原始列名查 → 找不到 → 字段空。
    # 反向 rename 一份回去,让 sheet 能找到原列。skill 直传不受影响(没 merge)。
    csv_rename = platform_cfg.get("merge", {}).get("csv_rename", {})
    reverse_rename = {v: k for k, v in csv_rename.items()}
    if reverse_rename:
        for row in csv_rows:
            for renamed, original in reverse_rename.items():
                if renamed in row and original not in row:
                    row[original] = row[renamed]

    built = build_payload(mapping, csv_rows)

    success_ids, errors, warnings = [], [], []
    for item in built:
        result = upload_with_verify(item["payload"], creds, expected_po=item["po"])
        if result["status"] in ("OK", "OK_PO_MISMATCH") and result["claimed_order_id"]:
            success_ids.append(result["claimed_order_id"])
        if result["errors"]:
            errors.extend([f"PO={item['po']}: {e}" for e in result["errors"]])
        if result["warnings"]:
            kept = [w for w in result["warnings"] if not _is_suppressed(w)]
            if kept:
                warnings.extend([f"PO={item['po']}: {w}" for w in kept])

    total = len(built)
    success = len(success_ids)
    failed = total - success

    if failed > 0:
        status = "partial" if success > 0 else "error"
        message = f"{success}/{total} orders pushed, {failed} error(s)"
    elif warnings:
        status = "warning"
        message = f"All {total} orders pushed with {len(warnings)} warning(s)"
    else:
        status = "success"
        message = f"All {total} orders pushed successfully"

    return {
        "status": status,
        "message": message,
        "total": total,
        "success": success,
        "failed": failed,
        "success_ids": success_ids,
        "errors": errors,
        "warnings": warnings,
    }
