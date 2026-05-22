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
    "只判断用户请求是否属于“配置文件、脚本或代码文件的生成、修改、优化，并预期保存为当前目录文件”。\n"
    "允许的例子：生成 nginx.conf、修改现有 docker-compose.yml、优化 Kubernetes YAML、重写 systemd unit、补全 toml/ini/json/yaml 配置、编写 bash/python 脚本、修改 .py/.sh/.js/.go 等代码文件。\n"
    "拒绝的例子：日志分析、故障排查建议、命令解释、通用问答、保存到当前目录之外的路径。\n"
    "输出必须是严格 JSON，格式：{\"decision\":\"allow\"|\"reject\",\"reason\":\"一句简短原因\"}"
)
FILE_OUTPUT_PROMPT = (
    "对于允许的请求，你必须在回答末尾追加且只追加一个文件块，严格使用如下格式：\n"
    "<opsai_write_file>\n"
    "文件内容\n"
    "</opsai_write_file>\n"
    "限制：\n"
    "1. 不要输出文件名，文件名由命令行参数 `--output` 指定。\n"
    "2. 如果用户要求修改已有文件，请输出修改后的完整文件内容，不要输出 diff。\n"
    "3. 不要输出与文件生成、修改、优化无关的建议性内容。"
)
CLEAR_COMMANDS = {"/clear", "clear", "清除上下文"}
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
    name: str
    status: str
    backup_name: str | None = None


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
        self._started = False

    def start(self) -> None:
        if not should_render_processing_timer():
            return
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._started:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        print("\r" + " " * 32 + "\r", end="", file=sys.stderr, flush=True)
        self._started = False

    def _run(self) -> None:
        started_at = time.monotonic()
        while not self._stop_event.is_set():
            elapsed = max(0, int(time.monotonic() - started_at))
            print(
                format_processing_status(elapsed),
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
        help="读取文件内容并一并发送给模型，可重复使用。",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="输出文件名，只允许当前目录下的单个文件名。",
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
    output_name: str,
    file_paths: list[str],
    stdin_content: str,
) -> list[dict[str, str]]:
    file_names = [Path(path).name for path in file_paths]
    details = [
        f"用户需求: {user_prompt}",
        f"目标输出文件: {output_name}",
        f"附加文件名: {', '.join(file_names) if file_names else '无'}",
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


def read_file_content(file_path: str) -> tuple[str, str]:
    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        raise ValueError(f"文件不存在: {path}")
    if not path.is_file():
        raise ValueError(f"不是普通文件: {path}")
    return path.name, path.read_bytes().decode("utf-8", errors="replace")


def read_stdin_content() -> str:
    if sys.stdin.isatty():
        return ""
    return sys.stdin.read()


def build_attachments(
    file_paths: list[str],
    stdin_content: str,
    max_input_chars: int,
) -> list[InputAttachment]:
    sources: list[tuple[str, str]] = []
    for file_path in file_paths:
        name, content = read_file_content(file_path)
        sources.append((f"文件: {name}", content))
    if stdin_content:
        sources.append(("stdin", stdin_content))

    attachments: list[InputAttachment] = []
    remaining = max_input_chars
    for label, content in sources:
        original_chars = len(content)
        truncated_content, truncated = truncate_text(content, remaining)
        injected_chars = len(truncated_content)
        omitted = original_chars > 0 and injected_chars == 0
        if injected_chars > 0:
            remaining -= injected_chars
        attachments.append(
            InputAttachment(
                source_name=name if label.startswith("文件: ") else "stdin",
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
    output_name: str,
    attachments: list[InputAttachment],
) -> tuple[str, str]:
    prefix = f"目标输出文件: {output_name}\n用户需求: {user_prompt}"
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


def normalize_current_dir_filename(name: str) -> str:
    candidate = name.strip().strip("\"'`")
    candidate = candidate.replace("\\", "/")
    if candidate.startswith("./"):
        candidate = candidate[2:]
    if not candidate or candidate in {".", ".."}:
        raise ValueError(f"非法文件名: {name}")
    if candidate.startswith("/") or candidate.startswith("../") or candidate.startswith("~"):
        raise ValueError(f"拒绝写入当前目录之外的路径: {name}")
    if re.match(r"^[A-Za-z]:/", candidate):
        raise ValueError(f"拒绝写入当前目录之外的路径: {name}")
    if "/" in candidate:
        raise ValueError(f"拒绝写入当前目录之外的路径: {name}")
    return candidate


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


def confirm_apply_changes(paths: list[Path]) -> bool:
    if not paths:
        return True

    names = "、".join(path.name for path in paths)
    if any(path.exists() for path in paths):
        prompt = f"以下文件将被写入或覆盖：{names}。是否继续？[y/N]: "
    else:
        prompt = f"以下文件将被写入：{names}。是否继续？[y/N]: "

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


def select_diff_baseline(
    output_path: Path,
    attachments: list[InputAttachment],
) -> tuple[str, str] | None:
    if not attachments:
        return "/dev/null", ""

    exact_name_matches = [
        attachment
        for attachment in attachments
        if attachment.source_name == output_path.name
    ]
    if len(exact_name_matches) == 1:
        attachment = exact_name_matches[0]
        return f"a/{attachment.source_name}", attachment.full_content

    file_attachments = [
        attachment
        for attachment in attachments
        if attachment.source_name != "stdin"
    ]
    if len(file_attachments) == 1 and len(attachments) == 1:
        attachment = file_attachments[0]
        return f"a/{attachment.source_name}", attachment.full_content

    if len(attachments) == 1 and attachments[0].source_name == "stdin":
        attachment = attachments[0]
        return "a/stdin", attachment.full_content

    return None


def build_file_diff_from_content(
    target_name: str,
    fromfile: str,
    old_content: str,
    new_content: str,
) -> str:
    diff_lines = list(
        difflib.unified_diff(
            old_content.splitlines(),
            new_content.splitlines(),
            fromfile=fromfile,
            tofile=f"b/{target_name}",
            lineterm="",
        )
    )
    if not diff_lines:
        return f"--- {fromfile}\n+++ b/{target_name}\n@@ 无变更 @@"
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
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


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
        padded = line.ljust(terminal_width)
        return f"{background}{ANSI_BLACK}{padded}{ANSI_RESET}"

    line_number = match.group(1)
    marker = match.group(2)
    content = match.group(3)
    plain_line = f"{line_number} {marker} {content}"
    trailing_spaces = max(0, terminal_width - len(plain_line))
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
    attachments: list[InputAttachment],
) -> None:
    if len(requests) != 1:
        raise RuntimeError("模型必须只返回一个文件块。")

    fromfile, old_content = select_diff_baseline(output_path, attachments)
    diff_text = build_file_diff_from_content(
        output_path.name,
        fromfile,
        old_content,
        requests[0].content,
    )
    print(colorize_diff_text(render_diff_text(diff_text)))


def save_generated_files(
    requests: list[FileWriteRequest],
    output_path: Path,
) -> list[FileWriteResult]:
    results: list[FileWriteResult] = []
    if len(requests) != 1:
        raise RuntimeError("模型必须只返回一个文件块。")

    if not confirm_apply_changes([output_path]):
        results.append(FileWriteResult(name=output_path.name, status="已取消"))
        return results

    backup_name: str | None = None
    if output_path.exists():
        backup_name = f"{output_path.name}.{datetime.now().strftime('%Y%m%d%H%M%S')}.bak"
        backup_path = output_path.with_name(backup_name)
        if backup_path.exists():
            raise RuntimeError(f"备份文件已存在: {backup_name}")
        output_path.rename(backup_path)

    output_path.write_text(requests[0].content, encoding="utf-8")
    results.append(
        FileWriteResult(
            name=output_path.name,
            status="已保存",
            backup_name=backup_name,
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
            summary_lines.append(f"- {result.name}：{result.status}")
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
    output_name: str,
    file_paths: list[str],
    stdin_content: str,
    timer: ProcessingTimer | None = None,
) -> IntentDecision:
    reply = call_model_non_stream(
        config,
        build_intent_messages(user_prompt, output_name, file_paths, stdin_content),
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
    if not user_prompt and not args.file and not stdin_content:
        print("错误: 请输入自然语言需求，或使用 --clear-context 清除上下文。", file=sys.stderr)
        return 1
    if not user_prompt:
        user_prompt = "请基于附加输入生成或优化文件，并保存为当前目录文件。"

    if user_prompt in CLEAR_COMMANDS and not args.file and not stdin_content:
        clear_history(config.history_file)
        print("上下文已清除。")
        return 0

    if not args.output:
        print("错误: --output/-o 为必填项。", file=sys.stderr)
        return 1
    try:
        output_filename = normalize_current_dir_filename(args.output)
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    output_path = Path.cwd() / output_filename

    try:
        processing_timer = ProcessingTimer()
        intent = detect_request_intent(
            config,
            user_prompt,
            output_filename,
            args.file,
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
            "错误: 当前工具只支持配置文件、脚本或代码文件的生成、修改、优化，并保存为当前目录文件。"
            f"识别结果: {intent.reason}",
            file=sys.stderr,
        )
        return 1

    try:
        attachments = build_attachments(
            args.file,
            stdin_content,
            max_input_chars,
        )
    except ValueError as exc:
        processing_timer.stop()
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    request_user_content, history_user_content = build_user_contents(
        user_prompt,
        output_filename,
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
    print_file_diffs(file_write_requests, output_path, attachments)
    try:
        file_results = save_generated_files(file_write_requests, output_path)
    except (RuntimeError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    for result in file_results:
        if result.backup_name:
            print(f"已备份原文件: {result.backup_name}", file=sys.stderr)
        print(f"文件{result.status}: {result.name}", file=sys.stderr)

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
