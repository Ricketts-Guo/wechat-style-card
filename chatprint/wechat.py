"""Optional read-only connector to a separately installed, initialized WeChat CLI."""
from __future__ import annotations

import json
import shutil
import subprocess
import importlib.metadata

from .importers import normalize_import
from .raw_adapter import AdapterUnavailable, load_messages


def cli_status() -> dict:
    command = shutil.which("wechat-cli") or shutil.which("wx")
    try:
        adapter = importlib.metadata.version("wechat-cli") == "0.2.4"
    except importlib.metadata.PackageNotFoundError:
        adapter = False
    return {"available": bool(command) or adapter, "command": command, "python_adapter_available": adapter, "sessions_available": bool(command),
        "warnings": [] if command or adapter else ["未检测到 WeChat CLI。你可以直接导入 JSON、JSONL 或 CSV 文件。"]}


def _read(args: list[str]) -> object:
    status = cli_status()
    if not status["command"]:
        raise ValueError("未检测到 WeChat CLI；请先使用文件导入。")
    try:
        proc = subprocess.run([status["command"], *args], capture_output=True, text=True, encoding="utf-8", timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("WeChat CLI 读取失败或超时；请在终端检查该工具的初始化状态。") from None
    if proc.returncode:
        # Do not reflect CLI output: it can contain database paths and private messages.
        raise ValueError("WeChat CLI 尚未就绪或读取失败。请在终端检查登录、权限和初始化状态。")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise ValueError("WeChat CLI 没有输出 JSON。请确认工具版本支持 JSON 输出。") from None
    if not isinstance(data, (list, dict)):
        raise ValueError("WeChat CLI 返回的数据格式无法识别。")
    return data


def sessions() -> dict:
    status = cli_status()
    if not status["command"] and status["python_adapter_available"]:
        return {"sessions": [], "manual_only": True,
            "warnings": ["检测到缓存适配器，但 CLI 命令不在环境路径中。请输入聊天名或稳定聊天 ID；只读取已有当前缓存。"]}
    data = _read(["sessions", "--limit", "100"])
    rows = data if isinstance(data, list) else data.get("sessions", data.get("data", []))
    if not isinstance(rows, list):
        rows = []
    return {"sessions": rows, "warnings": []}


def history(chat: str, limit: int = 1000) -> object:
    if not isinstance(chat, str):
        raise ValueError("请输入有效的聊天名称。")
    chat = chat.strip()
    if not chat or len(chat) > 200 or chat.startswith("-") or any(ord(c) < 32 for c in chat):
        raise ValueError("请输入有效的聊天名称。")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
        raise ValueError("本次读取条数需在 1–10000 之间。")
    try:
        return load_messages(chat, limit=limit)
    except AdapterUnavailable as exc:
        adapter_warning = str(exc)
    data = normalize_import(json.dumps(_read(["history", chat, "--limit", str(limit)]), ensure_ascii=False), "wechat-cli.json")
    data["warnings"] = list(dict.fromkeys(data["warnings"] + [adapter_warning, "本次使用 CLI 格式化记录，类型与身份完整性取决于上游导出。"] ))
    return data
