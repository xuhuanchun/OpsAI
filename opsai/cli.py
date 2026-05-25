from __future__ import annotations

import argparse
import difflib
import json
import os
import platform
import re
import shutil
import sys
import threading
import time
import tomllib
import unicodedata
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

import requests


DEFAULT_SYSTEM_PROMPT = (
    "你是一位文件工程助手，只负责配置文件、脚本和代码文件的生成、修改和优化。"
    "对允许的请求，你必须产出可直接落盘的完整文件内容。"
    "不要回答与文件生成、修改、优化无关的需求。"
)
INTENT_SYSTEM_PROMPT = (
    "你是一位低成本意图识别器。"
    "只判断用户请求是否属于“配置文件、脚本或代码文件的生成、修改、优化，并预期保存为命令行 `-f/--file` 指定的单个文件路径”。\n"
    "允许的例子：生成 nginx.conf、修改现有 docker-compose.yml、优化 Kubernetes YAML、重写 systemd unit、补全 toml/ini/json/yaml 配置、编写 bash/python 脚本、修改 .py/.sh/.js/.go 等代码文件。\n"
    "拒绝的例子：日志分析、故障排查建议、命令解释、通用问答。\n"
    "输出必须是严格 JSON，格式：{\"decision\":\"allow\"|\"reject\",\"reason\":\"一句简短原因\"}"
)
FILE_OUTPUT_PROMPT = (
    "对于允许的请求，你必须先输出结果报告，再在回答末尾追加且只追加一个文件块，严格使用如下格式：\n"
    "结果报告要求：\n"
    "1. 如果是修改已有文件，简洁列出修改点，并说明每一项这样修改的原因。\n"
    "2. 如果是新建文件，简洁列出新建要点，并说明文件将包含的主要内容。\n"
    "3. 如果生成的是脚本，结果报告里必须额外给出简洁可执行的使用示例。\n"
    "4. 结果报告不要输出 diff，不要重复粘贴完整文件内容，也不要额外写“结果报告”标题。\n"
    "文件块格式：\n"
    "<opsai_write_file>\n"
    "文件内容\n"
    "</opsai_write_file>\n"
    "限制：\n"
    "1. 不要输出文件名，目标文件路径由命令行参数 `-f/--file` 指定。\n"
    "2. 如果用户要求修改已有文件，请输出修改后的完整文件内容，不要输出 diff。\n"
    "3. 不要输出与文件生成、修改、优化无关的建议性内容。"
)
CLEAR_COMMANDS = {"/clear", "clear", "清除上下文"}
INTERACTIVE_PROMPT_LABEL = "请录入需求（支持多行，ENTER 回行，CTRL-D 提交）："
FILE_BLOCK_PATTERN = re.compile(
    r"<opsai_write_file(?:\s+name=\"[^\"]+\")?>(.*?)</opsai_write_file>",
    re.S,
)
HUNK_HEADER_PATTERN = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)
COMMON_COMMANDS = (
    "sh",
    "bash",
    "zsh",
    "python3",
    "pip",
    "git",
    "curl",
    "wget",
    "openssl",
    "ssh",
    "scp",
    "rsync",
    "jq",
    "yq",
    "tar",
    "sed",
    "awk",
    "docker",
    "docker-compose",
    "kubectl",
    "helm",
    "systemctl",
    "journalctl",
    "nginx",
)
ANSI_RESET = "\x1b[0m"
ANSI_BOLD = "\x1b[1m"
ANSI_DIM = "\x1b[2m"
ANSI_RED = "\x1b[31m"
ANSI_GREEN = "\x1b[32m"
ANSI_CYAN = "\x1b[36m"
ANSI_BLACK = "\x1b[30m"
ANSI_GRAY = "\x1b[90m"
ANSI_BG_LIGHT_RED = "\x1b[48;5;224m"
ANSI_BG_LIGHT_GREEN = "\x1b[48;5;194m"


@dataclass
class AppConfig:
    base_url: str
    api_key: str
    model: str
    system_prompt: str
    history_rounds: int
    history_file: Path
    timeout_seconds: int
    verify_ssl: bool
    ca_file: Path | None
    max_input_chars: int


@dataclass
class InputAttachment:
    source_name: str
    label: str
    content: str
    full_content: str
    original_chars: int
    injected_chars: int
    truncated: bool
    omitted: bool


@dataclass
class FileWriteRequest:
    content: str


@dataclass
class FileWriteResult:
    path: str
    status: str
    backup_path: str | None = None


@dataclass
class IntentDecision:
    decision: str
    reason: str


def format_processing_status(seconds: int) -> str:
    return f"\r处理中({seconds}秒)"


class ProcessingTimer:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        self._elapsed_seconds = 0

    def start(self) -> None:
        if self._started_at is not None:
            return
        self._stop_event = threading.Event()
        self._started_at = time.monotonic()
        if not should_render_processing_timer():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._started_at is None:
            return
        self._elapsed_seconds = max(0, int(time.monotonic() - self._started_at))
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            print("\r" + " " * 32 + "\r", end="", file=sys.stderr, flush=True)
        self._thread = None
        self._started_at = None

    @property
    def elapsed_seconds(self) -> int:
        if self._started_at is not None:
            return max(0, int(time.monotonic() - self._started_at))
        return self._elapsed_seconds

    def _run(self) -> None:
        while not self._stop_event.is_set():
            print(
                format_processing_status(self.elapsed_seconds),
                end="",
                file=sys.stderr,
                flush=True,
            )
            if self._stop_event.wait(1):
                return


def should_render_processing_timer() -> bool:
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(sys.stderr, "isatty", lambda: False)())


def use_color_stream(stream: object) -> bool:
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def format_highlighted_label(text: str, *, stream: object) -> str:
    if not use_color_stream(stream):
        return text
    return f"{ANSI_BOLD}{ANSI_CYAN}{text}{ANSI_RESET}"


def default_config_candidates() -> list[Path]:
    script_path = Path(sys.argv[0]).expanduser()
    if not script_path.is_absolute():
        script_path = (Path.cwd() / script_path).resolve()
    else:
        script_path = script_path.resolve()

    package_root = Path(__file__).resolve().parent.parent
    candidates = [
        script_path.parent / "config.toml",
        package_root / "config.toml",
        Path.cwd() / "config.toml",
    ]

    seen: set[Path] = set()
    unique_candidates: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_candidates.append(resolved)
    return unique_candidates


def resolve_config_path(config_arg: str | None) -> Path:
    if config_arg:
        return Path(config_arg).expanduser().resolve()

    for candidate in default_config_candidates():
        if candidate.exists():
            return candidate
    return default_config_candidates()[0]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="opsai",
        description="根据自然语言运维需求，调用大模型返回操作建议。",
    )
    parser.add_argument(
        "prompt",
        nargs="*",
        help="自然语言需求，输入 /clear 可清除上下文。",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=None,
        help="配置文件路径；未指定时优先读取启动脚本同目录下的 config.toml。",
    )
    parser.add_argument(
        "-f",
        "--file",
        action="append",
        default=[],
        help="目标文件路径。必须且只能指定一次；文件存在时作为输入基线并在确认后写回，不存在时新建。",
    )
    parser.add_argument(
        "--max-input-chars",
        type=int,
        default=None,
        help="覆盖配置中的附件输入字符预算，用字符数近似控制 token。",
    )
    parser.add_argument(
        "--clear-context",
        action="store_true",
        help="清除上下文后退出。",
    )
    return parser.parse_args()


def load_config(config_path: Path) -> AppConfig:
    if not config_path.exists():
        raise FileNotFoundError(f"未找到配置文件: {config_path}")

    with config_path.open("rb") as file:
        raw = tomllib.load(file)

    llm = raw.get("llm", {})
    memory = raw.get("memory", {})
    input_config = raw.get("input", {})

    missing_fields = [
        name for name in ("base_url", "api_key", "model") if not llm.get(name)
    ]
    if missing_fields:
        joined = "、".join(missing_fields)
        raise ValueError(f"配置文件缺少必要字段: llm.{joined}")

    history_rounds = int(memory.get("history_rounds", 3))
    if history_rounds < 0:
        raise ValueError("memory.history_rounds 不能小于 0")

    history_file_value = memory.get("history_file", ".opsai_history.json")
    history_file = Path(history_file_value).expanduser()
    if not history_file.is_absolute():
        history_file = (config_path.parent / history_file).resolve()

    timeout_seconds = int(llm.get("timeout_seconds", 60))
    if timeout_seconds <= 0:
        raise ValueError("llm.timeout_seconds 必须大于 0")

    verify_ssl = bool(llm.get("verify_ssl", True))
    ca_file_value = llm.get("ca_file")
    ca_file: Path | None = None
    if ca_file_value:
        ca_file = Path(str(ca_file_value)).expanduser()
        if not ca_file.is_absolute():
            ca_file = (config_path.parent / ca_file).resolve()
        if verify_ssl and not ca_file.exists():
            raise ValueError(f"llm.ca_file 指向的证书文件不存在: {ca_file}")

    max_input_chars = int(input_config.get("max_input_chars", 12000))
    if max_input_chars <= 0:
        raise ValueError("input.max_input_chars 必须大于 0")

    return AppConfig(
        base_url=str(llm["base_url"]).rstrip("/"),
        api_key=str(llm["api_key"]),
        model=str(llm["model"]),
        system_prompt=str(llm.get("system_prompt", DEFAULT_SYSTEM_PROMPT)),
        history_rounds=history_rounds,
        history_file=history_file,
        timeout_seconds=timeout_seconds,
        verify_ssl=verify_ssl,
        ca_file=ca_file,
        max_input_chars=max_input_chars,
    )


def load_history(history_file: Path) -> list[dict[str, str]]:
    if not history_file.exists():
        return []

    try:
        data = json.loads(history_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"上下文文件损坏，无法解析: {history_file}") from exc

    if not isinstance(data, list):
        raise ValueError(f"上下文文件格式错误: {history_file}")

    messages: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role in {"user", "assistant"} and isinstance(content, str):
            messages.append({"role": role, "content": content})
    return messages


def save_history(history_file: Path, messages: list[dict[str, str]]) -> None:
    history_file.parent.mkdir(parents=True, exist_ok=True)
    history_file.write_text(
        json.dumps(messages, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def trim_history(messages: list[dict[str, str]], history_rounds: int) -> list[dict[str, str]]:
    if history_rounds == 0:
        return []
    return messages[-(history_rounds * 2) :]


def clear_history(history_file: Path) -> None:
    if history_file.exists():
        history_file.unlink()


def build_messages(
    system_prompt: str,
    history_messages: list[dict[str, str]],
    user_prompt: str,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": FILE_OUTPUT_PROMPT},
        {"role": "system", "content": build_runtime_context()},
        *history_messages,
        {"role": "user", "content": user_prompt},
    ]


def build_intent_messages(
    user_prompt: str,
    target_path: Path,
    target_exists: bool,
    stdin_content: str,
) -> list[dict[str, str]]:
    details = [
        f"用户需求: {user_prompt}",
        f"目标文件路径: {target_path}",
        f"目标文件是否已存在: {'是' if target_exists else '否'}",
        f"是否提供 stdin: {'是' if bool(stdin_content) else '否'}",
    ]
    return [
        {"role": "system", "content": INTENT_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(details)},
    ]


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    if limit <= 0:
        return "", True
    marker = "\n...[内容因长度限制已截断]...\n"
    if limit <= len(marker):
        return text[:limit], True
    remaining = limit - len(marker)
    head = remaining // 2
    tail = remaining - head
    return f"{text[:head]}{marker}{text[-tail:]}", True


def read_file_content(path: Path) -> tuple[str, str]:
    if not path.exists():
        raise ValueError(f"文件不存在: {path}")
    if not path.is_file():
        raise ValueError(f"不是普通文件: {path}")
    return str(path), path.read_bytes().decode("utf-8", errors="replace")


def read_stdin_content() -> str:
    if sys.stdin.isatty():
        return ""
    return sys.stdin.read()


def build_attachments(
    target_path: Path,
    stdin_content: str,
    max_input_chars: int,
) -> list[InputAttachment]:
    sources: list[tuple[str, str, str]] = []
    if target_path.exists():
        source_name, content = read_file_content(target_path)
        sources.append((source_name, f"目标文件: {source_name}", content))
    if stdin_content:
        sources.append(("stdin", "stdin", stdin_content))

    attachments: list[InputAttachment] = []
    remaining = max_input_chars
    for source_name, label, content in sources:
        original_chars = len(content)
        truncated_content, truncated = truncate_text(content, remaining)
        injected_chars = len(truncated_content)
        omitted = original_chars > 0 and injected_chars == 0
        if injected_chars > 0:
            remaining -= injected_chars
        attachments.append(
            InputAttachment(
                source_name=source_name,
                label=label,
                content=truncated_content,
                full_content=content,
                original_chars=original_chars,
                injected_chars=injected_chars,
                truncated=truncated or omitted,
                omitted=omitted,
            )
        )
    return attachments


def summarize_attachments(attachments: list[InputAttachment]) -> str:
    if not attachments:
        return ""
    lines = ["附加输入摘要："]
    for attachment in attachments:
        status = "未注入"
        if not attachment.omitted:
            status = "已完整注入"
            if attachment.truncated:
                status = "已截断后注入"
        lines.append(
            f"- {attachment.label}：原始 {attachment.original_chars} 字符，"
            f"注入 {attachment.injected_chars} 字符，{status}"
        )
    return "\n".join(lines)


def build_user_contents(
    user_prompt: str,
    target_path: Path,
    attachments: list[InputAttachment],
) -> tuple[str, str]:
    prefix = f"目标文件路径: {target_path}\n用户需求: {user_prompt}"
    if not attachments:
        return prefix, prefix

    summary = summarize_attachments(attachments)
    detail_lines = [summary, "附加输入内容："]
    for attachment in attachments:
        if attachment.omitted:
            continue
        detail_lines.append(f"[{attachment.label}]")
        detail_lines.append(attachment.content)

    request_content = f"{prefix}\n\n" + "\n".join(detail_lines)
    history_content = f"{prefix}\n\n{summary}"
    return request_content, history_content


def parse_json_object(text: str) -> dict[str, object]:
    candidate = text.strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", candidate, re.S)
        if not match:
            raise ValueError(f"无法从返回中解析 JSON: {text}")
        return json.loads(match.group(0))


def parse_intent_decision(text: str) -> IntentDecision:
    payload = parse_json_object(text)
    decision = payload.get("decision")
    reason = payload.get("reason")
    if decision not in {"allow", "reject"} or not isinstance(reason, str):
        raise ValueError(f"意图识别返回格式异常: {payload}")
    return IntentDecision(decision=decision, reason=reason.strip())


def resolve_target_path(path_text: str) -> Path:
    candidate = path_text.strip().strip("\"'`")
    if not candidate:
        raise ValueError("目标文件路径不能为空")

    path = Path(candidate).expanduser()
    if path.name in {"", ".", ".."}:
        raise ValueError(f"非法目标文件路径: {path_text}")
    return path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()


def extract_file_write_requests(reply: str) -> tuple[str, list[FileWriteRequest]]:
    requests: list[FileWriteRequest] = []

    def replace(match: re.Match[str]) -> str:
        content = match.group(1)
        if content.startswith("\r\n"):
            content = content[2:]
        elif content.startswith("\n"):
            content = content[1:]
        requests.append(FileWriteRequest(content=content))
        return ""

    cleaned = FILE_BLOCK_PATTERN.sub(replace, reply)
    cleaned = cleaned.strip()
    return cleaned, requests


def read_confirmation_answer(prompt: str) -> str | None:
    print(prompt, end="", file=sys.stderr, flush=True)
    if hasattr(sys.stdin, "read_confirmation_line"):
        answer = sys.stdin.read_confirmation_line()
        return answer if answer != "" else None
    if sys.stdin.isatty():
        answer = sys.stdin.readline()
        return answer if answer != "" else None

    fallback_paths = ["/dev/tty"]
    if os.name == "nt":
        fallback_paths.insert(0, "CONIN$")

    for path in fallback_paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as stream:
                answer = stream.readline()
        except OSError:
            continue
        return answer if answer != "" else None
    return None


def measure_display_width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def count_rendered_lines(text: str, terminal_width: int) -> int:
    width = max(terminal_width, 1)
    line_count = 1
    current_width = 0
    for char in text:
        if char == "\n":
            line_count += 1
            current_width = 0
            continue
        char_width = max(measure_display_width(char), 1)
        if current_width and current_width + char_width > width:
            line_count += 1
            current_width = 0
        current_width += char_width
    return line_count


def calculate_visual_cursor(text: str, terminal_width: int) -> tuple[int, int]:
    width = max(terminal_width, 1)
    line = 0
    column = 0
    for char in text:
        if char == "\n":
            line += 1
            column = 0
            continue
        char_width = max(measure_display_width(char), 1)
        if column and column + char_width > width:
            line += 1
            column = 0
        column += char_width
    return line, column


def clear_rendered_lines(stream: object, line_count: int, cursor_line: int) -> None:
    if line_count <= 0:
        return
    write = getattr(stream, "write")
    write("\r")
    for _ in range(cursor_line):
        write("\x1b[1A")
    for index in range(line_count):
        write("\x1b[2K")
        if index < line_count - 1:
            write("\x1b[1B\r")
    for _ in range(line_count - 1):
        write("\x1b[1A")
    write("\r")


def render_multiline_editor(
    prompt_text: str,
    content: str,
    cursor_index: int,
    *,
    stream: object,
    previous_render_state: tuple[int, int] | None = None,
) -> tuple[int, int]:
    terminal_width = max(shutil.get_terminal_size(fallback=(80, 24)).columns, 20)
    if previous_render_state is not None:
        clear_rendered_lines(stream, previous_render_state[0], previous_render_state[1])
    write = getattr(stream, "write")
    write(prompt_text)
    write("\r\n")
    write(content.replace("\n", "\r\n"))
    end_line, _end_column = calculate_visual_cursor(content, terminal_width)
    cursor_line, cursor_column = calculate_visual_cursor(content[:cursor_index], terminal_width)
    if end_line > cursor_line:
        write(f"\x1b[{end_line - cursor_line}A")
    write("\r")
    if cursor_column > 0:
        write(f"\x1b[{cursor_column}C")
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()
    return 1 + count_rendered_lines(content, terminal_width), 1 + cursor_line


def read_editor_key(read_char) -> str | None:
    char = read_char()
    if char in {"", None}:
        return ""
    if char != "\x1b":
        return char
    starter = read_char()
    if starter in {"", None}:
        return "\x1b"
    if starter not in {"[", "O"}:
        return "\x1b"
    tail = ""
    while True:
        char = read_char()
        if char in {"", None}:
            return "\x1b"
        tail += char
        if char.isalpha() or char == "~":
            break
    sequence = starter + tail
    return {
        "[A": "up",
        "[B": "down",
        "[C": "right",
        "[D": "left",
        "[H": "home",
        "[F": "end",
        "[1~": "home",
        "[4~": "end",
        "[7~": "home",
        "[8~": "end",
        "OH": "home",
        "OF": "end",
    }.get(sequence, "\x1b")


def get_cursor_row_col(text: str, cursor_index: int) -> tuple[int, int]:
    prefix = text[:cursor_index]
    row = prefix.count("\n")
    line_start = prefix.rfind("\n")
    if line_start == -1:
        return row, len(prefix)
    return row, len(prefix) - line_start - 1


def get_cursor_index_from_row_col(text: str, row: int, column: int) -> int:
    lines = text.split("\n")
    if not lines:
        return 0
    clamped_row = max(0, min(row, len(lines) - 1))
    clamped_column = max(0, min(column, len(lines[clamped_row])))
    index = 0
    for current_row in range(clamped_row):
        index += len(lines[current_row]) + 1
    return index + clamped_column


def collect_multiline_editor_text(
    read_char,
    *,
    stream: object,
    prompt_text: str,
) -> str | None:
    buffer: list[str] = []
    cursor_index = 0
    preferred_column: int | None = None
    render_state = render_multiline_editor(
        prompt_text,
        "",
        cursor_index,
        stream=stream,
    )
    write = getattr(stream, "write")
    flush = getattr(stream, "flush", None)

    while True:
        char = read_editor_key(read_char)
        text = "".join(buffer)
        if char in {"", None}:
            if callable(flush):
                flush()
            return text.strip() or None
        if char == "\x03":
            clear_rendered_lines(stream, render_state[0], render_state[1])
            write("^C\r\n")
            if callable(flush):
                flush()
            raise KeyboardInterrupt
        if char == "\x04":
            clear_rendered_lines(stream, render_state[0], render_state[1])
            write(prompt_text)
            write("\r\n")
            write(text.replace("\n", "\r\n"))
            write("\r\n")
            if callable(flush):
                flush()
            return text.strip() or None
        if char == "left":
            if cursor_index > 0:
                cursor_index -= 1
                preferred_column = None
                render_state = render_multiline_editor(
                    prompt_text,
                    text,
                    cursor_index,
                    stream=stream,
                    previous_render_state=render_state,
                )
            continue
        if char == "right":
            if cursor_index < len(buffer):
                cursor_index += 1
                preferred_column = None
                render_state = render_multiline_editor(
                    prompt_text,
                    text,
                    cursor_index,
                    stream=stream,
                    previous_render_state=render_state,
                )
            continue
        if char == "home":
            row, _column = get_cursor_row_col(text, cursor_index)
            cursor_index = get_cursor_index_from_row_col(text, row, 0)
            preferred_column = 0
            render_state = render_multiline_editor(
                prompt_text,
                text,
                cursor_index,
                stream=stream,
                previous_render_state=render_state,
            )
            continue
        if char == "end":
            row, _column = get_cursor_row_col(text, cursor_index)
            cursor_index = get_cursor_index_from_row_col(text, row, len(text.split("\n")[row]))
            preferred_column = None
            render_state = render_multiline_editor(
                prompt_text,
                text,
                cursor_index,
                stream=stream,
                previous_render_state=render_state,
            )
            continue
        if char in {"up", "down"}:
            row, column = get_cursor_row_col(text, cursor_index)
            target_row = row - 1 if char == "up" else row + 1
            target_column = column if preferred_column is None else preferred_column
            next_index = get_cursor_index_from_row_col(text, target_row, target_column)
            if next_index != cursor_index:
                cursor_index = next_index
                if preferred_column is None:
                    preferred_column = column
                render_state = render_multiline_editor(
                    prompt_text,
                    text,
                    cursor_index,
                    stream=stream,
                    previous_render_state=render_state,
                )
            continue
        preferred_column = None
        if char in {"\x08", "\x7f"}:
            if cursor_index <= 0:
                continue
            del buffer[cursor_index - 1]
            cursor_index -= 1
            render_state = render_multiline_editor(
                prompt_text,
                "".join(buffer),
                cursor_index,
                stream=stream,
                previous_render_state=render_state,
            )
            continue
        if char == "\r":
            char = "\n"
        if char == "\n":
            buffer.insert(cursor_index, "\n")
            cursor_index += 1
            render_state = render_multiline_editor(
                prompt_text,
                "".join(buffer),
                cursor_index,
                stream=stream,
                previous_render_state=render_state,
            )
            continue
        if unicodedata.category(char) == "Cc":
            continue
        buffer.insert(cursor_index, char)
        cursor_index += 1
        render_state = render_multiline_editor(
            prompt_text,
            "".join(buffer),
            cursor_index,
            stream=stream,
            previous_render_state=render_state,
        )


def read_native_multiline_editor(
    input_stream: object,
    *,
    output_stream: object,
    prompt_text: str,
) -> str | None:
    if os.name == "nt":
        return collect_multiline_editor_text(
            lambda: getattr(input_stream, "read")(1),
            stream=output_stream,
            prompt_text=prompt_text,
        )

    import termios
    import tty

    fd = getattr(input_stream, "fileno")()
    original_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return collect_multiline_editor_text(
            lambda: getattr(input_stream, "read")(1),
            stream=output_stream,
            prompt_text=prompt_text,
        )
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original_settings)


def read_interactive_user_prompt() -> str | None:
    prompt_text = format_highlighted_label(INTERACTIVE_PROMPT_LABEL, stream=sys.stderr)
    if hasattr(sys.stdin, "read_interactive_text"):
        print(prompt_text, file=sys.stderr, flush=True)
        text = sys.stdin.read_interactive_text()
        return text.strip() if text is not None else None
    if sys.stdin.isatty():
        return read_native_multiline_editor(
            sys.stdin,
            output_stream=sys.stderr,
            prompt_text=prompt_text,
        )

    fallback_paths = ["/dev/tty"]
    if os.name == "nt":
        fallback_paths.insert(0, "CONIN$")

    for path in fallback_paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as stream:
                text = read_native_multiline_editor(
                    stream,
                    output_stream=sys.stderr,
                    prompt_text=prompt_text,
                )
        except OSError:
            continue
        return text
    return None


def confirm_apply_changes(paths: list[Path]) -> bool:
    if not paths:
        return True

    names = "、".join(str(path) for path in paths)
    if any(path.exists() for path in paths):
        prompt = f"以下文件将被写入或覆盖：{names}。是否继续？[y/N]: "
    else:
        prompt = f"以下文件将被写入：{names}。是否继续？[y/N]: "
    prompt = "\n" + format_highlighted_label(prompt, stream=sys.stderr)

    while True:
        answer = read_confirmation_answer(prompt)
        if answer is None:
            raise RuntimeError("需要交互确认，但当前无法读取确认输入。")
        normalized = answer.strip().lower()
        if normalized in {"y", "yes"}:
            return True
        if normalized in {"", "n", "no"}:
            return False
        print("请输入 y 或 n。", file=sys.stderr)


def read_diff_baseline(target_path: Path) -> tuple[str, str]:
    if not target_path.exists():
        return "/dev/null", ""
    if not target_path.is_file():
        raise ValueError(f"不是普通文件: {target_path}")
    return str(target_path), target_path.read_bytes().decode("utf-8", errors="replace")


def build_file_diff_from_content(
    target_label: str,
    fromfile: str,
    old_content: str,
    new_content: str,
) -> str:
    diff_lines = list(
        difflib.unified_diff(
            old_content.splitlines(),
            new_content.splitlines(),
            fromfile=fromfile,
            tofile=target_label,
            lineterm="",
        )
    )
    if not diff_lines:
        return f"--- {fromfile}\n+++ {target_label}\n@@ 无变更 @@"
    return "\n".join(diff_lines)


def get_diff_line_number_width(diff_text: str) -> int:
    max_line_number = 1
    for line in diff_text.splitlines():
        match = HUNK_HEADER_PATTERN.match(line)
        if not match:
            continue
        old_start = int(match.group("old_start"))
        old_count = int(match.group("old_count") or "1")
        new_start = int(match.group("new_start"))
        new_count = int(match.group("new_count") or "1")
        max_line_number = max(
            max_line_number,
            old_start + max(old_count - 1, 0),
            new_start + max(new_count - 1, 0),
        )
    return len(str(max_line_number))


def format_display_diff_line(
    line_number: int | None,
    marker: str,
    content: str,
    width: int,
) -> str:
    indent = " " * 4
    if line_number is None:
        line_number_text = " " * width
    else:
        line_number_text = str(line_number).rjust(width)
    return f"{indent}{line_number_text} {marker} {content}"


def render_diff_text(diff_text: str) -> str:
    rendered_lines: list[str] = []
    width = get_diff_line_number_width(diff_text)
    old_line_no = 0
    new_line_no = 0

    for line in diff_text.splitlines():
        if line.startswith(("--- ", "+++ ")):
            rendered_lines.append(format_display_diff_line(None, " ", line, width))
            continue
        if line == "@@ 无变更 @@":
            rendered_lines.append(format_display_diff_line(None, " ", line, width))
            continue

        match = HUNK_HEADER_PATTERN.match(line)
        if match:
            old_line_no = int(match.group("old_start"))
            new_line_no = int(match.group("new_start"))
            rendered_lines.append(format_display_diff_line(None, " ", line, width))
            continue

        if line.startswith("-"):
            rendered_lines.append(
                format_display_diff_line(old_line_no, "-", line[1:], width)
            )
            old_line_no += 1
            continue

        if line.startswith("+"):
            rendered_lines.append(
                format_display_diff_line(new_line_no, "+", line[1:], width)
            )
            new_line_no += 1
            continue

        if line.startswith(" "):
            rendered_lines.append(
                format_display_diff_line(new_line_no, " ", line[1:], width)
            )
            old_line_no += 1
            new_line_no += 1
            continue

        rendered_lines.append(format_display_diff_line(None, " ", line, width))

    return "\n".join(rendered_lines)


def use_color_output() -> bool:
    return use_color_stream(sys.stdout)


def colorize_diff_text(diff_text: str) -> str:
    if not use_color_output():
        return diff_text

    colored_lines: list[str] = []
    terminal_width = shutil.get_terminal_size(fallback=(80, 24)).columns
    for line in diff_text.splitlines():
        stripped = line.lstrip()
        marker_match = re.match(r"^\s*\d+\s([ +\-])\s", line)
        marker = marker_match.group(1) if marker_match else ""
        if stripped.startswith(("--- ", "+++ ")):
            colored_lines.append(f"{ANSI_DIM}{line}{ANSI_RESET}")
        elif stripped.startswith("@@"):
            colored_lines.append(f"{ANSI_CYAN}{line}{ANSI_RESET}")
        elif marker == "+":
            colored_lines.append(
                colorize_diff_change_line(
                    line,
                    terminal_width=terminal_width,
                    background=ANSI_BG_LIGHT_GREEN,
                    marker_color=ANSI_GREEN,
                )
            )
        elif marker == "-":
            colored_lines.append(
                colorize_diff_change_line(
                    line,
                    terminal_width=terminal_width,
                    background=ANSI_BG_LIGHT_RED,
                    marker_color=ANSI_RED,
                )
            )
        else:
            colored_lines.append(line)
    return "\n".join(colored_lines)


def colorize_diff_change_line(
    line: str,
    *,
    terminal_width: int,
    background: str,
    marker_color: str,
) -> str:
    match = re.match(r"^(\s*\d+)\s([+\-])\s(.*)$", line)
    if not match:
        trailing_spaces = max(0, terminal_width - measure_display_width(line))
        padded = f"{line}{' ' * trailing_spaces}"
        return f"{background}{ANSI_BLACK}{padded}{ANSI_RESET}"

    line_number = match.group(1)
    marker = match.group(2)
    content = match.group(3)
    plain_line = f"{line_number} {marker} {content}"
    trailing_spaces = max(0, terminal_width - measure_display_width(plain_line))
    return (
        f"{background}"
        f"{ANSI_GRAY}{line_number}"
        f"{ANSI_BLACK} "
        f"{marker_color}{marker}"
        f"{ANSI_BLACK} {content}"
        f"{' ' * trailing_spaces}"
        f"{ANSI_RESET}"
    )


def print_file_diffs(
    requests: list[FileWriteRequest],
    output_path: Path,
) -> None:
    if len(requests) != 1:
        raise RuntimeError("模型必须只返回一个文件块。")

    fromfile, old_content = read_diff_baseline(output_path)
    diff_text = build_file_diff_from_content(
        str(output_path),
        fromfile,
        old_content,
        requests[0].content,
    )
    print(format_highlighted_label("DiffView", stream=sys.stdout))
    print(colorize_diff_text(render_diff_text(diff_text)))


def build_result_report(
    report_text: str,
    output_path: Path,
    file_exists: bool,
    user_prompt: str,
) -> str:
    cleaned = report_text.strip()
    cleaned = re.sub(r"^\s*结果报告\s*[:：]?\s*\n*", "", cleaned, count=1)
    if cleaned:
        return cleaned

    action = "修改" if file_exists else "新建"
    reason = user_prompt or "满足当前文件生成或修改请求"
    return "\n".join(
        [
            f"操作类型：{action}",
            f"目标文件：{output_path}",
            f"说明：本次将{action}目标文件，具体内容以 DIFF 与最终文件为准。",
            f"原因：{reason}",
        ]
    )


def print_result_report(
    report_text: str,
    output_path: Path,
    file_exists: bool,
    user_prompt: str,
    elapsed_seconds: int,
) -> None:
    print()
    print(
        format_highlighted_label(
            f"OpsAI已为您处理完成，耗时{elapsed_seconds}秒。要点如下：",
            stream=sys.stdout,
        )
    )
    print(build_result_report(report_text, output_path, file_exists, user_prompt))


def save_generated_files(
    requests: list[FileWriteRequest],
    output_path: Path,
) -> list[FileWriteResult]:
    results: list[FileWriteResult] = []
    if len(requests) != 1:
        raise RuntimeError("模型必须只返回一个文件块。")

    if not confirm_apply_changes([output_path]):
        results.append(FileWriteResult(path=str(output_path), status="已取消"))
        return results

    if output_path.exists() and not output_path.is_file():
        raise ValueError(f"不是普通文件: {output_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path_text: str | None = None
    if output_path.exists():
        backup_path = output_path.with_name(
            f"{output_path.name}.{datetime.now().strftime('%Y%m%d%H%M%S')}.bak"
        )
        if backup_path.exists():
            raise RuntimeError(f"备份文件已存在: {backup_path}")
        output_path.rename(backup_path)
        backup_path_text = str(backup_path)

    output_path.write_text(requests[0].content, encoding="utf-8")
    results.append(
        FileWriteResult(
            path=str(output_path),
            status="已保存",
            backup_path=backup_path_text,
        )
    )
    return results


def build_assistant_history_content(reply: str, file_results: list[FileWriteResult]) -> str:
    parts: list[str] = []
    stripped_reply = reply.strip()
    if stripped_reply:
        parts.append(stripped_reply)
    if file_results:
        summary_lines = ["文件处理结果："]
        for result in file_results:
            summary_lines.append(f"- {result.path}：{result.status}")
        parts.append("\n".join(summary_lines))
    return "\n\n".join(parts).strip()


def build_runtime_context() -> str:
    available_commands = [
        command for command in COMMON_COMMANDS if shutil.which(command) is not None
    ]
    command_text = "、".join(available_commands) if available_commands else "未识别到常用运维命令"
    shell_name = Path(os.environ.get("SHELL", "")).name or "未知"

    return (
        "当前系统环境如下，请将其作为操作建议的背景信息：\n"
        f"- 当前时间: {datetime.now().astimezone().isoformat()}\n"
        f"- Shell 类型: {shell_name}\n"
        f"- 操作系统: {platform.system()} {platform.release()}\n"
        f"- 平台版本: {platform.platform()}\n"
        f"- 架构: {platform.machine()}\n"
        f"- Python: {platform.python_version()}\n"
        f"- 可用常用命令: {command_text}"
    )


def get_verify_value(config: AppConfig) -> bool | str:
    if not config.verify_ssl:
        return False
    if config.ca_file is not None:
        return str(config.ca_file)
    return True


def extract_content(payload: dict[str, object]) -> str:
    try:
        choices = payload["choices"]
    except KeyError as exc:
        raise RuntimeError(f"模型返回格式异常: {payload}") from exc

    if not isinstance(choices, list):
        raise RuntimeError(f"模型返回格式异常: {payload}")
    if not choices:
        return ""

    choice = choices[0]
    if not isinstance(choice, dict):
        raise RuntimeError(f"模型返回格式异常: {payload}")

    delta = choice.get("delta")
    if isinstance(delta, dict):
        content = delta.get("content", "")
        if isinstance(content, str):
            return content

    message = choice.get("message")
    if isinstance(message, dict):
        content = message.get("content", "")
        if isinstance(content, str):
            return content

    if choice.get("finish_reason") is not None:
        return ""

    raise RuntimeError(f"模型返回格式异常: {payload}")


def build_request_body(
    messages: list[dict[str, str]],
    model: str,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if extra:
        body.update(extra)
    return body


def call_model_non_stream(
    config: AppConfig,
    messages: list[dict[str, str]],
    *,
    print_reply: bool,
    extra_body: dict[str, object] | None = None,
    timer: ProcessingTimer | None = None,
) -> str:
    managed_timer = timer is None
    if timer is None:
        timer = ProcessingTimer()
    request_body = build_request_body(
        messages,
        config.model,
        extra=extra_body,
    )
    try:
        timer.start()
        response = requests.post(
            f"{config.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            json=request_body,
            timeout=config.timeout_seconds,
            verify=get_verify_value(config),
        )
        if managed_timer:
            timer.stop()
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.HTTPError as exc:
        timer.stop()
        detail = exc.response.text if exc.response is not None else str(exc)
        status_code = exc.response.status_code if exc.response is not None else "unknown"
        raise RuntimeError(f"模型调用失败，HTTP {status_code}: {detail}") from exc
    except requests.exceptions.SSLError as exc:
        timer.stop()
        raise RuntimeError(
            "模型调用失败: SSL 证书校验失败。"
            "如果服务使用自签名或私有 CA 证书，请在配置中设置 llm.ca_file；"
            "如果只是临时联调，可设置 llm.verify_ssl = false。"
        ) from exc
    except requests.exceptions.RequestException as exc:
        timer.stop()
        raise RuntimeError(f"模型调用失败: {exc}") from exc
    except json.JSONDecodeError as exc:
        timer.stop()
        raise RuntimeError(f"模型返回不是合法 JSON: {response.text}") from exc

    reply = extract_content(payload)
    if not reply:
        raise RuntimeError(f"模型返回格式异常: {payload}")
    if print_reply:
        print(reply)
    return reply


def detect_request_intent(
    config: AppConfig,
    user_prompt: str,
    target_path: Path,
    stdin_content: str,
    timer: ProcessingTimer | None = None,
) -> IntentDecision:
    reply = call_model_non_stream(
        config,
        build_intent_messages(
            user_prompt,
            target_path,
            target_path.exists(),
            stdin_content,
        ),
        print_reply=False,
        extra_body={"max_tokens": 80},
        timer=timer,
    )
    try:
        return parse_intent_decision(reply)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def main() -> int:
    args = parse_args()
    config_path = resolve_config_path(args.config)

    try:
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    if args.clear_context:
        clear_history(config.history_file)
        print("上下文已清除。")
        return 0

    max_input_chars = args.max_input_chars or config.max_input_chars
    if max_input_chars <= 0:
        print("错误: --max-input-chars 必须大于 0。", file=sys.stderr)
        return 1

    stdin_content = read_stdin_content()

    user_prompt = " ".join(args.prompt).strip()
    if not user_prompt:
        try:
            user_prompt = read_interactive_user_prompt() or ""
        except KeyboardInterrupt:
            return 130
    if not user_prompt and not args.file and not stdin_content:
        print("错误: 请输入自然语言需求，或使用 --clear-context 清除上下文。", file=sys.stderr)
        return 1
    if not user_prompt:
        print("错误: 未录入自然语言需求。", file=sys.stderr)
        return 1

    if user_prompt in CLEAR_COMMANDS and not args.file and not stdin_content:
        clear_history(config.history_file)
        print("上下文已清除。")
        return 0

    if not args.file:
        print("错误: -f/--file 为必填项，且必须只指定一次。", file=sys.stderr)
        return 1
    if len(args.file) != 1:
        print("错误: -f/--file 只能指定一次。", file=sys.stderr)
        return 1
    try:
        output_path = resolve_target_path(args.file[0])
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    try:
        processing_timer = ProcessingTimer()
        intent = detect_request_intent(
            config,
            user_prompt,
            output_path,
            stdin_content,
            timer=processing_timer,
        )
    except RuntimeError as exc:
        processing_timer.stop()
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    if intent.decision != "allow":
        processing_timer.stop()
        print(
            "错误: 当前工具只支持配置文件、脚本或代码文件的生成、修改、优化，并保存到 -f/--file 指定的单个目标文件。"
            f"识别结果: {intent.reason}",
            file=sys.stderr,
        )
        return 1

    try:
        attachments = build_attachments(
            output_path,
            stdin_content,
            max_input_chars,
        )
    except ValueError as exc:
        processing_timer.stop()
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    request_user_content, history_user_content = build_user_contents(
        user_prompt,
        output_path,
        attachments,
    )

    try:
        history_messages = trim_history(
            load_history(config.history_file),
            config.history_rounds,
        )
        try:
            reply = call_model_non_stream(
                config,
                build_messages(config.system_prompt, history_messages, request_user_content),
                print_reply=False,
                timer=processing_timer,
            )
        except Exception:
            processing_timer.stop()
            raise
    except (RuntimeError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    visible_reply, file_write_requests = extract_file_write_requests(reply)
    if not file_write_requests:
        processing_timer.stop()
        print("错误: 模型未返回可保存的文件内容。", file=sys.stderr)
        return 1
    processing_timer.stop()
    output_exists = output_path.exists()
    print_file_diffs(file_write_requests, output_path)
    print_result_report(
        visible_reply,
        output_path,
        output_exists,
        user_prompt,
        processing_timer.elapsed_seconds,
    )
    try:
        file_results = save_generated_files(file_write_requests, output_path)
    except (RuntimeError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    for result in file_results:
        if result.backup_path:
            print(f"已备份原文件: {result.backup_path}", file=sys.stderr)
        print(f"文件{result.status}: {result.path}", file=sys.stderr)

    updated_history = trim_history(
        [
            *history_messages,
            {"role": "user", "content": history_user_content},
            {
                "role": "assistant",
                "content": build_assistant_history_content(visible_reply, file_results),
            },
        ],
        config.history_rounds,
    )
    save_history(config.history_file, updated_history)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
