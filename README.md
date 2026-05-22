# OpsAI

OpsAI is a lightweight CLI assistant for ops engineers, using LLMs to generate and refine config, script, and code files with colored diffs, confirmation before write, and automatic backups.

OpsAI 是一个面向运维工程师的轻量级命令行助手，使用大模型生成和优化配置、脚本与代码文件，支持彩色 diff、写入前确认和自动备份。

## 功能

- 可按自然语言指令直接在服务器上直接生成/修改各类配置文件、脚本代码文件，提升运维效率
- 自动感知当前系统环境，无需在需求中描述
- 默认记忆最近 3 轮对话，可连续对话进行持续优化
- 生成的内容以彩色 `DiffView`方式展示，行号、插入与删除行以不同色彩展示，一目了然
- 对处理情况进行总结，清晰了解到本轮修改的思路
- 支持管道作为附加输入，可以补充背景信息，或参考资料
- 支持通过 `/clear`、或 `--clear-context` 清空上下文，重开对话

## 安装

```bash
python3 -m pip install -e .
cp config.example.toml config.toml
```

然后编辑 `config.toml`，填入你的模型网关地址、`api_key` 和模型名。

如果你不想安装到环境里，也可以直接在项目目录运行：

```bash
python3 opsai.py -f nginx.conf "生成一个最小 nginx 配置"
python3 -m opsai --config config.toml -f docker-compose.yml "优化这个 docker-compose.yml"
```

如果你从别的目录启动脚本，例如：

```bash
python3 /path/to/opsai.py -f nginx.conf "生成一个最小 nginx 配置"
```

程序会优先读取脚本同目录下的 `config.toml`，不依赖当前工作目录。

## 用法

```bash
python3 /path/to/opsai.py -f nginx.conf "生成一个最小 nginx 配置"
python3 /path/to/opsai.py -f nginx.conf "优化这个配置"
cat base.conf | python3 /path/to/opsai.py -f /tmp/Caddyfile "基于附加输入生成 Caddy 配置"
opsai -f app.service "生成一个最小 systemd unit"
opsai /clear
opsai --clear-context
opsai --config /path/to/config.toml -f /etc/myapp/deploy.yaml "优化这个 Kubernetes Deployment"
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

- 目标文件输入：`python3 opsai.py -f nginx.conf "优化这个 nginx 配置"`
- 管道输入：`cat base.conf | python3 opsai.py -f /tmp/nginx.conf "基于附加输入生成 nginx 配置"`

为避免请求体过大，程序会对这些外部输入应用 `max_input_chars` 预算。超出时：

- 当前轮请求只注入截断后的内容
- 历史记录里只保存摘要，不保存大段原文

## 文件保存

程序会把文件写入 `-f/--file` 指定的目标路径。

- `-f/--file` 为必填项，且只能指定一次
- 如果 `-f` 指向的文件已存在，则为修改模式，反之为新增模式；
- 只有在你确认后（需录入y,回车默认不保存）才真正写盘，落盘前会备份被覆盖文件

示例：

```bash
python3 opsai.py -f demo.conf "生成一个最小 nginx 配置"
python3 opsai.py -f /tmp/demo.sh "生成脚本"
python3 opsai.py -f ./configs/demo.conf "优化这个配置"
```

落盘前你会先看到类似下面的预览：

```text
DiffView
```

```diff
--- /dev/null
+++ /tmp/demo.conf
@@ -0,0 +1,2 @@
+server {
+    listen 80;
```

```text
OpsAI已为您处理完成，耗时2秒。要点如下：
操作类型：新建
- 要点：生成最小 nginx 配置。
- 内容：包含一个监听 80 端口的 server 块。
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
