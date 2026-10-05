"""Optional, read-only structured reader for wechat-cli 0.2.4 caches.

No key files are loaded. No AppContext/DBCache instance is created, and no
decryption, init, key scan, media access or environment modification is run.
The installed CLI's public source supplies the SQL-row/decompression helpers.
Only already existing, current plaintext caches are read in SQLite mode=ro.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from .importers import normalize_import
from .models import filter_boundary


class AdapterUnavailable(ValueError):
    """The optional read-only adapter cannot provide reliable structured data."""


def _read_json(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def _readonly_connection(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _current_cache_entries(config, cache_dir):
    """Inspect existing metadata; never refresh a stale cache."""
    try:
        metadata = _read_json(Path(cache_dir) / "_mtimes.json")
    except (OSError, ValueError):
        raise AdapterUnavailable("尚无可读的 wechat-cli 数据库缓存，请导入带 sender_id 的规范化文件。")
    if not isinstance(metadata, dict):
        raise AdapterUnavailable("wechat-cli 缓存元数据格式不兼容，请导入规范化文件。")
    entries = {}
    for rel_key, info in metadata.items():
        if not isinstance(info, dict):
            continue
        relative = str(rel_key).replace("\\", "/")
        if relative.startswith("/") or ".." in relative.split("/"):
            continue
        original = Path(config["db_dir"]) / relative
        cached = Path(str(info.get("path", "")))
        try:
            wal = Path(str(original) + "-wal")
            if not cached.is_file() or original.stat().st_mtime != info.get("db_mt"):
                continue
            if (wal.stat().st_mtime if wal.exists() else 0) != info.get("wal_mt"):
                continue
        except OSError:
            continue
        entries[relative] = cached
    if not any(re.fullmatch(r"message/message_\d+\.db", key) for key in entries):
        raise AdapterUnavailable("现有消息缓存缺失或已过期；本工具不会主动解密刷新，请导入规范化文件。")
    return entries


def _load_names(entries):
    names = {}
    path = entries.get("contact/contact.db")
    if path:
        try:
            with closing(_readonly_connection(path)) as connection:
                for username, nickname, remark in connection.execute("SELECT username,nick_name,remark FROM contact"):
                    if username:
                        names[str(username)] = str(remark or nickname or username)
        except sqlite3.Error:
            pass
    return names


def _resolve_chat(chat_name, names):
    if not isinstance(chat_name, str) or not chat_name.strip():
        raise AdapterUnavailable("请指定聊天名或稳定聊天 ID。")
    query = chat_name.strip()
    if query in names or query.startswith("wxid_") or "@chatroom" in query:
        return query
    exact = [key for key, name in names.items() if name == query]
    if len(exact) > 1:
        raise AdapterUnavailable("存在同名聊天，请使用稳定聊天 ID，避免读取错误会话。")
    return exact[0] if exact else query


def records_from_database(connection, table_name, chat_id, chat_name, names,
                          messages_module, *, db_key="message/message_0.db", self_id="",
                          limit=5000, start_ts=None, end_ts=None):
    """Pure adapter boundary, tested with temporary SQLite and fake CLI helpers."""
    if not re.fullmatch(r"Msg_[0-9a-f]{32}", table_name):
        raise AdapterUnavailable("聊天数据表名无效。")
    try:
        id_map = messages_module._load_name2id_maps(connection)
        rows = messages_module._query_messages(connection, table_name, start_ts=start_ts,
                                               end_ts=end_ts, limit=limit, offset=0)
    except (sqlite3.Error, TypeError, AttributeError) as exc:
        raise AdapterUnavailable("wechat-cli 数据结构与 0.2.4 不兼容，请导入规范化文件。") from exc
    output, warnings = [], []
    is_group = "@chatroom" in chat_id
    for row in rows:
        if len(row) != 6:
            raise AdapterUnavailable("wechat-cli 消息行结构不兼容，请导入规范化文件。")
        local_id, local_type, create_time, real_sender_id, raw_content, content_type = row
        try:
            base = int(local_type) & 0xFFFFFFFF
        except (ValueError, TypeError):
            warnings.append("部分消息缺少有效原始类型，已跳过。")
            continue
        try:
            raw = messages_module.decompress_content(raw_content, content_type)
        except Exception:
            raw = None
        if raw is None or not isinstance(raw, str):
            raw = ""
            warnings.append("部分消息内容无法解码，已保留类型频次但不分析情绪；未把它们当作中性文字。")
        sender_id = str(id_map.get(real_sender_id) or "")
        content = raw
        if is_group and ":\n" in raw:
            raw_sender, content = raw.split(":\n", 1)
            if not sender_id and re.fullmatch(r"[A-Za-z0-9_.@-]{1,160}", raw_sender) and raw_sender != chat_id:
                sender_id = raw_sender
        system = base in (10000, 10002)
        if not system and (not sender_id or sender_id == chat_id and is_group):
            warnings.append("部分消息无法解析稳定发送者 ID，已跳过；未根据聊天名猜测发送者。")
            continue
        output.append({
            "id": "adapter:" + hashlib.sha256(f"{db_key}:{chat_id}:{local_id}".encode()).hexdigest()[:24],
            "chat_id": chat_id, "chat_name": chat_name,
            "sender_id": sender_id, "sender_name": "我" if sender_id and sender_id == self_id else names.get(sender_id, sender_id),
            "timestamp": create_time, "type": local_type, "text": content,
            "is_group": is_group, "is_system": system,
        })
    return output, warnings


def load_messages(chat_name: str, limit: int = 5000, start: str = "", end: str = "",
                  config_path: str | None = None, *, cache_dir: str | None = None) -> dict:
    """Read current, initialized CLI caches and return normalized messages.

    Raises AdapterUnavailable on unsupported dependency/schema/cache state.
    cache_dir is an explicit test/custom-cache hook, not an auto-scan path.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100000:
        raise AdapterUnavailable("读取数量必须在 1 至 100000 之间。")
    try:
        installed = importlib.metadata.version("wechat-cli")
        if installed != "0.2.4":
            raise AdapterUnavailable("结构化缓存适配器当前支持 wechat-cli 0.2.4，请导入规范化文件。")
        module = importlib.import_module("wechat_cli.core.messages")
    except AdapterUnavailable:
        raise
    except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
        raise AdapterUnavailable("当前 Python 环境没有独立安装的 wechat-cli 0.2.4；可使用 JSON/CSV 规范化导入。") from exc
    for name in ("_query_messages", "_load_name2id_maps", "decompress_content"):
        if not callable(getattr(module, name, None)):
            raise AdapterUnavailable("wechat-cli 接口不兼容，请导入规范化文件。")
    path = Path(config_path or os.environ.get("WECHAT_CLI_CONFIG") or Path.home() / ".wechat-cli" / "config.json").expanduser().resolve()
    try:
        config = _read_json(path)
    except (OSError, ValueError) as exc:
        raise AdapterUnavailable("找不到已初始化的 wechat-cli 配置，请导入规范化文件。") from exc
    if not isinstance(config, dict) or not isinstance(config.get("db_dir"), str) or not config["db_dir"]:
        raise AdapterUnavailable("wechat-cli 配置缺少 db_dir；本工具不会自动扫描，请导入规范化文件。")
    if not Path(config["db_dir"]).is_absolute():
        config["db_dir"] = str(path.parent / config["db_dir"])
    entries = _current_cache_entries(config, cache_dir or str(Path(tempfile.gettempdir()) / "wechat_cli_cache"))
    names = _load_names(entries)
    chat_id = _resolve_chat(chat_name, names)
    chat_display = names.get(chat_id, chat_name.strip())
    # Match self ID against known contacts rather than treating the recipient as self.
    account_directory = Path(config["db_dir"]).parent.name
    candidates = [account_directory]
    match = re.fullmatch(r"(.+)_([0-9a-fA-F]{4,})", account_directory)
    if match:
        candidates.insert(0, match[1])
    self_id = next((candidate for candidate in candidates if candidate in names), "")
    start_dt, end_dt = filter_boundary(start), filter_boundary(end, end=True)
    if start_dt and end_dt and start_dt > end_dt:
        raise AdapterUnavailable("开始日期不能晚于结束日期。")
    start_ts = int(start_dt.timestamp()) if start_dt else None
    end_ts = int(end_dt.timestamp()) if end_dt else None
    table_name = "Msg_" + hashlib.md5(chat_id.encode()).hexdigest()
    records, warnings = [], []
    found_tables = 0
    for db_key, db_path in sorted(entries.items()):
        if not re.fullmatch(r"message/message_\d+\.db", db_key):
            continue
        try:
            with closing(_readonly_connection(db_path)) as connection:
                found = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)).fetchone()
                if not found:
                    continue
                found_tables += 1
                rows, row_warnings = records_from_database(
                    connection, table_name, chat_id, chat_display, names, module,
                    db_key=db_key, self_id=self_id, limit=limit, start_ts=start_ts, end_ts=end_ts,
                )
                records.extend(rows)
                warnings.extend(row_warnings)
        except sqlite3.Error as exc:
            raise AdapterUnavailable("现有消息缓存无法只读打开，请导入规范化文件。") from exc
    if not found_tables:
        raise AdapterUnavailable("当前缓存未包含指定聊天，请确认聊天 ID 或导入规范化文件。")
    records.sort(key=lambda record: (record["timestamp"], record["id"]))
    records = records[-limit:]
    result = normalize_import(json.dumps(records, ensure_ascii=False), "adapter.json")
    if len(records) == limit:
        warnings.append(f"读取达到 {limit} 条上限，仅统计最新样本；可缩小时间范围或提高上限。")
    if found_tables > 1:
        warnings.append("消息来自多个数据库分片；原始行只有局部 ID，可能无法去除跨分片的历史副本。")
    warnings.append("通过已有 wechat-cli 0.2.4 当前缓存只读导入，未执行密钥扫描、初始化、解密或媒体读取。")
    result["warnings"] = list(dict.fromkeys(result["warnings"] + warnings))
    result["adapter"] = "wechat-cli-0.2.4-readonly-cache"
    return result
