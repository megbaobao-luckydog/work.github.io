"""全局业务配置 — 统一从 config.json 读写，敏感值从 .env 覆盖"""

import json
import os

from dotenv import load_dotenv

load_dotenv()

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    oms = cfg.setdefault("oms", {})
    if os.environ.get("NETO_ENDPOINT"):
        oms["endpoint"] = os.environ["NETO_ENDPOINT"]
    if os.environ.get("NETO_USERNAME"):
        oms["username"] = os.environ["NETO_USERNAME"]
    if os.environ.get("NETO_API_KEY"):
        oms["api_key"] = os.environ["NETO_API_KEY"]
    if os.environ.get("TW_SHEET_ID"):
        cfg.setdefault("tw", {})["sheet_id"] = os.environ["TW_SHEET_ID"]
    if os.environ.get("AMAZON_SHEET_ID"):
        cfg.setdefault("amazon", {})["sheet_id"] = os.environ["AMAZON_SHEET_ID"]

    return cfg


def save_config(config: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
