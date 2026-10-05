"""Model-boundary tests use synthetic messages and never call the paid API."""
from __future__ import annotations

import copy
import io
import json
import os
import threading
import unittest
import urllib.error
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from chatprint import jev


def message(number=1, sender="sender-a", chat="chat-a", kind="text", text="谢谢你！", timestamp=None):
    return {"id": str(number), "sender_id": sender, "sender_name": "张三" if sender == "sender-a" else "李四",
            "chat_id": chat, "chat_name": "测试聊天", "type": kind, "text": text,
            "timestamp": timestamp if timestamp is not None else f"2026-10-05T10:{number % 60:02d}:00+08:00"}


def prepared_messages(count=1):
    return jev.prepare_messages([message(n + 1) for n in range(count)], limit=count)["messages"]


def choice(options, label, top=.96):
    remainder = (1 - top) / (len(options) - 1)
    return {"type": "choice", "choice": label,
            "probabilities": {name: top if name == label else remainder for name in options},
            "confidence": (len(options) * top - 1) / (len(options) - 1)}


def response_for(prepared, emotion="positive", top=.96):
    answers = {}
    for i in range(len(prepared)):
        answers[f"m{i}_emotion"] = choice(jev.EMOTIONS, emotion, top)
        answers[f"m{i}_intent"] = choice(jev.INTENTS, "support")
        answers[f"m{i}_style"] = choice(jev.STYLES, "polite")
    return {"model": "jev-1.13.0", "answers": answers,
            "usage": {"input_tokens": 100, "output_tokens": 10}}


class JsonResponse(io.BytesIO):
    def __init__(self, data):
        super().__init__(json.dumps(data, ensure_ascii=False).encode("utf-8"))


def http_error(code, headers=None):
    parsed = Message()
    for name, value in (headers or {}).items():
        parsed[name] = value
    return urllib.error.HTTPError(jev.ENDPOINT, code, "upstream included private content", parsed,
                                  io.BytesIO(b"private-message-and-key-must-not-be-reflected"))


class PreparationTests(unittest.TestCase):
    def test_media_contents_are_never_in_model_input(self):
        rows = [message(1)] + [message(i + 2, kind=kind, text="MEDIA_SECRET_" + kind)
                              for i, kind in enumerate(("file", "voice", "sticker", "image", "video", "link", "other"))]
        preview = jev.prepare_messages(rows)
        payload = jev.build_payload(preview["messages"])
        self.assertEqual(preview["selected"], 1)
        self.assertNotIn("MEDIA_SECRET", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(payload["state"]["messages"][0]["preceding_context"], [])

    def test_preview_and_payload_use_identical_redacted_context(self):
        rows = [message(1, text="李四，邮件 me@example.com，手机 13812345678，https://example.org/private"),
                message(2, sender="sender-b", text="张三，微信 wxid_private123，证件 110101200001011234"),
                message(3, text="收到，谢谢！")]
        preview = jev.prepare_messages(rows, limit=3)
        state = jev.build_payload(preview["messages"])["state"]["messages"]
        for local, remote in zip(preview["messages"], state):
            self.assertEqual(local["text"], remote["text"])
            self.assertEqual(local["context"], remote["preceding_context"])
        serialized = json.dumps(state, ensure_ascii=False)
        for private in ("李四", "张三", "13812345678", "me@example.com", "https://example.org", "wxid_private123", "110101200001011234", "sender-a", "chat-a"):
            self.assertNotIn(private, serialized)

    def test_context_is_preceding_and_same_chat(self):
        rows = [message(3, chat="chat-a", text="third-a"), message(2, chat="chat-b", text="second-b"),
                message(1, chat="chat-a", text="first-a"), message(4, chat="chat-a", text="fourth-a")]
        preview = jev.prepare_messages(rows, limit=4)
        by_id = {row["id"]: row for row in preview["messages"]}
        self.assertEqual(by_id["1"]["context"], [])
        self.assertEqual(by_id["2"]["context"], [])
        self.assertEqual([c["text"] for c in by_id["3"]["context"]], ["first-a"])
        self.assertEqual([c["text"] for c in by_id["4"]["context"]], ["first-a", "third-a"])

    def test_sampling_does_not_let_prolific_sender_take_entire_budget(self):
        rows = [message(i + 1) for i in range(20)]
        rows += [message(i + 30, sender="sender-b") for i in range(4)]
        preview = jev.prepare_messages(rows, limit=6)
        self.assertEqual(sorted(Counter(row["speaker"] for row in preview["messages"]).values()), [3, 3])
        self.assertEqual(preview["total"], 24)

    def test_short_sender_does_not_prevent_using_remaining_budget(self):
        rows = [message(i + 1) for i in range(10)] + [message(20, sender="sender-b")]
        preview = jev.prepare_messages(rows, limit=6)
        self.assertEqual(preview["selected"], 6)
        self.assertEqual(sorted(Counter(row["speaker"] for row in preview["messages"]).values()), [1, 5])

    def test_system_messages_are_excluded_from_analysis(self):
        rows = [message(1), {**message(2, text="SYSTEM_PRIVATE_NOTICE"), "is_system": True}]
        preview = jev.prepare_messages(rows)
        self.assertEqual(preview["selected"], 1)
        self.assertNotIn("SYSTEM_PRIVATE_NOTICE", json.dumps(jev.build_payload(preview["messages"])))

    def test_unknown_timestamp_does_not_become_known_preceding_context(self):
        rows = [message(1, text="UNKNOWN_TIME", timestamp=""), message(2, text="KNOWN_TIME")]
        preview = jev.prepare_messages(rows)
        known = next(row for row in preview["messages"] if row["id"] == "2")
        self.assertNotIn("UNKNOWN_TIME", json.dumps(known["context"]))

    def test_previously_analyzed_targets_remain_in_redacted_context(self):
        rows = [message(1, text="李四，手机 13812345678"),
                message(2, sender="sender-b", text="谢谢你提醒")]
        complete = jev.prepare_messages(rows, limit=2)
        skipped = jev.prepare_messages(rows, limit=2, exclude_ids=["1"])
        expected = next(row for row in complete["messages"] if row["id"] == "2")
        self.assertEqual(skipped["total"], 1)
        self.assertEqual(skipped["selected"], 1)
        self.assertEqual(skipped["messages"], [expected])
        self.assertNotIn("13812345678", json.dumps(skipped))
        self.assertIn("[手机号]", json.dumps(skipped, ensure_ascii=False))

    def test_excluding_every_target_does_not_create_analysis_questions(self):
        preview = jev.prepare_messages([message(1), message(2)], exclude_ids=["1", "2"])
        self.assertEqual(preview["selected"], 0)
        self.assertEqual(preview["total"], 0)
        self.assertEqual(jev.build_payload(preview["messages"])["questions"], {})

    def test_invalid_limits_are_rejected(self):
        for limit in (0, -1, 201, True, "30", 2.5, None):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                jev.prepare_messages([message()], limit=limit)

    def test_empty_input_uses_no_api_questions(self):
        self.assertEqual(jev.prepare_messages([])["selected"], 0)
        self.assertEqual(jev.build_payload([])["questions"], {})


class ResponseValidationTests(unittest.TestCase):
    def setUp(self):
        self.prepared = prepared_messages()
        self.good = response_for(self.prepared)

    def test_complete_response_preserves_distribution_and_provenance(self):
        result = jev.decode_response(self.good, self.prepared)[0]
        self.assertEqual(result["emotion"], "positive")
        self.assertEqual(result["message_id"], "1")
        self.assertEqual(result["model"], "jev-1.13.0")
        self.assertFalse(result["needs_review"])
        self.assertAlmostEqual(sum(result["probabilities"].values()), 1)

    def test_low_confidence_becomes_unknown_instead_of_neutral(self):
        result = jev.decode_response(response_for(self.prepared, top=.35), self.prepared)[0]
        self.assertEqual(result["emotion"], "unknown")
        self.assertEqual(result["original_emotion"], "positive")
        self.assertTrue(result["needs_review"])

    def test_explicit_unknown_is_marked_for_review(self):
        result = jev.decode_response(response_for(self.prepared, emotion="unknown"), self.prepared)[0]
        self.assertEqual(result["emotion"], "unknown")
        self.assertTrue(result["needs_review"])

    def test_invalid_probability_and_confidence_numbers_are_rejected(self):
        for value in (True, -0.01, 1.01, float("nan"), float("inf"), "0.9", None):
            for field in ("probabilities", "confidence"):
                data = copy.deepcopy(self.good)
                if field == "probabilities":
                    data["answers"]["m0_emotion"][field]["positive"] = value
                else:
                    data["answers"]["m0_emotion"][field] = value
                with self.subTest(value=value, field=field), self.assertRaises(jev.JevError):
                    jev.decode_response(data, self.prepared)

    def test_missing_extra_and_inconsistent_options_are_rejected(self):
        for kind in ("missing", "extra", "wrong-choice", "list-choice", "dict-choice", "wrong-type", "wrong-sum", "nonwinning-choice", "missing-answer"):
            data = copy.deepcopy(self.good)
            answer = data["answers"]["m0_emotion"]
            if kind == "missing": del answer["probabilities"]["unknown"]
            elif kind == "extra": answer["probabilities"]["invented"] = 0
            elif kind == "wrong-choice": answer["choice"] = "invented"
            elif kind == "list-choice": answer["choice"] = []
            elif kind == "dict-choice": answer["choice"] = {}
            elif kind == "wrong-type": answer["type"] = "noul"
            elif kind == "wrong-sum": answer["probabilities"]["positive"] = .1
            elif kind == "nonwinning-choice": answer["choice"] = "negative"
            else: del data["answers"]["m0_style"]
            with self.subTest(kind=kind), self.assertRaises(jev.JevError):
                jev.decode_response(data, self.prepared)

    def test_confidence_cannot_override_uncertain_distribution(self):
        data = response_for(self.prepared, top=.2)
        data["answers"]["m0_emotion"]["confidence"] = 1.0
        try:
            rows = jev.decode_response(data, self.prepared)
        except jev.JevError:
            return  # Rejecting malformed service output is also acceptable.
        self.assertEqual(rows[0]["emotion"], "unknown")
        self.assertTrue(rows[0]["needs_review"])


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.prepared = prepared_messages()
        self.payload = jev.build_payload(self.prepared)
        self.good = response_for(self.prepared)
        self.key = "synthetic-test-key-never-real"

    def test_invalid_keys_are_rejected(self):
        for key in ("", "   ", None, True, "key\nsecret", "中文密钥"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                jev.JevClient(key)

    def test_authentication_failure_is_not_retried_and_hides_upstream_body(self):
        with mock.patch.object(jev, "_open_request", side_effect=http_error(401)) as opening, mock.patch.object(jev.time, "sleep") as sleeping:
            with self.assertRaises(jev.JevError) as raised:
                jev.JevClient(self.key).evaluate(self.payload)
        self.assertEqual(opening.call_count, 1)
        sleeping.assert_not_called()
        self.assertNotIn(self.key, str(raised.exception))
        self.assertNotIn("private", str(raised.exception))

    def test_rate_limit_is_retried_and_success_is_cached(self):
        with mock.patch.object(jev, "_open_request", side_effect=[http_error(429, {"Retry-After": "2"}), JsonResponse(self.good)]) as opening, mock.patch.object(jev.time, "sleep") as sleeping:
            client = jev.JevClient(self.key)
            result = client.evaluate(self.payload)
            cached = client.evaluate(self.payload)
        self.assertEqual(opening.call_count, 2)
        sleeping.assert_called_once_with(2.0)
        self.assertEqual(result["answers"], cached["answers"])
        self.assertTrue(cached["cached"])

    def test_retry_after_milliseconds_is_honored(self):
        with mock.patch.object(jev, "_open_request", side_effect=[http_error(429, {"retry-after-ms": "2500"}), JsonResponse(self.good)]), mock.patch.object(jev.time, "sleep") as sleeping:
            jev.JevClient(self.key).evaluate(self.payload)
        sleeping.assert_called_once_with(2.5)

    def test_http_date_retry_header_and_large_delays_do_not_retry_early(self):
        retry_date = (datetime.now(timezone.utc) + timedelta(seconds=4)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        with mock.patch.object(jev, "_open_request", side_effect=[http_error(429, {"Retry-After": retry_date}), JsonResponse(self.good)]), mock.patch.object(jev.time, "sleep") as sleeping:
            jev.JevClient(self.key).evaluate(self.payload)
        self.assertEqual(sleeping.call_count, 1)
        self.assertGreater(sleeping.call_args.args[0], 2)
        self.assertLessEqual(sleeping.call_args.args[0], 4.1)
        with mock.patch.object(jev, "_open_request", side_effect=http_error(429, {"Retry-After": "120"})) as opening, mock.patch.object(jev.time, "sleep") as sleeping:
            with self.assertRaises(jev.JevError):
                jev.JevClient(self.key).evaluate(self.payload)
        self.assertEqual(opening.call_count, 1)
        sleeping.assert_not_called()

    def test_key_is_only_in_authorization_and_never_in_results_or_files(self):
        observed = []
        def fake_open(request, **kwargs):
            observed.append(request)
            return JsonResponse(self.good)
        with mock.patch.object(jev, "_open_request", side_effect=fake_open), mock.patch("builtins.open", side_effect=AssertionError("client must not write credentials")):
            result = jev.JevClient(self.key).evaluate(self.payload)
        self.assertEqual(observed[0].get_header("Authorization"), "Bearer " + self.key)
        self.assertNotIn(self.key, observed[0].data.decode())
        self.assertNotIn(self.key, json.dumps(result))

    def test_failed_batches_produce_errors_without_emotion_rows(self):
        prepared = prepared_messages(3)
        fake_client = mock.Mock(model=jev.MODEL)
        fake_client.evaluate.side_effect = jev.JevError("API Key 无效或已失效，请重新配置。")
        result = jev.analyze_prepared(fake_client, prepared, batch_size=1)
        self.assertEqual(result["analyses"], [])
        self.assertEqual(len(result["errors"]), 3)
        self.assertEqual(fake_client.evaluate.call_count, 1)
        self.assertEqual(result["usage"]["input_tokens"], 0)

    def test_invalid_response_does_not_create_neutral_analysis(self):
        fake_client = mock.Mock(model=jev.MODEL)
        fake_client.evaluate.return_value = {"answers": {}}
        result = jev.analyze_prepared(fake_client, self.prepared)
        self.assertEqual(result["analyses"], [])
        self.assertEqual(len(result["errors"]), 1)

    def test_consumed_tokens_remain_visible_when_classification_validation_fails(self):
        fake_client = mock.Mock(model=jev.MODEL)
        fake_client.evaluate.return_value = {"model": "jev-1.13.0", "answers": {},
                                             "usage": {"input_tokens": 123, "output_tokens": 7}}
        result = jev.analyze_prepared(fake_client, self.prepared)
        self.assertEqual(result["analyses"], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["usage"], {"input_tokens": 123, "output_tokens": 7})
        self.assertEqual(result["requests"], 1)

    def test_invalid_classification_is_evicted_then_valid_result_is_cached(self):
        malformed = {"model": "jev-1.13.0", "answers": {},
                     "usage": {"input_tokens": 100, "output_tokens": 10}}
        client = jev.JevClient(self.key)
        with mock.patch.object(jev, "_open_request", side_effect=[JsonResponse(malformed), JsonResponse(self.good)]) as opening:
            first = jev.analyze_prepared(client, self.prepared)
            second = jev.analyze_prepared(client, self.prepared)
            third = jev.analyze_prepared(client, self.prepared)
        self.assertEqual(first["analyses"], [])
        self.assertEqual(len(second["analyses"]), 1)
        self.assertEqual(second["errors"], [])
        self.assertEqual(opening.call_count, 2)
        self.assertEqual(third["cached_requests"], 1)
        self.assertEqual(third["usage"]["input_tokens"], 0)

    def test_cancellation_stops_before_sending_next_batch(self):
        fake_client = mock.Mock(model=jev.MODEL)
        result = jev.analyze_prepared(fake_client, self.prepared, cancelled=lambda: True)
        self.assertTrue(result["cancelled"])
        fake_client.evaluate.assert_not_called()


class RedirectProtectionTests(unittest.TestCase):
    def test_all_redirect_statuses_stop_before_forwarding_key_to_another_origin(self):
        redirected_requests = []
        source_requests = []
        redirect_code = [302]
        class Destination(BaseHTTPRequestHandler):
            def do_GET(self):
                redirected_requests.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"answers": {}}')
            do_POST = do_GET
            def log_message(self, *args):
                pass
        destination = HTTPServer(("127.0.0.1", 0), Destination)
        target_url = f"http://localhost:{destination.server_port}/collect?private-location=hidden"
        class Source(BaseHTTPRequestHandler):
            def do_POST(self):
                source_requests.append(self.headers.get("Authorization"))
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(redirect_code[0])
                self.send_header("Location", target_url)
                self.send_header("Content-Length", "0")
                self.end_headers()
            def log_message(self, *args):
                pass
        source = HTTPServer(("127.0.0.1", 0), Source)
        workers = [threading.Thread(target=httpd.serve_forever, daemon=True) for httpd in (source, destination)]
        for worker in workers:
            worker.start()
        key = "synthetic-redirect-test-key"
        try:
            with mock.patch.object(jev, "ENDPOINT", f"http://127.0.0.1:{source.server_port}/v1/systemone"), mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}):
                for code in (301, 302, 303, 307, 308):
                    redirect_code[0] = code
                    with self.subTest(code=code), self.assertRaises(jev.JevError) as raised:
                        jev.JevClient(key).evaluate({"model": jev.MODEL, "state": "synthetic", "questions": {}})
                    self.assertIn(str(code), str(raised.exception))
                    self.assertNotIn(key, str(raised.exception))
                    self.assertNotIn("private-location", str(raised.exception))
            self.assertEqual(redirected_requests, [])
            self.assertEqual(source_requests, ["Bearer " + key] * 5)
        finally:
            for httpd in (source, destination):
                httpd.shutdown()
                httpd.server_close()
            for worker in workers:
                worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
