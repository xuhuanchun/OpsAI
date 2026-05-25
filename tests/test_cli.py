from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import textwrap
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from opsai.cli import (
    FILE_OUTPUT_PROMPT,
    INTENT_SYSTEM_PROMPT,
    build_result_report,
    build_file_diff_from_content,
    build_runtime_context,
    colorize_diff_text,
    colorize_diff_change_line,
    collect_multiline_editor_text,
    default_config_candidates,
    format_highlighted_label,
    format_processing_status,
    load_config,
    main,
    measure_display_width,
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
        interactive_text: str = "",
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        old_argv = sys.argv[:]
        old_stdin = sys.stdin

        class FakeStdin(io.StringIO):
            def __init__(self_inner, text: str, confirmation: str, interactive: str):
                super().__init__(text)
                self_inner._confirmation = io.StringIO(confirmation)
                self_inner._interactive = io.StringIO(interactive)

            def isatty(self_inner):
                return stdin_isatty

            def read_confirmation_line(self_inner):
                return self_inner._confirmation.readline()

            def read_interactive_text(self_inner):
                return self_inner._interactive.read()

        sys.argv = ["opsai", "--config", str(config), *args]
        try:
            sys.stdin = FakeStdin(stdin_text, confirm_text, interactive_text)
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

    def test_colorize_diff_change_line_keeps_wide_comment_within_terminal_width(self):
        terminal_width = measure_display_width("    1 + # 中文注释")
        colored = colorize_diff_change_line(
            "    1 + # 中文注释",
            terminal_width=terminal_width,
            background="\x1b[48;5;194m",
            marker_color="\x1b[32m",
        )
        plain = re.sub(r"\x1b\[[0-9;]*m", "", colored)

        self.assertEqual(measure_display_width(plain), terminal_width)

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

    def test_format_highlighted_label_with_force_color(self):
        old_force_color = os.environ.get("FORCE_COLOR")
        try:
            os.environ["FORCE_COLOR"] = "1"
            rendered = format_highlighted_label("DiffView", stream=sys.stdout)
        finally:
            if old_force_color is None:
                os.environ.pop("FORCE_COLOR", None)
            else:
                os.environ["FORCE_COLOR"] = old_force_color

        self.assertEqual(rendered, "\x1b[1m\x1b[36mDiffView\x1b[0m")

    def test_format_highlighted_status_with_force_color(self):
        old_force_color = os.environ.get("FORCE_COLOR")
        try:
            os.environ["FORCE_COLOR"] = "1"
            rendered = format_highlighted_label(
                "OpsAI已为您处理完成，耗时2秒。要点如下：",
                stream=sys.stdout,
            )
        finally:
            if old_force_color is None:
                os.environ.pop("FORCE_COLOR", None)
            else:
                os.environ["FORCE_COLOR"] = old_force_color

        self.assertEqual(
            rendered,
            "\x1b[1m\x1b[36mOpsAI已为您处理完成，耗时2秒。要点如下：\x1b[0m",
        )

    def test_collect_multiline_editor_text_supports_enter_and_ctrl_d(self):
        chars = iter(["第", "一", "行", "\r", "第", "二", "行", "\x04"])
        output = io.StringIO()

        collected = collect_multiline_editor_text(
            lambda: next(chars, ""),
            stream=output,
            prompt_text="请录入需求",
        )

        self.assertEqual(collected, "第一行\n第二行")
        self.assertTrue(output.getvalue().endswith("请录入需求\r\n第一行\r\n第二行\r\n"))

    def test_collect_multiline_editor_text_supports_arrow_keys(self):
        chars = iter(
            [
                "a",
                "b",
                "c",
                "d",
                "\x1b",
                "[",
                "D",
                "\x1b",
                "[",
                "D",
                "X",
                "\x1b",
                "[",
                "C",
                "Y",
                "\r",
                "1",
                "2",
                "\x1b",
                "[",
                "A",
                "\x1b",
                "[",
                "D",
                "\x1b",
                "[",
                "B",
                "Z",
                "\x04",
            ]
        )
        output = io.StringIO()

        collected = collect_multiline_editor_text(
            lambda: next(chars, ""),
            stream=output,
            prompt_text="请录入需求",
        )

        self.assertEqual(collected, "abXcY\n1Z2d")
        self.assertTrue(output.getvalue().endswith("请录入需求\r\nabXcY\r\n1Z2d\r\n"))

    def test_collect_multiline_editor_text_ctrl_c_exits(self):
        chars = iter(["第", "一", "\x03"])
        output = io.StringIO()

        with self.assertRaises(KeyboardInterrupt):
            collect_multiline_editor_text(
                lambda: next(chars, ""),
                stream=output,
                prompt_text="请录入需求",
            )

        self.assertTrue(output.getvalue().endswith("^C\r\n"))

    def test_report_header_is_stripped_from_visible_reply(self):
        stripped = build_result_report(
            "结果报告：\n操作类型：修改\n- 修改点：更新配置。",
            Path("/tmp/demo.conf"),
            True,
            "修改配置",
        )

        self.assertEqual(stripped, "操作类型：修改\n- 修改点：更新配置。")

    def test_intent_prompt_allows_scripts_and_code_files(self):
        self.assertIn("编写 bash/python 脚本", INTENT_SYSTEM_PROMPT)
        self.assertIn("修改 .py/.sh/.js/.go 等代码文件", INTENT_SYSTEM_PROMPT)

    def test_file_output_prompt_requires_script_usage_example(self):
        self.assertIn("如果生成的是脚本", FILE_OUTPUT_PROMPT)
        self.assertIn("使用示例", FILE_OUTPUT_PROMPT)

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

    def test_file_argument_is_required(self):
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
        self.assertIn("-f/--file 为必填项", stderr)

    def test_file_argument_cannot_repeat(self):
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
                "-f",
                "a.conf",
                "-f",
                "b.conf",
                "生成 nginx 配置",
            )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("-f/--file 只能指定一次", stderr)

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

    def test_missing_prompt_reads_interactively(self):
        model_messages: dict[str, object] = {}

        def fake_detect_request_intent(*args, **kwargs):
            return mock.Mock(decision="allow", reason="配置文件生成请求")

        def fake_call_model_non_stream(_config, messages, **kwargs):
            model_messages["messages"] = messages
            return (
                "操作类型：新建\n- 要点：生成最小配置文件。\n"
                "<opsai_write_file>\nworker_processes auto;\n</opsai_write_file>"
            )

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
                    timeout_seconds = 5

                    [memory]
                    history_rounds = 3
                    history_file = ".opsai_history.json"
                    """
                ),
                encoding="utf-8",
            )
            target = (root / "generated.conf").resolve()
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                with mock.patch("opsai.cli.detect_request_intent", side_effect=fake_detect_request_intent), mock.patch(
                    "opsai.cli.call_model_non_stream",
                    side_effect=fake_call_model_non_stream,
                ):
                    code, stdout, stderr = self.run_cli(
                        config,
                        "-f",
                        str(target),
                        confirm_text="y\n",
                        interactive_text="生成一个最小 nginx 配置",
                    )
                saved = target.read_text(encoding="utf-8")
            finally:
                os.chdir(old_cwd)

        request_content = model_messages["messages"][-1]["content"]
        self.assertEqual(code, 0)
        self.assertIn("DiffView", stdout)
        self.assertIn("请录入需求（支持多行，ENTER 回行，CTRL-D 提交）：", stderr)
        self.assertIn(f"文件已保存: {target}", stderr)
        self.assertIn("生成一个最小 nginx 配置", request_content)
        self.assertNotIn("请基于已提供内容生成或优化目标文件，并保存到指定路径。", request_content)
        self.assertEqual(saved, "worker_processes auto;\n")

    def test_missing_prompt_requires_non_empty_interactive_input(self):
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
                "-f",
                "a.conf",
                stdin_isatty=False,
                interactive_text="",
            )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("请录入需求（支持多行，ENTER 回行，CTRL-D 提交）：", stderr)
        self.assertIn("未录入自然语言需求", stderr)

    def test_missing_prompt_ctrl_c_returns_130(self):
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
            with mock.patch("opsai.cli.read_interactive_user_prompt", side_effect=KeyboardInterrupt):
                code, stdout, stderr = self.run_cli(config, "-f", "a.conf")

        self.assertEqual(code, 130)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")

    def test_file_input_is_injected_and_history_only_keeps_summary(self):
        responses = [
            self.intent_response("allow", "配置文件优化请求"),
            self.file_response(
                "操作类型：修改\n- 修改点：将 worker_processes 设置为 auto。\n- 原因：让 worker 数量随 CPU 自动适配。\n<opsai_write_file>\nworker_processes auto;\n</opsai_write_file>"
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
                target = (root / "nginx.optimized.conf").resolve()
                target.write_text("A" * 80, encoding="utf-8")
                old_cwd = Path.cwd()
                try:
                    os.chdir(root)
                    code, stdout, stderr = self.run_cli(
                        config,
                        "-f",
                        str(target),
                        "优化这个 nginx 配置",
                        confirm_text="y\n",
                    )
                    history = json.loads((root / ".opsai_history.json").read_text(encoding="utf-8"))
                    saved = target.read_text(encoding="utf-8")
                finally:
                    os.chdir(old_cwd)
        finally:
            self.shutdown(server, thread)

        request_content = requests_seen[1]["messages"][-1]["content"]
        self.assertEqual(code, 0)
        self.assertIn("DiffView", stdout)
        self.assertIn("OpsAI已为您处理完成，耗时", stdout)
        self.assertIn("操作类型：修改", stdout)
        self.assertIn(f"--- {target}", stdout)
        self.assertIn(f"+++ {target}", stdout)
        self.assertLess(stdout.index("DiffView"), stdout.index("OpsAI已为您处理完成，耗时"))
        self.assertIn("文件已保存: ", stderr)
        self.assertIn(str(target), stderr)
        self.assertIn(f"目标文件: {target}", request_content)
        self.assertIn("已截断后注入", request_content)
        self.assertIn("内容因长度限制已截断", request_content)
        self.assertIn("附加输入摘要", history[-2]["content"])
        self.assertNotIn("A" * 60, history[-2]["content"])
        self.assertEqual(saved, "worker_processes auto;\n")

    def test_stdin_input_is_injected_with_interactive_prompt(self):
        responses = [
            self.intent_response("allow", "基于附加输入生成配置"),
            self.file_response(
                "操作类型：新建\n- 要点：生成最小配置文件。\n- 内容：包含 line=1 作为示例。\n<opsai_write_file>\nline=1\n</opsai_write_file>"
            ),
        ]
        server, thread, requests_seen = self.serve(responses)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = self.make_config(root, server.server_port)
                target = (root / "generated.conf").resolve()
                old_cwd = Path.cwd()
                try:
                    os.chdir(root)
                    code, stdout, stderr = self.run_cli(
                        config,
                        "-f",
                        str(target),
                        stdin_text="listen 80;\nserver_name _;\n",
                        stdin_isatty=False,
                        confirm_text="y\n",
                        interactive_text="基于附加输入生成配置",
                    )
                    saved = target.read_text(encoding="utf-8")
                finally:
                    os.chdir(old_cwd)
        finally:
            self.shutdown(server, thread)

        request_content = requests_seen[1]["messages"][-1]["content"]
        self.assertEqual(code, 0)
        self.assertIn("DiffView", stdout)
        self.assertIn("OpsAI已为您处理完成，耗时", stdout)
        self.assertIn("操作类型：新建", stdout)
        self.assertIn("--- /dev/null", stdout)
        self.assertIn(f"+++ {target}", stdout)
        self.assertIn(f"文件已保存: {target}", stderr)
        self.assertIn(f"目标文件路径: {target}", request_content)
        self.assertIn("基于附加输入生成配置", request_content)
        self.assertIn("[stdin]", request_content)
        self.assertIn("listen 80;", request_content)
        self.assertEqual(saved, "line=1\n")

    def test_save_file_to_specified_path(self):
        responses = [
            self.intent_response("allow", "配置文件生成请求"),
            self.file_response(
                "操作类型：新建\n- 要点：生成最小 nginx 配置。\n- 内容：写入 listen 80;。\n<opsai_write_file>\nlisten 80;\n</opsai_write_file>"
            ),
        ]
        server, thread, requests_seen = self.serve(responses)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = self.make_config(root, server.server_port)
                target = (root / "demo.conf").resolve()
                old_cwd = Path.cwd()
                try:
                    os.chdir(root)
                    code, stdout, stderr = self.run_cli(
                        config,
                        "-f",
                        "demo.conf",
                        "生成 nginx 配置",
                        confirm_text="y\n",
                    )
                    saved_content = target.read_text(encoding="utf-8")
                    history = json.loads((root / ".opsai_history.json").read_text(encoding="utf-8"))
                finally:
                    os.chdir(old_cwd)
        finally:
            self.shutdown(server, thread)

        self.assertEqual(code, 0)
        self.assertIn("DiffView", stdout)
        self.assertIn("OpsAI已为您处理完成，耗时", stdout)
        self.assertIn(f"+++ {target}", stdout)
        self.assertEqual(saved_content, "listen 80;\n")
        self.assertIn("\n以下文件将被写入", stderr)
        self.assertIn(f"文件已保存: {target}", stderr)
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
        self.assertIn("+++ demo.conf", diff_text)
        self.assertIn("+worker_processes auto;", diff_text)

    def test_reject_non_config_request_by_intent(self):
        responses = [self.intent_response("reject", "这是日志分析请求，不是配置文件生成或修改")]
        server, thread, requests_seen = self.serve(responses)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = self.make_config(root, server.server_port)
                target = (root / "ignored.conf").resolve()
                code, stdout, stderr = self.run_cli(
                    config,
                    "-f",
                    str(target),
                    "分析一下 nginx 502 日志",
                )
        finally:
            self.shutdown(server, thread)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("只支持配置文件、脚本或代码文件的生成、修改、优化", stderr)
        self.assertEqual(len(requests_seen), 1)

    def test_save_file_outside_current_directory(self):
        responses = [
            self.intent_response("allow", "配置文件生成请求"),
            self.file_response(
                "操作类型：新建\n- 要点：生成示例配置。\n- 内容：写入 line=1。\n<opsai_write_file>\nline=1\n</opsai_write_file>"
            ),
        ]
        server, thread, _requests_seen = self.serve(responses)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = self.make_config(root, server.server_port)
                cwd_dir = root / "cwd"
                cwd_dir.mkdir()
                target = (root / "nested" / "demo.conf").resolve()
                old_cwd = Path.cwd()
                try:
                    os.chdir(cwd_dir)
                    code, stdout, stderr = self.run_cli(
                        config,
                        "-f",
                        str(target),
                        "生成配置",
                        confirm_text="y\n",
                    )
                    saved = target.read_text(encoding="utf-8")
                finally:
                    os.chdir(old_cwd)
        finally:
            self.shutdown(server, thread)

        self.assertEqual(code, 0)
        self.assertIn("DiffView", stdout)
        self.assertIn("OpsAI已为您处理完成，耗时", stdout)
        self.assertIn(f"+++ {target}", stdout)
        self.assertIn(f"文件已保存: {target}", stderr)
        self.assertEqual(saved, "line=1\n")

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
                                        "content": "操作类型：修改\n- 修改点：覆盖旧内容。\n- 原因：验证取消写入流程。\n<opsai_write_file>\nnew\n</opsai_write_file>"
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
                target = (root / "demo.txt").resolve()
                target.write_text("old", encoding="utf-8")
                old_cwd = Path.cwd()
                try:
                    os.chdir(root)
                    code, stdout, stderr = self.run_cli(
                        config,
                        "-f",
                        "demo.txt",
                        "修改 demo.txt",
                        confirm_text="n\n",
                    )
                    current = target.read_text(encoding="utf-8")
                finally:
                    os.chdir(old_cwd)
        finally:
            self.shutdown(server, thread)

        self.assertEqual(code, 0)
        self.assertIn(f"--- {target}", stdout)
        self.assertIn(f"+++ {target}", stdout)
        self.assertIn("DiffView", stdout)
        self.assertIn("OpsAI已为您处理完成，耗时", stdout)
        self.assertIn("\n以下文件将被写入或覆盖：", stderr)
        self.assertIn(f"文件已取消: {target}", stderr)
        self.assertEqual(current, "old")

    def test_overwrite_existing_file_creates_timestamp_backup(self):
        responses = [
            self.intent_response("allow", "配置文件生成请求"),
            self.file_response(
                "操作类型：修改\n- 修改点：将内容替换为 new。\n- 原因：验证备份流程。\n<opsai_write_file>\nnew\n</opsai_write_file>"
            ),
        ]
        server, thread, _requests_seen = self.serve(responses)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = self.make_config(root, server.server_port)
                target = (root / "demo.txt").resolve()
                target.write_text("old\n", encoding="utf-8")
                old_cwd = Path.cwd()
                try:
                    os.chdir(root)
                    code, stdout, stderr = self.run_cli(
                        config,
                        "-f",
                        "demo.txt",
                        "修改 demo.txt",
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
        self.assertIn("DiffView", stdout)
        self.assertIn("OpsAI已为您处理完成，耗时", stdout)
        self.assertEqual(current, "new\n")
        self.assertEqual(len(backups), 1)
        self.assertRegex(backup_name, r"^demo\.txt\.\d{14}\.bak$")
        self.assertEqual(backup_content, "old\n")
        self.assertIn(f"已备份原文件: {backups[0].resolve()}", stderr)
        self.assertIn(f"文件已保存: {target}", stderr)


if __name__ == "__main__":
    unittest.main()
