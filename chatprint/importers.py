"""Import normalized JSON/JSONL/CSV and public wechat-cli JSON exports.

The upstream wechat-cli history/search JSON contains formatted strings, not
stable sender IDs. Group strings are deliberately rejected because equal
display names cannot safely identify a person. Structured group exports with
sender_id (for example from the optional adapter) are fully supported.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter

from .models import DEFAULT_TZ, MESSAGE_TYPES, SYSTEM_SENDER_ID, parse_timestamp, resolve_timezone

_NUMERIC_TYPES = {1: "text", 3: "image", 34: "voice", 43: "video", 47: "sticker"}
_TYPE_ALIASES = {
    "文本": "text", "文字": "text", "txt": "text", "文件": "file", "attachment": "file",
    "表情": "sticker", "表情包": "sticker", "emoji": "sticker", "emoticon": "sticker",
    "语音": "voice", "audio": "voice", "图片": "image", "photo": "image",
    "视频": "video", "链接": "link", "链接/文件": "other", "system": "other",
    "系统": "other", "撤回": "other", "location": "other", "call": "other",
}
_SYSTEM_TYPES = {10000, 10002}
_LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)\]\s*(.*)$", re.DOTALL)
_PREFIX_TYPES = {
    "[文件]": "file", "[表情]": "sticker", "[表情包]": "sticker", "[语音]": "voice",
    "[图片]": "image", "[视频]": "video", "[链接]": "link",
    "[链接/文件]": "other", "[小程序]": "other", "[位置]": "other", "[名片]": "other",
    "[通话]": "other", "[系统]": "other", "[撤回]": "other",
}


def _pick(data, *keys, default=None):
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return default


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if value in (1, "1", "true", "True", "yes"):
        return True
    if value in (0, "0", "false", "False", "no"):
        return False
    return None


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _app_subtype(data, text: str, packed_subtype: int):
    explicit = _pick(data, "app_type", "appmsg_type", "sub_type", "subtype")
    if explicit is not None:
        try:
            return int(explicit)
        except (ValueError, TypeError):
            pass
    if packed_subtype:
        return packed_subtype
    # No external entities, DTDs or oversized XML; never inspect media files.
    if len(text) <= 20000 and "<appmsg" in text and not re.search(r"<!DOCTYPE|<!ENTITY", text, re.I):
        try:
            root = ET.fromstring(text)
            appmsg = root if root.tag == "appmsg" else root.find(".//appmsg")
            if appmsg is not None:
                return int(appmsg.findtext("type") or 0)
        except (ValueError, ET.ParseError):
            pass
    return None


def _message_type(data, text, warnings):
    raw = _pick(data, "type", "local_type", "msg_type", "message_type", "Type")
    system = _as_bool(data.get("is_system")) is True
    if raw is None:
        warnings.append("部分记录未提供消息类型，已列为其他；未根据文字内容猜测媒介类型。")
        return "other", system
    numeric = None
    if isinstance(raw, int) and not isinstance(raw, bool) or isinstance(raw, str) and re.fullmatch(r"\d+", raw.strip()):
        numeric = int(raw)
    if numeric is not None:
        base, subtype = numeric & 0xFFFFFFFF, numeric >> 32
        if base in _SYSTEM_TYPES:
            return "other", True
        if base == 49:
            app_type = _app_subtype(data, text, subtype)
            if app_type == 6:
                return "file", system
            if app_type == 5:
                return "link", system
            if app_type == 57:
                # Quoted text is still a text-form expression. Exclude the quoted
                # counterpart's body so its emotion is not attributed to sender.
                return "text", system
            warnings.append("部分 49 类型消息属于其他应用消息或缺少可验证子类型，已列为其他，未把链接或小程序算作文件。")
            return "other", system
        if base in _NUMERIC_TYPES:
            return _NUMERIC_TYPES[base], system
        warnings.append(f"未知或非四类的微信消息类型 {base} 已列为其他。")
        return "other", system
    name = str(raw).strip().lower()
    if name in ("system", "系统", "撤回", "recalled"):
        system = True
    if name in MESSAGE_TYPES:
        return name, system
    if name in _TYPE_ALIASES:
        return _TYPE_ALIASES[name], system
    warnings.append(f"未知消息类型 {str(raw)[:40]} 已列为其他。")
    return "other", system


def _plain_text(data, raw_text, kind):
    if kind != "text" or "<appmsg" not in raw_text:
        return raw_text if kind == "text" else ""
    if len(raw_text) > 20000 or re.search(r"<!DOCTYPE|<!ENTITY", raw_text, re.I):
        return ""
    try:
        root = ET.fromstring(raw_text)
        app = root if root.tag == "appmsg" else root.find(".//appmsg")
        return (app.findtext("title") or "").strip() if app is not None else ""
    except ET.ParseError:
        return ""


def _context(envelope, previous):
    ctx = dict(previous)
    for key in ("chat_id", "chat_name", "chat", "username", "is_group", "timezone", "source_timezone", "account_id", "account_name", "offset", "limit", "count", "keyword", "failures"):
        if key in envelope and envelope[key] is not None:
            ctx[key] = envelope[key]
    return ctx


def _flatten(value, context=None):
    ctx = context or {}
    if isinstance(value, list):
        for row in value:
            yield from _flatten(row, ctx)
    elif isinstance(value, str):
        yield value, ctx
    elif isinstance(value, dict):
        updated = _context(value, ctx)
        for key in ("messages", "results", "records", "rows", "chats"):
            if key in value and isinstance(value[key], list):
                updated["_source"] = "search" if key == "results" and "keyword" in value else ctx.get("_source", "")
                if key == "messages" and "username" in value and "chat" in value:
                    updated["_source"] = "history"
                yield from _flatten(value[key], updated)
                return
        if "data" in value and isinstance(value["data"], (dict, list)):
            yield from _flatten(value["data"], updated)
            return
        # A message has a type/content/time/sender field. Empty or metadata-only
        # objects are rejected rather than silently converted to phantom records.
        if any(key in value for key in ("id", "message_id", "local_type", "type", "text", "content", "message_content", "timestamp", "create_time", "sender_id", "sender_name")):
            yield value, ctx
        else:
            raise ValueError("JSON 未包含 messages/results 数组或可识别的消息记录。")
    else:
        raise ValueError("消息必须是对象或 wechat-cli 格式化字符串。")


def _parse_line(line, ctx, warnings):
    match = _LINE_RE.match(line.strip())
    if not match:
        raise ValueError("无法识别 wechat-cli 消息行；需要 [日期 时间] 发送者: 内容。")
    timestamp, body = match.groups()
    row = {"timestamp": timestamp}
    if ctx.get("_source") == "search":
        chat_match = re.match(r"^\[([^\]]+)\]\s*(.*)$", body, re.DOTALL)
        if not chat_match:
            raise ValueError("搜索结果缺少聊天名称。")
        chat_name, body = chat_match.groups()
        row.update(chat_id="search:" + _short_hash(chat_name), chat_name=chat_name)
        warnings.append("wechat-cli search 是关键词筛选且可能截断的结果，只能反映该搜索样本，不能代表完整聊天。")
    sender_match = re.match(r"^([^:\n]{1,160}):\s?(.*)$", body, re.DOTALL)
    if sender_match:
        row["sender_name"], body = sender_match.groups()
    elif body.startswith(("[系统]", "[撤回]")):
        row.update(sender_id=SYSTEM_SENDER_ID, sender_name="系统", is_system=True)
    else:
        raise ValueError("消息行缺少明确发送者；不能把联系人当成发送者。")
    # Formatted placeholders do not retain raw type. Inference is explicit and
    # carries a warning because a literal text '[表情]' is indistinguishable.
    row["type"] = "text"
    for prefix, kind in _PREFIX_TYPES.items():
        if body.startswith(prefix):
            row["type"] = kind
            if prefix in ("[系统]", "[撤回]"):
                row.update(sender_id=SYSTEM_SENDER_ID, sender_name="系统", is_system=True)
            break
    if row["type"] == "text" and "\n  ↳ 回复" in body:
        body = body.split("\n  ↳ 回复", 1)[0]
        warnings.append("引用消息仅保留发送者新写的文字，已移除被引用内容。")
    row["text"] = body
    warnings.append("wechat-cli 格式化输出不含原始消息类型或稳定发送者 ID；类型按占位符推断，文字 '[表情]' 可能有歧义。请优先导入带 sender_id/type 的结构化导出。")
    return row


def _normalize_row(row, ctx, index, warnings, occurrences):
    if isinstance(row, str):
        row = _parse_line(row, ctx, warnings)
        if ctx.get("_source") == "search" and _as_bool(ctx.get("is_group")) is not False and not row.get("is_system"):
            raise ValueError("搜索格式化输出未声明群聊或私聊，无法可靠识别同名发送者；请使用结构化导出。")
        if _as_bool(ctx.get("is_group")) is True and not row.get("is_system"):
            raise ValueError("群聊格式化输出缺少稳定 sender_id，无法区分同名成员；请使用结构化导出。")
    elif not isinstance(row, dict):
        raise ValueError("消息记录不是对象")

    chat_object = row.get("chat") if isinstance(row.get("chat"), dict) else {}
    chat_id = str(_pick(row, "chat_id", "conversation_id", "talker", "ChatRoomId", default=_pick(chat_object, "id", "username", default=_pick(ctx, "chat_id", "username", default="")))).strip()
    chat_name = str(_pick(row, "chat_name", "conversation_name", default=_pick(chat_object, "name", default=_pick(ctx, "chat_name", "chat", default="")))).strip()
    if not chat_name and isinstance(row.get("chat"), str):
        chat_name = row["chat"].strip()
    if not chat_id:
        chat_id = "name:" + _short_hash(chat_name) if chat_name else "import:default"
        warnings.append("部分聊天缺少稳定 chat_id；使用导出中的聊天名或默认会话，无法可靠跨文件合并同名聊天。")
    if not chat_name:
        chat_name = chat_id if chat_id != "import:default" else "导入的聊天"
    group = _as_bool(_pick(row, "is_group", default=ctx.get("is_group"))) is True or "@chatroom" in chat_id
    sender_object = row.get("sender") if isinstance(row.get("sender"), dict) else {}
    sender_id = str(_pick(row, "sender_id", "sender_username", "from_user", "FromUserName", default=_pick(sender_object, "id", "username", default=""))).strip()
    sender_name = str(_pick(row, "sender_name", "sender_display_name", "display_name", default=_pick(sender_object, "name", "display_name", default=""))).strip()
    if not sender_name and isinstance(row.get("sender"), str):
        sender_name = row["sender"].strip()

    raw_text = _pick(row, "text", "content", "message_content", "StrContent", default="")
    if not isinstance(raw_text, str):
        raise ValueError("消息文字必须是字符串")
    kind, system = _message_type(row, raw_text, warnings)
    if system:
        sender_id, sender_name = SYSTEM_SENDER_ID, "系统"
    else:
        self_flag = _as_bool(_pick(row, "is_send", "is_sender", "IsSend"))
        if not sender_id and self_flag is True:
            sender_id = "self:" + str(_pick(row, "account_id", default=ctx.get("account_id", "current-import")))
            sender_name = sender_name or "我"
            if not row.get("account_id") and not ctx.get("account_id"):
                warnings.append("自己发送的消息未提供 account_id，仅在当前导入内使用‘我’的身份；请勿混合不同微信账号的数据。")
        # A raw DB rowid is local to its database: scope by chat if no resolved
        # username was provided, to avoid merging unrelated rowids across DBs.
        if not sender_id and row.get("real_sender_id") is not None:
            if group:
                raise ValueError("群聊仅有数据库局部 real_sender_id，无法确认跨分片身份；请解析为稳定 sender_id。")
            sender_id = "local:" + _short_hash(chat_id + ":" + str(row["real_sender_id"]))
            warnings.append("部分发送者仅有数据库局部 real_sender_id，身份限定在当前聊天；建议 adapter 解析为 Name2Id.user_name。")
        if not sender_id:
            if not sender_name:
                raise ValueError("缺少明确的 sender_id 或 sender_name；不能根据联系人推测发送者。")
            if group:
                raise ValueError("群聊缺少稳定 sender_id，无法区分同名成员。")
            sender_id = "name:" + _short_hash(chat_id + ":" + sender_name)
            warnings.append("部分私聊仅有发送者显示名，身份按当前聊天隔离；无法可靠跨聊天识别同一人。")
        if not sender_name:
            sender_name = sender_id
            warnings.append("部分发送者无显示名，使用其稳定 sender_id 展示。")

    timezone_name = _pick(row, "source_timezone", "timezone", default=_pick(ctx, "source_timezone", "timezone"))
    # Modern WeChat exports use Beijing's fixed +08 offset. Windows may not
    # provide an IANA database, so Shanghai imports must not depend on tzdata.
    # All other declared timezones still go through strict timezone validation.
    tz = DEFAULT_TZ if timezone_name == "Asia/Shanghai" else resolve_timezone(timezone_name)
    timestamp, assumed = parse_timestamp(_pick(row, "timestamp", "create_time", "time", "datetime", "CreateTime"), tz)
    if assumed:
        warnings.append("不含时区的时间按导出所声明时区解释；未声明时默认北京时间 +08:00。展示和日期筛选统一使用北京时间。")
    if not timestamp:
        warnings.append("部分消息缺少时间，可参与总体统计，但设置日期范围时将被排除。")
    source_id = _pick(row, "id", "message_id", "msg_id", "msgid", "MsgSvrID", "server_id", "local_id")
    text = _plain_text(row, raw_text, kind).strip()
    if kind == "text" and not text:
        warnings.append("部分文字消息为空，不进入情绪分析。")
    if source_id is None:
        signature = json.dumps([chat_id, sender_id, timestamp, kind, text], ensure_ascii=False)
        digest = _short_hash(signature)
        occurrences[digest] += 1
        message_id = f"auto:{digest}:{occurrences[digest]}"
        warnings.append("部分记录没有消息 ID；为保留同一分钟连续发送的相同消息，这些记录不按内容去重。")
    elif "id" in row:
        message_id = str(source_id)
    else:
        message_id = "msg:" + _short_hash(chat_id + ":" + str(source_id))
    result = {
        "id": message_id, "chat_id": chat_id, "chat_name": chat_name,
        "sender_id": sender_id, "sender_name": sender_name, "timestamp": timestamp,
        "type": kind, "text": text,
    }
    if system:
        result["is_system"] = True
    return result, source_id is not None


def normalize_import(content: str, filename: str = "messages.json") -> dict:
    """Parse an export. Invalid records are skipped with explicit warnings.

    Syntax/format errors raise ValueError. The result always contains messages
    and warnings. Raw media payloads are never retained or inspected.
    """
    if not isinstance(content, str):
        raise ValueError("导入内容必须是 UTF-8 文本")
    content = content.lstrip("\ufeff")
    if not content.strip():
        return {"messages": [], "warnings": ["导入文件为空。"]}
    suffix = filename.lower().rsplit(".", 1)[-1]
    if suffix == "csv":
        reader = csv.DictReader(io.StringIO(content))
        if not reader.fieldnames or not any(key in reader.fieldnames for key in ("sender_id", "sender_name", "sender", "real_sender_id", "is_send", "IsSend")):
            raise ValueError("CSV 需要 sender_id 或 sender_name 等明确发送者列。")
        records = list(reader)
        if any(None in row for row in records):
            raise ValueError("CSV 有行的列数超过表头，请检查逗号和引号。")
    else:
        try:
            records = json.loads(content)
        except json.JSONDecodeError as original:
            if suffix not in ("jsonl", "ndjson") and len(content.strip().splitlines()) <= 1:
                raise ValueError("文件不是有效 JSON；请导入 JSON、JSONL 或 CSV。") from original
            records = []
            for line_number, line in enumerate(content.splitlines(), 1):
                if line.strip():
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"第 {line_number} 行不是有效 JSON。") from exc
    warnings = []
    messages = []
    seen = {}
    occurrences = Counter()
    rows = list(_flatten(records))
    if len(rows) > 100000:
        raise ValueError("单次导入最多支持 100000 条消息，请按聊天或日期拆分文件。")
    for index, (row, ctx) in enumerate(rows, 1):
        if ctx.get("failures"):
            warnings.append("上游导出报告查询失败；本次样本可能不完整。")
        if ctx.get("_source") == "history":
            warnings.append("wechat-cli history 导出可能仅包含分页或指定时间范围；统计只反映本次导入样本。")
        try:
            normalized, has_id = _normalize_row(row, ctx, index, warnings, occurrences)
        except (ValueError, TypeError) as exc:
            warnings.append(f"第 {index} 条消息未导入：{exc}")
            continue
        if has_id:
            key = (normalized["chat_id"], normalized["id"])
            if key in seen:
                warnings.append("重复消息 ID 已去重，每个聊天内同一消息只计一次。")
                if seen[key] != normalized:
                    warnings.append("同一消息 ID 对应不同内容，已保留第一次出现的记录，请检查导出。")
                continue
            seen[key] = normalized
        messages.append(normalized)
    # Analysis references require globally unique IDs. Raw normalized data may
    # reuse '1' in multiple chats; disambiguate deterministically by chat ID.
    collision_ids = {key for key, n in Counter(m["id"] for m in messages).items() if n > 1}
    if collision_ids:
        for message in messages:
            if message["id"] in collision_ids:
                message["id"] += "@" + _short_hash(message["chat_id"])
        warnings.append("不同聊天复用了消息 ID，已按聊天 ID 区分，以防情绪结果关联到错误消息。")
    if not messages:
        warnings.append("没有可统计的消息。")
    return {"messages": messages, "warnings": list(dict.fromkeys(warnings))}
