# OpsAI

OpsAI is a lightweight CLI assistant for ops engineers, using LLMs to generate and refine config, script, and code files with colored diffs, confirmation before write, and automatic backups.

OpsAI 是一个面向运维工程师的轻量级命令行助手，使用大模型生成和优化配置、脚本与代码文件，支持彩色 diff、写入前确认和自动备份。

## 功能

- 可按自然语言指令直接在服务器上直接生成/修改各类配置文件、脚本代码文件，提升运维效率
- 自动感知当前系统环境，无需在需求中描述
- 默认记忆最近 3 轮对话，可连续对话进行持续优化
- 文件生成后，以彩色diff方式进行预览，包括行号，新增行与删除行提示，确定后保存
- 支持 `stdin` 和 `--file/-f` 读取外部内容作为参照内容
- 支持通过 `/clear`、或 `--clear-context` 清空上下文，重开对话

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

## 外部输入

支持两种方式把日志、配置、脚本、代码内容带进当前请求：

- 管道输入：`cat base.conf | python3 opsai.py -o nginx.conf "基于附加输入生成 nginx 配置"`
- 文件输入：`python3 opsai.py -f nginx.conf -f docker-compose.yml -o merged.conf "优化这些配置并保存为新的文件"`

为避免请求体过大，程序会对这些外部输入应用 `max_input_chars` 预算。超出时：

- 当前轮请求只注入截断后的内容
- 历史记录里只保存摘要，不保存大段原文

## 文件保存

程序会把文件写入**当前工作目录**。

- `--output/-o` 为输出文件名（必填），输出文件名只认这个参数
- 只允许保存为当前目录下的单个文件名，例如 `demo.conf`
- 如果指令里出现绝对路径、`..`、子目录等当前目录之外的目标，程序会直接拒绝
- 不管文件是否已存在，都会先展示类似 unified diff 的变更预览
- 如果本次通过 `-f` 或 `stdin` 提供了原始配置内容，diff 会优先以这些输入内容作为对比基线
- 如果没有外部输入，diff 会把原始内容视为空内容
- 只有在你确认后（需录入y,回车默认不保存）才真正写盘，落盘前会备份被覆盖文件
- 如果终端支持 ANSI 颜色，会用高亮新增、删除和 hunk 区域

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
