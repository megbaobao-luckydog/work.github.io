"""FastAPI 入口 — 对应设计图 Dashboard + Settings 两个页面的接口"""

import os
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.app.routes import dashboard, settings, upload

FRONTEND_DIR = os.path.join(PROJECT_ROOT, "frontend")

app = FastAPI(title="Shipping Label Manager")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": str(exc)})


# API 路由 — 必须在静态文件之前注册
app.include_router(settings.router, prefix="/api/settings", tags=["Settings"])
app.include_router(upload.router, prefix="/api/upload", tags=["Upload"])
app.include_router(dashboard.router, prefix="/api", tags=["Dashboard"])



@app.get("/")
def root():
    return RedirectResponse("/static/dashboard.html")


# 前端静态页面: http://localhost:8000/static/dashboard.html
app.mount("/static", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
