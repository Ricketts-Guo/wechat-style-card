"""Real loopback HTTP boundary tests; all content and credentials are synthetic."""
from __future__ import annotations

import http.client
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from chatprint import server


def message(number=1, kind="text", text="谢谢你！"):
    return {"id": str(number), "chat_id": "demo-chat", "chat_name": "演示聊天",
            "sender_id": "demo-user", "sender_name": "测试者", "timestamp": "2026-10-05T10:00:00+08:00",
            "type": kind, "text": text}


class HttpBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            cls.state = server.AppState()
        cls.httpd = server.LocalServer(("127.0.0.1", 0), state=cls.state)
        cls.port = cls.httpd.server_port
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        with self.state.lock:
            self.state.client = None
            self.state.jobs.clear()
            self.state.previews.clear()

    def request(self, path, method="GET", body=None, headers=None, raw=None):
        payload = raw if raw is not None else json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        fields = dict(headers or {})
        if payload is not None and "Content-Type" not in fields:
            fields["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            connection.request(method, path, body=payload, headers=fields)
            response = connection.getresponse()
            data = response.read()
            parsed = json.loads(data) if "application/json" in response.getheader("Content-Type", "") else data.decode("utf-8")
            return response.status, dict(response.getheaders()), parsed
        finally:
            connection.close()

    def test_settings_only_reveal_configuration_status(self):
        status, headers, body = self.request("/api/settings")
        self.assertEqual(status, 200)
        self.assertFalse(body["configured"])
        self.assertNotIn("api_key", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_credentials_are_never_echoed_logged_or_written(self):
        key = "synthetic-local-key-for-test"
        with mock.patch("builtins.print") as printing, mock.patch.object(Path, "write_text", side_effect=AssertionError("no credential file")), mock.patch.object(Path, "write_bytes", side_effect=AssertionError("no credential file")):
            status, _, configured = self.request("/api/settings", "POST", {"api_key": key})
            get_status, _, settings = self.request("/api/settings")
        self.assertEqual((status, get_status), (200, 200))
        self.assertTrue(configured["configured"])
        self.assertNotIn(key, json.dumps(configured))
        self.assertNotIn(key, json.dumps(settings))
        printing.assert_not_called()
        self.assertEqual(self.state.client._api_key, key)

    def test_clear_key_is_supported(self):
        self.state.configure("synthetic-local-key")
        status, _, body = self.request("/api/settings", "POST", {"api_key": ""})
        self.assertEqual(status, 200)
        self.assertFalse(body["configured"])

    def test_invalid_key_types_and_values_are_rejected(self):
        for key in (None, True, {}, "x" * 4097, "secret\nheader-injection", "含中文"):
            with self.subTest(key=type(key).__name__):
                status, _, body = self.request("/api/settings", "POST", {"api_key": key})
                self.assertEqual(status, 400)
                self.assertIn("error", body)
                self.assertIsNone(self.state.client)

    def test_configuration_is_blocked_while_analysis_is_running(self):
        self.state.jobs["in-flight"] = {"status": "running"}
        status, _, body = self.request("/api/settings", "POST", {"api_key": "synthetic-new-key"})
        self.assertEqual(status, 400)
        self.assertIsNone(self.state.client)

    def test_analyze_requires_user_owned_key(self):
        status, _, body = self.request("/api/analyze", "POST", {"messages": [message()]})
        self.assertEqual(status, 400)
        self.assertIn("API Key", body["error"])
        self.assertEqual(self.state.jobs, {})

    def test_analyze_empty_sample_is_rejected_before_api_call(self):
        self.state.configure("synthetic-local-key")
        with mock.patch.object(self.state.client, "evaluate") as evaluate:
            status, _, body = self.request("/api/analyze", "POST", {"messages": []})
        self.assertEqual(status, 400)
        evaluate.assert_not_called()
        self.assertEqual(self.state.jobs, {})

    def test_preview_requires_no_key_and_omits_nontext_media(self):
        status, _, preview = self.request("/api/preview", "POST", {
            "messages": [message(), message(2, "sticker", "sticker-secret"), message(3, "voice", "voice-secret"), message(4, "file", "file-secret")]})
        self.assertEqual(status, 200)
        self.assertEqual(preview["selected"], 1)
        self.assertNotIn("secret", json.dumps(preview))
        self.assertIsNone(self.state.client)

    def test_preview_requires_anonymization(self):
        for value in (False, "true", 1, None):
            with self.subTest(value=value):
                status, _, body = self.request("/api/preview", "POST", {"messages": [message()], "anonymize": value})
                self.assertEqual(status, 400)
                self.assertIn("error", body)

    def test_selected_snapshot_preserves_exact_preview_context_and_speaker_alias(self):
        rows = [message(1, text="原始前文 https://example.org/private"),
                {**message(2, text="张三，收到！"), "sender_id": "second-user", "sender_name": "李四",
                 "timestamp": "2026-10-05T10:01:00+08:00"}]
        status, _, preview = self.request("/api/preview", "POST", {"messages": rows, "limit": 2})
        self.assertEqual(status, 200)
        expected = next(row for row in preview["messages"] if row["id"] == "2")
        self.assertTrue(expected["context"])
        with mock.patch.object(self.state, "start", return_value="synthetic-job") as start:
            status, _, body = self.request("/api/analyze", "POST", {
                "preview_id": preview["preview_id"], "selected_ids": ["2"],
                "messages": [message(2, text="TAMPERED_TEXT")], "limit": 1})
        self.assertEqual(status, 202)
        self.assertEqual(body["selected"], 1)
        start.assert_called_once_with([expected])
        self.assertNotIn("TAMPERED_TEXT", json.dumps(start.call_args.args))

    def test_snapshot_rejects_unknown_expired_or_invalid_selection(self):
        _, _, preview = self.request("/api/preview", "POST", {"messages": [message()]})
        cases = [{"preview_id": "missing", "selected_ids": ["1"]},
                 {"preview_id": preview["preview_id"], "selected_ids": ["not-in-preview"]},
                 {"preview_id": preview["preview_id"], "selected_ids": []},
                 {"preview_id": preview["preview_id"], "selected_ids": "1"},
                 {"preview_id": preview["preview_id"], "selected_ids": [1]},
                 {"preview_id": preview["preview_id"], "selected_ids": ["1"] * 201}]
        with mock.patch.object(self.state, "start") as start:
            for body in cases:
                with self.subTest(body=body):
                    self.assertEqual(self.request("/api/analyze", "POST", body)[0], 400)
            with self.state.lock:
                self.state.previews[preview["preview_id"]]["created"] -= 601
            self.assertEqual(self.request("/api/analyze", "POST", {
                "preview_id": preview["preview_id"], "selected_ids": ["1"]})[0], 400)
            start.assert_not_called()

    def test_preview_cache_has_bounded_size_and_no_raw_credentials(self):
        self.state.configure("synthetic-secret-not-in-preview")
        for _ in range(14):
            status, _, preview = self.request("/api/preview", "POST", {"messages": [message()]})
            self.assertEqual(status, 200)
        self.assertLessEqual(len(self.state.previews), 12)
        self.assertNotIn("synthetic-secret-not-in-preview", json.dumps(self.state.previews))

    def test_same_approved_selection_reuses_existing_paid_job(self):
        _, _, preview = self.request("/api/preview", "POST", {"messages": [message(1), message(2)]})
        def fake_start(items):
            self.state.jobs["synthetic-idempotent-job"] = {"status": "running"}
            return "synthetic-idempotent-job"
        with mock.patch.object(self.state, "start", side_effect=fake_start) as start:
            first = self.request("/api/analyze", "POST", {"preview_id": preview["preview_id"], "selected_ids": ["1", "2"]})
            repeated = self.request("/api/analyze", "POST", {"preview_id": preview["preview_id"], "selected_ids": ["2", "1", "2"]})
        self.assertEqual((first[0], repeated[0]), (202, 202))
        self.assertEqual(first[2]["job_id"], repeated[2]["job_id"])
        self.assertEqual(repeated[2]["selected"], 2)
        self.assertEqual(start.call_count, 1)

    def test_different_approved_selection_starts_a_distinct_job(self):
        _, _, preview = self.request("/api/preview", "POST", {"messages": [message(1), message(2)]})
        def fake_start(items):
            job_id = "synthetic-job-" + items[0]["id"]
            self.state.jobs[job_id] = {"status": "completed"}
            return job_id
        with mock.patch.object(self.state, "start", side_effect=fake_start) as start:
            first = self.request("/api/analyze", "POST", {"preview_id": preview["preview_id"], "selected_ids": ["1"]})
            second = self.request("/api/analyze", "POST", {"preview_id": preview["preview_id"], "selected_ids": ["2"]})
        self.assertEqual((first[0], second[0]), (202, 202))
        self.assertNotEqual(first[2]["job_id"], second[2]["job_id"])
        self.assertEqual(start.call_count, 2)

    def test_http_preview_excluded_targets_remain_in_context(self):
        rows = [message(1, text="前文 https://example.org/private"),
                {**message(2, text="收到"), "timestamp": "2026-10-05T10:01:00+08:00"}]
        status, _, preview = self.request("/api/preview", "POST", {"messages": rows, "exclude_ids": ["1"]})
        self.assertEqual(status, 200)
        self.assertEqual(preview["total"], 1)
        self.assertEqual([m["id"] for m in preview["messages"]], ["2"])
        self.assertEqual(preview["messages"][0]["context"][0]["text"], "前文 [链接]")

    def test_invalid_exclusion_lists_are_rejected(self):
        for ids in ("1", {}, [1], [None]):
            with self.subTest(ids=ids):
                self.assertEqual(self.request("/api/preview", "POST", {"messages": [message()], "exclude_ids": ids})[0], 400)

    def test_history_endpoint_preserves_adapter_metadata_and_warnings(self):
        imported = {"messages": [message()], "warnings": ["SYNTHETIC_CACHE_WARNING"], "adapter": "synthetic-readonly-cache"}
        with mock.patch.object(server.wechat, "history", return_value=imported):
            status, _, body = self.request("/api/wechat/history", "POST", {"chat": "synthetic"})
        self.assertEqual(status, 200)
        self.assertEqual(body, imported)

    def test_adapter_only_sessions_return_manual_mode_without_executing_cli(self):
        capability = {"available": True, "command": None, "python_adapter_available": True, "sessions_available": False, "warnings": []}
        with mock.patch.object(server.wechat, "cli_status", return_value=capability), mock.patch.object(server.wechat, "_read") as reading:
            status, _, body = self.request("/api/wechat/sessions")
        self.assertEqual(status, 200)
        self.assertTrue(body["manual_only"])
        self.assertEqual(body["sessions"], [])
        self.assertTrue(body["warnings"])
        self.assertTrue(any("聊天 ID" in warning for warning in body["warnings"]))
        reading.assert_not_called()

    def test_loopback_hostnames_allowed_external_or_rebound_hosts_rejected(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            with self.subTest(host=host):
                self.assertEqual(self.request("/api/settings", headers={"Host": host})[0], 200)
        for host in ("attacker.example", f"127.0.0.1:{self.port}.evil.example", f"0.0.0.0:{self.port}", "localhost", "localhost:1"):
            with self.subTest(host=host):
                self.assertEqual(self.request("/api/settings", headers={"Host": host})[0], 403)

    def test_external_and_null_origins_rejected_before_mutation(self):
        for origin in ("https://attacker.example", "null", f"http://localhost:{self.port}.evil.example", f"https://localhost:{self.port}"):
            with self.subTest(origin=origin):
                status, _, body = self.request("/api/settings", "POST", {"api_key": "synthetic-key"}, headers={"Origin": origin})
                self.assertEqual(status, 403)
                self.assertIsNone(self.state.client)
        self.assertEqual(self.request("/api/settings", headers={"Origin": f"http://127.0.0.1:{self.port}"})[0], 200)

    def test_static_directory_traversal_and_nonasset_files_are_not_served(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / "web").mkdir()
            (directory / "private.js").write_text("PRIVATE_OUTSIDE_WEB", encoding="utf-8")
            (directory / "web" / "index.html").write_text("safe-index", encoding="utf-8")
            (directory / "web" / "private.py").write_text("PRIVATE_SOURCE", encoding="utf-8")
            with mock.patch.object(server, "WEB", directory / "web"):
                self.assertEqual(self.request("/")[2], "safe-index")
                for path in ("/../private.js", "/%2e%2e/private.js", "/%2e%2e%2fprivate.js", "/private.py", "/%2f..%2fprivate.js"):
                    with self.subTest(path=path):
                        status, _, body = self.request(path)
                        self.assertEqual(status, 404)
                        self.assertNotIn("PRIVATE", json.dumps(body))

    def test_static_symlink_outside_web_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / "web").mkdir()
            outside = directory / "private.js"
            outside.write_text("PRIVATE_SYMLINK_TARGET", encoding="utf-8")
            try:
                (directory / "web" / "escape.js").symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("Creating symlinks requires platform permission")
            with mock.patch.object(server, "WEB", directory / "web"):
                self.assertEqual(self.request("/escape.js")[0], 404)

    def test_unknown_api_and_jobs_return_404(self):
        for path in ("/api/unknown", "/api/jobs/nonexistent"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request("/api/jobs/nonexistent/cancel", "POST", {})[0], 404)

    def test_invalid_body_shape_and_json_are_rejected(self):
        for raw in (b"not-json", b"[]", b"null", b'"text"', b"{broken"):
            with self.subTest(raw=raw):
                self.assertEqual(self.request("/api/preview", "POST", raw=raw)[0], 400)

    def test_empty_oversized_and_wrong_content_type_bodies_are_rejected(self):
        self.assertEqual(self.request("/api/preview", "POST", raw=b"")[0], 413)
        self.assertEqual(self.request("/api/preview", "POST", raw=b"{}", headers={"Content-Length": str(server.MAX_BODY + 1)})[0], 413)
        self.assertEqual(self.request("/api/preview", "POST", raw=b"{}", headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("/api/preview", "POST", raw=b"{}", headers={"Content-Length": "invalid"})[0], 400)

    def test_invalid_message_arrays_and_sample_limits_are_rejected(self):
        for messages in (None, {}, "rows", [True], ["text"], [None]):
            with self.subTest(messages=messages):
                self.assertEqual(self.request("/api/preview", "POST", {"messages": messages})[0], 400)
        for limit in (0, 201, True, "30", None):
            with self.subTest(limit=limit):
                self.assertEqual(self.request("/api/preview", "POST", {"messages": [message()], "limit": limit})[0], 400)
        with mock.patch.object(server, "MAX_MESSAGES", 1):
            self.assertEqual(self.request("/api/preview", "POST", {"messages": [message(), message(2)]})[0], 400)

    def test_invalid_analyses_are_rejected(self):
        for rows in (None, {}, [None], ["row"]):
            with self.subTest(rows=rows):
                self.assertEqual(self.request("/api/summary", "POST", {"messages": [message()], "analyses": rows})[0], 400)

    def test_report_export_never_contains_configured_api_key(self):
        key = "synthetic-configured-key-to-hide"
        self.state.configure(key)
        status, _, body = self.request("/api/report", "POST", {"messages": [message()], "analyses": []})
        self.assertEqual(status, 200)
        self.assertIn("markdown", body)
        self.assertNotIn(key, body["markdown"])


class JobTests(unittest.TestCase):
    def test_failed_model_results_are_not_synthesized_as_neutral(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            state = server.AppState()
        state.configure("synthetic-key-for-job-test")
        prepared = server.prepare_messages([message()])["messages"]
        finished = threading.Event()
        failure = {"analyses": [], "errors": [{"message_id": "1", "error": "service unavailable"}],
                   "usage": {"input_tokens": 0, "output_tokens": 0}, "models": [], "requests": 0, "cached_requests": 0}
        def fake_analyze(client, items, progress=None, cancelled=None):
            try:
                self.assertEqual(items, prepared)
                return failure
            finally:
                finished.set()
        with mock.patch.object(server, "analyze_prepared", side_effect=fake_analyze):
            job_id = state.start(prepared)
            self.assertTrue(finished.wait(2), "analysis worker did not complete")
            # The event precedes update by a few instructions; acquire/release the
            # state lock until the worker's terminal state is observable.
            for _ in range(100):
                with state.lock:
                    job = dict(state.jobs[job_id])
                if job["status"] != "running":
                    break
                threading.Event().wait(.005)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["analyses"], [])
        self.assertEqual(job["completed"], 1)
        self.assertNotIn("synthetic-key", json.dumps(job))

    def test_unexpected_worker_error_marks_every_remaining_message_failed(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            state = server.AppState()
        state.configure("synthetic-key-for-job-test")
        prepared = server.prepare_messages([message(1), message(2), message(3)])["messages"]
        finished = threading.Event()
        def fail_after_partial_progress(client, items, progress=None, cancelled=None):
            progress({"analyses": [{"message_id": "1", "emotion": "positive"}], "errors": [],
                      "usage": {"input_tokens": 100, "output_tokens": 10}, "models": ["synthetic-model"], "requests": 1, "cached_requests": 0})
            finished.set()
            raise RuntimeError("PRIVATE_UPSTREAM_MESSAGE_AND_KEY")
        with mock.patch.object(server, "analyze_prepared", side_effect=fail_after_partial_progress):
            job_id = state.start(prepared)
            self.assertTrue(finished.wait(2))
            for _ in range(100):
                with state.lock:
                    job = dict(state.jobs[job_id])
                if job["status"] != "running":
                    break
                threading.Event().wait(.005)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["completed"], 3)
        self.assertEqual({row["message_id"] for row in job["errors"]}, {"2", "3"})
        self.assertEqual(job["usage"]["input_tokens"], 100)
        self.assertNotIn("PRIVATE_UPSTREAM", json.dumps(job))
        self.assertIn("elapsed_seconds", job)


class CommandBoundaryTests(unittest.TestCase):
    def test_doctor_json_can_be_captured_by_non_utf8_windows_stdout(self):
        from chatprint.__main__ import main
        output_bytes = io.BytesIO()
        captured_stdout = io.TextIOWrapper(output_bytes, encoding="cp1252", errors="strict", write_through=True)
        capability = {"available": False, "command": None, "python_adapter_available": False,
                      "sessions_available": False, "warnings": ["未检测到微信工具，可以导入虚构示例。"]}
        try:
            with mock.patch.object(sys, "argv", ["chatprint", "doctor"]), mock.patch.object(sys, "stdout", captured_stdout), mock.patch.object(sys, "stderr", io.StringIO()), mock.patch.object(server.wechat, "cli_status", return_value=capability), mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
                exit_code = main()
            captured_stdout.flush()
            payload = output_bytes.getvalue().decode("utf-8")
        finally:
            captured_stdout.detach()
        self.assertEqual(exit_code, 0)
        parsed = json.loads(payload)
        self.assertEqual(parsed["wechat"]["warnings"], capability["warnings"])
        self.assertFalse(parsed["jev_key_configured"])

    def test_adapter_only_capability_distinguishes_session_listing(self):
        with mock.patch.object(server.wechat.shutil, "which", return_value=None), mock.patch.object(server.wechat.importlib.metadata, "version", return_value="0.2.4"), mock.patch.object(server.wechat, "_read") as reading:
            capability = server.wechat.cli_status()
        self.assertTrue(capability["available"])
        self.assertTrue(capability["python_adapter_available"])
        self.assertFalse(capability["sessions_available"])
        self.assertIsNone(capability["command"])
        reading.assert_not_called()

    def test_wechat_chat_names_cannot_become_cli_options_after_stripping(self):
        with mock.patch.object(server.wechat, "_read") as reading:
            for name in ("--version", " --version", "\t--version", "-h", "  -h  ", "", None, "invalid\nname"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    server.wechat.history(name)
            reading.assert_not_called()

    def test_wechat_history_uses_argument_list_and_preserves_chat_name(self):
        with mock.patch.object(server.wechat, "load_messages", side_effect=server.wechat.AdapterUnavailable("synthetic cache unavailable")), mock.patch.object(server.wechat, "_read", return_value={"messages": []}) as reading:
            server.wechat.history("  演示聊天;$(do-not-run)  ", 25)
        reading.assert_called_once_with(["history", "演示聊天;$(do-not-run)", "--limit", "25"])

    def test_wechat_prefers_structured_adapter_without_invoking_cli(self):
        imported = {"messages": [message()], "warnings": ["synthetic warning"], "adapter": "synthetic-adapter"}
        with mock.patch.object(server.wechat, "load_messages", return_value=imported) as adapter, mock.patch.object(server.wechat, "_read") as reading:
            result = server.wechat.history("  演示聊天  ", 30)
        self.assertEqual(result, imported)
        adapter.assert_called_once_with("演示聊天", limit=30)
        reading.assert_not_called()

    def test_wechat_adapter_failure_falls_back_to_normalized_cli_with_warnings(self):
        raw = {"messages": [message(1), message(2, "sticker")], "warnings": []}
        with mock.patch.object(server.wechat, "load_messages", side_effect=server.wechat.AdapterUnavailable("SYNTHETIC_ADAPTER_FAILURE")), mock.patch.object(server.wechat, "_read", return_value=raw) as reading:
            result = server.wechat.history("演示聊天", 30)
        self.assertEqual(len(result["messages"]), 2)
        self.assertIn("SYNTHETIC_ADAPTER_FAILURE", result["warnings"])
        self.assertTrue(any("格式化" in warning for warning in result["warnings"]))
        reading.assert_called_once_with(["history", "演示聊天", "--limit", "30"])

    def test_cli_report_reads_analysis_as_utf8_even_with_windows_default_encoding(self):
        from chatprint.__main__ import main
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            input_file, analysis_file, report_file = folder / "input.json", folder / "analyses.json", folder / "report.md"
            rows = [{**message(), "id": "中文消息"}]
            input_file.write_text(json.dumps({"messages": rows}, ensure_ascii=False), encoding="utf-8")
            analysis_file.write_text(json.dumps({"analyses": [{"message_id": "中文消息", "emotion": "positive", "source": "manual"}]}, ensure_ascii=False), encoding="utf-8")
            def read_with_windows_default(path, encoding=None, errors=None, **kwargs):
                return path.read_bytes().decode(encoding or "cp1252", errors or "strict")
            arguments = ["chatprint", "report", str(input_file), "--analyses", str(analysis_file), "--output", str(report_file)]
            with mock.patch.object(sys, "argv", arguments), mock.patch.object(Path, "read_text", autospec=True, side_effect=read_with_windows_default), mock.patch("sys.stderr", new=io.StringIO()):
                exit_code = main()
            self.assertEqual(exit_code, 0)
            self.assertIn("情绪分析覆盖：1/1", report_file.read_text(encoding="utf-8"))


class EvaluationToolTests(unittest.TestCase):
    def test_unconfigured_evaluation_message_survives_windows_pipe_encoding(self):
        script = Path(__file__).resolve().parents[1] / "tools" / "evaluate.py"
        spec = importlib.util.spec_from_file_location("chatprint_synthetic_eval_encoding_test", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        output_bytes = io.BytesIO()
        captured_stdout = io.TextIOWrapper(output_bytes, encoding="cp1252", errors="strict", write_through=True)
        opener = mock.Mock()
        opener.open.return_value = io.BytesIO(b'{"configured": false}')
        try:
            with tempfile.TemporaryDirectory() as folder:
                output = Path(folder) / "not-created.json"
                args = ["evaluate.py", "--output", str(output)]
                with mock.patch.object(sys, "argv", args), mock.patch.object(sys, "stdout", captured_stdout), mock.patch.object(sys, "stderr", io.StringIO()), mock.patch.object(module.urllib.request, "build_opener", return_value=opener):
                    exit_code = module.main()
                captured_stdout.flush()
                text = output_bytes.getvalue().decode("utf-8")
                self.assertEqual(exit_code, 2)
                self.assertIn("尚未配置 Jev Key", text)
                self.assertFalse(output.exists())
                self.assertEqual(opener.open.call_count, 1)
        finally:
            captured_stdout.detach()

    def test_evaluation_redirect_stops_before_leaving_loopback(self):
        script = Path(__file__).resolve().parents[1] / "tools" / "evaluate.py"
        spec = importlib.util.spec_from_file_location("chatprint_synthetic_eval_test", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        observed = []
        class RedirectingLocalService(BaseHTTPRequestHandler):
            def do_GET(self):
                observed.append(self.path)
                self.send_response(302)
                self.send_header("Location", "https://synthetic.invalid/private-location")
                self.send_header("Content-Length", "0")
                self.end_headers()
            def log_message(self, *args):
                pass
        local = HTTPServer(("127.0.0.1", 0), RedirectingLocalService)
        worker = threading.Thread(target=local.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                output = Path(folder) / "not-created.json"
                args = ["evaluate.py", "--url", f"http://127.0.0.1:{local.server_port}", "--output", str(output)]
                with mock.patch.object(sys, "argv", args), mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}), mock.patch.object(urllib.request.HTTPSHandler, "https_open", side_effect=AssertionError("must not leave loopback")) as external:
                    with self.assertRaises(RuntimeError) as raised:
                        module.main()
                self.assertIn("重定向", str(raised.exception))
                self.assertNotIn("private-location", str(raised.exception))
                external.assert_not_called()
                self.assertFalse(output.exists())
                self.assertEqual(observed, ["/api/settings"])
        finally:
            local.shutdown()
            local.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
