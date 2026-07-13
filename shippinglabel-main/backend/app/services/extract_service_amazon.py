"""ExtractService (Amazon) — Amazon PDF 面单提取: 文本 + 右上角 Logo OCR（PyMuPDF + pytesseract）"""

import io
import os
import re
from glob import glob

import fitz
import pandas as pd
from PIL import Image

from backend.app.services.zpl_service import pos_for_pdf


import pytesseract


def _ocr_top_logo(page: fitz.Page, doc: fitz.Document) -> str:
    """
    检测页面顶部是否有图片，有则 OCR 识别文字并返回。
    用于识别快递公司 Logo（如 CouriersPlease 右上角、PARCELPOINT 顶部居中等）。
    """
    images = page.get_images()
    seen = set()

    for img in images:
        xref = img[0]
        if xref in seen:
            continue
        seen.add(xref)

        rects = page.get_image_rects(xref)
        for r in rects:
            # 右上角 (CouriersPlease等) 或 顶部居中 (PARCELPOINT等)
            is_top_right = r.x0 > page.rect.width * 0.5 and r.y0 < 60
            is_top_center = r.y0 < 30
            if is_top_right or is_top_center:
                try:
                    base_image = doc.extract_image(xref)
                    img_data = base_image["image"]
                    pil_img = Image.open(io.BytesIO(img_data))
                    # 放大提高 OCR 精度
                    pil_img = pil_img.resize(
                        (pil_img.width * 3, pil_img.height * 3), Image.LANCZOS
                    )
                    raw = pytesseract.image_to_string(pil_img).strip()
                    if raw:
                        # 降噪: 去换行 → 去特殊符号 → 合并多余空格
                        text = raw.replace("\n", " ")
                        text = re.sub(r"[^A-Za-z0-9\s]", "", text)
                        text = re.sub(r"\s+", " ", text).strip()
                        if text:
                            return text
                except Exception as e:
    return ""


def extract(pdf_folder: str, file_pattern: str = "*.pdf") -> pd.DataFrame:
    """
    逐文件 → 逐页 → 逐行提取文本，同时 OCR 右上角 Logo。

    返回 DataFrame 结构:
        | PDF File Name | Page Number | Line Number | Data | Logo_Text | ZPL_PO |
    Logo_Text 为该页右上角 Logo 的 OCR 结果，无 Logo 则为空字符串。
    ZPL_PO 为该页 PO（Order ID），从同名 .zpl 源文件明文解析；无 ZPL 源则为空字符串。
    渲染后的 PDF 字体是 Identity-H 无 ToUnicode，文本层是乱码，PO 只能从 ZPL 源读。
    同一页的所有行共享相同的 Logo_Text / ZPL_PO。
    """
    pdf_files = glob(os.path.join(pdf_folder, file_pattern))

    if not pdf_files:
        raise FileNotFoundError(f"No PDF files found in {pdf_folder}")

    all_data = []

    for pdf_file_path in pdf_files:
        pdf_file_name = os.path.basename(pdf_file_path)
        doc = fitz.open(pdf_file_path)
        page_count = len(doc)

        # 从同名 .zpl 源解析每页 PO（顺序 = 页序）
        zpl_pos = pos_for_pdf(pdf_file_path)
        if zpl_pos:
            if len(zpl_pos) != page_count:

        for page_idx in range(page_count):
            page = doc[page_idx]
            text = page.get_text()

            # OCR 右上角 Logo
            logo_text = _ocr_top_logo(page, doc)
            if logo_text:

            zpl_po = zpl_pos[page_idx] if page_idx < len(zpl_pos) else ""

            page_number = page_idx + 1
            lines = text.split("\n")
            emitted = 0
            for line_number, line in enumerate(lines, start=1):
                if line.strip():
                    all_data.append([pdf_file_name, page_number, line_number, line, logo_text, zpl_po])
                    emitted += 1
            # 该页无可用文本（乱码/空白）也要保留一行，否则该页连同其 ZPL_PO 会从后续流程消失
            if emitted == 0:
                all_data.append([pdf_file_name, page_number, 1, "", logo_text, zpl_po])

        doc.close()

    return pd.DataFrame(
        all_data,
        columns=["PDF File Name", "Page Number", "Line Number", "Data", "Logo_Text", "ZPL_PO"],
    )
