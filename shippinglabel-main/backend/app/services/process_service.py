"""ProcessService (TW) — 数据加工: 将 ExtractService 的扁平原始数据加工为带 Invoice_Number 的结果"""

import re

import pandas as pd

from backend.app.config import load_config



def _find_po(text: str, prefix: str, exclude: list = None) -> str:
    """从文本中找最后一个 <prefix>+纯数字 的独立词（如 TW53416910），找不到返回空字符串。
    要求前缀后必须全是数字，从而天然排除以同前缀开头的地名/运单号（如 TWEED、TWMF*）。
    exclude 列表保留作为额外兜底（前缀后是数字但仍需排除的特例）。"""
    exclude = exclude or []
    result = ""
    for word in text.split():
        if not word.startswith(prefix):
            continue
        rest = word[len(prefix):]
        if not rest.isdigit():
            continue
        if any(word.startswith(ex) for ex in exclude):
            continue
        result = word
    return result



def _match_courier(text: str, courier_map: dict, hierarchy: dict = None) -> str:
    """在文本中匹配快递公司关键词，返回快递名称（大小写不敏感）。
    如果 hierarchy 有 children，优先匹配 children，parent 关键词最后兜底。"""
    if hierarchy is None:
        hierarchy = {}
    text_lower = text.lower()
    # 判断哪些 name 是有 children 的 parent
    parents_with_children = {
        name for name in set(courier_map.values())
        if name in hierarchy and hierarchy[name].get("children")
    }
    # 先匹配 children / 无 children 的独立 courier
    for keyword, name in courier_map.items():
        if name not in parents_with_children and keyword.lower() in text_lower:
            return name
    # 再匹配有 children 的 parent（兜底）
    for keyword, name in courier_map.items():
        if name in parents_with_children and keyword.lower() in text_lower:
            return name
    return ""


def _extract_weight(text: str, pattern: str) -> str:
    """用正则从文本中提取重量(kg)，返回最后一个匹配的数值字符串。"""
    matches = re.findall(pattern, text, re.IGNORECASE)
    return matches[-1] if matches else ""


def _extract_volume(text: str, pattern: str) -> str:
    """用正则从文本中提取体积(m3)，返回最后一个匹配的数值字符串。"""
    matches = re.findall(pattern, text, re.IGNORECASE)
    return matches[-1] if matches else ""


def _is_sender_context(text: str, match_pos: int, sender_keywords: list, window: int = 100) -> bool:
    """检查匹配位置前 window 个字符内是否包含发件人关键词（忽略大小写）。
    只看前方，因为发件人标识（FROM、SENDER、仓库名）出现在发件人地址之前。"""
    start = max(0, match_pos - window)
    context = text[start:match_pos].upper()
    return any(kw.upper() in context for kw in sender_keywords)


def _normalize_state_names(text: str, fullname_map: dict) -> str:
    """将州全称替换为简称，如 'New South Wales' → 'NSW'。"""
    for full, abbr in fullname_map.items():
        text = re.sub(re.escape(full), abbr, text, flags=re.IGNORECASE)
    return text


def _extract_state_postcode(text: str, au_states: list, sender_keywords: list = None, state_fullnames: dict = None) -> tuple[str, str]:
    """
    从文本中提取收件人的澳洲州缩写和邮编(4位数字)。
    支持两种格式:
      - 'QLD 4006'  (州在前)
      - '4005 QLD'  (邮编在前, Couriers Please)
    用 (?<!\d) 和 (?!\d) 防止匹配到电话/年份等连续数字。
    通过 sender_keywords 检查匹配位置周围是否为发件人地址，是则跳过。
    """
    if sender_keywords is None:
        sender_keywords = []
    if state_fullnames:
        text = _normalize_state_names(text, state_fullnames)
    states = '|'.join(au_states)
    # 格式1: STATE 1234
    p1 = r'(?<!\d)\b(' + states + r')\s+(\d{4})\b(?!\d)'
    # 格式2: 1234 STATE
    p2 = r'(?<!\d)\b(\d{4})\s+(' + states + r')\b(?!\d)'

    for m in re.finditer(p1, text):
        if not _is_sender_context(text, m.start(), sender_keywords):
            return m.group(1), m.group(2)

    for m in re.finditer(p2, text):
        if not _is_sender_context(text, m.start(), sender_keywords):
            return m.group(2), m.group(1)

    return "", ""


def process(raw_df: pd.DataFrame, config: dict = None) -> tuple[pd.DataFrame, dict]:
    """
    输入: ExtractService 返回的扁平 DataFrame
        | PDF File Name | Page Number | Line Number | Data |

    输出: (去重后的结果 DataFrame, 快递分组映射 dict)
        DataFrame: | PDF File Name | PO # | Label_Qty | Single/Multi_Ctn | Courier | Invoice_Number | 1.0 | 2.0 | ... |
        快递分组映射: { "Toll": { "batch1.pdf": [0, 2], "batch2.pdf": [1] }, ... }
            第一层: 快递公司名称
            第二层: PDF 文件名
            第三层: 该文件中属于该快递的页码列表 (从 0 开始，供 PyMuPDF 使用)
    """
    if config is None:
        config = load_config()


    po_prefix = config["po_prefix"]
    po_prefix_exclude = config.get("po_prefix_exclude", [])
    courier_map = config["courier_map"]
    invoice_prefix = config["invoice_prefix"]
    hierarchy = config.get("courier_hierarchy", {})

    # 从 hierarchy 构建 courier→parent 映射和 courier→code 映射
    parent_map = {}   # { "Kiwi": "Allied", "Allied": "Allied", "Aus_Post": "Aus_Post", ... }
    code_map = {}     # { "Allied": "RY", "Kiwi": "KW", "Aus_Post": "AP", ... }
    for parent, info in hierarchy.items():
        parent_map[parent] = parent
        code_map[parent] = info.get("code", "XX")
        for child, child_info in info.get("children", {}).items():
            parent_map[child] = parent
            code_map[child] = child_info.get("code", "XX")

    # ========== 步骤 1: Pivot — 扁平行展开为宽表 ==========
    df = raw_df.pivot_table(
        index=["PDF File Name", "Page Number"],
        columns="Line Number",
        values="Data",
        aggfunc=" ".join,
    ).reset_index()

    data_cols = [c for c in df.columns if isinstance(c, (int, float))]

    # 将每行所有数据列拼成一个完整文本，供后续步骤复用
    full_text = df[data_cols].fillna("").apply(lambda row: " ".join(row), axis=1)


    # ========== 步骤 2: PO # — 拆词匹配 ==========
    df["PO #"] = full_text.apply(lambda t: _find_po(t, po_prefix, po_prefix_exclude))

    po_found = (df["PO #"] != "").sum()

    # ========== 步骤 3: Courier — 关键词匹配 ==========
    df["Courier"] = full_text.apply(lambda t: _match_courier(t, courier_map, hierarchy) or "Unknown")

    # 调试：看匹配结果分布
    courier_counts = df["Courier"].value_counts().to_dict()

    # Parent_Courier: 有 parent 用 parent，没有 parent 用自身
    df["Parent_Courier"] = df["Courier"].map(lambda c: parent_map.get(c, c))

    courier_found = (df["Courier"] != "").sum()
    unmatched = df[df["Courier"] == ""]["PO #"].tolist()

    # ========== 步骤 3a: Weight — 正则提取重量 ==========
    weight_regex = config.get("weight_regex", r"(\d+\.?\d*)\s*[kK][gG]")
    df["Weight"] = full_text.apply(lambda t: _extract_weight(t, weight_regex))
    weight_found = (df["Weight"] != "").sum()

    # ========== 步骤 3b: State — 州(排除发件人) ==========
    au_states = config.get("au_states", [])
    sender_keywords = config.get("sender_keywords", [])
    nz_couriers = config.get("nz_couriers", ["Kiwi"])
    state_postcode = full_text.apply(lambda t: _extract_state_postcode(t, au_states, sender_keywords, config.get("au_state_fullnames", {})))
    df["State"] = state_postcode.apply(lambda x: x[0])
    df["Postcode"] = state_postcode.apply(lambda x: x[1])
    # NZ 快递的订单，State 直接设为 NZ
    df.loc[df["Courier"].isin(nz_couriers), "State"] = "NZ"
    state_found = (df["State"] != "").sum()
    postcode_found = (df["Postcode"] != "").sum()

    # ========== 步骤 4: Label_Qty + Single/Multi_Ctn ==========
    df["Label_Qty"] = df.groupby("PO #")["PO #"].transform("count")
    df["Single/Multi_Ctn"] = df["Label_Qty"].map(lambda q: "S" if q == 1 else "M")

    # ========== 步骤 5: Invoice_Number — 按模板拼接 ==========
    invoice_template = config.get("invoice_template", ["{prefix}", "{courier_code}", "{S/M}", "{PO}"])
    # "parent" 用一级 courier code，"child" 用二级 courier code
    courier_level = config.get("invoice_courier_level", "child")

    def _build_invoice(row):
        if courier_level == "parent":
            courier_code = code_map.get(parent_map.get(row["Courier"], row["Courier"]), "XX")
        else:
            courier_code = code_map.get(row["Courier"], "XX")
        parts_map = {
            "{prefix}": invoice_prefix,
            "{courier_code}": courier_code,
            "{S/M}": row["Single/Multi_Ctn"],
            "{PO}": row["PO #"],
        }
        return "".join(parts_map.get(p, p) for p in invoice_template)

    df["Invoice_Number"] = df.apply(_build_invoice, axis=1)

    # ========== 步骤 5b: 空值检查 — 逐行检查关键字段 ==========
    check_cols = ["PO #", "Courier", "Parent_Courier", "Weight", "State", "Postcode", "Invoice_Number"]
    for idx, row in df.iterrows():
        empty_fields = [col for col in check_cols if col in row.index and (not row[col] or str(row[col]).strip() == "")]
        if empty_fields:

    # ========== 步骤 6a: 保存每页元数据（去重前）==========
    page_metadata = []
    for _, row in df.iterrows():
        page_metadata.append({
            "file": row["PDF File Name"],
            "page_idx": int(row["Page Number"]) - 1,  # PyMuPDF 从 0 开始
            "Parent_Courier": row["Parent_Courier"],
            "Courier": row["Courier"],
            "Weight": row["Weight"] or "",
            "State": row["State"] or "",
            "Postcode": row["Postcode"] or "",
        })

    # 兼容旧接口: 生成 courier_file_pages
    courier_file_pages = {}
    for pm in page_metadata:
        courier = pm["Courier"]
        fname = pm["file"]
        if courier not in courier_file_pages:
            courier_file_pages[courier] = {}
        if fname not in courier_file_pages[courier]:
            courier_file_pages[courier][fname] = []
        courier_file_pages[courier][fname].append(pm["page_idx"])


    # ========== 步骤 6b: 去重 — 每个 PO 只保留一条 ==========
    before_dedup = len(df)
    df = df.drop_duplicates(subset="PO #", keep="first")

    meta_cols = ["PDF File Name", "PO #", "Label_Qty", "Single/Multi_Ctn", "Parent_Courier", "Courier", "Weight", "State", "Postcode", "Invoice_Number"]
    df = df[meta_cols + data_cols].reset_index(drop=True)


    return df, courier_file_pages, page_metadata
