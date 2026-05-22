# OpsAI

OpsAI is a lightweight CLI assistant for ops engineers, using LLMs to generate and refine config, script, and code files with colored diffs, confirmation before write, and automatic backups.

OpsAI 是一个面向运维工程师的轻量级命令行助手，使用大模型生成和优化配置、脚本与代码文件，支持彩色 diff、写入前确认和自动备份。

一个 Python 命令行工具：只做配置文件、脚本和代码文件的生成、修改和优化，并把结果保存为当前目录文件。

## 功能

- 从 `config.toml` 读取模型配置
- 只接受配置文件、脚本、代码文件的生成、修改、优化类请求
- 先做一轮低成本意图识别，不符合定位的请求直接拒绝
- 控制台等待生成完成后再展示说明与 diff 预览
- 请求等待期间会在 `stderr` 显示 `处理中(XX秒)` 动态计时
- 默认保留最近 3 轮对话，可配置
- 每次请求自动采集当前系统环境摘要并注入上下文
- 支持 `stdin` 和 `--file/-f` 读取外部内容
- 用字符预算近似控制大日志/大配置的输入体积
- 可按自然语言指令把生成结果保存为当前目录文件
- 落盘前先展示 diff，并且必须人工确认
- 支持通过 `/clear`、`clear`、`清除上下文` 或 `--clear-context` 清空上下文
- 使用 `requests` 发起 HTTPS 请求

## 安装

```bash
python3 -m pip install -e .
cp config.example.toml config.toml
```

然后编辑 `config.toml`，填入你的模型网关地址、`api_key` 和模型名。

如果你不想安装到环境里，也可以直接在项目目录运行：

```bash
python3 opsai.py -o nginx.conf "生成一个最小 nginx 配置"
python3 -m opsai --config config.toml -o docker-compose.optimized.yml "基于附带文件优化 docker-compose.yml"
```

如果你从别的目录启动脚本，例如：

```bash
python3 /path/to/opsai.py -o nginx.conf "生成一个最小 nginx 配置"
```

程序会优先读取脚本同目录下的 `config.toml`，不依赖当前工作目录。

## 用法

```bash
python3 /path/to/opsai.py -o nginx.conf "生成一个最小 nginx 配置"
python3 /path/to/opsai.py -f nginx.conf -o nginx.optimized.conf "优化这个配置"
cat base.conf | python3 /path/to/opsai.py -o Caddyfile "基于附加输入生成 Caddy 配置"
opsai -o app.service "生成一个最小 systemd unit"
opsai "/clear"
opsai --clear-context
opsai --config /path/to/config.toml -o deploy.optimized.yaml "基于附带 YAML 优化 Kubernetes Deployment"
```

## 配置说明

```toml
[llm]
base_url = "https://your-openai-compatible-endpoint/v1"
api_key = "your-api-key"
model = "your-model-name"
system_prompt = "你是一位文件工程助手，只负责配置文件、脚本和代码文件的生成、修改和优化。对允许的请求，你必须产出可直接落盘的完整文件内容。不要回答与文件生成、修改、优化无关的需求。"
timeout_seconds = 60
verify_ssl = true
# ca_file = "/path/to/your/ca.pem"

[memory]
history_rounds = 3
history_file = ".opsai_history.json"

[input]
max_input_chars = 12000
```

- `base_url`：OpenAI 兼容接口地址，程序会请求 `${base_url}/chat/completions`
- `verify_ssl`：是否校验 HTTPS 证书，默认 `true`
- `ca_file`：自定义 CA 证书文件路径，适用于公司内网网关、自签名证书或私有 CA
- `history_rounds`：保留最近多少轮对话，1 轮等于 1 条用户消息 + 1 条模型回复
- `history_file`：上下文存储文件路径；相对路径相对于配置文件目录解析
- `max_input_chars`：当前轮附加输入的字符预算，用字符数近似控制 token；超出时会自动截断

## 意图识别

为节省 token，工具会先做一轮低成本意图识别，只发送：

- 用户需求
- `--output/-o` 指定的目标输出文件名
- 附加文件名
- 是否提供了 `stdin`

只有当请求被识别为“配置文件、脚本或代码文件的生成、修改、优化，并保存为当前目录文件”时，才会继续把完整附件内容送入主模型。其他请求会直接拒绝。

另外，输出文件名不再从自然语言中提取，必须通过 `--output/-o` 显式指定。

## 外部输入

支持两种方式把日志、配置、脚本、代码内容带进当前请求：

- 管道输入：`cat base.conf | python3 opsai.py -o nginx.conf "基于附加输入生成 nginx 配置"`
- 文件输入：`python3 opsai.py -f nginx.conf -f docker-compose.yml -o merged.conf "优化这些配置并保存为新的文件"`

为避免请求体过大，程序会对这些外部输入应用 `max_input_chars` 预算。超出时：

- 当前轮请求只注入截断后的内容
- 历史记录里只保存摘要，不保存大段原文
- 如果意图识别阶段已经判定为不支持，则不会发送这些大段内容

## 文件保存

允许请求必须产出结构化文件块，程序会把文件写入**当前工作目录**。

- `--output/-o` 为必填，输出文件名只认这个参数
- 只允许保存为当前目录下的单个文件名，例如 `demo.conf`
- 如果指令里出现绝对路径、`..`、子目录等当前目录之外的目标，程序会直接拒绝
- 不管文件是否已存在，都会先展示类似 unified diff 的变更预览
- 如果本次通过 `-f` 或 `stdin` 提供了原始配置内容，diff 会优先以这些输入内容作为对比基线
- 如果没有外部输入，diff 会把原始内容视为空内容
- 只有在你确认后才真正写盘
- 如果终端支持 ANSI 颜色，会用接近 Codex 的方式高亮新增、删除和 hunk 区域

示例：

```bash
python3 opsai.py -o demo.conf "生成一个最小 nginx 配置"
python3 opsai.py -o /tmp/demo.sh "生成脚本"   # 会被拒绝，原因是路径不在当前目录
```

落盘前你会先看到类似下面的预览：

```diff
--- /dev/null
+++ b/demo.conf
@@ -0,0 +1,2 @@
+server {
+    listen 80;
```

## 上下文注入

每次发起请求时，程序都会额外注入一段当前系统环境摘要，作为模型判断的背景信息。当前包含：

- 当前时间
- Shell 类型
- 操作系统、平台版本、架构
- Python 版本
- 本机已安装的常用运维命令摘要

程序不会发送主机名、当前用户和环境变量，只发送上述摘要字段。

## 证书问题

如果你看到类似 `CERTIFICATE_VERIFY_FAILED` 的报错，通常有两种处理方式：

1. 推荐做法：把网关使用的根证书或 CA 证书保存为 PEM 文件，并在配置中设置 `llm.ca_file`
2. 临时联调：把 `llm.verify_ssl` 设置为 `false`

示例：

```toml
[llm]
base_url = "https://your-openai-compatible-endpoint/v1"
api_key = "your-api-key"
model = "your-model-name"
verify_ssl = false
```
