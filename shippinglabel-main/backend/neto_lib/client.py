"""Neto API client — AddOrder 推送 + GetOrder 复核。"""


import requests



def _headers(action, creds):
    return {
        "NETOAPI_ACTION": action,
        "NETOAPI_KEY": creds["api_key"],
        "NETOAPI_USERNAME": creds["username"],
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def post_addorder(payload, creds):
    """POST AddOrder。返回 (status_code, parsed_response)。"""
    r = requests.post(
        creds["endpoint"],
        headers=_headers("AddOrder", creds),
        json=payload,
        timeout=30,
    )
    return r.status_code, (r.json() if r.ok else r.text)


def get_order(order_id, creds):
    """GetOrder 复核。返回 Order list(可能为空)。"""
    if not order_id:
        return []
    payload = {
        "Filter": {
            "OrderID": [order_id],
            "OutputSelector": ["OrderID", "OrderStatus", "PurchaseOrderNumber", "Username"],
        }
    }
    r = requests.post(
        creds["endpoint"],
        headers=_headers("GetOrder", creds),
        json=payload,
        timeout=30,
    )
    return r.json().get("Order", [])


def _extract_messages(resp, severity):
    if not isinstance(resp, dict):
        return []
    msgs = resp.get("Messages", {}).get(severity, [])
    if isinstance(msgs, dict):
        msgs = [msgs]
    return [m.get("Message", "") for m in msgs if isinstance(m, dict) and m.get("Message")]


def extract_errors(resp):
    if isinstance(resp, dict) and resp.get("Ack") == "Error":
        return _extract_messages(resp, "Error")
    return []


def extract_warnings(resp):
    return _extract_messages(resp, "Warning")


def extract_created_order_id(resp):
    """AddOrder 成功(Ack ∈ Success/Warning)时,response 里返回的 Neto OrderID。"""
    if not isinstance(resp, dict) or resp.get("Ack") == "Error":
        return None
    order = resp.get("Order")
    if isinstance(order, dict):
        return order.get("OrderID") or None
    if isinstance(order, list) and order:
        first = order[0]
        if isinstance(first, dict):
            return first.get("OrderID") or None
    return None


def upload_with_verify(payload, creds, expected_po=None):
    """AddOrder + GetOrder 复核,返回结构化结果。

    返回 {
        status: "OK" | "OK_PO_MISMATCH" | "FAIL_VALIDATION" | "FAIL_CLAIMED_NOT_FOUND" | "FAIL_HTTP",
        http_status: int,
        ack: str | None,
        claimed_order_id: str | None,
        verified: dict | None,
        errors: [str],
        warnings: [str],
    }
    """
    http_status, resp = post_addorder(payload, creds)
    if http_status != 200:
        return {
            "status": "FAIL_HTTP",
            "http_status": http_status,
            "ack": None,
            "claimed_order_id": None,
            "verified": None,
            "errors": [str(resp)[:500]],
            "warnings": [],
        }

    ack = resp.get("Ack") if isinstance(resp, dict) else None
    errors = extract_errors(resp)
    warnings = extract_warnings(resp)
    claimed_id = extract_created_order_id(resp)

    in_neto = get_order(claimed_id, creds) if claimed_id else []
    verified = in_neto[0] if in_neto else None

    if verified:
        neto_po = verified.get("PurchaseOrderNumber", "")
        if expected_po and neto_po != expected_po:
            status = "OK_PO_MISMATCH"
        else:
            status = "OK"
    elif errors:
        status = "FAIL_VALIDATION"
    elif claimed_id:
        status = "FAIL_CLAIMED_NOT_FOUND"
    else:
        status = "FAIL_VALIDATION"

    return {
        "status": status,
        "http_status": http_status,
        "ack": ack,
        "claimed_order_id": claimed_id,
        "verified": verified,
        "errors": errors,
        "warnings": warnings,
    }
