# Browser MCP Tools

浏览器自动化 MCP Server，提供 57 个工具覆盖浏览器控制、元素操作、网络抓包、设备模拟等场景。支持 stdio/SSE 双传输模式，可对接 Claude Desktop、Trae 等任意 MCP 客户端。

## 特性

- **57 个 MCP 工具**：浏览器控制、元素定位与操作、iframe 切换、文件上传、显式等待、网络抓包、鼠标键盘模拟、设备模拟等
- **双传输模式**：stdio（本地子进程）+ SSE（远程 HTTP，可供多客户端连接）
- **Chrome attach 模式**：通过远程调试端口复用已运行的 Chrome，登录态持久化
- **跨平台**：Windows / macOS / Linux，自动搜索 Chrome 安装路径
- **免虚拟环境**：基于 PEP 723 + uv，一行命令运行

## 快速开始

### 方式一：uv 运行（推荐，免虚拟环境）

**1. 安装 uv**（一次性，跨平台）

```bash
# Windows (PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**2. 获取代码并启动**

```bash
git clone https://github.com/demolute/browser_mcp_tools.git
cd browser_mcp_tools

# stdio 模式（本地 MCP 客户端调用）
uv run browser_mcp.py

# SSE 模式（远程 HTTP，供多客户端连接）
MCP_TRANSPORT=sse uv run browser_mcp.py
```

首次运行时 uv 会自动创建隔离环境并安装所有依赖，后续运行直接复用缓存。

### 方式二：传统虚拟环境

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
python browser_mcp.py
```

## 前置要求

- **Python** ≥ 3.10（uv 方式下 uv 会自动管理 Python 版本，无需手动装）
- **Google Chrome** 或 Chromium 内核浏览器（Edge / Chromium 均可）
  - 默认自动搜索标准安装路径
  - 非默认位置请配置 `CHROME_PATH`（见下文）

## 配置项说明

所有配置为可选项，通过环境变量或 `agent.env` 文件管理：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `CHROME_PATH` | 自动搜索 | 浏览器可执行文件路径 |
| `MCP_TRANSPORT` | `stdio` | 传输模式：`stdio` / `sse` / `streamable-http` |
| `MCP_HOST` | `127.0.0.1` | SSE/HTTP 模式监听地址 |
| `MCP_PORT` | `8765` | SSE/HTTP 模式监听端口 |
| `REMOTE_DEBUG_PORT` | `9222` | Chrome 远程调试端口 |

### CHROME_PATH 配置示例

仅在 Chrome 装在非默认位置或使用 Edge/Chromium 时需要：

```dotenv
# Windows - Chrome
CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe

# Windows - Edge
CHROME_PATH=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe

# macOS
CHROME_PATH=/Applications/Google Chrome.app/Contents/MacOS/Google Chrome

# Linux
CHROME_PATH=/usr/bin/google-chrome
```

### SSE 远程模式配置

```dotenv
MCP_TRANSPORT=sse
MCP_HOST=127.0.0.1   # 仅本机访问；局域网共享改为 0.0.0.0（注意安全风险）
MCP_PORT=8765
```

## 对接 MCP 客户端

### stdio 模式

适用于 Claude Desktop、Trae 等 MCP 客户端的本地子进程调用。所有配置项可直接写在 `env` 字段中：

```json
{
  "mcpServers": {
    "browser-mcp": {
      "command": "uv",
      "args": ["run", "/absolute/path/to/browser_mcp.py"],
      "env": {
        "CHROME_PATH": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
      }
    }
  }
}
```

**CHROME_PATH 各平台示例**：

| 平台 | 值 |
|------|-----|
| Windows - Chrome | `C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe` |
| Windows - Edge | `C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe` |
| macOS | `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` |
| Linux | `/usr/bin/google-chrome` |

> 不使用 uv 时，`command` 改为 Python 解释器路径，`args` 改为 `["/absolute/path/to/browser_mcp.py"]`。
> Windows 路径中的反斜杠在 JSON 里需转义为双反斜杠 `\\`。
> Chrome 装在默认位置时可省略 `CHROME_PATH`，会自动搜索。

### SSE 模式

先在终端启动 Server：

```bash
MCP_TRANSPORT=sse uv run browser_mcp.py
# 输出: [browser_mcp] 以 sse 模式启动，连接端点: http://127.0.0.1:8765/sse
```

然后在 MCP 客户端配置中填入 URL：

```json
{
  "mcpServers": {
    "browser-mcp": {
      "url": "http://127.0.0.1:8765/sse"
    }
  }
}
```

## 工具列表

共 57 个工具，按类别分组：

| 类别 | 工具示例 | 说明 |
|------|----------|------|
| 浏览器控制 | `open_browser`, `close_browser`, `navigate`, `back`, `forward`, `refresh` | 打开/关闭/导航 |
| 窗口管理 | `maximize_window`, `minimize_window`, `set_window_size`, `fullscreen_window`, `open_new_window`, `switch_window`, `close_window` | 窗口操作 |
| 元素定位 | `get_element_text`, `get_element_attribute`, `wait_for_element`, `wait_for_element_visible`, `wait_for_clickable` | 查找与等待 |
| 元素操作 | `click_element`, `input_text`, `clear_input`, `double_click`, `right_click`, `hover` | 交互操作 |
| iframe | `switch_to_frame`, `switch_to_nested_frame`, `switch_to_default` | 嵌套 iframe 切换 |
| 文件上传 | `upload_file`, `upload_file_direct` | 绕过系统对话框 |
| 等待机制 | `wait_for_text`, `wait_until`, `wait_for_page_load`, `set_implicit_wait` | 显式等待 |
| 弹窗处理 | `get_alert_text`, `accept_alert`, `dismiss_alert`, `send_alert_text` | Alert 交互 |
| Cookie | `get_cookie`, `add_cookie`, `delete_cookie`, `delete_all_cookies` | Cookie 管理 |
| 鼠标操作 | `drag_and_drop`, `drag_and_drop_by_offset`, `click_and_hold`, `release`, `move_mouse_by_offset`, `scroll_page` | 鼠标模拟 |
| 键盘操作 | `type_text`, `press_key`, `press_keys`, `key_down`, `key_up` | 键盘模拟（含组合键） |
| 网络抓包 | `start_network_capture`, `stop_network_capture`, `clear_network_log`, `get_network_requests`, `get_network_request_detail`, `get_network_response_body` | CDP 网络监听 |
| 页面信息 | `get_page_info`, `get_page_source`, `take_screenshot` | 页面快照 |
| 设备模拟 | `set_device_metrics`, `clear_device_metrics`, `emulate_network_conditions`, `clear_network_conditions`, `set_geolocation` | 设备/网络/位置模拟 |
| 脚本执行 | `execute_script`, `get_element_style`, `set_element_style`, `get_element_box`, `get_element_attributes`, `set_element_attribute` | JS 执行与样式 |

完整工具说明详见 [MCP工具快速使用指南.md](MCP工具快速使用指南.md)。

## 常见问题

### 启动时报 "未找到 Chrome"

Chrome 装在非默认位置，或使用 Edge/Chromium。配置 `CHROME_PATH` 指向浏览器可执行文件：

```dotenv
CHROME_PATH=/path/to/your/browser
```

### SSE 模式下客户端无法连接

1. 确认 Server 已启动且输出 `Uvicorn running on http://...`
2. 确认端口未被占用：`netstat -an | findstr 8765`
3. 确认防火墙未拦截端口
4. 局域网访问需配置 `MCP_HOST=0.0.0.0`

### Chrome 会被关闭吗

不会。Chrome 以独立进程运行，MCP Server 退出后 Chrome 继续运行。下次启动 Server 时通过远程调试端口自动 attach 回来，登录状态从固定用户数据目录恢复。

### 如何更新到最新版本

```bash
git pull
uv run browser_mcp.py   # uv 会自动安装新增依赖
```

## 技术细节

- **Chrome attach 模式**：通过 `--remote-debugging-port=9222` 启动 Chrome，Selenium 以 `debuggerAddress` 方式 attach，避免重新启动浏览器实例
- **登录态持久化**：固定用户数据目录 `~/.browser_mcp_chrome_profile`，即使浏览器关闭后重新打开，cookie/session 仍保留
- **网络抓包**：独立 WebSocket 连接到 CDP，与 Selenium driver 解耦，避免同线程死锁
- **JS 事件模拟**：Chrome 150+ attach 模式下原生 `click()`/`send_keys()` 被禁用，改用 `dispatchEvent` 模拟鼠标键盘事件

## 许可

MIT
