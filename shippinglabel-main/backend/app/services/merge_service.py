"""MergeService — 校验 + 合并: 将 ProcessService 的结果与用户 CSV 合并输出"""

import os
import re
from datetime import datetime

import time

import pandas as pd
import requests



def _rename_csv_columns(df: pd.DataFrame, csv_rename: dict) -> pd.DataFrame:
    """应用 csv_rename；若重命名后出现重复列（如 CSV 已含 PO #，又把 Order ID 改成 PO #，
    常见于误传系统导出文件），保留第一个、丢弃重复，避免 df[col] 返回 DataFrame 而崩溃。"""
    if not csv_rename:
        return df
    df = df.rename(columns=csv_rename)
    if df.columns.duplicated().any():
        dup = df.columns[df.columns.duplicated()].unique().tolist()
        df = df.loc[:, ~df.columns.duplicated()]
    return df


def validate(pdf_df: pd.DataFrame, csv_df: pd.DataFrame, merge_cfg: dict = None) -> dict:
    """
    以 CSV（订单清单）为主的校验（均为非阻断警告）:
      1. PDF 缺失的 PO — CSV 有但 PDF 没提取到（标签未打/识别失败）→ warn
      2. 匹配率 — 匹配数 / CSV 总 PO 数
    """
    if merge_cfg is None:
        merge_cfg = {}
    csv_rename = merge_cfg.get("csv_rename", {})
    csv_df = _rename_csv_columns(csv_df, csv_rename)
    merge_key = merge_cfg.get("merge_key", "PO #")
    pdf_pos = set(pdf_df[merge_key].dropna().unique())
    csv_pos = set(csv_df[merge_key].dropna().astype(str).unique())

    pdf_missing = sorted(csv_pos - pdf_pos)   # CSV 有但 PDF 没有
    csv_missing = sorted(pdf_pos - csv_pos)   # PDF 有但 CSV 没有
    matched = pdf_pos & csv_pos

    if pdf_missing:
    if csv_missing:

    # 构造前端可直接渲染的 checks 列表
    checks = [
        {
            "label": f"Compared with CSV: {len(csv_pos)} POs",
            "status": "pass",
        },
        {
            "label": f"Matched: {len(matched)}/{len(csv_pos)} POs",
            "status": "pass" if len(matched) == len(csv_pos) else "warn",
        },
    ]
    if pdf_missing:
        checks.append({
            "label": f"PDF missing {len(pdf_missing)} PO(s): {', '.join(pdf_missing)}",
            "status": "warn",
        })
    if csv_missing:
        checks.append({
            "label": f"CSV missing {len(csv_missing)} PO(s): {', '.join(csv_missing)}",
            "status": "warn",
        })

    return {
        "pdf_missing": pdf_missing,
        "csv_missing": csv_missing,
        "csv_row_count": len(csv_df),
        "pdf_po_count": len(pdf_pos),
        "csv_po_count": len(csv_pos),
        "matched_count": len(matched),
        "checks": checks,
    }


def merge(pdf_df: pd.DataFrame, csv_df: pd.DataFrame, merge_cfg: dict = None, how: str = None) -> pd.DataFrame:
    """
    将用户 CSV 与 PDF 提取结果合并。
    合并键、合并列、合并策略均从 merge_cfg 读取。
    how 参数可覆盖 merge_cfg 中的策略（预览用 outer，导出用 left）。
    """
    if merge_cfg is None:
        merge_cfg = {}
    # CSV 列名重命名（如 "Order ID" → "PO #"）
    csv_rename = merge_cfg.get("csv_rename", {})
    csv_df = _rename_csv_columns(csv_df, csv_rename)

    # 州全称标准化（如 "Western Australia" → "WA"）
    state_fullnames = merge_cfg.get("state_normalize", {})
    if state_fullnames:
        for col in ["Ship To State", "State"]:
            if col in csv_df.columns:
                for full, abbr in state_fullnames.items():
                    csv_df[col] = csv_df[col].str.replace(full, abbr, case=False, regex=False)

    merge_key = merge_cfg.get("merge_key", "PO #")
    merge_columns = merge_cfg.get("merge_columns", ["PO #", "Invoice_Number"])
    merge_strategy = how or merge_cfg.get("merge_strategy", "left")

    # 取 PDF 所有列，但去掉与 CSV 重复的列（merge_key 除外）
    pdf_cols = [c for c in pdf_df.columns if c not in csv_df.columns or c == merge_key]

    # 标记每列来源
    csv_only_cols = [c for c in csv_df.columns if c != merge_key]
    pdf_only_cols = [c for c in pdf_cols if c != merge_key]

    combined_df = pd.merge(
        csv_df,
        pdf_df[pdf_cols],
        on=merge_key,
        how=merge_strategy,
    )
    # PDF 字段排在前面展示
    pdf_first_cols = [merge_key] + pdf_only_cols + csv_only_cols
    pdf_first_cols = [c for c in pdf_first_cols if c in combined_df.columns]
    combined_df = combined_df[pdf_first_cols]

    return combined_df


def _find_sku_column(df: pd.DataFrame) -> str:
    """查找 SKU 列。"""
    for col in ("Item#", "SKU"):
        if col in df.columns:
            return col
    return ""


def _find_qty_column(df: pd.DataFrame) -> str:
    """查找数量列。"""
    for col in ("Quantity", "Item Quantity", "Qty"):
        if col in df.columns:
            return col
    return ""


def _clean_sku(val: str) -> str:
    """清洗 Excel 格式 SKU: =\"FT-001\" → FT-001"""
    return re.sub(r'^="?|"$', '', str(val)).strip()


def _parse_kit_components(item: dict) -> list:
    """从 Neto Item 解析 KitComponents，返回 [(component_sku, assemble_qty), ...] 或空列表。"""
    kc = item.get("KitComponents", [])
    if not kc or not isinstance(kc, list):
        return []
    comps = kc[0].get("KitComponent", []) if kc else []
    if not comps:
        return []
    return [(c["ComponentSKU"], int(c["AssembleQuantity"])) for c in comps]


def _kit_str(kit_comps: list) -> str:
    """将 kit 组件列表转为可读字符串。"""
    if not kit_comps:
        return ""
    return " + ".join(f"{sku} x{qty}" for sku, qty in kit_comps)


def _query_neto_inventory(skus: list, oms_cfg: dict) -> dict:
    """批量查询 Neto 库存，返回 {sku: {available, kit}} 字典。"""
    endpoint = oms_cfg.get("endpoint", "")
    api_key = oms_cfg.get("api_key", "")
    if not endpoint or not api_key:
        return {}

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "NETOAPI_ACTION": "GetItem",
        "NETOAPI_USERNAME": oms_cfg.get("username", ""),
        "NETOAPI_KEY": api_key,
    }

    BATCH = 50
    inventory = {}
    start_time = time.time()
    for i in range(0, len(skus), BATCH):
        batch = skus[i:i + BATCH]
        try:
            payload = {
                "Filter": {
                    "SKU": batch,
                    "OutputSelector": ["SKU", "AvailableSellQuantity", "KitComponents"],
                }
            }
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=30)
            items = resp.json().get("Item", [])
            for item in items:
                sku = item.get("SKU", "")
                avail_raw = item.get("AvailableSellQuantity")
                available = int(avail_raw) if avail_raw and str(avail_raw).strip() else 0
                kit_comps = _parse_kit_components(item)
                inventory[sku] = {"available": available, "kit_comps": kit_comps, "kit": _kit_str(kit_comps)}
        except Exception as e:
    elapsed = round(time.time() - start_time, 2)
    return inventory


def validate_stock(df: pd.DataFrame, oms_cfg: dict, merge_cfg: dict = None, threshold: int = 0) -> dict:
    """
    校验 CSV 中每个订单的 SKU 库存是否充足。
    - 有 Kit: 查组件库存, available = min(component_avail / assemble_qty)
    - 无 Kit: 直接查 SKU 自身库存
    - threshold: 库存 ≤ threshold 视为 low_stock
    返回: { results: [...], summary: {...} }
    """
    if merge_cfg is None:
        merge_cfg = {}
    csv_rename = merge_cfg.get("csv_rename", {})
    df = _rename_csv_columns(df, csv_rename)

    # 原始列（供前端"显示全部字段"用，排除内部临时列）
    orig_cols = [c for c in df.columns]

    merge_key = merge_cfg.get("merge_key", "PO #")
    sku_col = _find_sku_column(df)
    qty_col = _find_qty_column(df)

    if not sku_col:
        return {"error": f"SKU column not found. Columns: {df.columns.tolist()}"}
    if not qty_col:
        return {"error": f"Quantity column not found. Columns: {df.columns.tolist()}"}

    df["_clean_sku"] = df[sku_col].fillna("").astype(str).apply(_clean_sku)
    unique_skus = [s for s in df["_clean_sku"].unique() if s]

    if not unique_skus:
        return {"results": [], "summary": {"total": 0, "ok": 0, "low_stock": 0, "out_of_stock": 0}}

    # 第一次查询: 所有 SKU
    inventory = _query_neto_inventory(unique_skus, oms_cfg)

    # 第二次查询: 收集所有 kit 组件 SKU
    component_skus = set()
    for sku, inv in inventory.items():
        for comp_sku, _ in inv["kit_comps"]:
            if comp_sku not in inventory:
                component_skus.add(comp_sku)
    if component_skus:
        comp_inv = _query_neto_inventory(list(component_skus), oms_cfg)
        inventory.update(comp_inv)

    # ===== 按下单时间排序：先下单的先占库存，库存占完后时间最晚的订单判为不够 =====
    date_col = next((c for c in ["Order Place Date", "Order Date", "Date"] if c in df.columns), None)
    if date_col:
        parsed = pd.to_datetime(
            df[date_col].astype(str).str.replace(" GMT", "", regex=False).str.strip(),
            errors="coerce",
        )
        df = df.assign(_order_dt=parsed).sort_values("_order_dt", kind="stable", na_position="last")
    else:

    def _parse_qty(v):
        try:
            return int(float(str(v).strip()))
        except (ValueError, TypeError):
            return 0

    # 每个 SKU 跨所有订单的总需求量（考虑订单数量 × 商品数量）
    total_need_by_sku = (
        df.groupby("_clean_sku")[qty_col].apply(lambda s: sum(_parse_qty(x) for x in s)).to_dict()
    )
    # 每个 SKU 跨所有订单的总订单数（含可满足的 ok 单），用于 "6 of 8 orders OK" 展示
    order_count_by_sku = df.groupby("_clean_sku").size().to_dict()
    satisfiable_by_sku = {}  # 该 SKU 可满足的订单数（库存够分配的单）

    # 订单层级聚合：同一 order_id（PO #）下的商品行总数 / 可发货行数，用于 "Partial order · 2/3 items OK"
    order_items_total = {}  # {order_id: 该单商品行总数}
    order_items_ok = {}     # {order_id: 该单可发货商品行数（ok / low_stock）}

    def _sku_total_stock(inv):
        """该 SKU 的总可用件数（kit 取组件瓶颈 min(comp_avail // 单耗)）"""
        kc = inv["kit_comps"]
        if kc:
            return min((inventory[c]["available"] // q if c in inventory else 0) for c, q in kc)
        return inv["available"]

    # 可分配余量（随分配递减）；inventory 已含 kit 组件
    remaining = {sku: inv["available"] for sku, inv in inventory.items()}

    # 逐单（时间序）分配库存
    all_entries = []      # 每个商品行都建一条（含可发的 ok 行）
    problem_skus = set()  # 至少有一行缺货/不够的 SKU —— 这些 SKU 要展示名下全部订单
    for _, row in df.iterrows():
        sku = row["_clean_sku"]
        if not sku:
            continue
        order_id = str(row.get(merge_key, "")).strip()
        need = _parse_qty(row[qty_col])

        inv = inventory.get(sku)
        if inv is None:
            status = "not_found"
            available = None
            kit_comps = []
        else:
            kit_comps = inv["kit_comps"]
            available = _sku_total_stock(inv)  # 该 SKU 总库存（展示用）
            # 这一单需要的底层资源 (resource_sku, 件数)：kit 换算到组件
            reqs = [(c, need * q) for c, q in kit_comps] if kit_comps else [(sku, need)]

            if available == 0:
                status = "out_of_stock"
            elif all(remaining.get(r, 0) >= u for r, u in reqs):
                # 库存够这一单 → 扣减余量
                for r, u in reqs:
                    remaining[r] = remaining.get(r, 0) - u
                status = "low_stock" if (threshold > 0 and available <= threshold) else "ok"
                satisfiable_by_sku[sku] = satisfiable_by_sku.get(sku, 0) + 1
            else:
                # 时间靠后、库存已被先下单的订单占完 → 不够
                for r, u in reqs:
                    remaining[r] = 0
                status = "insufficient"

        # 订单层级计数：该单一个商品行，是否可发货（ok/low_stock 视为可发）
        if order_id:
            order_items_total[order_id] = order_items_total.get(order_id, 0) + 1
            if status in ("ok", "low_stock"):
                order_items_ok[order_id] = order_items_ok.get(order_id, 0) + 1

        entry = {
            "order_id": order_id,
            "sku": sku,
            "need": need,
            "total_need": total_need_by_sku.get(sku, need),
            "sku_total_orders": order_count_by_sku.get(sku, 1),
            "available": available if available is not None else "N/A",
            "status": status,
            "fields": {c: str(row.get(c, "")) for c in orig_cols},
        }
        if kit_comps:
            entry["is_kit"] = True
            entry["kit"] = _kit_str(kit_comps)
            entry["components"] = []
            for comp_sku, comp_qty in kit_comps:
                comp_inv = inventory.get(comp_sku)
                comp_avail = comp_inv["available"] if comp_inv else 0
                entry["components"].append({
                    "sku": comp_sku,
                    "qty": comp_qty,
                    "available": comp_avail,
                })
            # 标记 bottleneck
            if entry["components"]:
                min_avail = min(c["available"] // c["qty"] for c in entry["components"])
                for c in entry["components"]:
                    c["bottleneck"] = (c["available"] // c["qty"]) == min_avail
        all_entries.append(entry)
        if status in ("insufficient", "low_stock", "out_of_stock", "not_found"):
            problem_skus.add(sku)

    # 有缺口的 SKU → 返回名下全部订单（含可发的 ok 行作对照）；其余 SKU 完全省略
    results = [e for e in all_entries if e["sku"] in problem_skus]

    # 循环结束后回填 SKU 维度 & 订单维度的聚合数（loop 内读取会是阶段值）
    for entry in results:
        entry["sku_satisfiable_orders"] = satisfiable_by_sku.get(entry["sku"], 0)
        oid = entry["order_id"]
        if oid:
            entry["order_items_total"] = order_items_total.get(oid, 1)
            entry["order_items_ok"] = order_items_ok.get(oid, 0)

    # 统计按全量行的状态计（results 现含 ok 对照行，不能再用 len(results)）
    issue_count = sum(1 for e in all_entries if e["status"] != "ok")
    summary = {
        "total": len(df),
        "ok": sum(1 for e in all_entries if e["status"] == "ok"),
        "insufficient": sum(1 for e in all_entries if e["status"] == "insufficient"),
        "low_stock": sum(1 for e in all_entries if e["status"] == "low_stock"),
        "out_of_stock": sum(1 for e in all_entries if e["status"] == "out_of_stock"),
        "not_found": sum(1 for e in all_entries if e["status"] == "not_found"),
    }

    df.drop(columns=["_clean_sku", "_order_dt"], inplace=True, errors="ignore")
    return {"results": results, "summary": summary, "columns": orig_cols}


def enrich_inventory(df: pd.DataFrame, oms_cfg: dict) -> pd.DataFrame:
    """
    根据 DataFrame 中的 SKU 列，批量查询 Neto 库存，
    追加 AvailableSellQuantity 列。
    """
    sku_col = _find_sku_column(df)
    if not sku_col:
        return df

    skus = df[sku_col].dropna().astype(str).apply(_clean_sku)
    unique_skus = [s for s in skus.unique() if s]

    if not unique_skus:
        return df

    # 使用共享的 Neto 查询函数
    inventory = _query_neto_inventory(unique_skus, oms_cfg)

    # 未找到的 SKU
    missing = [s for s in unique_skus if s not in inventory]
    if missing:

    # 清洗 SKU 列用于匹配
    cleaned = skus.apply(_clean_sku)
    df["AvailableSellQuantity"] = cleaned.map(
        lambda s: str(inventory[s]["available"]) if s and s in inventory else ""
    )
    df["Kit"] = cleaned.map(
        lambda s: inventory[s]["kit"] if s and s in inventory else ""
    )


    df.attrs["_inventory_missing"] = missing
    df.attrs["_inventory_incomplete"] = []
    return df


def format_for_neto(df: pd.DataFrame, neto_cfg: dict) -> pd.DataFrame:
    """
    将合并后的 DataFrame 格式化为 Neto 导入兼容格式:
    1. 删除 Neto 不需要的内部列
    2. 重命名列为 Neto 期望的名称
    3. 添加 Neto 必填的静态字段
    """
    if not neto_cfg:
        return df

    out = df.copy()

    # 1. 删除不需要的列
    drop_cols = neto_cfg.get("drop_columns", [])
    drop_cols = [c for c in drop_cols if c in out.columns]
    if drop_cols:
        out = out.drop(columns=drop_cols)

    # 2. 复制列 (如 PO # → 额外添加 Order ID 列，保留原列)
    copy_cols = neto_cfg.get("copy_columns", {})
    for src, dst in copy_cols.items():
        if src in out.columns and dst not in out.columns:
            out[dst] = out[src]

    # 3. 添加静态列 (如 Username, Sales Channel, Order Status)
    static_cols = neto_cfg.get("static_columns", {})
    for col, val in static_cols.items():
        if col not in out.columns:
            out[col] = val

    # 4. 添加 OMS 预览列 — 让用户在导出前看到将要推送给 Neto 的字段
    oms_preview = neto_cfg.get("oms_preview", {})
    if oms_preview:
        # 4a. order_mapping: CSV列 → Neto字段名（内容相同，列名不同）
        order_mapping = oms_preview.get("order_mapping", {})
        for csv_col, neto_field in order_mapping.items():
            if csv_col in out.columns:
                out[neto_field] = out[csv_col]

        # 4b. order_line_mapping: CSV列 → Neto字段名
        line_mapping = oms_preview.get("order_line_mapping", {})
        for csv_col, neto_field in line_mapping.items():
            if csv_col in out.columns:
                out[neto_field] = out[csv_col]

        # 4c. 固定值列
        defaults = oms_preview.get("order_defaults", {})
        for field, val in defaults.items():
            out[field] = val

        # 4d. 计算列: ShipFirstName = Postcode+State, ShipLastName = PO#
        postcode_col = oms_preview.get("postcode_col", "Ship To ZIP Code")
        state_col = oms_preview.get("state_col", "Ship To State")
        po_col = oms_preview.get("po_col", "PO #")

        if postcode_col in out.columns and state_col in out.columns:
            out["ShipFirstName"] = out[postcode_col].fillna("").astype(str) + out[state_col].fillna("").astype(str)
        if po_col in out.columns:
            out["ShipLastName"] = out[po_col]
            out["InternalOrderNotes"] = out[po_col]

        # 4e. UnitPrice 含 GST（×1.1）
        price_col = oms_preview.get("price_col", "Item Cost")
        if price_col in out.columns:
            def _gst_price(val):
                try:
                    price = float(str(val).strip().replace("$", "").replace(",", ""))
                    return f"{price * 1.1:.2f}"
                except (ValueError, TypeError):
                    return val
            out["UnitPrice(GST)"] = out[price_col].apply(_gst_price)

    return out


def save(df: pd.DataFrame, save_folder: str, output_cfg: dict = None) -> str:
    """
    将合并后的 DataFrame 保存为 CSV 文件。
    output_cfg 来自 config["merge_output"]，可自定义文件名模板和时间戳格式。
    返回保存的完整文件路径。
    """
    if output_cfg is None:
        output_cfg = {}
    ts_fmt = output_cfg.get("timestamp_format", "%Y%m%d%H%M%S")
    name_tpl = output_cfg.get("output_filename", "export_{ts}.csv")

    timestamp = datetime.now().strftime(ts_fmt)
    filename = name_tpl.replace("{ts}", timestamp)
    filepath = os.path.join(save_folder, filename)
    df.to_csv(filepath, index=False)
    return filepath
