"""Core behavior tests use fabricated records only."""
import csv
import io
import json
import hashlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from chatprint.analytics import make_report, summarize
from chatprint.demo import make_demo
from chatprint.importers import normalize_import
from chatprint.models import parse_timestamp
from chatprint.raw_adapter import AdapterUnavailable, load_messages, records_from_database


def row(identifier="m1", **extra):
    result = {"id": identifier, "chat_id": "c1", "chat_name": "示例",
              "sender_id": "p1", "sender_name": "林", "timestamp": "2026-10-01T10:00:00+08:00",
              "type": "text", "text": "谢谢！"}
    result.update(extra)
    return result


class ImportTests(unittest.TestCase):
    def test_numeric_message_types_and_text_emoji(self):
        kinds = [1, 3, 34, 43, 47, 49, 25, 48, (6 << 32) | 49]
        result = normalize_import(json.dumps([row(str(i), type=kind, text="😄") for i, kind in enumerate(kinds)]), "test.json")
        self.assertEqual([m["type"] for m in result["messages"]], ["text", "image", "voice", "video", "sticker", "other", "other", "other", "file"])
        self.assertEqual(result["messages"][0]["text"], "😄")
        self.assertEqual(result["messages"][4]["text"], "")

    def test_file_subtype_does_not_use_filename_guess(self):
        messages = [row("a", type=49, text="report.pdf"), row("b", type=49, app_type=6),
                    row("c", type=49, text="<msg><appmsg><type>5</type><title>文档.pdf</title></appmsg></msg>"),
                    row("d", type=49, text="<msg><appmsg><type>57</type><title>谢谢！</title><refermsg><content>你太差了</content></refermsg></appmsg></msg>")]
        result = normalize_import(json.dumps(messages))
        self.assertEqual([m["type"] for m in result["messages"]], ["other", "file", "link", "text"])
        self.assertEqual(result["messages"][3]["text"], "谢谢！")

    def test_jsonl_csv_raw_fields(self):
        records = [dict(local_id=1, local_type=47, create_time=1790812800000, sender_id="wx_a", sender_name="甲", chat_id="chat_a"),
                   dict(local_id=2, local_type=34, create_time=1790812800, sender_id="wx_b", sender_name="乙", chat_id="chat_a")]
        result = normalize_import("\n".join(json.dumps(r) for r in records), "example.jsonl")
        self.assertEqual(len(result["messages"]), 2)
        self.assertEqual(result["messages"][0]["timestamp"], result["messages"][1]["timestamp"])
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)
        from_csv = normalize_import(output.getvalue(), "example.csv")
        self.assertEqual(from_csv["messages"], result["messages"])

    def test_dedup_ids_preserve_same_minute_identical_stickers(self):
        a = row("x", type="sticker", text="")
        without_id = dict(a)
        without_id.pop("id")
        result = normalize_import(json.dumps([a, a, without_id, without_id]))
        self.assertEqual(len(result["messages"]), 3)
        self.assertEqual(len({m["id"] for m in result["messages"]}), 3)

    def test_id_collision_across_chats(self):
        result = normalize_import(json.dumps([row("1"), row("1", chat_id="c2")]))
        self.assertEqual(len(result["messages"]), 2)
        self.assertNotEqual(result["messages"][0]["id"], result["messages"][1]["id"])

    def test_missing_sender_does_not_guess_contact(self):
        message = row()
        message.pop("sender_id")
        message.pop("sender_name")
        result = normalize_import(json.dumps({"chat": "甲", "username": "wx_甲", "messages": [message]}))
        self.assertEqual(result["messages"], [])
        self.assertTrue(any("发送者" in w for w in result["warnings"]))

    def test_groups_same_name_different_ids_do_not_merge(self):
        result = normalize_import(json.dumps([row("a", chat_id="g@chatroom", sender_id="p1", sender_name="陈可"),
                                             row("b", chat_id="g@chatroom", sender_id="p2", sender_name="陈可")]))
        self.assertEqual(summarize(result["messages"])["total_people"], 2)

    def test_name_only_group_rejected(self):
        message = row(chat_id="g@chatroom")
        del message["sender_id"]
        result = normalize_import(json.dumps([message]))
        self.assertEqual(result["messages"], [])
        self.assertTrue(any("同名" in w for w in result["warnings"]))

    def test_native_cli_history_private_strings(self):
        source = {"chat": "甲", "username": "wx_a", "is_group": False, "limit": 50,
                  "messages": ["[2026-10-01 10:00] 甲: [表情]", "[2026-10-01 10:01] 我: 😄谢谢"]}
        result = normalize_import(json.dumps(source))
        self.assertEqual([m["type"] for m in result["messages"]], ["sticker", "text"])
        self.assertNotEqual(result["messages"][0]["sender_id"], result["messages"][1]["sender_id"])
        self.assertTrue(any("占位符" in w for w in result["warnings"]))

    def test_native_cli_search_result_with_truncation_warning(self):
        result = normalize_import(json.dumps({"keyword": "谢谢", "is_group": False, "results": ["[2026-10-01 10:00] [甲] 我: 谢谢"]}))
        self.assertEqual(result["messages"][0]["sender_name"], "我")
        self.assertTrue(any("截断" in w for w in result["warnings"]))

    def test_native_search_without_group_identity_and_local_group_id_rejected(self):
        result = normalize_import(json.dumps({"keyword": "谢谢", "results": ["[2026-10-01 10:00] [某群] 同名: 谢谢"]}))
        self.assertEqual(result["messages"], [])
        self.assertTrue(any("未声明群聊或私聊" in warning for warning in result["warnings"]))
        message = row(chat_id="g@chatroom", real_sender_id=1)
        del message["sender_id"]
        result = normalize_import(json.dumps([message]))
        self.assertEqual(result["messages"], [])

    def test_native_cli_group_strings_rejected(self):
        result = normalize_import(json.dumps({"chat": "组", "username": "g@chatroom", "is_group": True,
                                              "messages": ["[2026-10-01 10:00] 甲: 好"]}))
        self.assertEqual(result["messages"], [])

    def test_naive_timezone_explicit_and_default(self):
        source = {"timezone": "UTC", "messages": [row(timestamp="2026-10-01 02:00:00")]}
        self.assertEqual(normalize_import(json.dumps(source))["messages"][0]["timestamp"], "2026-10-01T10:00:00+08:00")
        self.assertEqual(parse_timestamp("2026-10-01 02:00:00")[0], "2026-10-01T02:00:00+08:00")

    def test_shanghai_source_timezone_without_windows_iana_database(self):
        inputs = [
            {"source_timezone": "Asia/Shanghai", "messages": [row(timestamp="2026-10-01 02:00:00")]},
            {"timezone": "Asia/Shanghai", "messages": [row(timestamp="2026-10-01 02:00:00")]},
            {"messages": [row(timestamp="2026-10-01 02:00:00", source_timezone="Asia/Shanghai")]},
        ]
        with patch("chatprint.models.ZoneInfo", side_effect=ZoneInfoNotFoundError("simulated Windows no tzdata")):
            for source in inputs:
                with self.subTest(source=source):
                    result = normalize_import(json.dumps(source))
                    self.assertEqual(len(result["messages"]), 1)
                    self.assertEqual(result["messages"][0]["timestamp"], "2026-10-01T02:00:00+08:00")
            unknown = normalize_import(json.dumps({"source_timezone": "Unknown/Timezone", "messages": [row(timestamp="2026-10-01 02:00:00")]}))
            self.assertEqual(unknown["messages"], [])
            self.assertTrue(any("无法识别时区" in warning for warning in unknown["warnings"]))

    def test_source_timezone_alias_preserves_explicit_offset_and_row_override(self):
        source = {"source_timezone": "UTC", "messages": [
            row("a", timestamp="2026-10-01 02:00:00"),
            row("b", timestamp="2026-10-01 02:00:00", source_timezone="Asia/Shanghai"),
            row("c", timestamp="2026-10-01T02:00:00-04:00"),
        ]}
        times = [m["timestamp"] for m in normalize_import(json.dumps(source))["messages"]]
        self.assertEqual(times, ["2026-10-01T10:00:00+08:00", "2026-10-01T02:00:00+08:00", "2026-10-01T14:00:00+08:00"])

    def test_empty_and_malformed_input(self):
        self.assertEqual(normalize_import("")["messages"], [])
        self.assertEqual(normalize_import("[]")["messages"], [])
        with self.assertRaises(ValueError):
            normalize_import('{"messages":')
        with self.assertRaises(ValueError):
            normalize_import('{"messages": []}\n{broken}', "file.jsonl")


class AnalyticsTests(unittest.TestCase):
    def test_manual_correction_all_dimensions_preserves_originals_and_counts(self):
        analysis = {
            "message_id": "a", "emotion": "positive", "sentiment": "positive", "intent": "support", "style": "polite",
            "original_emotion": "negative", "original_intent": "complaint", "original_style": "direct",
            "manual_original_emotion": "unknown", "manual_original_intent": "other", "manual_original_style": "unknown",
            "manual_original_review_dimensions": ["emotion", "intent", "style"],
            "manual_dimensions": ["emotion", "intent", "style"], "review_dimensions": [], "needs_review": False,
            "confidence": 0.3, "intent_confidence": 0.2, "style_confidence": 0.4,
            "probabilities": {"negative": 0.6, "positive": 0.4}, "source": "manual", "original_source": "jev", "model": "jev-example",
        }
        summary = summarize([row("a")], [analysis])
        person = summary["people"][0]
        self.assertEqual(person["emotion"]["counts"]["positive"], 1)
        self.assertEqual(person["emotion"]["manual_count"], 1)
        self.assertEqual(person["expression"]["intent_counts"]["support"], 1)
        self.assertEqual(person["expression"]["style_counts"]["polite"], 1)
        self.assertEqual(person["expression"]["intent_analyzed"], 1)
        self.assertEqual(person["expression"]["style_analyzed"], 1)
        self.assertEqual(person["expression"]["intent_manual_count"], 1)
        self.assertEqual(person["expression"]["style_manual_count"], 1)
        evidence = person["evidence"][0]
        for field in ("original_emotion", "original_intent", "original_style", "manual_original_emotion", "manual_original_intent", "manual_original_style", "manual_original_review_dimensions", "manual_dimensions", "original_source", "model", "probabilities"):
            self.assertEqual(evidence[field], analysis[field])
        self.assertFalse(evidence["needs_review"])
        self.assertEqual(evidence["review_dimensions"], [])
        report = make_report([row("a")], [analysis])
        self.assertIn("1 条使用人工修正的情绪分类", report)
        self.assertIn("表达意图：实际分析 1 条文字；其中 1 条人工修正", report)
        self.assertIn("文字风格：实际分析 1 条文字；其中 1 条人工修正", report)
        self.assertTrue(any("人工修正" in warning for warning in summary["warnings"]))

    def test_manual_partial_correction_keeps_uncorrected_review_and_independent_denominators(self):
        analyses = [
            {"message_id": "a", "emotion": "unknown", "intent": "question", "style": "unknown", "original_emotion": "negative",
             "original_intent": "sharing", "original_style": "direct", "source": "manual", "original_source": "jev",
             "manual_dimensions": ["intent"], "manual_original_intent": "other",
             "manual_original_review_dimensions": ["emotion", "intent", "style"],
             "review_dimensions": ["emotion", "style"], "needs_review": True},
            {"message_id": "b", "intent": "coordination", "style": "direct", "source": "manual", "manual_dimensions": ["intent", "style"], "review_dimensions": [], "needs_review": False},
        ]
        person = summarize([row("a"), row("b")], analyses)["people"][0]
        self.assertEqual(person["emotion"]["analyzed"], 1)
        self.assertEqual(person["emotion"]["manual_count"], 0)
        self.assertEqual(person["emotion"]["counts"]["unknown"], 1)
        self.assertEqual(person["expression"]["intent_analyzed"], 2)
        self.assertEqual(person["expression"]["style_analyzed"], 2)
        self.assertEqual(person["expression"]["intent_manual_count"], 2)
        self.assertEqual(person["expression"]["style_manual_count"], 1)
        self.assertEqual(person["evidence"][0]["review_dimensions"], ["emotion", "style"])
        self.assertTrue(person["evidence"][0]["needs_review"])
        self.assertFalse(person["evidence"][1]["needs_review"])

    def test_legacy_manual_emotion_does_not_claim_model_intent_style_were_corrected(self):
        person = summarize([row("a")], [{"message_id": "a", "emotion": "neutral", "intent": "sharing", "style": "direct",
                                        "source": "manual", "pre_manual_emotion": "unknown", "review_dimensions": [], "needs_review": False}])["people"][0]
        self.assertEqual(person["emotion"]["manual_count"], 1)
        self.assertEqual(person["expression"]["intent_manual_count"], 0)
        self.assertEqual(person["expression"]["style_manual_count"], 0)
        self.assertEqual(person["evidence"][0]["manual_dimensions"], ["emotion"])
        self.assertEqual(person["evidence"][0]["manual_original_emotion"], "unknown")
        self.assertIsNone(person["evidence"][0]["original_emotion"])

    def test_reset_manual_dimension_restores_review_and_model_counts(self):
        restored = {"message_id": "a", "emotion": "neutral", "intent": "other", "style": "direct", "source": "jev", "model": "jev-example",
                    "original_intent": "question", "manual_original_intent": "other", "manual_original_review_dimensions": ["intent"],
                    "manual_dimensions": [], "review_dimensions": ["intent"], "needs_review": True}
        summary = summarize([row("a")], [restored])
        person = summary["people"][0]
        self.assertEqual(person["expression"]["intent_counts"]["other"], 1)
        self.assertEqual(person["expression"]["intent_counts"]["question"], 0)
        self.assertEqual(person["expression"]["intent_manual_count"], 0)
        self.assertEqual(person["evidence"][0]["review_dimensions"], ["intent"])
        self.assertTrue(person["evidence"][0]["needs_review"])
        self.assertEqual(person["evidence"][0]["original_intent"], "question")
        self.assertFalse(any("人工修正" in warning for warning in summary["warnings"]))

    def test_manual_correction_keeps_fictional_demo_provenance_in_report(self):
        analysis = {"message_id": "a", "emotion": "positive", "intent": "support", "style": "polite", "source": "manual",
                    "original_source": "demo_annotation", "manual_dimensions": ["emotion", "intent", "style"],
                    "review_dimensions": [], "needs_review": False}
        summary = summarize([row("a")], [analysis])
        self.assertTrue(any("虚构示例" in warning for warning in summary["warnings"]))
        self.assertIn("虚构示例", make_report([row("a")], [analysis]))
        actual = {**analysis, "original_source": "jev"}
        self.assertFalse(any("虚构示例" in warning for warning in summarize([row("a")], [actual])["warnings"]))

    def test_review_dimensions_original_labels_and_probability_metadata_preserved(self):
        analysis = {
            "message_id": "a", "emotion": "neutral", "intent": "other", "style": "unknown",
            "original_emotion": "neutral", "original_intent": "question", "original_style": "direct",
            "confidence": 0.8, "intent_confidence": 0.4, "style_confidence": 0.3,
            "probabilities": {"neutral": 0.84, "unknown": 0.16},
            "intent_probabilities": {"question": 0.5, "other": 0.5},
            "style_probabilities": {"direct": 0.5, "unknown": 0.5},
            "review_dimensions": ["intent", "style"], "needs_review": False,
            "source": "jev", "model": "jev-fixture", "prompt_version": "fixture-v2",
        }
        evidence = summarize([row("a")], [analysis])["people"][0]["evidence"][0]
        for key in ("original_emotion", "original_intent", "original_style", "probabilities", "intent_probabilities", "style_probabilities", "review_dimensions", "prompt_version"):
            self.assertEqual(evidence[key], analysis[key])
        self.assertTrue(evidence["needs_review"])
        self.assertEqual(evidence["sentiment"], "neutral")
        self.assertEqual(evidence["intent"], "other")

    def test_legacy_review_flag_and_dimension_dictionary_compatibility(self):
        analyses = [
            {"message_id": "a", "emotion": "unknown", "original_emotion": "positive", "needs_review": True},
            {"message_id": "b", "emotion": "neutral", "intent": "other", "intent_confidence": 0.3, "needs_review": False},
            {"message_id": "c", "emotion": "neutral", "review_dimensions": {"emotion": False, "intent": True, "style": False}},
            {"message_id": "d", "intent": "support"},
        ]
        evidence = summarize([row(key) for key in ("a", "b", "c", "d")], analyses)["people"][0]["evidence"]
        self.assertEqual(evidence[0]["review_dimensions"], ["emotion"])
        self.assertEqual(evidence[1]["review_dimensions"], ["intent"])
        self.assertEqual(evidence[2]["review_dimensions"], ["intent"])
        self.assertEqual(evidence[3]["review_dimensions"], [])
        self.assertTrue(evidence[0]["needs_review"])
        self.assertTrue(evidence[1]["needs_review"])
        self.assertTrue(evidence[2]["needs_review"])
        self.assertFalse(evidence[3]["needs_review"])
        self.assertEqual(evidence[0]["style_probabilities"], {})

    def test_expression_separate_denominators_and_preserve_review_metadata(self):
        messages = [row(str(i)) for i in range(12)]
        analyses = [{"message_id": str(i), "emotion": "neutral", "intent": "support" if i < 6 else "other", "style": "polite",
                     "confidence": 0.71, "intent_confidence": 0.6, "style_confidence": 0.77,
                     "model": "jev-test", "needs_review": True} for i in range(10)]
        analyses.append({"message_id": "10", "sentiment": "positive"})
        person = summarize(messages, analyses)["people"][0]
        expression = person["expression"]
        self.assertEqual(expression["intent_analyzed"], 10)
        self.assertEqual(expression["intent_counts"]["support"], 6)
        self.assertEqual(expression["intent_percentages"]["support"], 60.0)
        self.assertEqual(expression["style_analyzed"], 10)
        self.assertIn("鼓励型表达", [t["label"] for t in person["tags"]])
        self.assertIn("礼貌表达", [t["label"] for t in person["tags"]])
        self.assertEqual(person["evidence"][0]["confidence"], 0.71)
        self.assertEqual(person["evidence"][0]["model"], "jev-test")
        self.assertTrue(person["evidence"][0]["needs_review"])

    def test_missing_emotion_not_analyzed_and_invalid_expression_unknown(self):
        person = summarize([row("a"), row("b")], [{"message_id": "a", "intent": "unsupported", "style": "unsupported"}])["people"][0]
        self.assertEqual(person["emotion"]["analyzed"], 0)
        self.assertEqual(person["expression"]["intent_counts"]["other"], 1)
        self.assertEqual(person["expression"]["style_counts"]["unknown"], 1)
        self.assertEqual(person["expression"]["style_analyzed"], 1)
        self.assertNotIn("鼓励型表达", [t["label"] for t in person["tags"]])

    def test_four_form_denominator(self):
        kinds = ["text", "text", "sticker", "voice", "file", "image", "video", "link", "other"]
        summary = summarize([row(str(i), type=kind) for i, kind in enumerate(kinds)])
        person = summary["people"][0]
        self.assertEqual(person["form_total"], 5)
        self.assertEqual(person["total"], 9)
        self.assertEqual(person["form_percentages"], {"text": 40.0, "file": 20.0, "sticker": 20.0, "voice": 20.0})

    def test_unanalyzed_not_neutral_and_unknown_not_neutral(self):
        messages = [row("a"), row("b"), row("c"), row("d", type="voice")]
        analyses = [{"message_id": "a", "sentiment": "positive"}, {"message_id": "b", "sentiment": "not_a_sentiment"},
                    {"message_id": "d", "sentiment": "negative"}, {"message_id": "a", "sentiment": "negative"}]
        emotion = summarize(messages, analyses)["people"][0]["emotion"]
        self.assertEqual(emotion["counts"], {"positive": 1, "negative": 0, "neutral": 0, "mixed": 0, "unknown": 1})
        self.assertEqual(emotion["analyzed"], 2)
        self.assertEqual(emotion["unanalyzed"], 1)
        self.assertEqual(emotion["coverage"], 66.7)

    def test_whole_day_timezone_and_chat_filter(self):
        messages = [row("a", timestamp="2026-09-30T16:00:00Z"),
                    row("b", timestamp="2026-10-01T23:59:59.999999+08:00"),
                    row("c", timestamp="2026-10-02T00:00:00+08:00"),
                    row("d", chat_id="c2"), row("e", timestamp="")]
        summary = summarize(messages, filters={"chat_id": "c1", "start": "2026-10-01", "end": "2026-10-01"})
        self.assertEqual(summary["total_messages"], 2)
        self.assertTrue(any("没有时间" in w for w in summary["warnings"]))
        with self.assertRaises(ValueError):
            summarize(messages, filters={"start": "2026-10-02", "end": "2026-10-01"})

    def test_system_notifications_excluded_people(self):
        imported = normalize_import(json.dumps([row("a"), row("b", type=10000)]))
        summary = summarize(imported["messages"])
        self.assertEqual(summary["total_messages"], 2)
        self.assertEqual(summary["total_people"], 1)
        self.assertEqual(summary["people"][0]["total"], 1)
        self.assertEqual(summary["system_messages"], 1)

    def test_small_sample_no_overclaim_and_threshold(self):
        small = summarize([row("a")])["people"][0]
        self.assertEqual([tag["label"] for tag in small["tags"]], ["样本不足"])
        large = summarize([row(str(i), type="sticker", text="") for i in range(20)])["people"][0]
        self.assertEqual(large["tags"][0]["label"], "表情包派")
        self.assertIn("100.0%", large["tags"][0]["reason"])

    def test_empty_summary_and_filtered_emotion(self):
        summary = summarize([])
        self.assertEqual(summary["people"], [])
        self.assertEqual(summary["total_messages"], 0)
        messages = [row("a"), row("b", chat_id="c2")]
        emotion = summarize(messages, [{"message_id": "b", "sentiment": "positive"}], {"chat_id": "c1"})["people"][0]["emotion"]
        self.assertEqual(emotion["analyzed"], 0)

    def test_anonymous_report_has_no_names_ids_or_raw_text(self):
        messages = [row("private_msg_991", sender_id="wx_secret_17", sender_name="隐私名字", chat_name="私密聊天名", text="这句原文绝不能出现在匿名报告")]
        analyses = [{"message_id": "private_msg_991", "sentiment": "positive", "reason": "原文可能带隐私"}]
        report = make_report(messages, analyses)
        for secret in ("隐私名字", "wx_secret_17", "private_msg_991", "私密聊天名", "这句原文绝不能出现在匿名报告", "原文可能带隐私"):
            self.assertNotIn(secret, report)
        self.assertIn("联系人 01", report)
        self.assertIn("未分析 0 条", report)
        self.assertIn("隐私名字", make_report(messages, analyses, anonymize=False))

    def test_markdown_escape_and_demo_provenance(self):
        report = make_report([row(sender_name="<script>alert('a')</script>|x")], anonymize=False)
        self.assertNotIn("<script>", report)
        demo = make_demo()
        self.assertGreaterEqual(len(demo["messages"]), 40)
        self.assertEqual(summarize(demo["messages"], demo["demo_analyses"])["total_people"], 4)
        self.assertTrue(all(a["source"] == "demo_annotation" for a in demo["demo_analyses"]))
        self.assertTrue(any("不是 Jev" in w for w in summarize(demo["messages"], demo["demo_analyses"])["warnings"]))


def fake_messages_module():
    def load_ids(connection):
        return dict(connection.execute("SELECT rowid,user_name FROM Name2Id"))
    def query(connection, table_name, start_ts=None, end_ts=None, limit=None, offset=0):
        sql = f"SELECT local_id,local_type,create_time,real_sender_id,message_content,WCDB_CT_message_content FROM [{table_name}]"
        clauses, params = [], []
        if start_ts is not None:
            clauses.append("create_time >= ?"); params.append(start_ts)
        if end_ts is not None:
            clauses.append("create_time <= ?"); params.append(end_ts)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY create_time DESC LIMIT ? OFFSET ?"
        return connection.execute(sql, params + [limit, offset]).fetchall()
    return SimpleNamespace(_load_name2id_maps=load_ids, _query_messages=query,
                           decompress_content=lambda content, ct: content.decode() if isinstance(content, bytes) else content)


class RawAdapterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="chatprint-fixture-")
        self.root = Path(self.directory.name)
        self.chat_id = "g@chatroom"
        self.table = "Msg_" + hashlib.md5(self.chat_id.encode()).hexdigest()
        self.database = self.root / "cache_messages.db"
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("CREATE TABLE Name2Id(user_name TEXT)")
            conn.executemany("INSERT INTO Name2Id(rowid,user_name) VALUES (?,?)", [(1, "wx_a"), (2, "wx_b"), (3, "wx_me")])
            conn.execute(f"CREATE TABLE [{self.table}](local_id INTEGER,local_type INTEGER,create_time INTEGER,real_sender_id INTEGER,message_content TEXT,WCDB_CT_message_content INTEGER)")
            conn.executemany(f"INSERT INTO [{self.table}] VALUES (?,?,?,?,?,?)", [
                (1, 1, 1790812800, 1, "谢谢", 0), (2, 47, 1790812860, 2, "", 0),
                (3, (6 << 32) | 49, 1790812920, 3, "<msg><appmsg><type>6</type></appmsg></msg>", 0),
                (4, 10000, 1790812980, 0, "系统通知", 0), (5, 1, 1790813040, 99, "wx_c:\n你好", 0),
                (6, 1, 1790813100, 99, "无发送者的文本", 0),
            ])

    def tearDown(self):
        self.directory.cleanup()

    def test_record_conversion_stable_names_group_prefix_system_and_no_writes(self):
        before = self.database.read_bytes()
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as connection:
            records, warnings = records_from_database(connection, self.table, self.chat_id, "组", {"wx_a": "同名", "wx_b": "同名"}, fake_messages_module(), self_id="wx_me")
        imported = normalize_import(json.dumps(records))
        summary = summarize(imported["messages"])
        self.assertEqual(summary["total_people"], 4)
        self.assertEqual(summary["system_messages"], 1)
        self.assertEqual(sum(p["name"] == "同名" for p in summary["people"]), 2)
        self.assertEqual(next(p for p in summary["people"] if p["name"] == "我")["types"]["file"], 1)
        self.assertTrue(any("发送者" in w for w in warnings))
        self.assertEqual(before, self.database.read_bytes())

    def test_full_reader_current_cache_fixture_no_keys_or_init(self):
        originals = self.root / "wx_me_abcd" / "db_storage"
        (originals / "message").mkdir(parents=True)
        encrypted = originals / "message" / "message_0.db"
        encrypted.write_bytes(b"fixture encrypted source never opened")
        config = self.root / "config.json"
        config.write_text(json.dumps({"db_dir": str(originals)}))
        metadata = {"message/message_0.db": {"path": str(self.database), "db_mt": encrypted.stat().st_mtime, "wal_mt": 0}}
        (self.root / "_mtimes.json").write_text(json.dumps(metadata))
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with patch("chatprint.raw_adapter.importlib.metadata.version", return_value="0.2.4"), patch("chatprint.raw_adapter.importlib.import_module", return_value=fake_messages_module()):
            result = load_messages(self.chat_id, config_path=str(config), cache_dir=str(self.root))
        self.assertEqual(result["adapter"], "wechat-cli-0.2.4-readonly-cache")
        self.assertEqual(len(result["messages"]), 5)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_stale_cache_rejected_and_unsafe_table_rejected(self):
        originals = self.root / "db_storage"
        (originals / "message").mkdir(parents=True)
        encrypted = originals / "message" / "message_0.db"
        encrypted.write_bytes(b"fixture")
        config = self.root / "config.json"
        config.write_text(json.dumps({"db_dir": str(originals)}))
        (self.root / "_mtimes.json").write_text(json.dumps({"message/message_0.db": {"path": str(self.database), "db_mt": encrypted.stat().st_mtime - 1, "wal_mt": 0}}))
        with patch("chatprint.raw_adapter.importlib.metadata.version", return_value="0.2.4"), patch("chatprint.raw_adapter.importlib.import_module", return_value=fake_messages_module()):
            with self.assertRaises(AdapterUnavailable):
                load_messages(self.chat_id, config_path=str(config), cache_dir=str(self.root))
        with closing(sqlite3.connect(self.database)) as conn:
            with self.assertRaises(AdapterUnavailable):
                records_from_database(conn, "Msg_x];drop table foo", self.chat_id, "组", {}, fake_messages_module())


if __name__ == "__main__":
    unittest.main()
