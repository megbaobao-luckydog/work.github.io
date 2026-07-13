"""ExtractService (TW) — Temple & Webster PDF 面单提取: 逐页逐行原始文本（PyMuPDF）"""

import os
from glob import glob

import fitz
import pandas as pd


# 超过此比例的字符为纵向排列时，判定为横向页面需要旋转
_LANDSCAPE_THRESHOLD = 0.5


def _is_landscape_content(page: fitz.Page) -> bool:
    """通过 PyMuPDF 文字方向检测页面是否为横向排版。"""
    blocks = page.get_text("dict")["blocks"]
    total = 0
    vertical = 0
    for b in blocks:
        for line in b.get("lines", []):
            d = (round(line["dir"][0], 2), round(line["dir"][1], 2))
            char_count = sum(len(s["text"]) for s in line["spans"])
            total += char_count
            if d == (0.0, 1.0):
                vertical += char_count
    if total == 0:
        return False
    return vertical / total > _LANDSCAPE_THRESHOLD


def extract(pdf_folder: str, file_pattern: str = "*.pdf") -> pd.DataFrame:
    """
    逐文件 → 逐页 → 逐行提取文本，输出扁平原始数据。
    自动检测横向排版页面并旋转后提取。

    返回 DataFrame 结构:
        | PDF File Name | Page Number | Line Number | Data |
    """
    pdf_files = glob(os.path.join(pdf_folder, file_pattern))

    if not pdf_files:
        raise FileNotFoundError(f"No PDF files found in {pdf_folder}")

    all_data = []

    for pdf_file_path in pdf_files:
        pdf_file_name = os.path.basename(pdf_file_path)
        doc = fitz.open(pdf_file_path)
        page_count = len(doc)

        for page_idx in range(page_count):
            page = doc[page_idx]

            if _is_landscape_content(page):
                page.set_rotation(90)
                text = page.get_text()
            else:
                text = page.get_text()

            page_number = page_idx + 1
            lines = text.split("\n")
            for line_number, line in enumerate(lines, start=1):
                if line.strip():
                    all_data.append([pdf_file_name, page_number, line_number, line])

        doc.close()

    return pd.DataFrame(
        all_data,
        columns=["PDF File Name", "Page Number", "Line Number", "Data"],
    )
