"""ZPL Service — 将 ZPL 文件通过 Labelary API 直接转换为 PDF（与官网一致）"""

import os
import re
import time
import urllib.request


LABELARY_BASE = "http://api.labelary.com/v1/printers/12dpmm/labels"
LABEL_WIDTH = 4        # 固定宽度 4 英寸
LABEL_HEIGHT = 6       # 固定高度 6 英寸
MAX_RETRIES = 2
RETRY_DELAY = 2
BATCH_THRESHOLD = 25   # 超过此数量则分批发送


def _get_label_url() -> str:
    return f"{LABELARY_BASE}/{LABEL_WIDTH}x{LABEL_HEIGHT}/"


def _label_blocks(zpl_data: str) -> list[str]:
    """切出真正的标签块。

    Amazon ZPL 在每张真标签前插一条 `^XA^MCY^XZ`（清屏/换页指令，无 ^FD 字段），
    Labelary 不会把它渲染成页。所以"真标签 = 含 ^FD 的块"，其顺序 = 渲染后的页序。
    """
    blocks = re.findall(r'\^XA.*?\^XZ', zpl_data, re.DOTALL)
    return [b for b in blocks if '^FD' in b]


def parse_label_pos(zpl_data: str) -> list[str]:
    """按页序解析每张标签的 PO（Order ID）。

    PO 印在标签侧边竖排、斜杠结尾，例 `^FD5CZJORVX/^FS`。
    锚点 `/\\^FS`（斜杠紧贴字段结束）只命中"整串以斜杠收尾"的字段，
    天然排除装饰串 `^FD / ^FS` 和中间带斜杠的日期。每张出现 2 次（左右），去重取一。
    返回列表第 i 项 = 第 i+1 页的 PO（解析不到则为空串）。
    """
    pos = []
    for i, b in enumerate(_label_blocks(zpl_data)):
        cands = sorted(set(re.findall(r'\^FD([A-Za-z0-9]+)/\^FS', b)))
        if len(cands) == 1:
            pos.append(cands[0])
        elif not cands:
            pos.append("")
        else:
            pos.append(cands[0])
    return pos


def pos_for_pdf(pdf_path: str) -> list[str]:
    """给定渲染后的 PDF 路径，找同名 .zpl 源文件并解析每页 PO。无 ZPL 源则返回空列表。"""
    zpl_path = os.path.splitext(pdf_path)[0] + ".zpl"
    if not os.path.exists(zpl_path):
        return []
    with open(zpl_path, "r", encoding="utf-8") as f:
        return parse_label_pos(f.read())


def count_zpl_labels(zpl_path: str) -> int:
    """统计 ZPL 文件中的真标签数量（含 ^FD 的块，不含 ^XA^MCY^XZ 清屏块）。"""
    with open(zpl_path, "r", encoding="utf-8") as f:
        zpl_data = f.read()
    return len(_label_blocks(zpl_data))


def _send_zpl(zpl_data: str) -> bytes:
    """发送 ZPL 数据到 Labelary API，返回 PDF bytes。"""
    url = _get_label_url()
    for attempt in range(MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, data=zpl_data.encode("utf-8"))
            req.add_header("Accept", "application/pdf")
            res = urllib.request.urlopen(req, timeout=120)
            return res.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            raise
    raise ValueError("Labelary API returned empty response")


def _split_zpl(zpl_data: str) -> list[str]:
    """将 ZPL 数据按 ^XA...^XZ 拆分为单个标签列表。"""
    labels = re.findall(r'\^XA.*?\^XZ', zpl_data, re.DOTALL)
    return labels


def convert_zpl_to_pdf(zpl_path: str, output_dir: str, on_progress=None) -> str:
    """
    将 ZPL 文件转换为 PDF。
    标签数 <= BATCH_THRESHOLD: 整体发送
    标签数 > BATCH_THRESHOLD: 分批发送，合并为一个 PDF

    on_progress: 可选回调 (converted, total, failed_count)。
    返回 (pdf_path, converted, total, failed)。
    """
    with open(zpl_path, "r", encoding="utf-8") as f:
        zpl_data = f.read()

    total = len(_label_blocks(zpl_data))
    if total == 0:
        raise ValueError(f"No valid ZPL labels found in {zpl_path}")


    if on_progress:
        on_progress(0, total, 0)

    base_name = os.path.splitext(os.path.basename(zpl_path))[0]
    output_path = os.path.join(output_dir, f"{base_name}.pdf")

    if total <= BATCH_THRESHOLD:
        # 整体发送
        pdf_bytes = _send_zpl(zpl_data)
        with open(output_path, "wb") as f:
            f.write(pdf_bytes)
        if on_progress:
            on_progress(total, total, 0)
    else:
        # 分批发送，合并 PDF
        import fitz  # PyMuPDF
        labels = _split_zpl(zpl_data)

        merged_pdf = fitz.open()
        converted = 0
        failed = 0

        for i in range(0, len(labels), BATCH_THRESHOLD):
            batch = labels[i:i + BATCH_THRESHOLD]
            batch_zpl = "\n".join(batch)
            batch_num = i // BATCH_THRESHOLD + 1
            try:
                pdf_bytes = _send_zpl(batch_zpl)
                batch_pdf = fitz.open(stream=pdf_bytes, filetype="pdf")
                merged_pdf.insert_pdf(batch_pdf)
                batch_pdf.close()
                converted += sum(1 for b in batch if '^FD' in b)
            except Exception as e:
                failed += sum(1 for b in batch if '^FD' in b)

            if on_progress:
                on_progress(converted, total, failed)

        merged_pdf.save(output_path)
        merged_pdf.close()

    return output_path, total, total, []
