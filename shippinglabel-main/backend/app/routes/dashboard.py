"""Dashboard 页面接口 — 基于 session 的工作流: 上传 PDF → 上传 CSV → 验证 → 合并下载"""

from __future__ import annotations  # 让 X|Y 注解延迟求值，兼容本地 Python 3.9

import os
import tempfile
import zipfile
from datetime import datetime

import fitz  # PyMuPDF
import pandas as pd
from fastapi import APIRouter, HTTPException

from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.app.config import load_config
from backend.app.services.extract_service_tw import extract as extract_tw
from backend.app.services.extract_service_amazon import extract as extract_amazon
from backend.app.services.process_service import process as process_tw
from backend.app.services.process_service_amazon import process as process_amazon
from backend.app.services.merge_service import validate, merge, save, enrich_inventory, format_for_neto, validate_stock
from backend.app.services.merge_service import (
    _rename_csv_columns, _find_sku_column, _find_qty_column, _clean_sku, _query_neto_inventory,
)
from backend.app.services.stock_warning_service import classify_warnings
from backend.app.services.group_service import group_pdfs
from backend.app.services.oms_service import push_orders
from backend.app.routes.upload import get_session

router = APIRouter()


def _learn_po_pattern(session: dict, config: dict, platform: str):
    """[已停用] 旧逻辑从 CSV Order ID 学正则，硬编码假设 D 开头。

    Amazon PO 现已从 ZPL 源明文直取（见 extract_service_amazon / process_service_amazon），
    不再需要正则，且新 PO（如 2F4UNOYN）无 D 前缀，旧学习逻辑会产出永远匹配不上的正则。
    保留空壳以兼容调用点。
    """
    return
    # 以下为旧实现，已不生效
    if platform != "amazon" or not session.get("csv_path"):
        return
    # 已学习过，直接用缓存
    if session.get("_learned_po_pattern"):
        config["po_pattern"] = session["_learned_po_pattern"]
        return
    merge_cfg = config.get("merge", {})
    csv_df = pd.read_csv(session["csv_path"], dtype=str).fillna("")
    csv_rename = merge_cfg.get("csv_rename", {})
    if csv_rename:
        csv_df = csv_df.rename(columns=csv_rename)
    if "PO #" not in csv_df.columns:
        return
    sample_ids = [oid for oid in csv_df["PO #"].dropna().unique() if len(oid) > 0]
    if not sample_ids:
        return
    endings = set(oid[-1] for oid in sample_ids)
    lengths = set(len(oid) for oid in sample_ids)
    if len(lengths) == 1:
        id_len = lengths.pop()
        ending_chars = "".join(sorted(endings))
        learned = f"D[A-Za-z0-9]{{{id_len - 2}}}[{ending_chars}]"
        config["po_pattern"] = learned
        session["_learned_po_pattern"] = learned


def _get_parent(row, parent_map):
    """获取行的 parent group。Parent_Courier 永远有值（没有 parent 时等于 Courier 自身）。"""
    return row.get("Parent_Courier", row.get("Courier", "Unknown"))


def _sort_by_hierarchy(df: pd.DataFrame, config: dict):
    """排序逻辑：
    1. 组间：按组内最大 Weight 排（desc）
    2. 组内：置顶 courier 按 top 数组顺序排最前 → 普通按 Weight → 置底按 bottom 数组顺序排最后
    """
    hierarchy = config.get("courier_hierarchy", {})
    sort_order = config.get("sort_order", {})
    courier_pin = config.get("courier_pin", {})
    top_list = courier_pin.get("top", [])
    bottom_list = courier_pin.get("bottom", [])

    # courier → parent 映射
    parent_map = {}
    for parent, info in hierarchy.items():
        parent_map[parent] = parent
        for child in info.get("children", {}):
            parent_map[child] = parent

    # 确定每行的 parent group
    if "Parent_Courier" not in df.columns and "Courier" not in df.columns:
        return df

    df["_group"] = df.apply(lambda row: _get_parent(row, parent_map), axis=1)

    # Weight 列转数值
    weight_col = None
    weight_asc = False
    for col, direction in sort_order.items():
        if col in ("Weight", "Postcode") and col in df.columns:
            weight_col = col
            weight_asc = (direction == "asc")
            break

    if weight_col:
        df["_sort_val"] = pd.to_numeric(df[weight_col], errors="coerce").fillna(0)
    else:
        df["_sort_val"] = 0

    # 1. 组间排序：按组内最大 Weight 排（desc = 重的组排前面）
    group_max = df.groupby("_group")["_sort_val"].max()
    df["_group_order"] = df["_group"].map(group_max)

    # 2. 组内排序：pin 分层
    # 置顶: 0 + top数组顺序, 普通: 1, 置底: 2 + bottom数组顺序
    top_order = {name: i for i, name in enumerate(top_list)}
    bottom_order = {name: i for i, name in enumerate(bottom_list)}

    def _pin_tier(courier):
        if courier in top_order:
            return (0, top_order[courier])
        if courier in bottom_order:
            return (2, bottom_order[courier])
        return (1, 0)

    df["_pin_tier"] = df["Courier"].map(lambda c: _pin_tier(c)[0])
    df["_pin_seq"] = df["Courier"].map(lambda c: _pin_tier(c)[1])

    # 排序: 组间(Weight desc) → 同组聚合 → 组内pin层 → pin序号 → Weight
    sort_cols = ["_group_order", "_group", "_pin_tier", "_pin_seq", "_sort_val"]
    ascending = [weight_asc, True, True, True, weight_asc]

    df = df.sort_values(sort_cols, ascending=ascending).reset_index(drop=True)

    # 清理临时列
    for tmp in ["_group", "_sort_val", "_group_order", "_pin_tier", "_pin_seq"]:
        if tmp in df.columns:
            df = df.drop(columns=[tmp])
    return df


# 平台 → (extract 函数, process 函数)
_PLATFORM_MAP = {
    "tw": (extract_tw, process_tw),
    "amazon": (extract_amazon, process_amazon),
}


# ── Request Models ───────────────────────────────────────────

class SessionRequest(BaseModel):
    session_id: str
    platform: str = "tw"  # "tw" 或 "amazon"
    edited_rows: list | None = None  # 前端编辑后的完整行列表;若提供则覆盖 merged_df


def _resolve(session_id: str):
    """从 session 中获取路径，不存在则 404"""
    s = get_session(session_id)
    if not s:
        raise HTTPException(404, f"Session not found: {session_id}")
    return s


def _apply_csv_to_page_metadata(page_metadata: list, session: dict, config: dict):
    """用 CSV 的 State/Postcode 覆盖 page_metadata"""
    merge_cfg = config.get("merge", {})
    if session.get("csv_path"):
        csv_df = pd.read_csv(session["csv_path"], dtype=str).fillna("")
        csv_rename = merge_cfg.get("csv_rename", {})
        if csv_rename:
            csv_df = csv_df.rename(columns=csv_rename)
        state_col = merge_cfg.get("csv_state_column", "Ship To State")
        pc_col = merge_cfg.get("csv_postcode_column", "Ship To ZIP Code")
        state_normalize = merge_cfg.get("state_normalize", {})
        if state_col in csv_df.columns and pc_col in csv_df.columns:
            csv_lookup = {}
            for _, row in csv_df.iterrows():
                po = str(row.get("PO #", "")).strip()
                if po:
                    st = str(row.get(state_col, "")).strip()
                    for full, abbr in state_normalize.items():
                        st = st.replace(full, abbr)
                    pc_raw = str(row.get(pc_col, "")).strip()
                    pc = pc_raw.split(".")[0].zfill(4) if pc_raw else ""
                    csv_lookup[po] = (st, pc)
            for pm in page_metadata:
                po = pm.get("PO", "")
                if po in csv_lookup:
                    st, pc = csv_lookup[po]
                    if st:
                        pm["State"] = st
                    if pc:
                        pm["Postcode"] = pc


def _apply_csv_state_postcode(df: pd.DataFrame, merge_cfg: dict) -> pd.DataFrame:
    """用 CSV 的 Ship To State / Ship To ZIP Code 覆盖 PDF 提取的 State / Postcode。
    CSV 值更可靠（来自 Amazon 订单系统），PDF 提取只做兜底。"""
    state_col = merge_cfg.get("csv_state_column", "Ship To State")
    postcode_col = merge_cfg.get("csv_postcode_column", "Ship To ZIP Code")

    if state_col in df.columns and "State" in df.columns:
        for idx, row in df.iterrows():
            csv_val = str(row.get(state_col, "")).strip()
            if csv_val:
                df.at[idx, "State"] = csv_val

    if postcode_col in df.columns and "Postcode" in df.columns:
        for idx, row in df.iterrows():
            csv_val = str(row.get(postcode_col, "")).strip()
            # 补前导零（澳洲邮编4位，如 870 → 0870）
            if csv_val and csv_val.replace(".", "").isdigit():
                csv_val = csv_val.split(".")[0].zfill(4)
            if csv_val:
                df.at[idx, "Postcode"] = csv_val

    return df


def _get_platform(platform: str):
    """根据 platform 返回 (extract_fn, process_fn, platform_config)"""
    if platform not in _PLATFORM_MAP:
        raise HTTPException(400, f"Unknown platform: {platform}. Valid: {list(_PLATFORM_MAP.keys())}")
    extract_fn, process_fn = _PLATFORM_MAP[platform]
    config = load_config()
    shared = config.get("shared", {})
    platform_cfg = {**shared, **config.get(platform, {})}
    return extract_fn, process_fn, platform_cfg


# ── STEP 0: Stock Validation ─────────────────────────────────

class StockValidateRequest(BaseModel):
    session_id: str
    platform: str = "tw"
    threshold: int = 0  # 库存 ≤ threshold 视为 low_stock


@router.post("/validate-stock")
def step0_validate_stock(req: StockValidateRequest):
    """校验 CSV 中所有 SKU 的库存是否充足（支持 Kit 组件展开 + 阈值预警）"""
    s = _resolve(req.session_id)
    if not s.get("csv_path"):
        raise HTTPException(400, "CSV not uploaded yet")

    _, _, config = _get_platform(req.platform)
    merge_cfg = config.get("merge", {})
    full_config = load_config()
    oms_cfg = full_config.get("oms", {})

    csv_df = pd.read_csv(s["csv_path"], dtype=str).fillna("")
    result = validate_stock(csv_df, oms_cfg, merge_cfg, threshold=req.threshold)

    # 缓存结果到 session
    s["_stock_validation"] = result
    return result


class RemoveInsufficientRequest(BaseModel):
    session_id: str
    platform: str = "tw"
    order_ids: list  # 要移除的 order ID 列表


@router.post("/remove-insufficient")
def step0_remove_insufficient(req: RemoveInsufficientRequest):
    """移除库存不足的订单，保存清洗后的 CSV"""
    s = _resolve(req.session_id)
    if not s.get("csv_path"):
        raise HTTPException(400, "CSV not uploaded yet")

    _, _, config = _get_platform(req.platform)
    merge_cfg = config.get("merge", {})
    csv_rename = merge_cfg.get("csv_rename", {})
    merge_key = merge_cfg.get("merge_key", "PO #")

    # 读取原始 CSV
    original_df = pd.read_csv(s["csv_path"], dtype=str).fillna("")
    original_rows = len(original_df)
    original_size = os.path.getsize(s["csv_path"])

    # 应用列重命名以匹配 merge_key
    working_df = original_df.copy()
    if csv_rename:
        working_df = working_df.rename(columns=csv_rename)

    # 过滤掉指定的订单
    remove_set = set(req.order_ids)
    mask = ~working_df[merge_key].astype(str).isin(remove_set)

    # 使用 mask 过滤原始 df（不带 rename 的）
    cleaned_df = original_df[mask.values].reset_index(drop=True)
    cleaned_rows = len(cleaned_df)

    # 保存清洗后的 CSV
    cleaned_path = s["csv_path"].replace(".csv", "_cleaned.csv")
    cleaned_df.to_csv(cleaned_path, index=False)
    cleaned_size = os.path.getsize(cleaned_path)

    # 更新 session 指向清洗后的 CSV
    s["_original_csv_path"] = s["csv_path"]
    s["csv_path"] = cleaned_path

    # 构建移除的订单详情
    removed_details = []
    stock_results = s.get("_stock_validation", {}).get("results", [])
    for oid in req.order_ids:
        reason = "Insufficient stock"
        for r in stock_results:
            if r["order_id"] == oid:
                reason = f"{r['status'].replace('_', ' ').title()} — SKU: {r['sku']} (need: {r['need']}, available: {r['available']})"
                break
        removed_details.append({"order_id": oid, "reason": reason})

    result = {
        "removed_count": original_rows - cleaned_rows,
        "remaining_count": cleaned_rows,
        "original_rows": original_rows,
        "cleaned_rows": cleaned_rows,
        "original_size": original_size,
        "cleaned_size": cleaned_size,
        "removed_details": removed_details,
    }
    return result


@router.get("/download-cleaned-csv")
def step0_download_cleaned(session_id: str):
    """下载清洗后的 CSV"""
    s = _resolve(session_id)
    csv_path = s.get("csv_path")
    if not csv_path or not os.path.isfile(csv_path):
        raise HTTPException(404, "Cleaned CSV not found")
    filename = os.path.basename(csv_path)
    return FileResponse(csv_path, media_type="text/csv", filename=filename)


# ── Stock Warning（库存预警监控）─────────────────────────────

def _build_warning_inputs(csv_df: pd.DataFrame, merge_cfg: dict, oms_cfg: dict):
    """从 CSV + Neto 库存构造 classify_warnings 的输入 (lines, stock)。

    库存查询 + kit 折算在这一层完成；classify_warnings 保持纯函数。"""
    csv_df = _rename_csv_columns(csv_df, merge_cfg.get("csv_rename", {}))
    merge_key = merge_cfg.get("merge_key", "PO #")
    sku_col = _find_sku_column(csv_df)
    qty_col = _find_qty_column(csv_df)
    if not sku_col or not qty_col:
        raise HTTPException(400, f"SKU/Qty column not found. Columns: {csv_df.columns.tolist()}")
    date_col = next((c for c in ["Order Place Date", "Order Date", "Date"] if c in csv_df.columns), None)

    csv_df = csv_df.copy()
    csv_df["_clean_sku"] = csv_df[sku_col].fillna("").astype(str).apply(_clean_sku)
    unique_skus = [x for x in csv_df["_clean_sku"].unique() if x]

    # 两遍查询：先查 SKU 本身，再补 kit 组件（与 validate_stock 一致）
    inventory = _query_neto_inventory(unique_skus, oms_cfg)
    comp_skus = {c for inv in inventory.values() for c, _ in inv["kit_comps"] if c not in inventory}
    if comp_skus:
        inventory.update(_query_neto_inventory(list(comp_skus), oms_cfg))

    def _total_stock(sku: str):
        inv = inventory.get(sku)
        if inv is None:
            return None    # Neto 无该 SKU 记录 → not_found（区别于库存为 0 的 out）
        kc = inv["kit_comps"]
        if kc:  # kit 取组件瓶颈 min(组件库存 // 单耗)
            return min((inventory[c]["available"] // q if c in inventory else 0) for c, q in kc)
        return inv["available"]

    stock = {sku: _total_stock(sku) for sku in unique_skus}

    def _parse_qty(v):
        try:
            return int(float(str(v).strip()))
        except (ValueError, TypeError):
            return 0

    lines = []
    for _, row in csv_df.iterrows():
        sku = row["_clean_sku"]
        if not sku:
            continue
        lines.append({
            "invoice": str(row.get(merge_key, "")).strip(),
            "sku": sku,
            "need": _parse_qty(row[qty_col]),
            "place_time": str(row.get(date_col, "")).strip() if date_col else "",
        })
    return lines, stock


@router.get("/stock-warning")
def stock_warning(session_id: str, platform: str = "tw", threshold: int = 0):
    """把所有订单归到三类预警（③ not_satisfy_all / ② partially_in_stock / ① out_of_stock）。

    threshold: 全局低库存阈值，SKU 总库存 <= threshold 时标 low_stock（纯标注，不改分类）。"""
    s = _resolve(session_id)
    if not s.get("csv_path"):
        raise HTTPException(400, "CSV not uploaded yet")

    _, _, config = _get_platform(platform)
    merge_cfg = config.get("merge", {})
    oms_cfg = load_config().get("oms", {})

    csv_df = pd.read_csv(s["csv_path"], dtype=str).fillna("")
    lines, stock = _build_warning_inputs(csv_df, merge_cfg, oms_cfg)
    result = classify_warnings(lines, stock, threshold=threshold)
    s["_stock_warning"] = result
    return result


# ── STEP 1: Extract ──────────────────────────────────────────

@router.post("/extract")
def step1_extract(req: SessionRequest):
    """提取 PDF 数据，如有 CSV 则自动 merge + validate，返回预览表格"""
    s = _resolve(req.session_id)
    pdf_dir = s["pdf_dir"]

    extract_fn, process_fn, config = _get_platform(req.platform)

    # Amazon 强制要求 CSV
    has_csv = bool(s.get("csv_path"))
    if req.platform == "amazon" and not has_csv:
        raise HTTPException(400, "Amazon platform requires CSV upload before Extract")

    # 从 CSV 学习 PO pattern
    _learn_po_pattern(s, config, req.platform)
    merge_cfg = config.get("merge", {})

    raw_df = extract_fn(pdf_dir, config.get("extract_output", {}).get("file_pattern", "*.pdf"))
    processed_df, _, page_metadata = process_fn(raw_df, config)

    pdf_count = raw_df["PDF File Name"].nunique()
    processed_df = processed_df.fillna("")
    s["_page_metadata"] = page_metadata
    validation = None
    # 去掉无名列(数字列如 1.0, 2.0)和 PDF File Name
    drop_cols = [c for c in processed_df.columns if isinstance(c, (int, float)) or c == "PDF File Name"]
    processed_df = processed_df.drop(columns=drop_cols, errors="ignore")

    if has_csv:
        csv_df = pd.read_csv(s["csv_path"], dtype=str).fillna("")
        validation = validate(processed_df, csv_df, merge_cfg)
        # outer join: process 列在前, CSV 列在后, 全部列展示
        preview_df = merge(processed_df, csv_df, merge_cfg, how="outer").fillna("")
        # 用 CSV 的 State/Postcode 覆盖 PDF 提取的（CSV 更可靠）
        preview_df = _apply_csv_state_postcode(preview_df, merge_cfg)
        # 追加 Neto 库存 (On Hand + Available)，缓存到 session
        full_config = load_config()
        oms_cfg = full_config.get("oms", {})
        preview_df = enrich_inventory(preview_df, oms_cfg)
        s["_inventory_cache"] = preview_df[["AvailableSellQuantity"]].to_dict() if "AvailableSellQuantity" in preview_df.columns else None
        s["_inventory_missing"] = getattr(preview_df, 'attrs', {}).get("_inventory_missing", [])
        s["_inventory_incomplete"] = getattr(preview_df, 'attrs', {}).get("_inventory_incomplete", [])
        # Neto 格式化: 添加 OMS 预览列
        neto_cfg = config.get("neto_export")
        if neto_cfg:
            preview_df = format_for_neto(preview_df, neto_cfg)
    else:
        preview_df = processed_df.copy()

    # 排序: 按 courier_hierarchy 定义顺序（使用覆盖后的 State/Postcode）
    group_keys = config.get("group_keys", ["Courier", "Weight"])
    preview_df = _sort_by_hierarchy(preview_df, config)

    po_list = processed_df["PO #"].tolist()
    preview_df.columns = [str(c) for c in preview_df.columns]

    result = {
        "pdf_count": pdf_count,
        "po_count": len(po_list),
        "po_list": po_list,
        "group_keys": group_keys,
        "has_csv": has_csv,
        "preview": {
            "columns": preview_df.columns.tolist(),
            "rows": preview_df.values.tolist(),
        },
    }
    if validation:
        result["validation"] = validation
    # Inventory 警告
    inv_missing = s.get("_inventory_missing", [])
    inv_incomplete = s.get("_inventory_incomplete", [])
    if inv_missing or inv_incomplete:
        inv_warnings = []
        if inv_missing:
            inv_warnings.append(f"SKU not found in Neto ({len(inv_missing)}): {', '.join(inv_missing)}")
        if inv_incomplete:
            inv_warnings.append(f"SKU missing inventory value ({len(inv_incomplete)}): {', '.join(inv_incomplete)}")
        result["inventory_warnings"] = inv_warnings
    return result


# ── STEP 1b: Download Extract CSV ────────────────────────────

@router.post("/extract/download")
def step1_download(req: SessionRequest):
    """下载预览表内容为 CSV（与预览一致）"""
    s = _resolve(req.session_id)
    extract_fn, process_fn, config = _get_platform(req.platform)
    _learn_po_pattern(s, config, req.platform)
    raw_df = extract_fn(s["pdf_dir"], config.get("extract_output", {}).get("file_pattern", "*.pdf"))
    processed_df, _, _ = process_fn(raw_df, config)
    processed_df = processed_df.fillna("")

    # 去掉无名列和 PDF File Name
    drop_cols = [c for c in processed_df.columns if isinstance(c, (int, float)) or c == "PDF File Name"]
    processed_df = processed_df.drop(columns=drop_cols, errors="ignore")

    # 如果有 CSV，outer join（与预览一致）
    merge_cfg = config.get("merge", {})
    if s.get("csv_path"):
        csv_df = pd.read_csv(s["csv_path"], dtype=str).fillna("")
        out_df = merge(processed_df, csv_df, merge_cfg, how="outer").fillna("")
        out_df = _apply_csv_state_postcode(out_df, merge_cfg)
    else:
        out_df = processed_df.copy()

    # 排序
    out_df = _sort_by_hierarchy(out_df, config)
    out_df.columns = [str(c) for c in out_df.columns]

    # Neto 格式化: 添加 OMS 预览列
    neto_cfg = config.get("neto_export")
    if neto_cfg:
        out_df = format_for_neto(out_df, neto_cfg)

    tmp_dir = tempfile.mkdtemp(prefix="pdf2csv_ext_")
    ext_cfg = config.get("extract_output", {})
    ts_fmt = ext_cfg.get("timestamp_format", "%Y%m%d%H%M%S")
    name_tpl = ext_cfg.get("output_filename", "extract_{ts}.csv")
    filename = name_tpl.replace("{ts}", datetime.now().strftime(ts_fmt))
    filepath = os.path.join(tmp_dir, filename)
    out_df.to_csv(filepath, index=False)

    return FileResponse(filepath, media_type="text/csv", filename=filename)


# ── STEP 3: Validate ────────────────────────────────────────



# ── STEP 4: Merge & Download ────────────────────────────────

@router.post("/merge")
def step4_merge(req: SessionRequest):
    """合并 PDF 数据与 CSV，返回文件下载"""
    s = _resolve(req.session_id)
    if not s["csv_path"]:
        raise HTTPException(400, "CSV not uploaded yet")

    extract_fn, process_fn, config = _get_platform(req.platform)
    _learn_po_pattern(s, config, req.platform)
    raw_df = extract_fn(s["pdf_dir"], config.get("extract_output", {}).get("file_pattern", "*.pdf"))
    processed_df, _, _ = process_fn(raw_df, config)
    processed_df = processed_df.fillna("")
    csv_df = pd.read_csv(s["csv_path"], dtype=str).fillna("")

    merge_cfg = config.get("merge", {})
    merged_df = merge(processed_df, csv_df, merge_cfg)
    merged_df = _apply_csv_state_postcode(merged_df, merge_cfg)
    # 追加 Neto 库存
    full_config = load_config()
    merged_df = enrich_inventory(merged_df, full_config.get("oms", {}))

    # Neto 格式化: 重命名列 + 添加静态字段 + 删除内部列
    neto_cfg = config.get("neto_export")
    if neto_cfg:
        merged_df = format_for_neto(merged_df, neto_cfg)

    tmp_dir = tempfile.mkdtemp(prefix="pdf2csv_merge_")
    filepath = save(merged_df, tmp_dir, config.get("merge_output"))

    return FileResponse(
        filepath,
        media_type="text/csv",
        filename=os.path.basename(filepath),
    )


# ── Print PDF: 按分组顺序重新拼接所有页面为一个 PDF ──────────

@router.post("/print-pdf")
def print_pdf(req: SessionRequest):
    """按 group_keys 顺序重新拼接所有 PDF 页面，返回单个 PDF 文件"""
    s = _resolve(req.session_id)
    pdf_dir = s["pdf_dir"]

    extract_fn, process_fn, config = _get_platform(req.platform)
    _learn_po_pattern(s, config, req.platform)
    group_keys = config.get("group_keys", ["Courier", "Weight"])
    raw_df = extract_fn(pdf_dir, config.get("extract_output", {}).get("file_pattern", "*.pdf"))
    _, _, page_metadata = process_fn(raw_df, config)

    # 应用人工校验 + CSV 覆盖
    _apply_csv_to_page_metadata(page_metadata, s, config)

    # 用 page_metadata 构建 DataFrame，复用 _sort_by_hierarchy 排序
    pm_df = pd.DataFrame(page_metadata)
    pm_df = _sort_by_hierarchy(pm_df, config)
    page_metadata = pm_df.to_dict("records")

    output_doc = fitz.open()
    src_cache = {}
    for pm in page_metadata:
        fname = pm["file"]
        page_idx = pm["page_idx"]
        if fname not in src_cache:
            pdf_path = os.path.join(pdf_dir, fname)
            if not os.path.isfile(pdf_path):
                continue
            src_cache[fname] = fitz.open(pdf_path)
        output_doc.insert_pdf(src_cache[fname], from_page=page_idx, to_page=page_idx)

    for src in src_cache.values():
        src.close()

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", prefix="print_labels_", delete=False)
    output_doc.save(tmp.name)
    output_doc.close()

    merge_out = config.get("merge_output", {})
    pdf_tpl = merge_out.get("pdf_output_filename", "grouped_{ts}.pdf")
    ts_fmt = merge_out.get("timestamp_format", "%Y%m%d%H%M%S")
    pdf_filename = pdf_tpl.replace("{ts}", datetime.now().strftime(ts_fmt))

    return FileResponse(
        tmp.name,
        media_type="application/pdf",
        filename=pdf_filename,
    )


# ── Group PDFs ───────────────────────────────────────────────

@router.post("/group")
def step5_group(req: SessionRequest):
    """按 config.group_keys 拆分 PDF，返回分组摘要"""
    s = _resolve(req.session_id)

    extract_fn, process_fn, config = _get_platform(req.platform)
    _learn_po_pattern(s, config, req.platform)
    group_keys = config.get("group_keys", ["Courier", "Weight"])
    raw_df = extract_fn(s["pdf_dir"], config.get("extract_output", {}).get("file_pattern", "*.pdf"))
    _, _, page_metadata = process_fn(raw_df, config)
    _apply_csv_to_page_metadata(page_metadata, s, config)

    # 先用统一排序逻辑排序 page_metadata
    pm_df = pd.DataFrame(page_metadata)
    pm_df = _sort_by_hierarchy(pm_df, config)
    page_metadata = pm_df.to_dict("records")

    result = group_pdfs(s["pdf_dir"], page_metadata, group_keys, sort_order=config.get("sort_order", {}))

    summary = {
        key: {"pages": info["pages"], "labels": info["labels"]}
        for key, info in result["groups"].items()
    }
    return {"group_keys": result["group_keys"], "groups": summary}


class GroupDownloadRequest(BaseModel):
    session_id: str
    group_key: str
    platform: str = "tw"


@router.post("/group/download")
def step5_download(req: GroupDownloadRequest):
    """下载指定分组的 PDF"""
    s = _resolve(req.session_id)

    extract_fn, process_fn, config = _get_platform(req.platform)
    _learn_po_pattern(s, config, req.platform)
    group_keys = config.get("group_keys", ["Courier", "Weight"])
    raw_df = extract_fn(s["pdf_dir"], config.get("extract_output", {}).get("file_pattern", "*.pdf"))
    _, _, page_metadata = process_fn(raw_df, config)
    _apply_csv_to_page_metadata(page_metadata, s, config)

    # 先用统一排序逻辑排序 page_metadata
    pm_df = pd.DataFrame(page_metadata)
    pm_df = _sort_by_hierarchy(pm_df, config)
    page_metadata = pm_df.to_dict("records")

    result = group_pdfs(s["pdf_dir"], page_metadata, group_keys, sort_order=config.get("sort_order", {}))

    if req.group_key not in result["groups"]:
        raise HTTPException(404, f"Group not found: {req.group_key}")

    info = result["groups"][req.group_key]
    safe_name = req.group_key.replace("/", "_")
    return FileResponse(
        info["path"],
        media_type="application/pdf",
        filename=f"{safe_name}_labels.pdf",
    )


@router.post("/group/download-all")
def step5_download_all(req: SessionRequest):
    """下载所有分组 PDF，打包成 zip"""
    s = _resolve(req.session_id)

    extract_fn, process_fn, config = _get_platform(req.platform)
    _learn_po_pattern(s, config, req.platform)
    group_keys = config.get("group_keys", ["Courier", "Weight"])
    raw_df = extract_fn(s["pdf_dir"], config.get("extract_output", {}).get("file_pattern", "*.pdf"))
    _, _, page_metadata = process_fn(raw_df, config)
    _apply_csv_to_page_metadata(page_metadata, s, config)

    # 先用统一排序逻辑排序 page_metadata
    pm_df = pd.DataFrame(page_metadata)
    pm_df = _sort_by_hierarchy(pm_df, config)
    page_metadata = pm_df.to_dict("records")

    result = group_pdfs(s["pdf_dir"], page_metadata, group_keys, sort_order=config.get("sort_order", {}))

    tmp_dir = tempfile.mkdtemp(prefix="pdf2csv_zip_")
    zip_path = os.path.join(tmp_dir, "grouped_labels.zip")
    with zipfile.ZipFile(zip_path, "w") as zf:
        for key, info in result["groups"].items():
            safe_name = key.replace("/", "_")
            zf.write(info["path"], f"{safe_name}_labels.pdf")

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename="grouped_labels.zip",
    )


# ── Send to OMS ─────────────────────────────────────────────

@router.post("/send-to-oms")
def send_to_oms(req: SessionRequest):
    """将 merge 后的数据推送到 OMS (Neto/Maropost)"""
    s = _resolve(req.session_id)
    if not s.get("csv_path"):
        raise HTTPException(400, "CSV not uploaded yet")

    extract_fn, process_fn, config = _get_platform(req.platform)
    _learn_po_pattern(s, config, req.platform)
    raw_df = extract_fn(s["pdf_dir"], config.get("extract_output", {}).get("file_pattern", "*.pdf"))
    processed_df, _, _ = process_fn(raw_df, config)
    processed_df = processed_df.fillna("")
    csv_df = pd.read_csv(s["csv_path"], dtype=str).fillna("")

    merge_cfg = config.get("merge", {})
    merged_df = merge(processed_df, csv_df, merge_cfg)
    merged_df = _apply_csv_state_postcode(merged_df, merge_cfg)

    if req.edited_rows:
        merged_df = pd.DataFrame(req.edited_rows).fillna("")

    full_config = load_config()
    oms_cfg = full_config.get("oms", {})
    platform_label = "TW" if req.platform == "tw" else "AM"
    oms_cfg = {**oms_cfg}
    oms_cfg["order_defaults"] = {**oms_cfg.get("order_defaults", {})}
    base_note = oms_cfg["order_defaults"].get("InternalOrderNotes", "")
    oms_cfg["order_defaults"]["InternalOrderNotes"] = f"[{platform_label}] {base_note}" if base_note else f"[{platform_label}]"

    # 使用平台级的字段映射 (TW 和 Amazon 列名不同)
    oms_cfg["order_mapping"] = config.get("order_mapping", {})
    oms_cfg["order_line_mapping"] = config.get("order_line_mapping", {})

    result = push_orders(merged_df, oms_cfg, platform=req.platform, platform_cfg=config)
    return result
