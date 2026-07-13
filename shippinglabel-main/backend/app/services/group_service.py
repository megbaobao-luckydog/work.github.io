"""GroupService — PDF 拆分: 按多层分组键拆分 PDF"""

import os
import tempfile
from collections import defaultdict

import fitz  # PyMuPDF


# 前端可选的分组字段
VALID_GROUP_KEYS = ["Parent_Courier", "Courier", "Weight", "State"]


def group_pdfs(pdf_folder: str, page_metadata: list, group_keys: list = None, sort_order: dict = None) -> dict:
    """
    按多层分组键将 PDF 页面拆分为独立文件。

    参数:
        pdf_folder: PDF 文件所在目录
        page_metadata: 每页元数据列表, 每项:
            { "file": "batch1.pdf", "page_idx": 0, "Courier": "Toll", "Weight": "18.35", "State": "QLD" }
        group_keys: 分组字段列表, 如 ["Courier", "Weight"]
                    顺序决定文件夹层级, 默认 ["Courier"]

    返回:
        {
            "group_keys": ["Courier", "Weight"],
            "groups": {
                "Toll/18.35": { "path": "/tmp/.../Toll_18.35.pdf", "pages": 2, "labels": {"Courier":"Toll","Weight":"18.35"} },
                ...
            }
        }
    """
    if group_keys is None:
        group_keys = ["Courier"]

    # 校验分组键
    for key in group_keys:
        if key not in VALID_GROUP_KEYS:
            raise ValueError(f"Invalid group key: {key}. Valid keys: {VALID_GROUP_KEYS}")

    if sort_order is None:
        sort_order = {}

    # 按分组键组合聚合页面
    groups = defaultdict(list)
    for pm in page_metadata:
        group_values = tuple(pm.get(k, "Unknown") for k in group_keys)
        groups[group_values].append(pm)

    # 按 sort_order 对分组键排序（Courier 按字符串，Weight 按数值）
    from functools import cmp_to_key

    def _cmp(a, b):
        for i, k in enumerate(group_keys):
            va, vb = a[i], b[i]
            reverse = sort_order.get(k, "asc") == "desc"
            if k in ("Weight", "Postcode"):
                try:
                    va = float(va)
                except (ValueError, TypeError):
                    va = float('inf')
                try:
                    vb = float(vb)
                except (ValueError, TypeError):
                    vb = float('inf')
            if va < vb:
                return 1 if reverse else -1
            elif va > vb:
                return -1 if reverse else 1
        return 0

    sorted_groups = sorted(groups.items(), key=cmp_to_key(lambda a, b: _cmp(a[0], b[0])))

    output_dir = tempfile.mkdtemp(prefix="pdf2csv_grouped_")
    result = {}

    # 缓存已打开的源 PDF，避免重复 open
    src_cache = {}

    for group_values, pages in sorted_groups:
        doc = fitz.open()  # 空 PDF

        for pm in pages:
            fname = pm["file"]
            page_idx = pm["page_idx"]

            if fname not in src_cache:
                pdf_path = os.path.join(pdf_folder, fname)
                if not os.path.isfile(pdf_path):
                    continue
                src_cache[fname] = fitz.open(pdf_path)

            src = src_cache[fname]
            doc.insert_pdf(src, from_page=page_idx, to_page=page_idx)

        page_count = len(doc)
        # 文件名: 各层级值用下划线拼接
        safe_name = "_".join(str(v).replace("/", "-") for v in group_values)
        filepath = os.path.join(output_dir, f"{safe_name}_labels.pdf")
        doc.save(filepath)
        doc.close()

        # key: 各层级值用 / 拼接（方便前端展示树形结构）
        group_key = "/".join(str(v) for v in group_values)
        labels = dict(zip(group_keys, group_values))
        result[group_key] = {"path": filepath, "pages": page_count, "labels": labels}


    # 关闭缓存的源文件
    for src in src_cache.values():
        src.close()

    return {"group_keys": group_keys, "groups": result}


# ── 兼容旧接口 ──────────────────────────────────────────────
def group_by_courier(pdf_folder: str, courier_file_pages: dict) -> dict:
    """旧接口兼容: 从 courier_file_pages 映射转为 page_metadata 后调用新接口"""
    page_metadata = []
    for courier, files in courier_file_pages.items():
        for fname, page_indices in files.items():
            for page_idx in page_indices:
                page_metadata.append({
                    "file": fname,
                    "page_idx": page_idx,
                    "Courier": courier,
                    "Weight": "Unknown",
                    "State": "Unknown",
                })

    result = group_pdfs(pdf_folder, page_metadata, group_keys=["Courier"])

    # 转为旧格式返回
    return {
        group_key.split("/")[0]: {"path": info["path"], "pages": info["pages"]}
        for group_key, info in result["groups"].items()
    }
