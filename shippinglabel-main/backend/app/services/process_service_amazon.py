"""ProcessService (Amazon) — Amazon 面单数据加工: 从 ExtractService 的扁平数据提取 Order ID、Courier 等"""

import re

import pandas as pd

from backend.app.config import load_config



def _find_po_regex(text: str, pattern: str) -> list[str]:
    """用正则从文本中提取所有匹配的 PO/Order ID，返回去重列表。"""
    matches = re.findall(pattern, text)
    seen = set()
    result = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


def _match_courier(text: str, courier_map: dict) -> str:
    """在文本中匹配快递公司关键词，返回快递名称。"""
    for keyword, name in courier_map.items():
        if keyword in text:
            return name
    return ""


def _normalize_state_names(text: str, fullname_map: dict) -> str:
    """将州全称替换为简称，如 'New South Wales' → 'NSW'。"""
    for full, abbr in fullname_map.items():
        text = re.sub(re.escape(full), abbr, text, flags=re.IGNORECASE)
    return text


def _is_sender_context(text: str, match_pos: int, sender_keywords: list, window: int = 100) -> bool:
    """检查匹配位置前 window 个字符内是否包含发件人关键词（忽略大小写）。
    用完整的发件人信息（公司名、街道、仓库名等）来判断。"""
    start = max(0, match_pos - window)
    context = text[start:match_pos].upper()
    return any(kw.upper() in context for kw in sender_keywords)


def _nearby_has_sender_keyword(text: str, match_pos: int, sender_keywords: list, window: int = 80) -> bool:
    """检查匹配位置前后 window 字符内是否包含发件人关键词（街道名排除法）。"""
    start = max(0, match_pos - window)
    end = min(len(text), match_pos + window)
    context = text[start:end].upper()
    return any(kw.upper() in context for kw in sender_keywords)


def _extract_state_postcode(text: str, au_states: list, sender_keywords: list = None, state_fullnames: dict = None) -> tuple[str, str]:
    """从文本中提取收件人的澳洲州缩写和邮编(4位数字)。

    逻辑:
      1. 收集所有候选并按 (state, postcode) 去重
      2. sender_keywords 上下文过滤
         - 剩余 = 1 → 返回（正常）
         - 剩余 = 0 → 二次验证
         - 剩余 > 1 → 标记需人工校验，返回空值
      3. 二次验证: Destination → SHIP TO → 街道名排除
      4. 仍失败 → 返回空值 + error 日志
    """
    if sender_keywords is None:
        sender_keywords = []
    if state_fullnames:
        text = _normalize_state_names(text, state_fullnames)
    states = '|'.join(au_states)

    # ========== 第一步: 收集所有候选 (state, postcode, 位置) ==========
    raw_candidates = []

    # 格式1: STATE 1234
    for m in re.finditer(r'(?<!\d)\b(' + states + r')\s+(\d{4})\b(?!\d)', text, re.IGNORECASE):
        raw_candidates.append((m.group(1).upper(), m.group(2), m.start()))

    # 格式2: 1234 STATE 或 1234 CITY, STATE
    for m in re.finditer(r'(?<!\d)\b(\d{4})\s+(' + states + r')\b(?!\d)', text, re.IGNORECASE):
        raw_candidates.append((m.group(2).upper(), m.group(1), m.start()))
    for m in re.finditer(r'(?<!\d)\b(\d{4})\s+[A-Za-z][A-Za-z\s]+,\s*(' + states + r')\b', text, re.IGNORECASE):
        raw_candidates.append((m.group(2).upper(), m.group(1), m.start()))

    if not raw_candidates:
        return "", ""

    # ========== 第二步: sender_keywords 上下文过滤（保留所有位置） ==========
    recipients = []
    senders = []
    for state, postcode, pos in raw_candidates:
        if _is_sender_context(text, pos, sender_keywords):
            senders.append((state, postcode, pos))
        else:
            recipients.append((state, postcode, pos))

    # 按 (state, postcode) 去重
    def _dedup(items):
        seen = set()
        result = []
        for s, p, pos in items:
            if (s, p) not in seen:
                seen.add((s, p))
                result.append((s, p, pos))
        return result

    unique_recipients = _dedup(recipients)

    # 去重后剩余 = 1 → 正常返回
    if len(unique_recipients) == 1:
        return unique_recipients[0][0], unique_recipients[0][1], False

    # 去重后剩余 > 1 → 需要人工校验
    if len(unique_recipients) > 1:
        return "", "", True

    # ========== 第三步: 剩余 = 0 → 二次验证 ==========

    # 策略①: Destination 标记
    dest_match = re.search(r'Destination\s*:\s*(' + states + r')\b', text, re.IGNORECASE)
    if dest_match:
        after_dest = text[dest_match.start():]
        pc_after = re.search(r'(?<!\d)\b(\d{4})\b(?!\d)', after_dest)
        if pc_after:
            return dest_match.group(1).upper(), pc_after.group(1), False

    # 策略②: SHIP TO 标记
    ship_to_pos = -1
    for marker in ["SHIP TO:", "SHIP TO"]:
        pos = text.upper().find(marker)
        if pos >= 0:
            ship_to_pos = pos + len(marker)
            break
    if ship_to_pos >= 0:
        for state, postcode, pos in senders:
            if pos > ship_to_pos:
                return state, postcode, False

    # 策略③: 街道名排除
    for state, postcode, pos in senders:
        if not _nearby_has_sender_keyword(text, pos, sender_keywords, window=80):
            return state, postcode, False

    # ========== 第四步: 仍失败 → 需人工校验 ==========
    return "", "", True


def process(raw_df: pd.DataFrame, config: dict = None) -> tuple[pd.DataFrame, dict, list]:
    """
    处理 Amazon PDF 面单。

    输入: extract_service_amazon 返回的 DataFrame（含 Logo_Text 列）
        | PDF File Name | Page Number | Line Number | Data | Logo_Text |

    特点:
    - 一页 PDF 可能包含多张面单 → 正则提取多个 Order ID
    - 结合 Logo_Text + 文本关键词匹配 Courier
    - 不需要 Invoice_Number

    返回: (df, courier_file_pages, page_metadata)
    """
    if config is None:
        config = load_config()


    po_pattern = config.get("po_pattern", r"D[A-Za-z0-9]{7,9}p")
    courier_map = config.get("courier_map", {})
    sender_keywords = config.get("sender_keywords", [])
    au_states = config.get("au_states", [])
    hierarchy = config.get("courier_hierarchy", {})

    # 构建 parent_map: Dragonfly→Others, CODE Express→Others, Couriers Please→Couriers Please
    parent_map = {}
    for parent, info in hierarchy.items():
        parent_map[parent] = parent
        for child in info.get("children", {}):
            parent_map[child] = parent

    # ========== 步骤 1: Pivot — 按页拼接全文 ==========
    df = raw_df.pivot_table(
        index=["PDF File Name", "Page Number"],
        columns="Line Number",
        values="Data",
        aggfunc=" ".join,
    ).reset_index()

    data_cols = [c for c in df.columns if isinstance(c, (int, float))]
    full_text = df[data_cols].fillna("").apply(lambda row: " ".join(row), axis=1)

    # 提取每页的 Logo_Text（同一页所有行共享，取第一个非空值）
    logo_by_page = raw_df.groupby(["PDF File Name", "Page Number"])["Logo_Text"].first().reset_index()
    df = df.merge(logo_by_page, on=["PDF File Name", "Page Number"], how="left")
    df["Logo_Text"] = df["Logo_Text"].fillna("")

    # 提取每页的 ZPL_PO（从 ZPL 源明文解析的 PO，同一页所有行共享）
    if "ZPL_PO" in raw_df.columns:
        zpl_by_page = raw_df.groupby(["PDF File Name", "Page Number"])["ZPL_PO"].first().reset_index()
        df = df.merge(zpl_by_page, on=["PDF File Name", "Page Number"], how="left")
        df["ZPL_PO"] = df["ZPL_PO"].fillna("")
    else:
        df["ZPL_PO"] = ""


    # ========== 步骤 2: 展开 — 每页一个 Order ID ==========
    # PO 来源优先级: ZPL 明文 > 正则兜底（渲染后 PDF 文本是乱码，正则仅在无 ZPL 源时退路）
    expanded = []
    for idx, row in df.iterrows():
        text = full_text.iloc[idx]
        zpl_po = str(row.get("ZPL_PO", "") or "").strip()
        order_ids = [zpl_po] if zpl_po else _find_po_regex(text, po_pattern)
        base = {
            "PDF File Name": row["PDF File Name"],
            "Page Number": row["Page Number"],
            "_full_text": text,
            "Logo_Text": row["Logo_Text"],
        }
        if not order_ids:
            expanded.append({**base, "PO #": ""})
        else:
            for oid in order_ids:
                expanded.append({**base, "PO #": oid})

    edf = pd.DataFrame(expanded)
    po_found = (edf["PO #"] != "").sum()
    zpl_used = (df["ZPL_PO"].astype(str).str.strip() != "").sum()

    # ========== 步骤 3: Courier — Logo OCR + 文本关键词（统一 courier_map）==========
    default_courier = config.get("default_courier", "Others")

    def _detect_courier(row):
        # 优先用 Logo OCR 结果匹配（同一份 courier_map）
        logo = row["Logo_Text"]
        if logo:
            match = _match_courier(logo, courier_map)
            if match:
                return match
        # 回退到页面文本关键词匹配
        text_match = _match_courier(row["_full_text"], courier_map)
        if text_match:
            return text_match
        return default_courier

    edf["Courier"] = edf.apply(_detect_courier, axis=1)
    edf["Parent_Courier"] = edf["Courier"].map(lambda c: parent_map.get(c, c) if c else "")

    courier_found = (edf["Courier"] != default_courier).sum()

    # ========== 步骤 3a: State/Postcode — 由 CSV 提供，不从 PDF 提取 ==========
    edf["State"] = ""
    edf["Postcode"] = ""

    # ========== 步骤 4: Label_Qty + S/M ==========
    edf["Label_Qty"] = edf.groupby("PO #")["PO #"].transform("count")
    edf["Single/Multi_Ctn"] = edf["Label_Qty"].map(lambda q: "S" if q == 1 else "M")

    # ========== 步骤 4b: Invoice_Number — prefix + courier_code + PO ==========
    invoice_prefix = config.get("invoice_prefix", "DF")
    # 构建 courier→code 映射 (parent + children)
    code_map = {}
    for parent, info in hierarchy.items():
        code_map[parent] = info.get("code", "")
        for child, child_info in info.get("children", {}).items():
            code_map[child] = child_info.get("code", "")

    # 用 Parent_Courier 的 code（子 courier 继承 parent code）
    edf["Invoice_Number"] = edf.apply(
        lambda row: invoice_prefix + code_map.get(row["Parent_Courier"], "") + row["PO #"], axis=1
    )

    # ========== 步骤 4c: 空值检查 — 逐行检查关键字段 ==========
    check_cols = ["PO #", "Courier", "Parent_Courier", "State", "Postcode", "Invoice_Number"]
    for idx, row in edf.iterrows():
        empty_fields = [col for col in check_cols if col in row.index and (not row[col] or str(row[col]).strip() == "")]
        if empty_fields:

    # ========== 步骤 5: 保存每页元数据（去重前）==========
    page_metadata = []
    for _, row in edf.iterrows():
        page_metadata.append({
            "file": row["PDF File Name"],
            "page_idx": int(row["Page Number"]) - 1,
            "Parent_Courier": row["Parent_Courier"],
            "Courier": row["Courier"],
            "State": row["State"] or "",
            "Postcode": row["Postcode"] or "",
            "PO": row.get("PO #", ""),
        })

    courier_file_pages = {}
    for pm in page_metadata:
        courier = pm["Courier"]
        fname = pm["file"]
        if courier not in courier_file_pages:
            courier_file_pages[courier] = {}
        if fname not in courier_file_pages[courier]:
            courier_file_pages[courier][fname] = []
        if pm["page_idx"] not in courier_file_pages[courier][fname]:
            courier_file_pages[courier][fname].append(pm["page_idx"])


    # ========== 步骤 6: 去重 — 每个 Order ID 只保留一条 ==========
    before_dedup = len(edf)
    edf = edf.drop_duplicates(subset="PO #", keep="first")

    meta_cols = ["PDF File Name", "PO #", "Label_Qty", "Single/Multi_Ctn",
                 "Parent_Courier", "Courier", "State", "Postcode", "Invoice_Number"]
    edf = edf[meta_cols].reset_index(drop=True)


    return edf, courier_file_pages, page_metadata



