"""Settings 页面接口 — PO 前缀、Courier 映射、Invoice 编码、输出配置"""


from fastapi import APIRouter, Request
from pydantic import BaseModel

from backend.app.config import load_config, save_config


router = APIRouter()


# ── Request / Response Models ────────────────────────────────

class OutputConfig(BaseModel):
    file_pattern: str
    output_filename: str
    timestamp_format: str
    merge_strategy: str


class ConfigModel(BaseModel):
    po_prefix: str
    courier_map: dict[str, str]
    invoice_prefix: str
    courier_codes: dict[str, str]
    output: OutputConfig


# ── Endpoints ────────────────────────────────────────────────

@router.get("")
def get_config():
    """获取当前配置 (从 config.json 读取)"""
    return load_config()


@router.put("")
async def update_config(request: Request):
    """保存配置 (Save Config 按钮，写入 config.json)"""
    config = await request.json()
    save_config(config)
    return {"message": "Config saved"}
