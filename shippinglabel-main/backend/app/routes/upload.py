"""Upload 接口 — 用户上传 PDF、ZPL 和 CSV 文件到服务器临时目录"""

from __future__ import annotations  # 让 X|Y 注解延迟求值，兼容本地 Python 3.9

import json
import os
import queue
import tempfile
import threading
import uuid
from datetime import datetime
from typing import List

import pandas as pd
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from starlette.responses import StreamingResponse

from backend.app.services.zpl_service import convert_zpl_to_pdf, count_zpl_labels


router = APIRouter()

# 内存中的 session 存储
_sessions: dict = {}


def get_session(session_id: str) -> dict | None:
    return _sessions.get(session_id)


@router.post("/pdfs")
async def upload_pdfs(files: List[UploadFile] = File(...), session_id: str = Form(None)):
    """上传多个 PDF 文件，创建或追加到已有 session"""
    if not files:
        raise HTTPException(400, "No files uploaded")

    # 追加到已有 session 或创建新的
    if session_id and session_id in _sessions:
        pdf_dir = _sessions[session_id]["pdf_dir"]
    else:
        session_id = str(uuid.uuid4())
        pdf_dir = tempfile.mkdtemp(prefix=f"pdf2csv_{session_id[:8]}_")
        _sessions[session_id] = {
            "pdf_dir": pdf_dir,
            "csv_path": None,
            "created_at": datetime.now(),
        }

    filenames = []
    for f in files:
        if not f.filename.lower().endswith(".pdf"):
            continue
        safe_name = os.path.basename(f.filename)
        filepath = os.path.join(pdf_dir, safe_name)
        content = await f.read()
        with open(filepath, "wb") as fp:
            fp.write(content)
        filenames.append(safe_name)

    if not filenames:
        raise HTTPException(400, "No valid PDF files uploaded")

    return {
        "session_id": session_id,
        "pdf_count": len(filenames),
        "filenames": filenames,
    }


@router.post("/csv")
async def upload_csv(
    file: UploadFile = File(...),
    session_id: str = Form(None),
):
    """上传 CSV 文件到已有 session，或创建新 session（支持 CSV 先于 PDF 上传）"""

    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "File must be a CSV")

    # 追加到已有 session 或创建新的
    if session_id and session_id in _sessions:
        session = _sessions[session_id]
    else:
        session_id = str(uuid.uuid4())
        pdf_dir = tempfile.mkdtemp(prefix=f"pdf2csv_{session_id[:8]}_")
        session = {
            "pdf_dir": pdf_dir,
            "csv_path": None,
            "created_at": datetime.now(),
        }
        _sessions[session_id] = session

    safe_name = os.path.basename(file.filename)
    csv_path = os.path.join(session["pdf_dir"], safe_name)
    content = await file.read()
    with open(csv_path, "wb") as fp:
        fp.write(content)

    session["csv_path"] = csv_path

    df = pd.read_csv(csv_path)
    return {
        "session_id": session_id,
        "csv_filename": safe_name,
        "row_count": len(df),
        "columns": df.columns.tolist(),
    }


@router.post("/zpl")
async def upload_zpl(file: UploadFile = File(...), session_id: str = Form(None)):
    """上传 ZPL 文件，转换为 PDF，通过 SSE 流式返回进度。"""
    if not file.filename.lower().endswith(".zpl"):
        raise HTTPException(400, "File must be a .zpl file")

    # 追加到已有 session 或创建新的
    if session_id and session_id in _sessions:
        pdf_dir = _sessions[session_id]["pdf_dir"]
    else:
        session_id = str(uuid.uuid4())
        pdf_dir = tempfile.mkdtemp(prefix=f"pdf2csv_{session_id[:8]}_")
        _sessions[session_id] = {
            "pdf_dir": pdf_dir,
            "csv_path": None,
            "created_at": datetime.now(),
        }

    # 保存 ZPL 文件
    safe_name = os.path.basename(file.filename)
    zpl_path = os.path.join(pdf_dir, safe_name)
    content = await file.read()
    with open(zpl_path, "wb") as fp:
        fp.write(content)

    # 预解析获取标签数
    total = count_zpl_labels(zpl_path)

    # 进度队列
    progress_q = queue.Queue()

    def on_progress(done, total_labels, failed_count):
        progress_q.put({"done": done, "total": total_labels, "failed": failed_count})

    # 后台线程执行转换
    result_holder = {}

    def run_convert():
        try:
            pdf_path, converted, total_labels, failed = convert_zpl_to_pdf(
                zpl_path, pdf_dir, on_progress=on_progress
            )
            result_holder["ok"] = {
                "session_id": session_id,
                "zpl_filename": safe_name,
                "pdf_filename": os.path.basename(pdf_path),
                "page_count": converted,
                "total_labels": total_labels,
            }
            if failed:
                result_holder["ok"]["failed"] = failed
        except Exception as e:
            result_holder["error"] = str(e)
        progress_q.put(None)  # 结束信号

    thread = threading.Thread(target=run_convert)
    thread.start()

    def event_stream():
        # 初始事件: 总数
        yield f"data: {json.dumps({'type': 'start', 'total': total})}\n\n"
        while True:
            msg = progress_q.get()
            if msg is None:
                break
            yield f"data: {json.dumps({'type': 'progress', **msg})}\n\n"
        # 最终结果
        if "ok" in result_holder:
            yield f"data: {json.dumps({'type': 'done', **result_holder['ok']})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'error', 'message': result_holder.get('error', 'Unknown error')})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
