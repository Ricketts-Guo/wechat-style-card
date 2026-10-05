"""Public, fictional demonstration data; no real WeChat records are used."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def make_demo() -> dict:
    people = [
        ("demo_me", "我", ["text"] * 20 + ["file"] * 3 + ["voice"] * 2 + ["sticker"] * 2 + ["image", "video", "link"]),
        ("demo_lin", "林小满", ["sticker"] * 16 + ["text"] * 6 + ["voice"] * 2 + ["file", "image"]),
        ("demo_chen", "陈可", ["voice"] * 15 + ["text"] * 8 + ["file"] * 2 + ["sticker", "image"]),
        # Same display name as demo_chen, deliberately a different stable ID.
        ("demo_chen_other", "陈可", ["file"] * 13 + ["text"] * 7 + ["voice", "sticker", "video", "other"]),
    ]
    snippets = [
        ("太好了，这个方案我很喜欢，谢谢你！", "positive", "support", "polite"),
        ("我今天有点沮丧，这个问题一直没解决。", "negative", "complaint", "direct"),
        ("资料已经更新，会议安排在周五下午。", "neutral", "coordination", "direct"),
        ("虽然进度有些慢，不过终于有了突破，很开心。", "mixed", "sharing", "direct"),
        ("你可真行🙂", "unknown", "other", "unknown"),
        ("好呀！", "positive", "other", "direct"),
        ("收到", "neutral", "coordination", "direct"),
        ("😄 谢谢", "positive", "support", "polite"),
    ]
    messages, analyses = [], []
    base = datetime(2026, 10, 1, 9, 0, tzinfo=timezone(timedelta(hours=8)))
    number = 0
    for person_index, (person_id, name, kinds) in enumerate(people):
        for index, kind in enumerate(kinds):
            number += 1
            text, sentiment, intent, style = snippets[(index + person_index) % len(snippets)] if kind == "text" else ("", "", "", "")
            chat_id = "demo_project@chatroom" if person_index else "demo_personal"
            message_id = f"demo_{number:04d}"
            messages.append({
                "id": message_id, "chat_id": chat_id,
                "chat_name": "虚构项目讨论群" if person_index else "虚构私聊示例",
                "sender_id": person_id, "sender_name": name,
                "timestamp": (base + timedelta(days=person_index, minutes=index * 11)).isoformat(),
                "type": kind, "text": text,
            })
            if kind == "text":
                analyses.append({
                    "message_id": message_id, "sentiment": sentiment,
                    "intent": intent, "style": style,
                    "reason": "虚构样例的人工情绪标注，用于展示；不是 Jev 响应。",
                    "source": "demo_annotation",
                })
    messages.append({
        "id": "demo_system", "chat_id": "demo_project@chatroom", "chat_name": "虚构项目讨论群",
        "sender_id": "__system__", "sender_name": "系统", "timestamp": base.isoformat(),
        "type": "other", "text": "", "is_system": True,
    })
    return {
        "messages": messages, "demo_analyses": analyses,
        "warnings": ["当前是完全虚构的演示数据，情绪结果是人工样例标注，不是 Jev API 实测结果。"],
        "is_demo": True,
    }
