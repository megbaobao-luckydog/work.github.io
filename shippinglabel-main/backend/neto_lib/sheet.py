"""从 Google Sheet 加载 Neto 字段 mapping。

Sheet schema(C/D/E 三列):
    A = Neto 字段名(如 "OrderID"、"OrderLine.SKU");"#" 开头视为分组注释
    B = Type — 仅人类可读标签,代码忽略
    C = 静态字面值
    D = CSV 列名(从 CSV 取值)
    E = 客户特化字面值
A 空行 = 延续上一字段(拼接)。
"""

import os


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
DEFAULT_CRED_PATH = "/app/sa.json"


def load_mapping(sheet_id: str, sheet_tab: str = "Sheet1", cred_path: str = None) -> dict:
    """读 sheet,返回 {field_name: [(c, d, e), ...]}。"""
    import gspread
    from google.oauth2.service_account import Credentials

    cred_path = cred_path or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", DEFAULT_CRED_PATH)
    creds = Credentials.from_service_account_file(cred_path, scopes=SCOPES)
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(sheet_id).worksheet(sheet_tab)
    rows = ws.get_all_values()

    mapping = {}
    current = None
    for r in rows[1:]:
        p = r + [""] * (5 - len(r))
        a = p[0].strip()
        if a and not a.startswith("#"):
            current = a
            mapping[current] = []
        if current:
            c, d, e = p[2].strip(), p[3].strip(), p[4].strip()
            if c or d or e:
                mapping[current].append((c, d, e))

    return mapping
