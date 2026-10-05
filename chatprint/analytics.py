"""Deterministic message statistics; emotional labels come only from analyses."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import math
from statistics import median

from .models import (
    EMOTIONS, EMOTION_LABELS, FORM_TYPES, MESSAGE_TYPES, SYSTEM_SENDER_ID,
    TYPE_LABELS, filter_boundary, parse_timestamp,
)

INTENT_LABELS = {"support": "鼓励与感谢", "question": "提问", "coordination": "协调安排", "sharing": "分享", "complaint": "抱怨", "other": "其他或不明确"}
STYLE_LABELS = {"playful": "轻松幽默", "polite": "礼貌", "direct": "直接", "unknown": "无法判断"}


def _review_dimensions(analysis, sentiment, intent, style):
    """Keep dimension-specific review metadata, including older result formats."""
    raw = analysis.get("review_dimensions")
    if isinstance(raw, dict):
        dimensions = [key for key in ("emotion", "intent", "style") if raw.get(key) is True]
    elif isinstance(raw, (list, tuple)):
        dimensions = [key for key in ("emotion", "intent", "style") if key in raw]
    else:
        dimensions = []
        if sentiment == "unknown" or analysis.get("needs_review") is True:
            # Older Jev responses had a single emotion-only needs_review flag.
            dimensions.append("emotion")
        for key, confidence_key, label in (("emotion", "confidence", sentiment), ("intent", "intent_confidence", intent), ("style", "style_confidence", style)):
            confidence = analysis.get(confidence_key)
            if label is not None and not isinstance(confidence, bool) and isinstance(confidence, (int, float)) and math.isfinite(confidence) and 0 <= confidence < 0.55:
                if key not in dimensions:
                    dimensions.append(key)
        if style == "unknown" and "style" not in dimensions:
            dimensions.append("style")
    return dimensions


def _percentage(count: int, total: int) -> float:
    return round(100 * count / total, 1) if total else 0.0


def filter_messages(messages: list, filters: dict | None = None) -> tuple[list, list]:
    """Date-only boundaries include the whole day in Asia/Shanghai (+08:00)."""
    filters = filters or {}
    start = filter_boundary(filters.get("start"))
    end = filter_boundary(filters.get("end"), end=True)
    if start and end and start > end:
        raise ValueError("开始日期不能晚于结束日期。")
    chat_id = filters.get("chat_id")
    result = []
    missing_time = 0
    invalid_time = 0
    for message in messages:
        if chat_id and message.get("chat_id") != chat_id:
            continue
        if start or end:
            value = message.get("timestamp", "")
            if not value:
                missing_time += 1
                continue
            try:
                iso, _ = parse_timestamp(value)
                stamp = datetime.fromisoformat(iso)
            except (ValueError, TypeError):
                invalid_time += 1
                continue
            if start and stamp < start or end and stamp > end:
                continue
        result.append(message)
    warnings = []
    if missing_time:
        warnings.append(f"日期筛选排除了 {missing_time} 条没有时间的消息。")
    if invalid_time:
        warnings.append(f"日期筛选排除了 {invalid_time} 条时间格式无效的消息。")
    return result, warnings


def _analysis_index(analyses, messages):
    eligible = {m["id"]: m for m in messages if m.get("type") == "text" and m.get("text", "").strip()}
    result = {}
    warnings = []
    aliases = {"正面": "positive", "负面": "negative", "中性": "neutral", "混合": "mixed", "无法判断": "unknown"}
    for analysis in analyses or []:
        if not isinstance(analysis, dict):
            warnings.append("格式无效的情绪分析结果已忽略。")
            continue
        message_id = str(analysis.get("message_id", analysis.get("id", "")))
        if message_id not in eligible:
            continue
        if message_id in result:
            warnings.append("同一文字消息的重复情绪结果已忽略，每条消息只计一次。")
            continue
        has_emotion = any(key in analysis for key in ("sentiment", "emotion", "label"))
        raw = analysis.get("sentiment", analysis.get("emotion", analysis.get("label", "unknown")))
        if isinstance(raw, dict):
            raw = raw.get("label", "unknown")
        sentiment = aliases.get(str(raw), str(raw).lower())
        if sentiment not in EMOTIONS:
            sentiment = "unknown"
            warnings.append("不支持的情绪标签已归为‘无法判断’，未归为中性。")
        source = str(analysis.get("source", ""))
        intent = analysis.get("intent")
        style = analysis.get("style")
        if isinstance(intent, dict):
            intent = intent.get("label")
        if isinstance(style, dict):
            style = style.get("label")
        if intent is not None and intent not in INTENT_LABELS:
            intent = "other"
            warnings.append("不支持的表达意图已归为‘其他或不明确’。")
        if style is not None and style not in STYLE_LABELS:
            style = "unknown"
            warnings.append("不支持的表达风格已归为‘无法判断’。")
        sentiment = sentiment if has_emotion else None
        review_dimensions = _review_dimensions(analysis, sentiment, intent, style)
        manual_dimensions = analysis.get("manual_dimensions")
        if isinstance(manual_dimensions, (list, tuple)):
            manual_dimensions = [key for key in ("emotion", "intent", "style") if key in manual_dimensions]
        elif source == "manual":
            # Older frontend results corrected only emotion and retained the
            # model's intent/style. New results always supply explicit dimensions.
            manual_dimensions = ["emotion"] if has_emotion else [key for key, value in (("intent", intent), ("style", style)) if value is not None]
        else:
            manual_dimensions = []
        manual_review = analysis.get("manual_original_review_dimensions", [])
        if isinstance(manual_review, dict):
            manual_review = [key for key in ("emotion", "intent", "style") if manual_review.get(key) is True]
        elif isinstance(manual_review, (list, tuple)):
            manual_review = [key for key in ("emotion", "intent", "style") if key in manual_review]
        else:
            manual_review = []
        result[message_id] = {
            "message_id": message_id, "sentiment": sentiment,
            "intent": intent, "style": style, "model": analysis.get("model", ""),
            "original_emotion": analysis.get("original_emotion", None if "emotion" in manual_dimensions else sentiment),
            "original_intent": analysis.get("original_intent", None if "intent" in manual_dimensions else intent),
            "original_style": analysis.get("original_style", None if "style" in manual_dimensions else style),
            "confidence": analysis.get("confidence"), "intent_confidence": analysis.get("intent_confidence"),
            "style_confidence": analysis.get("style_confidence"),
            "probabilities": analysis.get("probabilities", {}),
            "intent_probabilities": analysis.get("intent_probabilities", {}),
            "style_probabilities": analysis.get("style_probabilities", {}),
            "review_dimensions": review_dimensions,
            "needs_review": analysis.get("needs_review") is True or bool(review_dimensions),
            "prompt_version": analysis.get("prompt_version", ""),
            "manual_dimensions": manual_dimensions,
            "manual_original_emotion": analysis.get("manual_original_emotion", analysis.get("pre_manual_emotion")),
            "manual_original_intent": analysis.get("manual_original_intent", analysis.get("pre_manual_intent")),
            "manual_original_style": analysis.get("manual_original_style", analysis.get("pre_manual_style")),
            "manual_original_review_dimensions": manual_review,
            "pre_manual_emotion": analysis.get("pre_manual_emotion"),
            "pre_manual_intent": analysis.get("pre_manual_intent"), "pre_manual_style": analysis.get("pre_manual_style"),
            "original_source": analysis.get("original_source"),
            "reason": str(analysis.get("reason", ""))[:500], "source": source,
        }
        if manual_dimensions:
            warnings.append("当前统计包含人工修正的分类结果；模型原始标签和选项分布仅作为记录保留。")
        demo_sources = ("demo", "demo_annotation", "example", "manual_demo")
        if source in demo_sources or analysis.get("original_source") in demo_sources:
            warnings.append("当前包含虚构示例的人工情绪标注，属于演示数据，不是 Jev API 实测结果。")
    return result, list(dict.fromkeys(warnings))


def _tags(total, types, form_total, text_stats, emotions, analyzed, expression):
    tags = []
    if total < 20 or form_total < 20:
        tags.append({"label": "样本不足", "reason": f"共有 {total} 条消息，其中四类表达 {form_total} 条；不足 20 条时不生成形式偏好标签。"})
    else:
        ranked = sorted(FORM_TYPES, key=lambda kind: (-types[kind], FORM_TYPES.index(kind)))
        first, second = ranked[:2]
        first_rate = types[first] / form_total
        second_rate = types[second] / form_total
        if first_rate >= 0.4 and first_rate - second_rate >= 0.1:
            label = {"text": "文字派", "file": "文件派", "sticker": "表情包派", "voice": "语音派"}[first]
            tags.append({"label": label, "reason": f"在文字、文件、表情包、语音 {form_total} 条中，{TYPE_LABELS[first]}占 {_percentage(types[first], form_total)}%，领先第二种形式至少 10 个百分点。"})
        else:
            tags.append({"label": "多形式表达", "reason": "四类表达中没有一种同时达到 40% 且领先第二种形式 10 个百分点。"})
    if total >= 20 and text_stats["count"] >= 10:
        length = text_stats["median_length"]
        if length >= 40:
            tags.append({"label": "长句偏好", "reason": f"{text_stats['count']} 条非空文字的字符数中位数为 {length}，达到 40 字阈值。"})
        elif length <= 8:
            tags.append({"label": "短句偏好", "reason": f"{text_stats['count']} 条非空文字的字符数中位数为 {length}，不超过 8 字。"})
    if analyzed >= 20:
        for sentiment, label in (("positive", "正面表达较多"), ("negative", "负面表达较多")):
            if emotions[sentiment] / analyzed >= 0.5:
                tags.append({"label": label, "reason": f"在实际分析的 {analyzed} 条文字中，{EMOTION_LABELS[sentiment]}表达占 {_percentage(emotions[sentiment], analyzed)}%；描述该样本的表达，不判断人格。"})
    for category, label in (("support", "鼓励型表达"), ("question", "提问型表达"), ("coordination", "协调型表达")):
        denominator = expression["intent_analyzed"]
        if denominator >= 10 and expression["intent_counts"][category] / denominator >= 0.4:
            tags.append({"label": label, "reason": f"在实际分析意图的 {denominator} 条文字中，{INTENT_LABELS[category]}占 {_percentage(expression['intent_counts'][category], denominator)}%；只描述当前样本。"})
    for category, label in (("playful", "轻松表达"), ("polite", "礼貌表达")):
        denominator = expression["style_analyzed"]
        if denominator >= 10 and expression["style_counts"][category] / denominator >= 0.4:
            tags.append({"label": label, "reason": f"在实际分析风格的 {denominator} 条文字中，{STYLE_LABELS[category]}占 {_percentage(expression['style_counts'][category], denominator)}%；只描述当前样本。"})
    return tags


def summarize(messages: list, analyses: list | None = None, filters: dict | None = None) -> dict:
    selected, warnings = filter_messages(messages, filters)
    # Defensive de-duplication for callers that supply already-normalized data.
    seen = set()
    unique = []
    for message in selected:
        key = (message.get("chat_id", ""), message.get("id", ""))
        if key[1] and key in seen:
            warnings.append("重复消息 ID 已去重，每个聊天内同一消息只计一次。")
            continue
        if key[1]:
            seen.add(key)
        unique.append(message)
    selected = unique
    analysis_by_id, analysis_warnings = _analysis_index(analyses, selected)
    warnings.extend(analysis_warnings)
    people_messages = defaultdict(list)
    chat_names = {}
    system_messages = 0
    unidentified_messages = 0
    overall_types = {kind: 0 for kind in MESSAGE_TYPES}
    for message in selected:
        kind = message.get("type", "other")
        if kind not in overall_types:
            kind = "other"
        overall_types[kind] += 1
        chat_names.setdefault(message.get("chat_id", ""), message.get("chat_name", "导入的聊天"))
        if message.get("is_system") or message.get("sender_id") == SYSTEM_SENDER_ID:
            system_messages += 1
            continue
        if not message.get("sender_id"):
            unidentified_messages += 1
            continue
        people_messages[message["sender_id"]].append(message)
    if unidentified_messages:
        warnings.append(f"{unidentified_messages} 条消息缺少发送者身份，已保留总体条数但排除人物标签。")
    people = []
    for sender_id, records in people_messages.items():
        types = {kind: 0 for kind in MESSAGE_TYPES}
        for record in records:
            types[record["type"] if record.get("type") in types else "other"] += 1
        form_total = sum(types[kind] for kind in FORM_TYPES)
        maximum = max((types[kind] for kind in FORM_TYPES), default=0)
        leaders = [kind for kind in FORM_TYPES if types[kind] == maximum] if maximum else []
        dominant = leaders[0] if len(leaders) == 1 else "balanced" if leaders else None
        texts = [record for record in records if record.get("type") == "text" and record.get("text", "").strip()]
        text_stats = {
            "count": len(texts), "median_length": median([len(record["text"].strip()) for record in texts]) if texts else 0,
        }
        counts = {emotion: 0 for emotion in EMOTIONS}
        intent_counts = {key: 0 for key in INTENT_LABELS}
        style_counts = {key: 0 for key in STYLE_LABELS}
        manual_counts = {"emotion": 0, "intent": 0, "style": 0}
        evidence = []
        for record in texts:
            analysis = analysis_by_id.get(record["id"])
            if analysis:
                if analysis["sentiment"] is not None:
                    counts[analysis["sentiment"]] += 1
                    if "emotion" in analysis["manual_dimensions"]:
                        manual_counts["emotion"] += 1
                if analysis["intent"] is not None:
                    intent_counts[analysis["intent"]] += 1
                    if "intent" in analysis["manual_dimensions"]:
                        manual_counts["intent"] += 1
                if analysis["style"] is not None:
                    style_counts[analysis["style"]] += 1
                    if "style" in analysis["manual_dimensions"]:
                        manual_counts["style"] += 1
                if len(evidence) < 5:
                    evidence.append({
                        "message_id": record["id"], "timestamp": record.get("timestamp", ""),
                        "text": record["text"][:200], "sentiment": analysis["sentiment"],
                        "reason": analysis["reason"], "source": analysis["source"],
                        "intent": analysis["intent"], "style": analysis["style"], "model": analysis["model"],
                        "confidence": analysis["confidence"], "intent_confidence": analysis["intent_confidence"],
                        "style_confidence": analysis["style_confidence"], "needs_review": analysis["needs_review"],
                        "original_emotion": analysis["original_emotion"], "original_intent": analysis["original_intent"],
                        "original_style": analysis["original_style"], "probabilities": analysis["probabilities"],
                        "intent_probabilities": analysis["intent_probabilities"], "style_probabilities": analysis["style_probabilities"],
                        "review_dimensions": analysis["review_dimensions"], "prompt_version": analysis["prompt_version"],
                        "manual_dimensions": analysis["manual_dimensions"],
                        "manual_original_emotion": analysis["manual_original_emotion"],
                        "manual_original_intent": analysis["manual_original_intent"],
                        "manual_original_style": analysis["manual_original_style"],
                        "manual_original_review_dimensions": analysis["manual_original_review_dimensions"],
                        "pre_manual_emotion": analysis["pre_manual_emotion"], "original_source": analysis["original_source"],
                        "pre_manual_intent": analysis["pre_manual_intent"], "pre_manual_style": analysis["pre_manual_style"],
                    })
        analyzed = sum(counts.values())
        expression = {
            "intent_counts": intent_counts, "style_counts": style_counts,
            "intent_analyzed": sum(intent_counts.values()), "style_analyzed": sum(style_counts.values()),
            "intent_manual_count": manual_counts["intent"], "style_manual_count": manual_counts["style"],
            "intent_percentages": {key: _percentage(value, sum(intent_counts.values())) for key, value in intent_counts.items()},
            "style_percentages": {key: _percentage(value, sum(style_counts.values())) for key, value in style_counts.items()},
        }
        names = list(dict.fromkeys(record.get("sender_name", sender_id) for record in records))
        if len(names) > 1:
            warnings.append("同一稳定发送者 ID 存在多个显示名，按 ID 合并并显示最近一次名称。")
        people.append({
            "id": sender_id, "name": names[-1], "aliases": names, "total": len(records),
            "types": types, "form_total": form_total,
            "form_percentages": {kind: _percentage(types[kind], form_total) for kind in FORM_TYPES},
            "dominant_form": dominant,
            "tags": _tags(len(records), types, form_total, text_stats, counts, analyzed, expression),
            "expression": expression,
            "text_stats": text_stats,
            "emotion": {
                "counts": counts, "percentages": {key: _percentage(value, analyzed) for key, value in counts.items()},
                "analyzed": analyzed, "total_text": len(texts), "coverage": _percentage(analyzed, len(texts)),
                "unanalyzed": len(texts) - analyzed,
                "manual_count": manual_counts["emotion"],
            },
            "evidence": evidence,
        })
    people.sort(key=lambda person: (-person["total"], person["id"]))
    return {
        "total_messages": len(selected), "total_people": len(people), "people": people,
        "chats": [{"id": key, "name": name} for key, name in sorted(chat_names.items())],
        "types": overall_types, "system_messages": system_messages, "unidentified_messages": unidentified_messages,
        "form_total": sum(overall_types[kind] for kind in FORM_TYPES),
        "warnings": list(dict.fromkeys(warnings)),
        "filters": dict(filters or {}), "timezone": "Asia/Shanghai (+08:00)",
        "definitions": {
            "forms": "四类比例的分母是文字+文件+表情包+语音消息数；图片、视频、链接、其他另列。",
            "emotion": "情绪比例的分母是实际分析的非空文字消息数；未分析不等于中性。",
            "identity": "人物按稳定 sender_id 聚合；同名且不同 ID 不合并；系统通知不生成人物标签。",
            "tags": "标签仅描述当前导入范围的聊天表达习惯；至少 20 条四类消息才生成形式偏好。",
        },
    }


def _markdown(value):
    text = str(value).replace("\r", " ").replace("\n", " ")
    for char in ("\\", "`", "*", "_", "[", "]", "<", ">", "|", "#"):
        text = text.replace(char, "\\" + char)
    return text


def make_report(messages, analyses=None, anonymize=True, filters=None) -> str:
    """Export aggregated Markdown. Anonymized export excludes raw text/IDs.

    It intentionally contains neither chat names nor source excerpts when
    anonymized, so a shared card cannot accidentally expose private messages.
    """
    summary = summarize(messages, analyses, filters)
    lines = [
        "# ChatPrint · 聊天表达风格报告", "",
        f"当前样本：{summary['total_messages']} 条消息，{summary['total_people']} 位发送者；系统通知 {summary['system_messages']} 条。", "",
        "标签描述当前聊天样本中的表达习惯。", "",
        "- 四类比例的分母：文字、文件、表情包、语音的合计数量；图片、视频、链接、其他另列。",
        "- 情绪比例的分母：实际分析的非空文字消息数；未分析消息不计入中性。",
        "- 所有日期范围均按北京时间 +08:00 解释，结束日期包含当天全部时间。", "",
    ]
    if filters and (filters.get("start") or filters.get("end")):
        lines += [f"时间筛选：{_markdown(filters.get('start') or '不限')} 至 {_markdown(filters.get('end') or '不限')}。", ""]
    if not anonymize:
        lines += ["聊天范围：" + "、".join(_markdown(chat["name"]) for chat in summary["chats"]), ""]
    if not summary["people"]:
        lines += ["当前筛选范围没有可生成人物卡的消息。", ""]
    for number, person in enumerate(summary["people"], 1):
        name = f"联系人 {number:02d}" if anonymize else _markdown(person["name"])
        lines += [f"## {name}", "", f"消息总数：{person['total']}；四类表达合计：{person['form_total']}。", "",
                  "| 表达形式 | 数量 | 四类表达占比 |", "| --- | ---: | ---: |"]
        for kind in FORM_TYPES:
            lines.append(f"| {TYPE_LABELS[kind]} | {person['types'][kind]} | {person['form_percentages'][kind]}% |")
        lines += ["", "其他消息：" + "、".join(f"{TYPE_LABELS[kind]} {person['types'][kind]} 条" for kind in MESSAGE_TYPES if kind not in FORM_TYPES) + "。", ""]
        if person["tags"]:
            lines += ["标签及依据：", ""]
            for tag in person["tags"]:
                lines.append(f"- **{_markdown(tag['label'])}**：{_markdown(tag['reason'])}")
            lines.append("")
        emotion = person["emotion"]
        lines += [f"情绪分析覆盖：{emotion['analyzed']}/{emotion['total_text']} 条非空文字（{emotion['coverage']}%）；未分析 {emotion['unanalyzed']} 条。", ""]
        if emotion["manual_count"]:
            lines += [f"其中 {emotion['manual_count']} 条使用人工修正的情绪分类。", ""]
        if emotion["analyzed"]:
            lines += ["| 情绪表达 | 数量 | 已分析文字占比 |", "| --- | ---: | ---: |"]
            for key in EMOTIONS:
                lines.append(f"| {EMOTION_LABELS[key]} | {emotion['counts'][key]} | {emotion['percentages'][key]}% |")
            lines.append("")
        else:
            lines += ["尚无实际情绪分析结果。", ""]
        expression = person["expression"]
        for dimension, title, labels in (("intent", "表达意图", INTENT_LABELS), ("style", "文字风格", STYLE_LABELS)):
            denominator = expression[f"{dimension}_analyzed"]
            if denominator:
                manual_note = f"；其中 {expression[dimension + '_manual_count']} 条人工修正" if expression[dimension + "_manual_count"] else ""
                lines += [f"{title}：实际分析 {denominator} 条文字{manual_note}；比例以这些结果为分母。", "", "| 分类 | 数量 | 占比 |", "| --- | ---: | ---: |"]
                for key, label in labels.items():
                    lines.append(f"| {label} | {expression[dimension + '_counts'][key]} | {expression[dimension + '_percentages'][key]}% |")
                lines.append("")
        if not anonymize and person["evidence"]:
            lines += ["可核查的文字样本：", ""]
            for evidence in person["evidence"]:
                lines.append(f"- {_markdown(evidence['timestamp'])} · {EMOTION_LABELS.get(evidence['sentiment'], '未分析情绪')}：{_markdown(evidence['text'])}")
            lines.append("")
    if summary["warnings"]:
        lines += ["## 样本说明", ""]
        lines += [f"- {_markdown(warning)}" for warning in summary["warnings"]]
        lines.append("")
    if anonymize:
        lines += ["本报告使用联系人编号，未包含姓名、聊天名、身份 ID 或原文摘录。", ""]
    return "\n".join(lines)
