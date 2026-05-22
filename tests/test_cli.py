from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import textwrap
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from opsai.cli import (
    INTENT_SYSTEM_PROMPT,
    build_file_diff_from_content,
    build_runtime_context,
    colorize_diff_text,
    default_config_candidates,
    format_processing_status,
    load_config,
    main,
    render_diff_text,
    resolve_config_path,
)


class TestServer(HTTPServer):
    allow_reuse_address = True

    def __init__(self, server_address, handler_class, responses, requests_seen):
        super().__init__(server_address, handler_class)
        self.responses = responses
        self.requests_seen = requests_seen


class TestHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        payload = json.loads(body.decode("utf-8"))
        self.server.requests_seen.append(payload)
        response = self.server.responses.pop(0)

        self.send_response(response["status"])
        for key, value in response.get("headers", {}).items():
            self.send_header(key, value)
        self.end_headers()

        for chunk in response.get("body_chunks", []):
            self.wfile.write(chunk)
            self.wfile.flush()

    def log_message(self, format, *args):
        return


class CliTestCase(unittest.TestCase):
    def intent_response(self, decision: str, reason: str) -> dict[str, object]:
        return {
            "status": 200,
            "headers": {"Content-Type": "application/json"},
            "body_chunks": [
                json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {"decision": decision, "reason": reason},
                                        ensure_ascii=False,
                                    )
                                }
                            }
                        ]
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            ],
        }

    def file_response(self, content: str) -> dict[str, object]:
        return {
            "status": 200,
            "headers": {"Content-Type": "application/json"},
            "body_chunks": [
                json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": content,
                                }
                            }
                        ]
                    }
                ).encode("utf-8")
            ],
        }

    def make_config(self, directory: Path, port: int) -> Path:
        config = directory / "config.toml"
        config.write_text(
            textwrap.dedent(
                f"""
                [llm]
                base_url = "http://127.0.0.1:{port}"
                api_key = "test-key"
                model = "test-model"
                timeout_seconds = 5

                [memory]
                history_rounds = 3
                history_file = ".opsai_history.json"
                """
            ),
            encoding="utf-8",
        )
        return config

    def run_cli(
        self,
        config: Path,
        *args: str,
        stdin_text: str = "",
        stdin_isatty: bool = True,
        confirm_text: str = "",
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        old_argv = sys.argv[:]
        old_stdin = sys.stdin

        class FakeStdin(io.StringIO):
            def __init__(self_inner, text: str, confirmation: str):
                super().__init__(text)
                self_inner._confirmation = io.StringIO(confirmation)

            def isatty(self_inner):
                return stdin_isatty

            def read_confirmation_line(self_inner):
                return self_inner._confirmation.readline()

        sys.argv = ["opsai", "--config", str(config), *args]
        try:
            sys.stdin = FakeStdin(stdin_text, confirm_text)
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main()
        finally:
            sys.argv = old_argv
            sys.stdin = old_stdin
        return code, stdout.getvalue(), stderr.getvalue()

    def serve(self, responses: list[dict[str, object]]):
        requests_seen: list[dict[str, object]] = []
        server = TestServer(("127.0.0.1", 0), TestHandler, responses, requests_seen)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, requests_seen

    def shutdown(self, server: HTTPServer, thread: threading.Thread) -> None:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    def test_runtime_context_excludes_sensitive_fields(self):
        context = build_runtime_context()
        self.assertIn("可用常用命令", context)
        self.assertNotIn("主机名", context)
        self.assertNotIn("当前用户", context)
        self.assertNotIn("环境变量", context)
        self.assertNotIn("当前工作目录", context)

    def test_colorize_diff_text_with_force_color(self):
        old_force_color = os.environ.get("FORCE_COLOR")
        try:
            os.environ["FORCE_COLOR"] = "1"
            rendered = render_diff_text(
                "--- a/demo.conf\n+++ b/demo.conf\n@@ -1 +1 @@\n-old\n+new"
            )
            colored = colorize_diff_text(rendered)
        finally:
            if old_force_color is None:
                os.environ.pop("FORCE_COLOR", None)
            else:
                os.environ["FORCE_COLOR"] = old_force_color

        self.assertIn("\x1b[2m        --- a/demo.conf\x1b[0m", colored)
        self.assertIn("\x1b[36m        @@ -1 +1 @@\x1b[0m", colored)
        self.assertIn("\x1b[48;5;224m\x1b[90m    1\x1b[30m \x1b[31m-\x1b[30m old", colored)
        self.assertIn("\x1b[48;5;194m\x1b[90m    1\x1b[30m \x1b[32m+\x1b[30m new", colored)

    def test_render_diff_text_adds_line_numbers_without_blank_lines(self):
        rendered = render_diff_text(
            "--- /dev/null\n+++ b/demo.conf\n@@ -0,0 +1,2 @@\n+line1\n+line2"
        )

        self.assertEqual(
            rendered,
            "        --- /dev/null\n"
            "        +++ b/demo.conf\n"
            "        @@ -0,0 +1,2 @@\n"
            "    1 + line1\n"
            "    2 + line2",
        )

    def test_format_processing_status(self):
        self.assertEqual(format_processing_status(0), "\r处理中(0秒)")
        self.assertEqual(format_processing_status(12), "\r处理中(12秒)")

    def test_intent_prompt_allows_scripts_and_code_files(self):
        self.assertIn("编写 bash/python 脚本", INTENT_SYSTEM_PROMPT)
        self.assertIn("修改 .py/.sh/.js/.go 等代码文件", INTENT_SYSTEM_PROMPT)

    def test_load_config_allows_missing_ca_file_when_verify_ssl_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            config.write_text(
                textwrap.dedent(
                    """
                    [llm]
                    base_url = "https://example.com/v1"
                    api_key = "test-key"
                    model = "test-model"
                    verify_ssl = false
                    ca_file = "missing-ca.pem"
                    """
                ),
                encoding="utf-8",
            )

            loaded = load_config(config)

        self.assertFalse(loaded.verify_ssl)
        self.assertEqual(loaded.ca_file, (root / "missing-ca.pem").resolve())

    def test_clear_context_removes_history_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / ".opsai_history.json"
            history.write_text("[]", encoding="utf-8")
            config = root / "config.toml"
            config.write_text(
                textwrap.dedent(
                    """
                    [llm]
                    base_url = "http://127.0.0.1:1"
                    api_key = "test-key"
                    model = "test-model"

                    [memory]
                    history_file = ".opsai_history.json"
                    """
                ),
                encoding="utf-8",
            )

            code, stdout, stderr = self.run_cli(config, "/clear")
            history_exists = history.exists()

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "上下文已清除。\n")
        self.assertEqual(stderr, "")
        self.assertFalse(history_exists)

    def test_output_argument_is_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            config.write_text(
                textwrap.dedent(
                    """
                    [llm]
                    base_url = "http://127.0.0.1:1"
                    api_key = "test-key"
                    model = "test-model"
                    """
                ),
                encoding="utf-8",
            )
            code, stdout, stderr = self.run_cli(config, "生成 nginx 配置")

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("--output/-o 为必填项", stderr)

    def test_default_config_prefers_script_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script_dir = root / "runner"
            script_dir.mkdir()
            script_config = script_dir / "config.toml"
            script_config.write_text("", encoding="utf-8")
            cwd_dir = root / "cwd"
            cwd_dir.mkdir()
            cwd_config = cwd_dir / "config.toml"
            cwd_config.write_text("", encoding="utf-8")

            old_argv = sys.argv[:]
            old_cwd = Path.cwd()
            try:
                sys.argv = [str(script_dir / "opsai.py")]
                os.chdir(cwd_dir)
                resolved = resolve_config_path(None)
                candidates = default_config_candidates()
            finally:
                os.chdir(old_cwd)
                sys.argv = old_argv

        self.assertEqual(resolved, script_config.resolve())
        self.assertEqual(candidates[0], script_config.resolve())

    def test_file_input_is_injected_and_history_only_keeps_summary(self):
        responses = [
            self.intent_response("allow", "配置文件优化请求"),
            self.file_response(
                "已生成优化版\n<opsai_write_file name=\"nginx.optimized.conf\">\nworker_processes auto;\n</opsai_write_file>"
            ),
        ]
        server, thread, requests_seen = self.serve(responses)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = root / "config.toml"
                config.write_text(
                    textwrap.dedent(
                        f"""
                        [llm]
                        base_url = "http://127.0.0.1:{server.server_port}"
                        api_key = "test-key"
                        model = "test-model"
                        timeout_seconds = 5

                        [memory]
                        history_rounds = 3
                        history_file = ".opsai_history.json"

                        [input]
                        max_input_chars = 40
                        """
                    ),
                    encoding="utf-8",
                )
                sample = root / "nginx.conf"
                sample.write_text("A" * 80, encoding="utf-8")
                existing = root / "nginx.optimized.conf"
                existing.write_text("disk baseline\n", encoding="utf-8")
                old_cwd = Path.cwd()
                try:
                    os.chdir(root)
                    code, stdout, stderr = self.run_cli(
                        config,
                        "-f",
                        str(sample),
                        "-o",
                        "nginx.optimized.conf",
                        "优化附带 nginx.conf 并保存为 nginx.optimized.conf",
                        confirm_text="y\n",
                    )
                    history = json.loads((root / ".opsai_history.json").read_text(encoding="utf-8"))
                    saved = (root / "nginx.optimized.conf").read_text(encoding="utf-8")
                finally:
                    os.chdir(old_cwd)
        finally:
            self.shutdown(server, thread)

        request_content = requests_seen[1]["messages"][-1]["content"]
        self.assertEqual(code, 0)
        self.assertIn("--- a/nginx.conf", stdout)
        self.assertIn("+++ b/nginx.optimized.conf", stdout)
        self.assertNotIn("已生成优化版", stdout)
        self.assertNotIn("-disk baseline", stdout)
        self.assertIn("文件已保存: nginx.optimized.conf", stderr)
        self.assertIn("文件: nginx.conf", request_content)
        self.assertIn("已截断后注入", request_content)
        self.assertIn("内容因长度限制已截断", request_content)
        self.assertIn("附加输入摘要", history[-2]["content"])
        self.assertNotIn("A" * 60, history[-2]["content"])
        self.assertEqual(saved, "worker_processes auto;\n")

    def test_stdin_input_is_injected_with_default_prompt(self):
        responses = [
            self.intent_response("allow", "基于附加输入生成配置"),
            self.file_response(
                "已生成配置\n<opsai_write_file name=\"generated.conf\">\nline=1\n</opsai_write_file>"
            ),
        ]
        server, thread, requests_seen = self.serve(responses)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = self.make_config(root, server.server_port)
                old_cwd = Path.cwd()
                try:
                    os.chdir(root)
                    code, stdout, stderr = self.run_cli(
                        config,
                        "-o",
                        "generated.conf",
                        stdin_text="listen 80;\nserver_name _;\n",
                        stdin_isatty=False,
                        confirm_text="y\n",
                    )
                    saved = (root / "generated.conf").read_text(encoding="utf-8")
                finally:
                    os.chdir(old_cwd)
        finally:
            self.shutdown(server, thread)

        request_content = requests_seen[1]["messages"][-1]["content"]
        self.assertEqual(code, 0)
        self.assertIn("--- a/stdin", stdout)
        self.assertIn("+++ b/generated.conf", stdout)
        self.assertNotIn("已生成配置", stdout)
        self.assertIn("文件已保存: generated.conf", stderr)
        self.assertIn("目标输出文件: generated.conf", request_content)
        self.assertIn("请基于附加输入生成或优化文件，并保存为当前目录文件。", request_content)
        self.assertIn("[stdin]", request_content)
        self.assertIn("listen 80;", request_content)
        self.assertEqual(saved, "line=1\n")

    def test_save_file_in_current_directory(self):
        responses = [
            self.intent_response("allow", "配置文件生成请求"),
            self.file_response(
                "已生成配置\n<opsai_write_file name=\"demo.conf\">\nlisten 80;\n</opsai_write_file>"
            ),
        ]
        server, thread, requests_seen = self.serve(responses)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = self.make_config(root, server.server_port)
                old_cwd = Path.cwd()
                try:
                    os.chdir(root)
                    code, stdout, stderr = self.run_cli(
                        config,
                        "-o",
                        "demo.conf",
                        "生成 nginx 配置并保存为 demo.conf",
                        confirm_text="y\n",
                    )
                    saved_content = (root / "demo.conf").read_text(encoding="utf-8")
                    history = json.loads((root / ".opsai_history.json").read_text(encoding="utf-8"))
                finally:
                    os.chdir(old_cwd)
        finally:
            self.shutdown(server, thread)

        self.assertEqual(code, 0)
        self.assertIn("+++ b/demo.conf", stdout)
        self.assertNotIn("已生成配置", stdout)
        self.assertEqual(saved_content, "listen 80;\n")
        self.assertIn("是否继续？[y/N]", stderr)
        self.assertIn("文件已保存: demo.conf", stderr)
        self.assertFalse(requests_seen[1]["stream"])
        self.assertIn("文件处理结果", history[-1]["content"])
        self.assertNotIn("<opsai_write_file", history[-1]["content"])

    def test_no_external_input_diff_uses_empty_baseline(self):
        diff_text = build_file_diff_from_content(
            "demo.conf",
            "/dev/null",
            "",
            "worker_processes auto;\n",
        )

        self.assertIn("--- /dev/null", diff_text)
        self.assertIn("+++ b/demo.conf", diff_text)
        self.assertIn("+worker_processes auto;", diff_text)

    def test_reject_non_config_request_by_intent(self):
        responses = [self.intent_response("reject", "这是日志分析请求，不是配置文件生成或修改")]
        server, thread, requests_seen = self.serve(responses)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = self.make_config(root, server.server_port)
                code, stdout, stderr = self.run_cli(
                    config,
                    "-o",
                    "ignored.conf",
                    "分析一下 nginx 502 日志",
                )
        finally:
            self.shutdown(server, thread)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("只支持配置文件、脚本或代码文件的生成、修改、优化", stderr)
        self.assertEqual(len(requests_seen), 1)

    def test_reject_output_path_outside_current_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            config.write_text(
                textwrap.dedent(
                    """
                    [llm]
                    base_url = "http://127.0.0.1:1"
                    api_key = "test-key"
                    model = "test-model"
                    """
                ),
                encoding="utf-8",
            )
            code, stdout, stderr = self.run_cli(
                config,
                "-o",
                "/tmp/demo.conf",
                "生成配置",
            )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("拒绝写入当前目录之外的路径", stderr)

    def test_ask_before_overwrite_existing_file(self):
        responses = [
            self.intent_response("allow", "配置文件生成请求"),
            {
                "status": 200,
                "headers": {"Content-Type": "application/json"},
                "body_chunks": [
                    json.dumps(
                        {
                            "choices": [
                                {
                                    "message": {
                                        "content": "覆盖测试\n<opsai_write_file name=\"demo.txt\">\nnew\n</opsai_write_file>"
                                    }
                                }
                            ]
                        }
                    ).encode("utf-8")
                ],
            }
        ]
        server, thread, _requests_seen = self.serve(responses)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = self.make_config(root, server.server_port)
                target = root / "demo.txt"
                target.write_text("old", encoding="utf-8")
                old_cwd = Path.cwd()
                try:
                    os.chdir(root)
                    code, stdout, stderr = self.run_cli(
                        config,
                        "-o",
                        "demo.txt",
                        "生成文件并保存为 demo.txt",
                        confirm_text="n\n",
                    )
                    current = target.read_text(encoding="utf-8")
                finally:
                    os.chdir(old_cwd)
        finally:
            self.shutdown(server, thread)

        self.assertEqual(code, 0)
        self.assertIn("--- /dev/null", stdout)
        self.assertIn("+++ b/demo.txt", stdout)
        self.assertNotIn("覆盖测试", stdout)
        self.assertIn("是否继续？[y/N]", stderr)
        self.assertIn("文件已取消: demo.txt", stderr)
        self.assertEqual(current, "old")

    def test_overwrite_existing_file_creates_timestamp_backup(self):
        responses = [
            self.intent_response("allow", "配置文件生成请求"),
            self.file_response(
                "覆盖测试\n<opsai_write_file name=\"demo.txt\">\nnew\n</opsai_write_file>"
            ),
        ]
        server, thread, _requests_seen = self.serve(responses)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = self.make_config(root, server.server_port)
                target = root / "demo.txt"
                target.write_text("old\n", encoding="utf-8")
                old_cwd = Path.cwd()
                try:
                    os.chdir(root)
                    code, stdout, stderr = self.run_cli(
                        config,
                        "-o",
                        "demo.txt",
                        "生成文件并保存为 demo.txt",
                        confirm_text="y\n",
                    )
                    current = target.read_text(encoding="utf-8")
                    backups = sorted(root.glob("demo.txt.*.bak"))
                    backup_name = backups[0].name if backups else ""
                    backup_content = backups[0].read_text(encoding="utf-8") if backups else ""
                finally:
                    os.chdir(old_cwd)
        finally:
            self.shutdown(server, thread)

        self.assertEqual(code, 0)
        self.assertNotIn("覆盖测试", stdout)
        self.assertEqual(current, "new\n")
        self.assertEqual(len(backups), 1)
        self.assertRegex(backup_name, r"^demo\.txt\.\d{14}\.bak$")
        self.assertEqual(backup_content, "old\n")
        self.assertIn(f"已备份原文件: {backup_name}", stderr)
        self.assertIn("文件已保存: demo.txt", stderr)


if __name__ == "__main__":
    unittest.main()
