import base64
import json
import os
import re
import threading
import urllib.request
from pathlib import Path

import websocket
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from websocket import (
    WebSocketConnectionClosedException,
    WebSocketTimeoutException,
    create_connection,
)

# 加载配置（与 agent.py 共用 agent.env）
load_dotenv(Path(__file__).parent / "agent.env")

mcp = FastMCP()

# 持有 browser driver 引用，避免函数返回后被 GC 导致 Chrome 被关闭
_browser_driver = None

# 当前 iframe 信息：用于在 iframe 内操作元素
# None 表示在主 DOM 中，字符串表示 iframe 的 id/name，数字表示索引
# 支持嵌套 iframe：用 ">" 分隔，如 "outer>inner"
_current_frame = None  # 可以是 str、int、None 或嵌套 str 如 "outer>inner"

# 固定的 Chrome 用户数据目录，持久化登录状态（cookie/session）
# 即使浏览器关闭后重新打开，登录状态也会从磁盘恢复
_CHROME_USER_DATA_DIR = str(Path.home() / ".browser_mcp_chrome_profile")

# 远程调试端口，agent 重启后通过此端口 attach 到已运行的 Chrome
_REMOTE_DEBUG_PORT = 9222

# ---------------------------------------------------------------------------
# Network 抓取状态（后台线程 + 单 WebSocket 连接，独立于 Selenium）
# ---------------------------------------------------------------------------
# requestId -> 请求详情字典。每条记录字段:
#   requestId, url, method, resourceType, requestHeaders, postData,
#   status, statusText, responseHeaders, mimeType, remoteIPAddress, protocol,
#   encodedDataLength, finished, failed, errorText,
#   timestamp, responseTimestamp, finishedTimestamp
_network_requests: dict = {}
_network_lock = threading.Lock()             # 保护 _network_requests 与 _pending_commands 与 send
_network_ws = None                           # 持久 WebSocket 连接
_network_thread = None                       # 后台监听线程
_network_stop_event = threading.Event()      # 通知监听线程退出
_network_active = False                      # 是否正在监听

# CDP 命令-响应匹配（同一 WebSocket 上同步发送命令时使用）
_cdp_id_counter = 0
_cdp_id_lock = threading.Lock()
_pending_commands: dict = {}                 # id -> {"event": Event, "result": None, "error": None}

# 抓取过滤条件（在 start_network_capture 时设置，事件处理时读取）
_network_filter_url = ""
_network_filter_resource_type = ""

# 限制常量
_MAX_BODY_BYTES = 1_000_000                  # 单个响应体最大字节数
_MAX_DETAIL_CHARS = 50_000                   # 单条请求详情最大字符数


def _is_chrome_running() -> bool:
    """检查远程调试端口是否有 Chrome 在监听（即 Chrome 仍在运行）。"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", _REMOTE_DEBUG_PORT)) == 0


def _find_chrome() -> str | None:
    """查找 Chrome 可执行文件路径。

    优先级：
    1. 环境变量 CHROME_PATH（用户显式指定，适合 Chrome 装在非默认位置或使用 Edge/Chromium）
    2. 各平台默认安装路径（Windows/macOS/Linux）
    """
    # 1) 用户通过环境变量显式指定
    env_path = os.environ.get("CHROME_PATH", "").strip().strip('"').strip("'")
    if env_path:
        if Path(env_path).exists():
            return env_path
        # 路径配置错误时给出明确提示，避免静默回退导致困惑
        print(f"[browser_mcp] 警告: CHROME_PATH 指定的路径不存在: {env_path}，将回退到默认搜索")

    # 2) 各平台默认安装路径
    candidates = [
        # Windows
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe"),
        # macOS
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        # Linux（包管理器安装的常见路径）
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    return None


def _attach_chrome():
    """通过远程调试端口 attach 到已运行的 Chrome，返回 driver 或 None。"""
    options = webdriver.ChromeOptions()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{_REMOTE_DEBUG_PORT}")
    # MCP 子进程下需显式指定 chromedriver 路径（selenium-manager 可能失效）
    driver_path = _find_chromedriver()
    service = ChromeService(executable_path=driver_path) if driver_path else None
    try:
        return webdriver.Chrome(service=service, options=options)
    except Exception:
        return None


def _find_chromedriver() -> str | None:
    """从 selenium 缓存目录查找已下载的 chromedriver。

    MCP stdio 子进程环境下 selenium-manager 可能无法正常获取 chromedriver
    （子进程 stdin/stdout 被 MCP 协议占用），显式指定路径可绕过此问题。
    """
    cache = Path.home() / ".cache" / "selenium" / "chromedriver"
    drivers = sorted(cache.glob("**/chromedriver.exe"))
    return str(drivers[-1]) if drivers else None


def _rule_based_url(intent: str) -> str:
    """LLM 不可用时的规则映射 fallback。"""
    intent_lower = intent.lower()
    # intent 本身就是 URL 时直接返回
    if intent_lower.startswith(("http://", "https://")):
        return intent
    rules = {
        "b站": "https://www.bilibili.com",
        "bilibili": "https://www.bilibili.com",
        "github": "https://github.com",
        "新闻": "https://news.baidu.com",
        "百度": "https://www.baidu.com",
        "google": "https://www.google.com",
        "youtube": "https://www.youtube.com",
        "淘宝": "https://www.taobao.com",
        "京东": "https://www.jd.com",
        "知乎": "https://www.zhihu.com",
        "微博": "https://weibo.com",
        "菜鸟教程": "https://www.runoob.com",
    }
    for key, url in rules.items():
        if key in intent_lower:
            return url
    # 兜底：百度搜索
    return f"https://www.baidu.com/s?wd={intent}"


def _infer_url(intent: str) -> str:
    """用模型把用户需求映射到目标 URL。

    Args:
        intent: 用户的自然语言需求，例如"我想看新闻"、"打开 GitHub"

    Returns:
        可直接访问的完整 URL
    """
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        model=os.environ["MODEL_NAME"],
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.environ["OPENAI_BASE_URL"],
        temperature=0,  # URL 推断要确定性
    )

    # system prompt 放在消息列表开头（符合 project_memory 约定）
    system = SystemMessage(content=(
        "你是 URL 推断助手。根据用户需求输出一个可直接访问的完整 URL。\n"
        "只输出 URL 本身，不要任何解释、前缀或 markdown 标记。\n"
        "如果需求明确指向某个网站，直接输出该网站首页 URL。\n"
        "如果需求是搜索类，输出百度搜索 URL：https://www.baidu.com/s?wd=关键词\n"
        "如果需求不明确，默认用百度搜索该需求。\n"
        "示例：\n"
        '需求"我想看新闻" -> https://news.baidu.com\n'
        '需求"打开 GitHub" -> https://github.com\n'
        '需求"搜索 Python 教程" -> https://www.baidu.com/s?wd=Python%20教程\n'
    ))
    try:
        response = llm.invoke([system, HumanMessage(content=intent)])
        # 提取第一个 http(s) URL，防止模型输出多余文字
        match = re.search(r"https?://\S+", response.content)
        if match:
            return match.group(0)
    except Exception:
        # LLM 调用失败（如 API 限流），降级到规则映射
        pass
    return _rule_based_url(intent)


def _ensure_frame():
    """确保 driver 在正确的 iframe 上下文中。

    支持嵌套 iframe：_current_frame 可以是 "outer>inner" 格式。
    """
    global _current_frame
    if _browser_driver is None:
        return False
    try:
        _browser_driver.switch_to.default_content()
        if _current_frame is not None:
            # 解析嵌套 iframe 路径
            parts = str(_current_frame).split(">")
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                try:
                    target: str | int = int(part)
                except ValueError:
                    target = part
                _browser_driver.switch_to.frame(target)
        return True
    except Exception:
        try:
            _browser_driver.switch_to.default_content()
            _current_frame = None
        except Exception:
            pass
        return False


@mcp.tool()
def switch_to_frame(frame: str) -> str:
    """切换到指定的 iframe，后续的元素操作将在该 iframe 内进行。

    对应 Selenium 的 driver.switch_to.frame()。

    Args:
        frame: iframe 的 id、name 属性值，或索引数字（字符串形式，如 "0"、"1"）
               例如 "vw-video-up-frame" 表示切换到 id="vw-video-up-frame" 的 iframe
    """
    global _current_frame
    if _browser_driver is None:
        return "浏览器未打开，请先调用 open_browser"
    # 尝试解析为数字索引
    try:
        frame_target: str | int = int(frame)
    except ValueError:
        frame_target = frame
    try:
        _browser_driver.switch_to.default_content()  # 先回到主 DOM
        _browser_driver.switch_to.frame(frame_target)
        _current_frame = frame_target
        return f"已切换到 iframe: {frame}"
    except Exception as e:
        return f"切换 iframe 失败: {e}"


@mcp.tool()
def switch_to_nested_frame(frames: str) -> str:
    """逐层切换到嵌套 iframe 内部。

    Args:
        frames: iframe id/name 列表，用逗号分隔，从外到内。
                例如 "vw-video-up-frame,my-inner-frame" 表示先切换到外层，再切换到内层
    """
    global _current_frame
    if _browser_driver is None:
        return "浏览器未打开，请先调用 open_browser"
    frame_list = [f.strip() for f in frames.split(",") if f.strip()]
    if not frame_list:
        return "iframe 列表不能为空"
    try:
        _browser_driver.switch_to.default_content()
        current = None
        for i, frame in enumerate(frame_list):
            try:
                target: str | int = int(frame)
            except ValueError:
                target = frame
            _browser_driver.switch_to.frame(target)
            current = target
        # 保存嵌套路径
        _current_frame = ">".join(frame_list)
        return f"已切换到嵌套 iframe: {' > '.join(frame_list)}"
    except Exception as e:
        return f"切换嵌套 iframe 失败 (第 {i+1} 层 '{frame}'): {e}"


@mcp.tool()
def switch_to_default() -> str:
    """从 iframe 切换回主文档（driver.switch_to.default_content()）。"""
    global _current_frame
    if _browser_driver is None:
        return "浏览器未打开"
    _browser_driver.switch_to.default_content()
    _current_frame = None
    return "已切换回主文档"


# ---------------------------------------------------------------------------
# 浏览器工具集（对应 runoob Selenium WebDriver 文档的操作）
# ---------------------------------------------------------------------------

# 元素定位方式映射（对应 runoob 元素定位文档的 8 种 By 方式）
_BY_MAP = {
    "id": By.ID,
    "name": By.NAME,
    "class_name": By.CLASS_NAME,
    "tag_name": By.TAG_NAME,
    "css_selector": By.CSS_SELECTOR,
    "xpath": By.XPATH,
    "link_text": By.LINK_TEXT,
    "partial_link_text": By.PARTIAL_LINK_TEXT,
}


def _find_element(by: str, value: str, wait_clickable: bool = False):
    """根据定位方式和值查找元素。

    Args:
        wait_clickable: True 时用显式等待等元素可交互（用于点击/输入），
                        False 时只等元素出现（用于获取文本/属性）。

    Returns:
        (element, None) 成功，或 (None, error_msg) 失败
    """
    if _browser_driver is None:
        return None, "浏览器未打开，请先调用 open_browser"
    # 确保在正确的 iframe 上下文中
    if not _ensure_frame():
        return None, "iframe 切换失败"
    by_method = _BY_MAP.get(by)
    if by_method is None:
        return None, f"不支持的定位方式: {by}，可选: {', '.join(_BY_MAP.keys())}"
    try:
        # 显式等待（对应 runoob 文档的等待机制最佳实践）
        # 注意：不与 implicitly_wait 混用，否则会导致不可预测的超时
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        if wait_clickable:
            # 等元素可见且可交互（用于点击/输入）
            try:
                element = WebDriverWait(_browser_driver, 10).until(
                    EC.element_to_be_clickable((by_method, value))
                )
            except Exception:
                # 降级：页面可能有多个同名元素（如百度首页有隐藏的 kw），
                # 找到所有匹配元素，选第一个可见且有尺寸的，并滚动到视口
                elements = _browser_driver.find_elements(by_method, value)
                element = None
                for el in elements:
                    if el.is_displayed() and el.size.get("width", 0) > 0:
                        _browser_driver.execute_script(
                            "arguments[0].scrollIntoView({block:'center'});", el
                        )
                        element = el
                        break
                if element is None:
                    raise Exception("所有匹配元素都不可见")
        else:
            # 只等元素出现在 DOM 中（用于获取文本/属性）
            element = WebDriverWait(_browser_driver, 10).until(
                EC.presence_of_element_located((by_method, value))
            )
        return element, None
    except Exception as e:
        return None, f"未找到/不可交互元素 (by={by}, value={value}): {e}"


@mcp.tool()
def open_browser(intent: str) -> str:
    """打开浏览器并访问对应网站。

    登录状态（cookie/session）持久化方案（三层 fallback）：
    1. 当前 driver 仍存活 → 直接导航到新 URL
    2. driver 失效但 Chrome 进程仍在运行（agent 重启后）→ 通过远程调试端口 attach
    3. Chrome 也已退出 → 用 subprocess 独立启动 Chrome（脱离 agent 进程生命周期），
       再 attach；登录状态从固定用户数据目录恢复

    Chrome 以独立进程运行，agent 退出后 Chrome 继续运行，重启后 attach 回来。

    Args:
        intent: 用户的自然语言需求，例如"我想看新闻"、"打开 GitHub"、"搜索 Python 教程"
    """
    global _browser_driver, _current_frame
    url = _infer_url(intent)
    _current_frame = None  # 导航到新页面后重置 iframe 状态

    # 1) 当前 driver 仍存活 → 直接导航
    if _browser_driver is not None:
        try:
            _browser_driver.current_url  # 探活
            _browser_driver.get(url)
            return f"已在当前浏览器中导航到: {url}（保持登录状态）"
        except Exception:
            _browser_driver = None  # driver 已失效

    # 2) Chrome 仍在运行（agent 重启后）→ attach
    if _is_chrome_running():
        _browser_driver = _attach_chrome()
        if _browser_driver is not None:
            try:
                _browser_driver.get(url)
            except Exception:
                pass  # 页面加载超时不影响 attach 成功
            return f"已连接到运行中的浏览器，导航到: {url}（保持登录状态）"

    # 3) 没有运行的 Chrome → 用 subprocess 独立启动（脱离 MCP server 生命周期）
    import subprocess
    import time
    chrome_path = _find_chrome()
    if chrome_path is None:
        return "未找到 Chrome，请安装 Google Chrome，或在 agent.env 中配置 CHROME_PATH 指定浏览器路径"
    subprocess.Popen(
        [
            chrome_path,
            f"--user-data-dir={_CHROME_USER_DATA_DIR}",
            f"--remote-debugging-port={_REMOTE_DEBUG_PORT}",
            # Chrome 111+ 默认拒绝 http:// origin 的 WebSocket 连接到 DevTools,
            # 必须显式允许,否则 CDP WebSocket 连接会收到 403 Forbidden
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ],
        # Chrome 完全脱离当前进程，不会随 MCP server 退出而被杀
        creationflags=0x00000008,  # DETACHED_PROCESS
    )
    # 等待 Chrome 调试端口就绪（最多 15 秒）
    for _ in range(30):
        if _is_chrome_running():
            break
        time.sleep(0.5)
    # attach 到刚启动的 Chrome
    _browser_driver = _attach_chrome()
    if _browser_driver is None:
        return f"Chrome 已启动但 attach 失败，URL: {url}"
    return f"已打开浏览器，访问: {url}"


@mcp.tool()
def close_browser() -> str:
    """关闭浏览器并结束 WebDriver 会话（driver.quit()）。"""
    global _browser_driver
    if _browser_driver is None:
        return "浏览器未打开"
    _browser_driver.quit()
    _browser_driver = None
    return "已关闭浏览器"


@mcp.tool()
def navigate(url: str) -> str:
    """在当前浏览器中打开指定 URL（driver.get(url)）。

    Args:
        url: 要访问的完整 URL，例如 "https://www.runoob.com"
    """
    global _browser_driver, _current_frame
    _current_frame = None  # 导航到新页面后重置 iframe 状态
    # 探活：driver 失效时尝试重新 attach
    if _browser_driver is not None:
        try:
            _browser_driver.current_url
        except Exception:
            _browser_driver = None
    if _browser_driver is None and _is_chrome_running():
        _browser_driver = _attach_chrome()
    if _browser_driver is None:
        return "浏览器未打开，请先调用 open_browser"
    _browser_driver.get(url)
    return f"已导航到: {url}"


@mcp.tool()
def back() -> str:
    """后退到上一个页面（driver.back()）。"""
    if _browser_driver is None:
        return "浏览器未打开"
    _browser_driver.back()
    return "已后退到上一个页面"


@mcp.tool()
def forward() -> str:
    """前进到下一个页面（driver.forward()）。"""
    if _browser_driver is None:
        return "浏览器未打开"
    _browser_driver.forward()
    return "已前进到下一个页面"


@mcp.tool()
def refresh() -> str:
    """刷新当前页面（driver.refresh()）。"""
    if _browser_driver is None:
        return "浏览器未打开"
    _browser_driver.refresh()
    return "已刷新当前页面"


@mcp.tool()
def maximize_window() -> str:
    """最大化浏览器窗口（driver.maximize_window()）。"""
    if _browser_driver is None:
        return "浏览器未打开"
    _browser_driver.maximize_window()
    return "已最大化窗口"


@mcp.tool()
def minimize_window() -> str:
    """最小化浏览器窗口（driver.minimize_window()）。"""
    if _browser_driver is None:
        return "浏览器未打开"
    _browser_driver.minimize_window()
    return "已最小化窗口"


@mcp.tool()
def set_window_size(width: int, height: int) -> str:
    """设置浏览器窗口大小（driver.set_window_size(width, height)）。

    Args:
        width: 窗口宽度（像素），例如 1024
        height: 窗口高度（像素），例如 768
    """
    if _browser_driver is None:
        return "浏览器未打开"
    _browser_driver.set_window_size(width, height)
    return f"已设置窗口大小: {width}x{height}"


@mcp.tool()
def fullscreen_window() -> str:
    """将浏览器窗口设置为全屏模式（driver.fullscreen_window()）。"""
    if _browser_driver is None:
        return "浏览器未打开"
    _browser_driver.fullscreen_window()
    return "已进入全屏模式"


@mcp.tool()
def get_page_info() -> str:
    """获取当前页面的标题和 URL（driver.title / driver.current_url）。"""
    if _browser_driver is None:
        return "浏览器未打开"
    title = _browser_driver.title
    url = _browser_driver.current_url
    return f"页面标题: {title}\n当前 URL: {url}"


# --- 元素定位与操作（对应 runoob 元素定位 / 元素操作文档）---


@mcp.tool()
def click_element(by: str, value: str) -> str:
    """定位元素并点击（element.click()）。

    Args:
        by: 定位方式，可选: id, name, class_name, tag_name, css_selector, xpath, link_text, partial_link_text
        value: 定位值，例如 by="id" 时 value="submit-button"
    """
    element, err = _find_element(by, value, wait_clickable=True)
    if err:
        return err
    element.click()
    return f"已点击元素 (by={by}, value={value})"


@mcp.tool()
def input_text(by: str, value: str, text: str) -> str:
    """定位输入框并输入文本（会先 clear 清除原内容再 send_keys）。

    Args:
        by: 定位方式
        value: 定位值
        text: 要输入的文本
    """
    element, err = _find_element(by, value, wait_clickable=True)
    if err:
        return err
    element.clear()
    element.send_keys(text)
    return f"已在元素 (by={by}, value={value}) 中输入: {text}"


@mcp.tool()
def clear_input(by: str, value: str) -> str:
    """清除输入框的内容（element.clear()）。

    Args:
        by: 定位方式
        value: 定位值
    """
    element, err = _find_element(by, value)
    if err:
        return err
    element.clear()
    return f"已清除元素 (by={by}, value={value}) 的内容"


@mcp.tool()
def get_element_text(by: str, value: str) -> str:
    """获取元素的可见文本内容（element.text）。

    Args:
        by: 定位方式
        value: 定位值
    """
    element, err = _find_element(by, value)
    if err:
        return err
    return f"元素文本 (by={by}, value={value}): {element.text}"


@mcp.tool()
def get_element_attribute(by: str, value: str, attribute: str) -> str:
    """获取元素的指定属性值（element.get_attribute(attribute)）。

    Args:
        by: 定位方式
        value: 定位值
        attribute: 属性名，例如 "href", "value", "class", "type", "placeholder"
    """
    element, err = _find_element(by, value)
    if err:
        return err
    attr_value = element.get_attribute(attribute)
    return f"属性 {attribute} (by={by}, value={value}): {attr_value}"


@mcp.tool()
def upload_file(by: str, value: str, file_path: str) -> str:
    """上传文件到 <input type="file"> 元素（element.send_keys(file_path)）。

    对应 runoob Selenium 文件上传文档：
    1. 用 send_keys() 发送文件绝对路径到 file input
    2. 如果 file input 隐藏，用 JavaScript 使其可见
    3. 支持 shadow DOM 内的 file input 查找
    4. 支持在 iframe 内查找 file input（需先调用 switch_to_frame）

    文件上传不需要点击"选择文件"按钮（会弹出系统对话框 selenium 无法操作），
    而是直接将文件绝对路径发送到 <input type="file"> 元素。

    如果页面使用了 iframe（如 #vw-video-up-frame），请先调用 switch_to_frame 切换。

    Args:
        by: 定位方式（不确定时用 css_selector）
        value: 定位值（不确定时用 input[type='file']）
        file_path: 要上传的文件的绝对路径，例如 "d:\\视频\\test.mp4"
    """
    if _browser_driver is None:
        return "浏览器未打开，请先调用 open_browser"
    p = Path(file_path)
    if not p.exists():
        return f"文件不存在: {file_path}"
    abs_path = str(p.resolve())

    # 确保在正确的 iframe 上下文中（如果已调用 switch_to_frame）
    _ensure_frame()

    # 策略1：用指定 by/value 定位
    element, err = _find_element(by, value)

    # 策略2：定位失败时，用 JavaScript 深度查找（含 shadow DOM，在当前 frame 内）
    if err:
        find_script = """
        var result = [];
        function search(root) {
            var inputs = root.querySelectorAll('input[type="file"]');
            for (var i = 0; i < inputs.length; i++) {
                result.push(inputs[i]);
            }
            var all = root.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {
                if (all[i].shadowRoot) {
                    search(all[i].shadowRoot);
                }
            }
        }
        search(document);
        return result.length > 0 ? result[0] : null;
        """
        element = _browser_driver.execute_script(find_script)
        if element is None:
            # 策略3：点击上传区域触发隐藏的 file input
            # B站等平台的上传组件可能需要先点击才能显示 file input
            # 先尝试查找可见的上传区域
            try:
                # 在 iframe 内查找常见的上传区域
                upload_selectors = [
                    "#video-up-app",
                    ".video-entrance",
                    ".upload-body",
                    ".upload-wrp",
                    "#upload-area",
                    "[class*='upload']",
                    "[class*='drag']",
                    "[class*='drop']",
                ]
                clicked = False
                for sel in upload_selectors:
                    try:
                        areas = _browser_driver.find_elements(By.CSS_SELECTOR, sel)
                        for area in areas:
                            if area.is_displayed():
                                _browser_driver.execute_script(
                                    "arguments[0].click();", area
                                )
                                clicked = True
                                break
                        if clicked:
                            break
                    except Exception:
                        continue
                if clicked:
                    # 等待 file input 动态创建
                    import time
                    time.sleep(1)
                    element = _browser_driver.execute_script(find_script)
            except Exception:
                pass

            if element is None:
                return f"未找到 input[type=file] 元素（含 iframe/shadow DOM 搜索）"

    # 对应 runoob 文档：用 JS 使隐藏的 file input 可见
    try:
        _browser_driver.execute_script(
            "arguments[0].style.display = 'block'; "
            "arguments[0].style.visibility = 'visible'; "
            "arguments[0].style.opacity = '1'; "
            "arguments[0].removeAttribute('disabled'); "
            "arguments[0].scrollIntoView({block: 'center'});",
            element,
        )
    except Exception:
        pass  # 如果已可见则忽略

    # 对应 runoob 文档：用 send_keys 发送文件路径
    try:
        element.send_keys(abs_path)
        return f"已上传文件: {file_path}"
    except Exception as e:
        # fallback：如果 element.send_keys 失败，用 JS 的 File API
        try:
            _browser_driver.execute_script(
                "arguments[0].value = arguments[1];",
                element, abs_path,
            )
            return f"已上传文件（JS方式）: {file_path}"
        except Exception as e2:
            return f"上传失败: {e2}"


@mcp.tool()
def execute_script(script: str) -> str:
    """在当前页面执行 JavaScript 并返回结果（driver.execute_script(script)）。

    可用于探查 DOM 结构、操作隐藏元素、查找 shadow DOM 内的元素等。
    会自动切换到当前已选中的 iframe 上下文。

    Args:
        script: 要执行的 JavaScript 代码，return 语句的返回值会转为字符串返回
    """
    if _browser_driver is None:
        return "浏览器未打开，请先调用 open_browser"
    _ensure_frame()
    try:
        result = _browser_driver.execute_script(script)
        return str(result)
    except Exception as e:
        return f"执行失败: {e}"


@mcp.tool()
def upload_file_direct(css_selector: str, file_path: str) -> str:
    """直接用 CSS 选择器定位 file input 并上传文件（跳过等待机制）。

    适用于 iframe 内的隐藏 file input，需要先用 execute_script 让元素可见。

    Args:
        css_selector: CSS 选择器，如 "#video-up-app input[type='file']"
        file_path: 要上传的文件的绝对路径
    """
    if _browser_driver is None:
        return "浏览器未打开，请先调用 open_browser"
    p = Path(file_path)
    if not p.exists():
        return f"文件不存在: {file_path}"
    abs_path = str(p.resolve())
    _ensure_frame()
    try:
        element = _browser_driver.find_element(By.CSS_SELECTOR, css_selector)
        element.send_keys(abs_path)
        return f"已上传文件: {file_path}"
    except Exception as e:
        return f"上传失败: {e}"


# ---------------------------------------------------------------------------
# 等待机制工具集（对应 runoob Selenium 等待文档）
# ---------------------------------------------------------------------------

@mcp.tool()
def set_implicit_wait(seconds: int) -> str:
    """设置隐式等待时间（driver.implicitly_wait）。

    隐式等待是全局性的，设置后所有元素查找操作都会等待指定时间。
    不推荐与显式等待（wait_for_element 等）同时使用。

    Args:
        seconds: 等待秒数，例如 10
    """
    if _browser_driver is None:
        return "浏览器未打开，请先调用 open_browser"
    _browser_driver.implicitly_wait(seconds)
    return f"已设置隐式等待时间为 {seconds} 秒"


@mcp.tool()
def wait_for_element(by: str, value: str, timeout: int = 10) -> str:
    """等待元素出现在 DOM 中（显式等待）。

    对应 runoob 文档的 WebDriverWait + EC.presence_of_element_located。

    Args:
        by: 定位方式，可选: id, name, class_name, tag_name, css_selector, xpath, link_text, partial_link_text
        value: 定位值
        timeout: 最大等待秒数，默认 10 秒
    """
    if _browser_driver is None:
        return "浏览器未打开，请先调用 open_browser"
    _ensure_frame()
    by_method = _BY_MAP.get(by)
    if by_method is None:
        return f"不支持的定位方式: {by}，可选: {', '.join(_BY_MAP.keys())}"
    try:
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        element = WebDriverWait(_browser_driver, timeout).until(
            EC.presence_of_element_located((by_method, value))
        )
        return f"元素已出现 (by={by}, value={value})，文本: {element.text[:100]}"
    except Exception as e:
        return f"等待元素超时 ({timeout}s): {e}"


@mcp.tool()
def wait_for_element_visible(by: str, value: str, timeout: int = 10) -> str:
    """等待元素出现在 DOM 中并且可见（显式等待）。

    对应 runoob 文档的 WebDriverWait + EC.visibility_of_element_located。

    Args:
        by: 定位方式
        value: 定位值
        timeout: 最大等待秒数，默认 10 秒
    """
    if _browser_driver is None:
        return "浏览器未打开，请先调用 open_browser"
    _ensure_frame()
    by_method = _BY_MAP.get(by)
    if by_method is None:
        return f"不支持的定位方式: {by}，可选: {', '.join(_BY_MAP.keys())}"
    try:
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        element = WebDriverWait(_browser_driver, timeout).until(
            EC.visibility_of_element_located((by_method, value))
        )
        return f"元素已可见 (by={by}, value={value})，文本: {element.text[:100]}"
    except Exception as e:
        return f"等待元素可见超时 ({timeout}s): {e}"


@mcp.tool()
def wait_for_clickable(by: str, value: str, timeout: int = 10) -> str:
    """等待元素可点击（显式等待）。

    对应 runoob 文档的 WebDriverWait + EC.element_to_be_clickable。

    Args:
        by: 定位方式
        value: 定位值
        timeout: 最大等待秒数，默认 10 秒
    """
    if _browser_driver is None:
        return "浏览器未打开，请先调用 open_browser"
    _ensure_frame()
    by_method = _BY_MAP.get(by)
    if by_method is None:
        return f"不支持的定位方式: {by}，可选: {', '.join(_BY_MAP.keys())}"
    try:
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        element = WebDriverWait(_browser_driver, timeout).until(
            EC.element_to_be_clickable((by_method, value))
        )
        return f"元素已可点击 (by={by}, value={value})"
    except Exception as e:
        return f"等待元素可点击超时 ({timeout}s): {e}"


@mcp.tool()
def wait_for_text(by: str, value: str, text: str, timeout: int = 10) -> str:
    """等待元素包含指定文本（显式等待）。

    对应 runoob 文档的 WebDriverWait + EC.text_to_be_present_in_element。

    Args:
        by: 定位方式
        value: 定位值
        text: 要等待出现的文本
        timeout: 最大等待秒数，默认 10 秒
    """
    if _browser_driver is None:
        return "浏览器未打开，请先调用 open_browser"
    _ensure_frame()
    by_method = _BY_MAP.get(by)
    if by_method is None:
        return f"不支持的定位方式: {by}，可选: {', '.join(_BY_MAP.keys())}"
    try:
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        WebDriverWait(_browser_driver, timeout).until(
            EC.text_to_be_present_in_element((by_method, value), text)
        )
        return f"元素已包含文本 '{text}' (by={by}, value={value})"
    except Exception as e:
        return f"等待文本出现超时 ({timeout}s): {e}"


@mcp.tool()
def wait_until(script: str, timeout: int = 10) -> str:
    """等待 JavaScript 条件为真（通用等待）。

    执行给定的 JavaScript 脚本，直到返回值为 truthy（非空、非0、非false）。
    轮询间隔 0.5 秒，超时后返回最后一次执行结果。

    Args:
        script: JavaScript 代码，需要 return 一个 truthy 值表示条件满足。
                例如 "return document.readyState === 'complete'"
                例如 "return document.querySelectorAll('.item').length > 5"
                例如 "return document.getElementById('el').style.width === '100%'"
        timeout: 最大等待秒数，默认 10 秒
    """
    if _browser_driver is None:
        return "浏览器未打开，请先调用 open_browser"
    _ensure_frame()
    import time
    end_time = time.time() + timeout
    last_result = None
    # 确保脚本有 return 语句
    if "return" not in script:
        script = f"return ({script})"
    while time.time() < end_time:
        try:
            result = _browser_driver.execute_script(script)
            last_result = result
            # 检查结果是否为 truthy（处理各种类型）
            if result is True or (isinstance(result, str) and result) or (isinstance(result, (int, float)) and result != 0):
                return f"条件满足，结果: {str(result)[:200]}"
        except Exception as e:
            last_result = f"执行错误: {e}"
        time.sleep(0.5)
    return f"等待超时 ({timeout}s)，最后结果: {str(last_result)[:200]}"


@mcp.tool()
def wait_for_page_load(timeout: int = 30) -> str:
    """等待页面完全加载（document.readyState === 'complete'）。

    Args:
        timeout: 最大等待秒数，默认 30 秒
    """
    if _browser_driver is None:
        return "浏览器未打开，请先调用 open_browser"
    import time
    end_time = time.time() + timeout
    last_state = None
    while time.time() < end_time:
        try:
            state = _browser_driver.execute_script("return document.readyState")
            last_state = state
            if state == "complete":
                return f"页面已完全加载 (readyState={state})"
        except Exception as e:
            last_state = f"错误: {e}"
        time.sleep(0.5)
    return f"等待页面加载超时 ({timeout}s)，当前状态: {last_state}"


# ---------------------------------------------------------------------------
# 浏览器弹窗处理工具（对应 runoob 弹窗处理文档）
# ---------------------------------------------------------------------------

@mcp.tool()
def get_alert_text() -> str:
    """获取当前浏览器弹窗（alert/confirm/prompt）的文本。"""
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        alert = _browser_driver.switch_to.alert
        return f"弹窗文本: {alert.text}"
    except Exception as e:
        return f"获取弹窗文本失败: {e}"


@mcp.tool()
def accept_alert() -> str:
    """接受当前浏览器弹窗（点击确定/确认）。

    对应 runoob 文档的 alert.accept()。
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        alert = _browser_driver.switch_to.alert
        alert.accept()
        return "已接受弹窗（点击确定）"
    except Exception as e:
        return f"接受弹窗失败: {e}"


@mcp.tool()
def dismiss_alert() -> str:
    """取消当前浏览器弹窗（点击取消）。

    对应 runoob 文档的 alert.dismiss()。
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        alert = _browser_driver.switch_to.alert
        alert.dismiss()
        return "已取消弹窗（点击取消）"
    except Exception as e:
        return f"取消弹窗失败: {e}"


@mcp.tool()
def send_alert_text(text: str) -> str:
    """向 prompt 弹窗发送文本后接受。

    对应 runoob 文档的 alert.send_keys() + alert.accept()。

    Args:
        text: 要输入到 prompt 弹窗的文本
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        alert = _browser_driver.switch_to.alert
        alert.send_keys(text)
        alert.accept()
        return f"已向 prompt 弹窗输入文本并接受: {text}"
    except Exception as e:
        return f"发送弹窗文本失败: {e}"


# ---------------------------------------------------------------------------
# 窗口/标签页操作工具（对应 runoob 窗口切换文档）
# ---------------------------------------------------------------------------

@mcp.tool()
def get_window_handles() -> str:
    """获取所有打开的窗口/标签页句柄列表。"""
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        handles = _browser_driver.window_handles
        result = []
        for i, handle in enumerate(handles):
            _browser_driver.switch_to.window(handle)
            result.append(f"[{i}] handle={handle[:20]}... title={_browser_driver.title}")
        # 切回原窗口
        return f"窗口数量: {len(handles)}\n" + "\n".join(result)
    except Exception as e:
        return f"获取窗口句柄失败: {e}"


@mcp.tool()
def open_new_window(url: str = "") -> str:
    """打开新的浏览器窗口/标签页。

    Args:
        url: 新窗口要打开的 URL（可选，留空则打开空白页）
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        # 优先使用 Selenium 原生方法（通过 WebDriver 协议创建新窗口）
        # window.open() 在 attach 模式下因缺乏用户手势上下文会被 Chrome 拦截返回 null
        try:
            _browser_driver.switch_to.new_window('tab')
            if url:
                _browser_driver.get(url)
        except Exception:
            # 回退：通过 CDP HTTP API 创建新标签页（可直接带 url）
            target_url = url if url else "about:blank"
            req = urllib.request.Request(
                f"http://127.0.0.1:{_REMOTE_DEBUG_PORT}/json/new?{target_url}",
                method="PUT",
            )
            urllib.request.urlopen(req)
            handles = _browser_driver.window_handles
            _browser_driver.switch_to.window(handles[-1])
        handles = _browser_driver.window_handles
        return f"已打开新窗口 (共 {len(handles)} 个窗口)，当前在: {_browser_driver.title}"
    except Exception as e:
        return f"打开新窗口失败: {e}"


@mcp.tool()
def switch_window(index: int) -> str:
    """切换到指定索引的窗口/标签页。

    Args:
        index: 窗口索引（0 表示第一个，1 表示第二个...）
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        handles = _browser_driver.window_handles
        if index >= len(handles):
            return f"索引超出范围: 共 {len(handles)} 个窗口"
        _browser_driver.switch_to.window(handles[index])
        return f"已切换到窗口 [{index}]: {_browser_driver.title}"
    except Exception as e:
        return f"切换窗口失败: {e}"


@mcp.tool()
def close_window() -> str:
    """关闭当前窗口/标签页（driver.close()）。

    注意：如果这是最后一个窗口，浏览器将完全关闭。
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        _browser_driver.close()
        return "已关闭当前窗口"
    except Exception as e:
        return f"关闭窗口失败: {e}"


# ---------------------------------------------------------------------------
# Cookie 操作工具（对应 runoob Cookie 文档）
# ---------------------------------------------------------------------------

@mcp.tool()
def get_cookie(name: str = "") -> str:
    """获取指定 Cookie 或所有 Cookie。

    Args:
        name: Cookie 名称（可选，留空则返回所有 Cookie）
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        if name:
            cookie = _browser_driver.get_cookie(name)
            if cookie:
                return f"Cookie '{name}': name={cookie['name']}, value={cookie['value']}"
            return f"Cookie '{name}' 不存在"
        else:
            cookies = _browser_driver.get_cookies()
            if not cookies:
                return "当前页面没有 Cookie"
            result = []
            for c in cookies:
                result.append(f"{c['name']}={c['value']}")
            return f"Cookie 列表:\n" + "\n".join(result)
    except Exception as e:
        return f"获取 Cookie 失败: {e}"


@mcp.tool()
def add_cookie(name: str, value: str, domain: str = "", path: str = "/") -> str:
    """添加一个 Cookie。

    对应 runoob 文档的 driver.add_cookie()。

    Args:
        name: Cookie 名称
        value: Cookie 值
        domain: Cookie 域名（可选）
        path: Cookie 路径，默认 "/"
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        cookie_dict = {"name": name, "value": value, "path": path}
        if domain:
            cookie_dict["domain"] = domain
        _browser_driver.add_cookie(cookie_dict)
        return f"已添加 Cookie: {name}={value}"
    except Exception as e:
        return f"添加 Cookie 失败: {e}"


@mcp.tool()
def delete_cookie(name: str) -> str:
    """删除指定 Cookie。

    Args:
        name: 要删除的 Cookie 名称
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        _browser_driver.delete_cookie(name)
        return f"已删除 Cookie: {name}"
    except Exception as e:
        return f"删除 Cookie 失败: {e}"


@mcp.tool()
def delete_all_cookies() -> str:
    """删除所有 Cookie。"""
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        _browser_driver.delete_all_cookies()
        return "已删除所有 Cookie"
    except Exception as e:
        return f"删除所有 Cookie 失败: {e}"


# ---------------------------------------------------------------------------
# 页面信息工具（补充 runoob 文档的其他操作）
# ---------------------------------------------------------------------------

@mcp.tool()
def get_page_source() -> str:
    """获取当前页面的 HTML 源代码。"""
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        source = _browser_driver.page_source
        # 截断过长的源代码
        if len(source) > 50000:
            source = source[:50000] + "\n... (源代码过长，已截断)"
        return source
    except Exception as e:
        return f"获取页面源代码失败: {e}"


@mcp.tool()
def take_screenshot(file_path: str = "") -> str:
    """截取当前页面的屏幕截图。

    Args:
        file_path: 保存截图的文件路径（可选，留空则返回 base64 编码）
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        if file_path:
            _browser_driver.save_screenshot(file_path)
            return f"截图已保存到: {file_path}"
        else:
            import base64
            screenshot = _browser_driver.get_screenshot_as_base64()
            return f"截图已获取 (base64, 长度 {len(screenshot)} 字符)"
    except Exception as e:
        return f"截图失败: {e}"


@mcp.tool()
def scroll_page(direction: str = "down", pixels: int = 500) -> str:
    """滚动页面。

    Args:
        direction: 滚动方向，可选: up, down, left, right
        pixels: 滚动像素数，默认 500
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        scroll_map = {
            "down": f"window.scrollBy(0, {pixels})",
            "up": f"window.scrollBy(0, -{pixels})",
            "left": f"window.scrollBy(-{pixels}, 0)",
            "right": f"window.scrollBy({pixels}, 0)",
        }
        script = scroll_map.get(direction, scroll_map["down"])
        _browser_driver.execute_script(script)
        return f"已向 {direction} 方向滚动 {pixels} 像素"
    except Exception as e:
        return f"滚动失败: {e}"


# ---------------------------------------------------------------------------
# 鼠标操作工具（对应 runoob ActionChains 文档）
# 注意：Chrome attach 模式下 Selenium 原生 click/send_keys 可能失效，
# 因此所有工具均使用 JavaScript dispatchEvent 模拟，确保操作生效。
# ---------------------------------------------------------------------------

@mcp.tool()
def double_click(by: str, value: str) -> str:
    """双击指定元素（ActionChains.double_click）。

    使用 JS dispatchEvent('dblclick') 模拟双击，兼容 attach 模式。

    Args:
        by: 定位方式，可选: id, name, class_name, tag_name, css_selector, xpath, link_text, partial_link_text
        value: 定位值
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        _ensure_frame()
        element, err = _find_element(by, value)
        if err:
            return err
        _browser_driver.execute_script(
            "var el = arguments[0]; el.scrollIntoView({block:'center'}); "
            "el.dispatchEvent(new MouseEvent('dblclick', {bubbles: true, cancelable: true}));",
            element
        )
        return f"已双击元素 (by={by}, value={value})"
    except Exception as e:
        return f"双击失败: {e}"


@mcp.tool()
def right_click(by: str, value: str) -> str:
    """右键点击指定元素（ActionChains.context_click）。

    使用 JS dispatchEvent('contextmenu') 模拟右键，兼容 attach 模式。

    Args:
        by: 定位方式，可选: id, name, class_name, tag_name, css_selector, xpath, link_text, partial_link_text
        value: 定位值
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        _ensure_frame()
        element, err = _find_element(by, value)
        if err:
            return err
        _browser_driver.execute_script(
            "var el = arguments[0]; el.scrollIntoView({block:'center'}); "
            "el.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true, button: 2}));",
            element
        )
        return f"已右键点击元素 (by={by}, value={value})"
    except Exception as e:
        return f"右键点击失败: {e}"


@mcp.tool()
def hover(by: str, value: str) -> str:
    """鼠标悬停在指定元素上（ActionChains.move_to_element）。

    常用于触发下拉菜单或显示提示信息。

    Args:
        by: 定位方式，可选: id, name, class_name, tag_name, css_selector, xpath, link_text, partial_link_text
        value: 定位值
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        _ensure_frame()
        element, err = _find_element(by, value)
        if err:
            return err
        ActionChains(_browser_driver).move_to_element(element).perform()
        return f"已悬停在元素上 (by={by}, value={value})"
    except Exception as e:
        return f"悬停失败: {e}"


@mcp.tool()
def drag_and_drop(by_source: str, value_source: str, by_target: str, value_target: str) -> str:
    """将源元素拖放到目标元素（ActionChains.drag_and_drop）。

    使用 JS 模拟 HTML5 拖放事件链，兼容 attach 模式。

    Args:
        by_source: 源元素定位方式
        value_source: 源元素定位值
        by_target: 目标元素定位方式
        value_target: 目标元素定位值
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        _ensure_frame()
        source, err = _find_element(by_source, value_source)
        if err:
            return f"源元素: {err}"
        target, err = _find_element(by_target, value_target)
        if err:
            return f"目标元素: {err}"
        # 使用 JavaScript 模拟完整的 HTML5 拖放事件链
        drag_script = """
        var source = arguments[0];
        var target = arguments[1];
        source.scrollIntoView({block:'center'});
        target.scrollIntoView({block:'center'});

        function createDragEvent(type, dataTransfer) {
            var evt = new DragEvent(type, {bubbles: true, cancelable: true, dataTransfer: dataTransfer});
            return evt;
        }

        var dt = new DataTransfer();
        source.dispatchEvent(createDragEvent('dragstart', dt));
        target.dispatchEvent(createDragEvent('dragenter', dt));
        target.dispatchEvent(createDragEvent('dragover', dt));
        target.dispatchEvent(createDragEvent('drop', dt));
        source.dispatchEvent(createDragEvent('dragend', dt));
        """
        _browser_driver.execute_script(drag_script, source, target)
        return f"已将元素拖放到目标位置"
    except Exception as e:
        return f"拖放失败: {e}"


@mcp.tool()
def drag_and_drop_by_offset(by: str, value: str, x_offset: int, y_offset: int) -> str:
    """将元素拖动指定偏移量（ActionChains.drag_and_drop_by_offset）。

    Args:
        by: 定位方式
        value: 定位值
        x_offset: X 轴偏移量（像素）
        y_offset: Y 轴偏移量（像素）
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        _ensure_frame()
        element, err = _find_element(by, value)
        if err:
            return err
        ActionChains(_browser_driver).drag_and_drop_by_offset(element, x_offset, y_offset).perform()
        return f"已将元素拖动偏移量 (x={x_offset}, y={y_offset})"
    except Exception as e:
        return f"拖动失败: {e}"


@mcp.tool()
def click_and_hold(by: str, value: str) -> str:
    """按住鼠标左键在指定元素上（ActionChains.click_and_hold）。

    通常与 release 配合使用实现拖拽。

    Args:
        by: 定位方式
        value: 定位值
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        _ensure_frame()
        element, err = _find_element(by, value)
        if err:
            return err
        ActionChains(_browser_driver).click_and_hold(element).perform()
        return f"已按住元素 (by={by}, value={value})"
    except Exception as e:
        return f"按住失败: {e}"


@mcp.tool()
def release(by: str, value: str) -> str:
    """在指定元素上释放鼠标（ActionChains.release）。

    Args:
        by: 定位方式
        value: 定位值
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        _ensure_frame()
        element, err = _find_element(by, value)
        if err:
            return err
        ActionChains(_browser_driver).release(element).perform()
        return f"已在元素上释放鼠标 (by={by}, value={value})"
    except Exception as e:
        return f"释放失败: {e}"


@mcp.tool()
def move_mouse_by_offset(x: int, y: int) -> str:
    """将鼠标移动指定偏移量（ActionChains.move_by_offset）。

    Args:
        x: X 轴偏移量（像素）
        y: Y 轴偏移量（像素）
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(_browser_driver).move_by_offset(x, y).perform()
        return f"已移动鼠标偏移量 (x={x}, y={y})"
    except Exception as e:
        return f"移动鼠标失败: {e}"


# ---------------------------------------------------------------------------
# 键盘操作工具（对应 runoob Keys 文档）
# ---------------------------------------------------------------------------

# 按键名到 Keys 常量的映射
_KEY_MAP = {
    "enter": "ENTER",
    "return": "RETURN",
    "tab": "TAB",
    "escape": "ESCAPE",
    "esc": "ESCAPE",
    "space": "SPACE",
    "backspace": "BACKSPACE",
    "delete": "DELETE",
    "shift": "SHIFT",
    "control": "CONTROL",
    "ctrl": "CONTROL",
    "alt": "ALT",
    "command": "COMMAND",
    "meta": "META",
    "up": "ARROW_UP",
    "down": "ARROW_DOWN",
    "left": "ARROW_LEFT",
    "right": "ARROW_RIGHT",
    "arrow_up": "ARROW_UP",
    "arrow_down": "ARROW_DOWN",
    "arrow_left": "ARROW_LEFT",
    "arrow_right": "ARROW_RIGHT",
    "home": "HOME",
    "end": "END",
    "page_up": "PAGE_UP",
    "page_down": "PAGE_DOWN",
    "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4",
    "f5": "F5", "f6": "F6", "f7": "F7", "f8": "F8",
    "f9": "F9", "f10": "F10", "f11": "F11", "f12": "F12",
}


def _resolve_keys(key_names: list) -> list:
    """将按键名列表解析为 Keys 常量列表"""
    from selenium.webdriver.common.keys import Keys
    result = []
    for name in key_names:
        name_lower = name.lower().strip()
        if name_lower in _KEY_MAP:
            result.append(getattr(Keys, _KEY_MAP[name_lower]))
        elif len(name) == 1:
            # 单字符直接使用
            result.append(name)
        else:
            # 未识别的按键，尝试直接作为 Keys 属性
            try:
                result.append(getattr(Keys, name.upper()))
            except AttributeError:
                result.append(name)
    return result


@mcp.tool()
def press_key(by: str, value: str, key: str) -> str:
    """在指定元素上按下单个按键（send_keys）。

    使用 JS KeyboardEvent 模拟按键，兼容 attach 模式。
    对于特殊键（Enter/Tab/Escape 等），触发 keydown + keyup 事件。
    对于普通字符，调用 type_text 输入字符。

    Args:
        by: 定位方式
        value: 定位值
        key: 按键名，如 enter, tab, escape, space, backspace, delete,
             shift, control, alt, up, down, left, right, f1-f12 等
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        _ensure_frame()
        element, err = _find_element(by, value)
        if err:
            return err
        key_lower = key.lower().strip()
        # 按键名到 KeyboardEvent key 值的映射
        key_event_map = {
            "enter": "Enter", "return": "Enter",
            "tab": "Tab", "escape": "Escape", "esc": "Escape",
            "space": " ", "backspace": "Backspace", "delete": "Delete",
            "shift": "Shift", "control": "Control", "ctrl": "Control",
            "alt": "Alt", "up": "ArrowUp", "down": "ArrowDown",
            "left": "ArrowLeft", "right": "ArrowRight",
            "arrow_up": "ArrowUp", "arrow_down": "ArrowDown",
            "arrow_left": "ArrowLeft", "arrow_right": "ArrowRight",
            "home": "Home", "end": "End",
            "page_up": "PageUp", "page_down": "PageDown",
        }
        if key_lower in key_event_map:
            event_key = key_event_map[key_lower]
            _browser_driver.execute_script(
                """
                var el = arguments[0];
                var keyName = arguments[1];
                el.scrollIntoView({block:'center'});
                el.focus();
                el.dispatchEvent(new KeyboardEvent('keydown', {key: keyName, bubbles: true, cancelable: true}));
                el.dispatchEvent(new KeyboardEvent('keypress', {key: keyName, bubbles: true, cancelable: true}));
                el.dispatchEvent(new KeyboardEvent('keyup', {key: keyName, bubbles: true, cancelable: true}));
                """,
                element, event_key
            )
            # 特殊键处理
            if key_lower == "enter":
                _browser_driver.execute_script("arguments[0].dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));", element)
            elif key_lower == "backspace":
                _browser_driver.execute_script(
                    "var el = arguments[0]; var start = el.selectionStart || el.value.length; "
                    "if (start > 0) { el.value = el.value.substring(0, start-1) + el.value.substring(start); "
                    "el.selectionStart = el.selectionEnd = start-1; "
                    "el.dispatchEvent(new Event('input', {bubbles: true})); }",
                    element
                )
            elif key_lower == "space":
                _browser_driver.execute_script(
                    "var el = arguments[0]; var start = el.selectionStart || el.value.length; "
                    "el.value = el.value.substring(0, start) + ' ' + el.value.substring(start); "
                    "el.dispatchEvent(new Event('input', {bubbles: true}));",
                    element
                )
            return f"已按下按键: {key}"
        elif len(key) == 1:
            # 普通字符，直接输入
            return type_text(by, value, key)
        else:
            return f"未识别的按键: {key}"
    except Exception as e:
        return f"按键失败: {e}"


@mcp.tool()
def press_keys(by: str, value: str, keys: str) -> str:
    """在指定元素上按下组合键或多个按键。

    使用 JS 模拟组合键操作，兼容 attach 模式。
    对于 Ctrl+A 等组合键，直接执行对应操作（全选/复制/粘贴等）。

    Args:
        by: 定位方式
        value: 定位值
        keys: 按键组合，用 + 连接。例如:
              "control+a" - 全选
              "control+c" - 复制
              "control+v" - 粘贴
              "shift+a" - 输入大写 A
              "control+shift+a" - 三键组合
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        _ensure_frame()
        element, err = _find_element(by, value)
        if err:
            return err
        key_list = [k.strip().lower() for k in keys.split("+")]
        has_ctrl = "control" in key_list or "ctrl" in key_list
        has_shift = "shift" in key_list
        has_alt = "alt" in key_list
        # 获取非修饰键
        modifiers = {"control", "ctrl", "shift", "alt", "command", "meta"}
        normal_keys = [k for k in key_list if k not in modifiers]

        if has_ctrl and normal_keys:
            action_key = normal_keys[0]
            if action_key == "a":
                # Ctrl+A 全选
                _browser_driver.execute_script(
                    "var el = arguments[0]; el.focus(); el.select(); "
                    "el.dispatchEvent(new Event('select', {bubbles: true}));",
                    element
                )
                return f"已执行全选 (Ctrl+A)"
            elif action_key == "c":
                # Ctrl+C 复制（使用 execCommand）
                _browser_driver.execute_script(
                    "var el = arguments[0]; el.focus(); el.select(); "
                    "document.execCommand('copy');",
                    element
                )
                return f"已执行复制 (Ctrl+C)"
            elif action_key == "v":
                # Ctrl+V 粘贴（使用 execCommand）
                _browser_driver.execute_script(
                    "var el = arguments[0]; el.focus(); "
                    "document.execCommand('paste');",
                    element
                )
                return f"已执行粘贴 (Ctrl+V)"
            elif action_key == "x":
                _browser_driver.execute_script(
                    "var el = arguments[0]; el.focus(); el.select(); "
                    "document.execCommand('cut');",
                    element
                )
                return f"已执行剪切 (Ctrl+X)"
            else:
                return f"组合键 Ctrl+{action_key} 已模拟（无特定操作）"

        if has_shift and normal_keys:
            # Shift + 字符 = 大写
            text = "".join(k.upper() if len(k) == 1 else k for k in normal_keys)
            return type_text(by, value, text)

        if normal_keys:
            # 普通按键组合
            text = "".join(normal_keys)
            return type_text(by, value, text)

        return f"已按下组合键: {keys}"
    except Exception as e:
        return f"组合键失败: {e}"


@mcp.tool()
def type_text(by: str, value: str, text: str, clear: bool = False) -> str:
    """在指定元素中输入文本（send_keys）。

    使用 JS 设置 value 并触发 input 事件，兼容 attach 模式。

    Args:
        by: 定位方式
        value: 定位值
        text: 要输入的文本
        clear: 是否先清空输入框，默认 False
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        _ensure_frame()
        element, err = _find_element(by, value)
        if err:
            return err
        # 使用 JavaScript 设置值并触发事件
        clear_js = "true" if clear else "false"
        _browser_driver.execute_script(
            f"""
            var el = arguments[0];
            var text = arguments[1];
            var shouldClear = {clear_js};
            el.scrollIntoView({{block:'center'}});
            el.focus();
            if (shouldClear) el.value = '';
            // 使用 setRangeText 模拟真实输入
            var start = el.selectionStart || el.value.length;
            var end = el.selectionEnd || el.value.length;
            el.value = el.value.substring(0, start) + text + el.value.substring(end);
            el.selectionStart = el.selectionEnd = start + text.length;
            // 触发 input 和 change 事件
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            el.dispatchEvent(new KeyboardEvent('keyup', {{bubbles: true}}));
            """,
            element, text
        )
        return f"已输入文本: {text}"
    except Exception as e:
        return f"输入文本失败: {e}"


@mcp.tool()
def key_down(key: str, by: str = "", value: str = "") -> str:
    """按下按键不释放（ActionChains.key_down）。

    用于实现按住 Shift/Ctrl 等修饰键的同时进行其他操作。

    Args:
        key: 按键名（通常为 shift, control, alt）
        by: 定位方式（可选，指定元素则在元素上按下）
        value: 定位值（可选）
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        resolved = _resolve_keys([key])[0]
        actions = ActionChains(_browser_driver)
        if by and value:
            _ensure_frame()
            element, err = _find_element(by, value)
            if err:
                return err
            actions.key_down(resolved, element).perform()
        else:
            actions.key_down(resolved).perform()
        return f"已按下按键: {key}"
    except Exception as e:
        return f"按下按键失败: {e}"


@mcp.tool()
def key_up(key: str, by: str = "", value: str = "") -> str:
    """释放按键（ActionChains.key_up）。

    Args:
        key: 按键名
        by: 定位方式（可选）
        value: 定位值（可选）
    """
    if _browser_driver is None:
        return "浏览器未打开"
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        resolved = _resolve_keys([key])[0]
        actions = ActionChains(_browser_driver)
        if by and value:
            _ensure_frame()
            element, err = _find_element(by, value)
            if err:
                return err
            actions.key_up(resolved, element).perform()
        else:
            actions.key_up(resolved).perform()
        return f"已释放按键: {key}"
    except Exception as e:
        return f"释放按键失败: {e}"


# ---------------------------------------------------------------------------
# Network 抓取工具集（对应 Chrome DevTools Protocol 的 Network domain）
# 通过 websocket-client 独立连接 CDP，后台线程持续监听 Network 事件并缓存
# 请求/响应头；响应体按需通过 CDP Network.getResponseBody 实时获取。
# ---------------------------------------------------------------------------


def _get_page_ws_url():
    """通过 http://127.0.0.1:9222/json 获取当前 driver 所在 tab 的 webSocketDebuggerUrl。

    匹配策略:
    1. 若 _browser_driver 存活,用 driver.title + driver.current_url 与 page 列表比对
    2. 否则取第一个 type=='page' 的目标
    3. 找不到返回 None
    """
    if not _is_chrome_running():
        return None
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{_REMOTE_DEBUG_PORT}/json", timeout=2
        ) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        return None

    # 优先匹配 driver 所在的 tab
    if _browser_driver is not None:
        try:
            cur_title = _browser_driver.title
            cur_url = _browser_driver.current_url
            for p in pages:
                if p.get("title") == cur_title or p.get("url") == cur_url:
                    return p.get("webSocketDebuggerUrl")
        except Exception:
            pass
    # 兜底:取第一个 page
    return pages[0].get("webSocketDebuggerUrl")


def _send_cdp_command(method: str, params: dict = None, timeout: float = 5.0) -> dict:
    """在 _network_ws 上同步发送 CDP 命令并阻塞等待响应。

    Returns:
        成功: {"result": <CDP result dict>}
        失败: {"error": "描述信息"}
    """
    global _cdp_id_counter
    if _network_ws is None:
        return {"error": "网络抓取未启动,请先调用 start_network_capture"}

    with _cdp_id_lock:
        _cdp_id_counter += 1
        cmd_id = _cdp_id_counter

    msg = {"id": cmd_id, "method": method}
    if params:
        msg["params"] = params

    evt = threading.Event()
    with _network_lock:
        _pending_commands[cmd_id] = {"event": evt, "result": None, "error": None}
        # send 也在锁内,避免与监听线程或其他工具调用并发 send 造成数据交错
        try:
            _network_ws.send(json.dumps(msg))
        except Exception as e:
            _pending_commands.pop(cmd_id, None)
            return {"error": f"发送 CDP 命令失败: {e}"}

    # 在锁外等待,避免阻塞监听线程
    if not evt.wait(timeout=timeout):
        with _network_lock:
            _pending_commands.pop(cmd_id, None)
        return {"error": f"CDP 命令超时({timeout}s): {method}"}

    with _network_lock:
        entry = _pending_commands.pop(cmd_id, None)
    if entry is None:
        return {"error": "CDP 命令响应丢失"}
    if entry["error"]:
        return {"error": entry["error"]}
    return {"result": entry["result"]}


def _handle_message(msg: dict) -> None:
    """处理单条 CDP 消息:有 id 走命令响应分支,有 method 走事件分支。"""
    # 1. 命令响应(有 id 字段)
    if "id" in msg:
        cmd_id = msg["id"]
        with _network_lock:
            entry = _pending_commands.get(cmd_id)
            if entry is None:
                return  # 已超时被清理
            if "error" in msg:
                err = msg["error"]
                entry["error"] = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            else:
                entry["result"] = msg.get("result", {})
            entry["event"].set()
        return

    # 2. 事件(有 method 字段)
    method = msg.get("method", "")
    if not method.startswith("Network."):
        return  # 只处理 Network 域事件
    params = msg.get("params", {}) or {}
    handler = _NETWORK_EVENT_HANDLERS.get(method)
    if handler:
        handler(params)


def _on_request_will_be_sent(params: dict) -> None:
    """处理 Network.requestWillBeSent 事件:创建请求记录。"""
    rid = params.get("requestId")
    if not rid:
        return
    request = params.get("request", {}) or {}
    url = request.get("url", "")
    rtype = params.get("type", "")
    # 应用 URL/resourceType 过滤
    if _network_filter_url and _network_filter_url not in url:
        return
    if _network_filter_resource_type and rtype != _network_filter_resource_type:
        return
    entry = {
        "requestId": rid,
        "url": url,
        "method": request.get("method", ""),
        "resourceType": rtype,
        "requestHeaders": request.get("headers", {}) or {},
        "postData": request.get("postData"),
        "status": None,
        "statusText": None,
        "responseHeaders": None,
        "mimeType": None,
        "remoteIPAddress": None,
        "protocol": None,
        "encodedDataLength": None,
        "finished": False,
        "failed": False,
        "errorText": None,
        "timestamp": params.get("timestamp"),
        "responseTimestamp": None,
        "finishedTimestamp": None,
    }
    with _network_lock:
        _network_requests[rid] = entry


def _on_response_received(params: dict) -> None:
    """处理 Network.responseReceived 事件:更新响应信息。"""
    rid = params.get("requestId")
    if not rid:
        return
    response = params.get("response", {}) or {}
    with _network_lock:
        entry = _network_requests.get(rid)
        if entry is None:
            return  # 被过滤掉的请求或事件先于 requestWillBeSent 到达(罕见)
        entry["status"] = response.get("status")
        entry["statusText"] = response.get("statusText")
        entry["responseHeaders"] = response.get("headers", {}) or {}
        entry["mimeType"] = response.get("mimeType")
        entry["remoteIPAddress"] = response.get("remoteIPAddress")
        entry["protocol"] = response.get("protocol")
        entry["responseTimestamp"] = params.get("timestamp")


def _on_loading_finished(params: dict) -> None:
    """处理 Network.loadingFinished 事件:标记完成。"""
    rid = params.get("requestId")
    if not rid:
        return
    with _network_lock:
        entry = _network_requests.get(rid)
        if entry is None:
            return
        entry["finished"] = True
        entry["finishedTimestamp"] = params.get("timestamp")
        entry["encodedDataLength"] = params.get("encodedDataLength")


def _on_loading_failed(params: dict) -> None:
    """处理 Network.loadingFailed 事件:记录失败。"""
    rid = params.get("requestId")
    if not rid:
        return
    with _network_lock:
        entry = _network_requests.get(rid)
        if entry is None:
            return
        entry["failed"] = True
        entry["finished"] = True
        entry["errorText"] = params.get("errorText")
        entry["finishedTimestamp"] = params.get("timestamp")


# 事件名 -> 处理函数映射(模块级常量)
_NETWORK_EVENT_HANDLERS = {
    "Network.requestWillBeSent": _on_request_will_be_sent,
    "Network.responseReceived": _on_response_received,
    "Network.loadingFinished": _on_loading_finished,
    "Network.loadingFailed": _on_loading_failed,
}


def _network_listener(ws_url: str) -> None:
    """后台线程:连接 webSocketDebuggerUrl,Network.enable,循环 recv 消息并分发。

    断开时重连最多 3 次;跨进程导航时重新发现 target 并切换。

    注意:本函数内部发送 Network.enable 时不调用 _send_cdp_command,
    因为 _send_cdp_command 依赖 recv 循环处理响应,而 recv 循环在本线程中,
    同线程内等待响应会死锁。改为直接 send 后在 recv 循环前同步等待响应。
    """
    global _network_ws, _network_active
    max_retries = 3
    retry = 0
    while retry < max_retries and not _network_stop_event.is_set():
        try:
            ws = create_connection(ws_url, timeout=2.0)
        except Exception:
            retry += 1
            if retry >= max_retries:
                return
            _network_stop_event.wait(0.5)
            continue

        _network_ws = ws
        # 直接发送 Network.enable(不用 _send_cdp_command,避免同线程死锁)
        # 用一个固定的内部 id(外部命令从 _cdp_id_counter=1 开始,这里用 0 避免冲突)
        enable_id = 0
        try:
            ws.send(json.dumps({
                "id": enable_id,
                "method": "Network.enable",
                "params": {"maxPostDataSize": 65536},
            }))
        except Exception:
            try:
                ws.close()
            except Exception:
                pass
            _network_ws = None
            return

        # 同步等待 Network.enable 的响应(在进入主 recv 循环前)
        # 响应消息有 id 字段且等于 enable_id;事件消息有 method 字段
        ws.settimeout(5.0)
        enable_ok = False
        try:
            while not _network_stop_event.is_set():
                raw = ws.recv()
                if not raw:
                    break
                msg = json.loads(raw)
                if msg.get("id") == enable_id:
                    # 这是 Network.enable 的响应
                    if "error" not in msg:
                        enable_ok = True
                    break
                # 事件消息(Network.* 事件可能在 enable 响应前就到达)
                if "method" in msg:
                    _handle_message(msg)
        except Exception:
            pass

        if not enable_ok:
            try:
                ws.close()
            except Exception:
                pass
            _network_ws = None
            return

        # Network.enable 成功,进入正常 recv 循环
        _network_active = True
        ws.settimeout(0.5)  # recv 最多阻塞 0.5 秒,便于检查 stop_event
        while not _network_stop_event.is_set():
            try:
                raw = ws.recv()
            except WebSocketTimeoutException:
                continue
            except WebSocketConnectionClosedException:
                break  # 连接断开,触发外层重连
            except Exception:
                break

            if not raw:
                break  # 连接关闭

            try:
                msg = json.loads(raw)
            except Exception:
                continue
            _handle_message(msg)

        _network_active = False
        _network_ws = None
        try:
            ws.close()
        except Exception:
            pass

        # stop 触发的退出,不再重连
        if _network_stop_event.is_set():
            return

        # 异常断开:尝试重新发现 ws_url 后重连
        retry += 1
        new_ws_url = _get_page_ws_url()
        if new_ws_url and new_ws_url != ws_url:
            ws_url = new_ws_url  # 切换到新 target
            retry = 0  # 切换 target 视为新一轮重试
        _network_stop_event.wait(1.0)  # 退避 1 秒


# 合法 ResourceType 枚举值(用于 start_network_capture 校验)
_VALID_RESOURCE_TYPES = {
    "Document", "Stylesheet", "Image", "Media", "Font", "Script",
    "TextTrack", "XHR", "Fetch", "EventSource", "WebSocket", "Manifest",
    "SignedExchange", "Ping", "CSPViolationReport", "Other",
}


@mcp.tool()
def start_network_capture(url_filter: str = "", resource_type: str = "") -> str:
    """开始监听当前页面的网络请求(后台线程持续缓存请求/响应头)。

    通过 CDP 的 Network.enable 启用网络追踪,后台线程接收所有 Network.* 事件并
    缓存到内存。响应体不缓存,需要时调用 get_network_response_body 实时获取。

    重复调用会先停止已有监听再重新启动。页面导航时监听线程会自动重连。

    Args:
        url_filter: URL 子串过滤,只缓存 URL 中包含该子串的请求(大小写敏感)。
                    留空表示不过滤。例如 "api.example.com" 只抓该域名的请求。
        resource_type: 资源类型过滤,可选值:
                       XHR, Fetch, Script, Stylesheet, Image, Media, Document,
                       Font, WebSocket, Manifest, Other。
                       留空表示不过滤。例如 "XHR" 只抓 XHR 请求。
    """
    global _network_thread, _network_filter_url, _network_filter_resource_type
    global _network_stop_event, _network_active

    if not _is_chrome_running():
        return "Chrome 未启动,请先调用 open_browser"
    if resource_type and resource_type not in _VALID_RESOURCE_TYPES:
        return (
            f"不支持的 resource_type: {resource_type}\n"
            f"可选值: {', '.join(sorted(_VALID_RESOURCE_TYPES))}"
        )

    # 若已在监听,先停止
    if _network_active or _network_thread is not None:
        # 内部停止逻辑(不调用 MCP 工具自身,避免递归)
        _network_stop_event.set()
        if _network_thread is not None:
            _network_thread.join(timeout=2.0)
        if _network_ws is not None:
            try:
                _network_ws.close()
            except Exception:
                pass
        _network_stop_event = threading.Event()  # 重置 event
        _network_active = False

    ws_url = _get_page_ws_url()
    if not ws_url:
        return "未找到可监听的页面 tab,请先 open_browser 并确保有页面打开"

    # 设置过滤条件(在启动监听前设置,事件处理时读取)
    _network_filter_url = url_filter
    _network_filter_resource_type = resource_type

    # 启动后台监听线程
    _network_stop_event.clear()
    _network_thread = threading.Thread(
        target=_network_listener, args=(ws_url,), daemon=True
    )
    _network_thread.start()

    # 轮询等待 Network.enable 成功(最多 5 秒)
    # _network_active 会在 Network.enable 收到响应后变为 True
    import time as _time
    for _ in range(50):
        if _network_active:
            break
        if _network_thread is None or not _network_thread.is_alive():
            return (
                f"监听线程已退出,ws_url={ws_url}\n"
                "可能原因: WebSocket 连接被拒(检查 Chrome 是否带 --remote-allow-origins=* 启动)"
            )
        _time.sleep(0.1)
    if not _network_active:
        return (
            f"启动监听超时(5 秒内未收到 Network.enable 响应),ws_url={ws_url}\n"
            "请检查 Chrome 是否在 9222 端口运行,且带 --remote-allow-origins=* 参数"
        )

    filter_desc = []
    if url_filter:
        filter_desc.append(f"url 包含 '{url_filter}'")
    if resource_type:
        filter_desc.append(f"类型={resource_type}")
    filter_str = f",过滤: {' AND '.join(filter_desc)}" if filter_desc else ",无过滤"
    return (
        f"已开始监听网络请求{filter_str}\n"
        f"ws_url={ws_url}\n"
        f"当前已缓存 {len(_network_requests)} 条请求\n"
        "提示: 响应体不缓存,需要时调用 get_network_response_body 获取"
    )


@mcp.tool()
def stop_network_capture() -> str:
    """停止网络监听(发送 Network.disable 并关闭 WebSocket,后台线程退出)。

    已缓存的请求不会被清空,可继续用 get_network_requests /
    get_network_request_detail 查询。
    """
    global _network_active, _network_ws, _network_thread
    if not _network_active and _network_thread is None:
        return f"网络抓取未在运行,当前缓存 {len(_network_requests)} 条请求"

    _network_stop_event.set()
    # 尝试发送 Network.disable(失败忽略)
    if _network_ws is not None:
        try:
            _send_cdp_command("Network.disable", timeout=1.0)
        except Exception:
            pass

    if _network_thread is not None:
        _network_thread.join(timeout=2.0)
        _network_thread = None

    if _network_ws is not None:
        try:
            _network_ws.close()
        except Exception:
            pass
        _network_ws = None

    _network_active = False
    return f"已停止网络监听,缓存保留 {len(_network_requests)} 条请求"


@mcp.tool()
def clear_network_log() -> str:
    """清空已缓存的网络请求记录(不影响正在运行的监听)。"""
    with _network_lock:
        count = len(_network_requests)
        _network_requests.clear()
    return f"已清空 {count} 条网络请求记录"


@mcp.tool()
def get_network_requests(
    url_filter: str = "",
    resource_type: str = "",
    method: str = "",
    status: int = 0,
    limit: int = 50,
) -> str:
    """查询已缓存的网络请求列表(摘要信息)。

    支持多维度过滤,返回每个请求的 requestId、url、method、status、resourceType 摘要。
    requestId 是后续 get_network_request_detail / get_network_response_body 的入参。

    Args:
        url_filter: URL 子串过滤(大小写敏感),留空不过滤
        resource_type: 资源类型过滤(XHR/Fetch/Script 等),留空不过滤
        method: HTTP 方法过滤(如 "GET"/"POST"),大小写不敏感,留空不过滤
        status: HTTP 状态码过滤(如 200/404/500),0 表示不过滤
        limit: 最多返回条数,默认 50,最大 500
    """
    if not _network_requests:
        return "未启动抓取或暂无缓存,请先调用 start_network_capture"

    limit = max(1, min(500, limit))
    method_upper = method.upper() if method else ""

    # 拷贝快照后在锁外过滤,避免长时间持锁
    with _network_lock:
        snapshot = list(_network_requests.values())
    total = len(snapshot)

    filtered = []
    for entry in snapshot:
        if url_filter and url_filter not in entry.get("url", ""):
            continue
        if resource_type and entry.get("resourceType") != resource_type:
            continue
        if method_upper and entry.get("method", "").upper() != method_upper:
            continue
        if status and entry.get("status") != status:
            continue
        filtered.append(entry)

    if not filtered:
        return f"共 0 条匹配(总缓存 {total} 条)"

    truncated = len(filtered) > limit
    display = filtered[:limit]
    lines = [f"共 {len(filtered)} 条匹配(总缓存 {total} 条)"]
    for i, entry in enumerate(display):
        rid = entry.get("requestId", "")[:16]
        m = entry.get("method", "?")
        s = entry.get("status")
        s_str = str(s) if s is not None else "---"
        rt = entry.get("resourceType", "?")
        url = entry.get("url", "")
        # URL 过长截断
        if len(url) > 120:
            url = url[:117] + "..."
        lines.append(f"[{i}] requestId={rid} {m} {s_str} {rt} {url}")
    if truncated:
        lines.append(f"...(已截断,共 {len(filtered)} 条,limit={limit})")
    return "\n".join(lines)


@mcp.tool()
def get_network_request_detail(request_id: str) -> str:
    """获取单个请求的详细信息(请求头、响应头、postData、mimeType、状态等)。

    不包含响应体,响应体请用 get_network_response_body 单独获取。

    Args:
        request_id: 请求 ID,可从 get_network_requests 的返回中获取
    """
    with _network_lock:
        entry = _network_requests.get(request_id)
        if entry is None:
            # 模糊匹配:requestId 前缀
            candidates = [
                rid for rid in _network_requests
                if rid.startswith(request_id)
            ]
            if len(candidates) == 1:
                entry = _network_requests[candidates[0]].copy()
                request_id = candidates[0]
            else:
                return (
                    f"requestId 不存在: {request_id}\n"
                    f"缓存中共 {len(_network_requests)} 条请求,请用 get_network_requests 查询"
                )
        else:
            entry = entry.copy()

    # 格式化输出(在锁外构造,避免长时间持锁)
    lines = [
        f"requestId: {request_id}",
        f"URL: {entry.get('url', '')}",
        f"Method: {entry.get('method', '')}",
        f"ResourceType: {entry.get('resourceType', '')}",
    ]
    status = entry.get("status")
    status_text = entry.get("statusText")
    if status is not None:
        lines.append(f"Status: {status} {status_text or ''}".rstrip())
    else:
        lines.append("Status: (未收到响应)")
    if entry.get("mimeType"):
        lines.append(f"MIME: {entry['mimeType']}")
    if entry.get("remoteIPAddress"):
        lines.append(f"RemoteIP: {entry['remoteIPAddress']}")
    if entry.get("protocol"):
        lines.append(f"Protocol: {entry['protocol']}")

    lines.append("")
    lines.append("Request Headers:")
    req_headers = entry.get("requestHeaders") or {}
    if req_headers:
        for k, v in req_headers.items():
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  (无)")

    post_data = entry.get("postData")
    if post_data:
        lines.append("")
        lines.append("Request Body:")
        if len(post_data) > 2000:
            lines.append(f"  {post_data[:2000]}...(已截断,共 {len(post_data)} 字符)")
        else:
            lines.append(f"  {post_data}")

    lines.append("")
    lines.append("Response Headers:")
    resp_headers = entry.get("responseHeaders") or {}
    if resp_headers:
        for k, v in resp_headers.items():
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  (无)")

    lines.append("")
    state = []
    if entry.get("finished"):
        state.append("finished")
    if entry.get("failed"):
        state.append(f"failed({entry.get('errorText', '')})")
    if not state:
        state.append("pending")
    lines.append(f"State: {' '.join(state)}")
    if entry.get("encodedDataLength") is not None:
        lines.append(f"EncodedDataLength: {entry['encodedDataLength']}")

    result = "\n".join(lines)
    if len(result) > _MAX_DETAIL_CHARS:
        result = result[:_MAX_DETAIL_CHARS] + f"\n...(已截断,完整长度 {len(result)} 字符)"
    return result


@mcp.tool()
def get_network_response_body(request_id: str) -> str:
    """通过 CDP 实时获取指定请求的响应体。

    调用 Network.getResponseBody 实时拉取(不依赖缓存)。若响应体是二进制
    (base64Encoded=true),会自动解码并返回字节数;若是文本,返回截断后的字符串。

    注意:页面导航后旧请求的响应体可能丢失(CDP 返回
    "No resource with given identifier found"),请在导航前及时获取。

    Args:
        request_id: 请求 ID
    """
    if not _is_chrome_running():
        return "Chrome 未启动,请先调用 open_browser"
    if _network_ws is None:
        return "网络抓取未启动,请先调用 start_network_capture"

    # 校验 requestId 存在(支持前缀匹配)
    with _network_lock:
        if request_id not in _network_requests:
            candidates = [
                rid for rid in _network_requests
                if rid.startswith(request_id)
            ]
            if len(candidates) == 1:
                request_id = candidates[0]
            else:
                return (
                    f"requestId 不存在: {request_id}\n"
                    "请用 get_network_requests 查询有效的 requestId"
                )
        entry = _network_requests.get(request_id, {}).copy()

    if not entry.get("finished"):
        return f"请求尚未完成(requestId={request_id}),请等待 loadingFinished 后再获取响应体"

    result = _send_cdp_command(
        "Network.getResponseBody",
        {"requestId": request_id},
        timeout=10.0,
    )
    if "error" in result:
        return f"获取响应体失败: {result['error']}"

    body_info = result.get("result") or {}
    body = body_info.get("body", "")
    is_base64 = body_info.get("base64Encoded", False)

    if is_base64:
        try:
            body_bytes = base64.b64decode(body) if body else b""
            mime = entry.get("mimeType", "unknown")
            return (
                f"响应体为二进制(base64 已解码),共 {len(body_bytes)} 字节\n"
                f"mimeType: {mime}\n"
                "(为避免大量 token 消耗,二进制内容不直接返回)"
            )
        except Exception as e:
            return f"base64 解码失败: {e}"

    # 文本响应
    if len(body) > _MAX_BODY_BYTES:
        return (
            f"响应体过大({len(body)} 字符),已截断到 {_MAX_BODY_BYTES} 字符:\n"
            + body[:_MAX_BODY_BYTES]
            + f"\n...(已截断)"
        )
    return f"响应体({len(body)} 字符):\n{body}"


@mcp.tool()
def get_network_cookies(url: str = "") -> str:
    """通过 CDP Network.getCookies 获取当前页面的 cookies。

    若未启动 Network 抓取,会 fallback 到 Selenium 的 execute_cdp_cmd。

    Args:
        url: 指定 URL 时返回该 URL 适用域的 cookies;留空则返回当前页面
             及其所有子框架 URL 对应的 cookies
    """
    if not _is_chrome_running():
        return "Chrome 未启动,请先调用 open_browser"

    params = {"urls": [url]} if url else {}

    # 优先用 Network 抓取的 WebSocket 连接
    if _network_ws is not None:
        result = _send_cdp_command("Network.getCookies", params, timeout=5.0)
        if "error" in result:
            return f"获取 cookies 失败: {result['error']}"
        cookies = (result.get("result") or {}).get("cookies", [])
    elif _browser_driver is not None:
        # Fallback: 用 Selenium 的 execute_cdp_cmd
        try:
            cdp_result = _browser_driver.execute_cdp_cmd("Network.getCookies", params)
            cookies = cdp_result.get("cookies", [])
        except Exception as e:
            return f"获取 cookies 失败(Selenium fallback): {e}"
    else:
        return "网络抓取未启动且 Selenium driver 未初始化,请先 open_browser"

    if not cookies:
        return "当前页面没有 cookies"

    lines = [f"共 {len(cookies)} 条 cookies"]
    for i, c in enumerate(cookies):
        name = c.get("name", "")
        domain = c.get("domain", "")
        path = c.get("path", "/")
        secure = c.get("secure", False)
        http_only = c.get("httpOnly", False)
        same_site = c.get("sameSite", "")
        expires = c.get("expires", -1)
        value = c.get("value", "")
        # value 过长截断
        if len(value) > 80:
            value = value[:77] + "..."
        exp_str = f"expires={expires}" if expires and expires > 0 else "session"
        lines.append(
            f"[{i}] {name}={value}  domain={domain} path={path} "
            f"secure={secure} httpOnly={http_only} sameSite={same_site} {exp_str}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 元素与样式调试工具集(对应 Chrome DevTools 的 Elements 面板)
# 通过 JavaScript getComputedStyle / style / getBoundingClientRect 实现,
# 复用 _find_element 定位元素,无需 CDP nodeId 转换。
# ---------------------------------------------------------------------------


@mcp.tool()
def get_element_style(
    by: str, value: str, pseudo_element: str = "", properties: str = ""
) -> str:
    """获取元素的计算样式(computed style)。

    对应 Chrome DevTools Elements 面板的 Computed 标签。

    Args:
        by: 定位方式,可选: id, name, class_name, tag_name, css_selector, xpath, link_text, partial_link_text
        value: 定位值
        pseudo_element: 伪元素名称,如 ":hover"、":before"、":after"、":first-line"。
                        留空表示元素本身的样式。
        properties: 要查询的 CSS 属性名,逗号分隔,如 "color,background-color,font-size"。
                    留空表示返回全部计算样式。
    """
    if _browser_driver is None:
        return "浏览器未打开,请先调用 open_browser"
    element, err = _find_element(by, value)
    if err:
        return err
    _ensure_frame()
    script = """
    var el = arguments[0];
    var pseudo = arguments[1] || null;
    var props = arguments[2] || "";
    var style = window.getComputedStyle(el, pseudo);
    if (props) {
        var result = {};
        props.split(",").forEach(function(p) {
            p = p.trim();
            if (p) result[p] = style.getPropertyValue(p);
        });
        return JSON.stringify(result, null, 2);
    } else {
        var result = {};
        for (var i = 0; i < style.length; i++) {
            var name = style[i];
            result[name] = style.getPropertyValue(name);
        }
        return JSON.stringify(result, null, 2);
    }
    """
    try:
        result = _browser_driver.execute_script(
            script, element, pseudo_element, properties
        )
        pseudo_desc = f", 伪元素={pseudo_element}" if pseudo_element else ""
        return f"计算样式 (by={by}, value={value}{pseudo_desc}):\n{result}"
    except Exception as e:
        return f"获取计算样式失败: {e}"


@mcp.tool()
def set_element_style(
    by: str, value: str, property: str, style_value: str
) -> str:
    """设置元素的内联样式(inline style)。

    对应 Chrome DevTools Elements 面板的 Styles 标签中修改 element.style。

    Args:
        by: 定位方式
        value: 定位值
        property: CSS 属性名,如 "color"、"background-color"、"display"、"font-size"
        style_value: CSS 属性值,如 "red"、"none"、"block"、"14px"。
                     传空字符串清除该属性。
    """
    if _browser_driver is None:
        return "浏览器未打开,请先调用 open_browser"
    element, err = _find_element(by, value, wait_clickable=True)
    if err:
        return err
    _ensure_frame()
    script = """
    var el = arguments[0];
    var prop = arguments[1];
    var val = arguments[2];
    el.style.setProperty(prop, val);
    return el.style.getPropertyValue(prop);
    """
    try:
        actual = _browser_driver.execute_script(script, element, property, style_value)
        if not style_value:
            return f"已清除样式 {property} (by={by}, value={value})"
        return f"已设置样式 {property}={style_value} (by={by}, value={value}),实际值={actual}"
    except Exception as e:
        return f"设置样式失败: {e}"


@mcp.tool()
def get_element_box(by: str, value: str) -> str:
    """获取元素的位置、尺寸和盒模型信息。

    对应 Chrome DevTools Elements 面板的 Box Model 标签。
    返回 getBoundingClientRect() 的 x/y/width/height,以及
    margin/border/padding 的值。

    Args:
        by: 定位方式
        value: 定位值
    """
    if _browser_driver is None:
        return "浏览器未打开,请先调用 open_browser"
    element, err = _find_element(by, value)
    if err:
        return err
    _ensure_frame()
    script = """
    var el = arguments[0];
    var rect = el.getBoundingClientRect();
    var style = window.getComputedStyle(el);
    function parse(v) { return parseFloat(v) || 0; }
    return JSON.stringify({
        position: { x: rect.x, y: rect.y, top: rect.top, left: rect.left },
        size: { width: rect.width, height: rect.height },
        margin: {
            top: parse(style.marginTop), right: parse(style.marginRight),
            bottom: parse(style.marginBottom), left: parse(style.marginLeft)
        },
        border: {
            top: parse(style.borderTopWidth), right: parse(style.borderRightWidth),
            bottom: parse(style.borderBottomWidth), left: parse(style.borderLeftWidth)
        },
        padding: {
            top: parse(style.paddingTop), right: parse(style.paddingRight),
            bottom: parse(style.paddingBottom), left: parse(style.paddingLeft)
        },
        visible: rect.width > 0 && rect.height > 0,
        scroll: { scrollTop: el.scrollTop, scrollLeft: el.scrollLeft,
                  scrollWidth: el.scrollWidth, scrollHeight: el.scrollHeight }
    }, null, 2);
    """
    try:
        result = _browser_driver.execute_script(script, element)
        return f"盒模型 (by={by}, value={value}):\n{result}"
    except Exception as e:
        return f"获取盒模型失败: {e}"


@mcp.tool()
def get_element_attributes(by: str, value: str) -> str:
    """获取元素的所有 DOM 属性。

    对应 Chrome DevTools Elements 面板中查看元素属性。

    Args:
        by: 定位方式
        value: 定位值
    """
    if _browser_driver is None:
        return "浏览器未打开,请先调用 open_browser"
    element, err = _find_element(by, value)
    if err:
        return err
    _ensure_frame()
    script = """
    var el = arguments[0];
    var attrs = el.attributes;
    var result = {};
    for (var i = 0; i < attrs.length; i++) {
        result[attrs[i].name] = attrs[i].value;
    }
    result['<tagName>'] = el.tagName.toLowerCase();
    result['<innerHTML.length>'] = el.innerHTML.length;
    return JSON.stringify(result, null, 2);
    """
    try:
        result = _browser_driver.execute_script(script, element)
        return f"元素属性 (by={by}, value={value}):\n{result}"
    except Exception as e:
        return f"获取元素属性失败: {e}"


@mcp.tool()
def set_element_attribute(
    by: str, value: str, name: str, attr_value: str
) -> str:
    """设置元素的 DOM 属性。

    对应 Chrome DevTools Elements 面板中修改元素属性。

    Args:
        by: 定位方式
        value: 定位值
        name: 属性名,如 "class"、"data-test"、"disabled"、"href"
        attr_value: 属性值。传空字符串表示设为空值;
                    对于布尔属性(如 disabled),传 "true" 添加、传 "" 移除。
    """
    if _browser_driver is None:
        return "浏览器未打开,请先调用 open_browser"
    element, err = _find_element(by, value, wait_clickable=True)
    if err:
        return err
    _ensure_frame()
    script = """
    var el = arguments[0];
    var name = arguments[1];
    var val = arguments[2];
    if (val === "" && (name === "disabled" || name === "checked" || name === "readonly")) {
        el.removeAttribute(name);
    } else {
        el.setAttribute(name, val);
    }
    return el.getAttribute(name);
    """
    try:
        actual = _browser_driver.execute_script(script, element, name, attr_value)
        return f"已设置属性 {name}={attr_value} (by={by}, value={value}),实际值={actual}"
    except Exception as e:
        return f"设置属性失败: {e}"


# ---------------------------------------------------------------------------
# 设备模拟工具集(对应 Chrome DevTools 的 Device Mode / Sensors 面板)
# 通过 CDP Emulation/Network domain 实现,复用 _send_cdp_command(WebSocket 优先)
# 或 Selenium execute_cdp_cmd(fallback)。
# ---------------------------------------------------------------------------


def _send_cdp_or_fallback(method: str, params: dict = None) -> tuple:
    """发送 CDP 命令:WebSocket 优先,fallback 到 Selenium execute_cdp_cmd。

    Returns:
        (result_dict, None) 成功
        (None, error_msg) 失败
    """
    if _network_ws is not None:
        result = _send_cdp_command(method, params or {}, timeout=5.0)
        if "error" not in result:
            return result.get("result"), None
        return None, result["error"]
    if _browser_driver is not None:
        try:
            return _browser_driver.execute_cdp_cmd(method, params or {}), None
        except Exception as e:
            return None, str(e)
    return None, "浏览器未打开且网络抓取未启动,请先调用 open_browser"


@mcp.tool()
def set_device_metrics(
    width: int,
    height: int,
    device_scale_factor: float = 1.0,
    mobile: bool = False,
    user_agent: str = "",
) -> str:
    """设置设备尺寸模拟(对应 Chrome DevTools 的 Device Mode)。

    模拟指定屏幕尺寸、设备像素比和移动端布局。常用于响应式设计测试。

    Args:
        width: 屏幕宽度(像素),如 390(iPhone 14)
        height: 屏幕高度(像素),如 844(iPhone 14)
        device_scale_factor: 设备像素比(DPR),如 1.0(普通屏幕)、2.0(Retina)、3.0(手机)
        mobile: 是否启用移动端 viewport meta 标签处理
        user_agent: 可选,自定义 User-Agent 字符串。留空不修改 UA。
    """
    if not _is_chrome_running():
        return "Chrome 未启动,请先调用 open_browser"
    params = {
        "width": width,
        "height": height,
        "deviceScaleFactor": device_scale_factor,
        "mobile": mobile,
    }
    _, err = _send_cdp_or_fallback("Emulation.setDeviceMetricsOverride", params)
    if err:
        return f"设置设备尺寸失败: {err}"
    # 可选:同时设置 UA(只设置 userAgent 字符串,不设置 userAgentMetadata
    # 以避免 Chrome 150 对 UserAgentMetadata 格式的严格校验)
    ua_desc = ""
    if user_agent:
        ua_params = {"userAgent": user_agent}
        _, ua_err = _send_cdp_or_fallback(
            "Emulation.setUserAgentOverride", ua_params
        )
        if ua_err:
            return f"设备尺寸已设置,但 UA 设置失败: {ua_err}"
        ua_desc = f", UA={user_agent[:60]}..."
    return (
        f"已设置设备模拟: {width}x{height}, scale={device_scale_factor}, "
        f"mobile={mobile}{ua_desc}"
    )


@mcp.tool()
def clear_device_metrics() -> str:
    """清除设备尺寸模拟,恢复真实屏幕参数。"""
    if not _is_chrome_running():
        return "Chrome 未启动,请先调用 open_browser"
    _, err = _send_cdp_or_fallback("Emulation.clearDeviceMetricsOverride", {})
    if err:
        return f"清除设备模拟失败: {err}"
    return "已清除设备尺寸模拟,恢复真实参数"


@mcp.tool()
def emulate_network_conditions(
    offline: bool = False,
    latency: int = 0,
    download_throughput: int = -1,
    upload_throughput: int = -1,
    connection_type: str = "",
) -> str:
    """模拟网络条件(对应 Chrome DevTools 的 Network 面板 throttling)。

    常用于弱网测试、离线测试、加载性能优化。

    Args:
        offline: 是否模拟离线模式(True 时无法访问网络)
        latency: 每个请求的延迟(毫秒),如 200 表示 200ms 延迟
        download_throughput: 下载速度(bytes/s),-1 表示不限制。
                             如 50000 ≈ 50KB/s(3G 速度)
        upload_throughput: 上传速度(bytes/s),-1 表示不限制
        connection_type: 连接类型(可选),可选值:
                         cellular2g, cellular3g, cellular4g, bluetooth,
                         ethernet, wifi, wimax, other。留空不指定。
    """
    if not _is_chrome_running():
        return "Chrome 未启动,请先调用 open_browser"
    params = {
        "offline": offline,
        "latency": latency,
        "downloadThroughput": download_throughput,
        "uploadThroughput": upload_throughput,
    }
    if connection_type:
        params["connectionType"] = connection_type

    # 如果用 Selenium fallback,需要先 Network.enable 才能调用 emulateNetworkConditions
    if _network_ws is None and _browser_driver is not None:
        try:
            _browser_driver.execute_cdp_cmd("Network.enable", {})
        except Exception:
            pass  # 可能已启用,忽略错误

    _, err = _send_cdp_or_fallback("Network.emulateNetworkConditions", params)
    if err:
        return f"模拟网络条件失败: {err}"
    if offline:
        return "已模拟离线模式(无法访问网络)"
    parts = []
    if latency:
        parts.append(f"延迟={latency}ms")
    if download_throughput > 0:
        parts.append(f"下载={download_throughput}B/s({download_throughput // 1024}KB/s)")
    if upload_throughput > 0:
        parts.append(f"上传={upload_throughput}B/s({upload_throughput // 1024}KB/s)")
    if connection_type:
        parts.append(f"类型={connection_type}")
    desc = ", ".join(parts) if parts else "无限制"
    return f"已模拟网络条件: {desc}"


@mcp.tool()
def clear_network_conditions() -> str:
    """清除网络条件模拟,恢复正常网络。"""
    if not _is_chrome_running():
        return "Chrome 未启动,请先调用 open_browser"
    # Selenium fallback 时也需要先 Network.enable
    if _network_ws is None and _browser_driver is not None:
        try:
            _browser_driver.execute_cdp_cmd("Network.enable", {})
        except Exception:
            pass
    _, err = _send_cdp_or_fallback("Network.emulateNetworkConditions", {
        "offline": False,
        "latency": 0,
        "downloadThroughput": -1,
        "uploadThroughput": -1,
    })
    if err:
        return f"清除网络条件失败: {err}"
    return "已清除网络条件模拟,恢复正常网络"


@mcp.tool()
def set_geolocation(
    latitude: float, longitude: float, accuracy: float = 100
) -> str:
    """设置地理位置模拟(对应 Chrome DevTools 的 Sensors 面板)。

    常用于测试基于位置的 API(navigator.geolocation)和地图应用。

    Args:
        latitude: 纬度,如 39.9042(北京)
        longitude: 经度,如 116.4074(北京)
        accuracy: 精度(米),默认 100。传 0 或负值时清除地理位置模拟。
    """
    if not _is_chrome_running():
        return "Chrome 未启动,请先调用 open_browser"
    if accuracy <= 0:
        _, err = _send_cdp_or_fallback(
            "Emulation.clearGeolocationOverride", {}
        )
        if err:
            return f"清除地理位置失败: {err}"
        return "已清除地理位置模拟"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "accuracy": accuracy,
    }
    _, err = _send_cdp_or_fallback("Emulation.setGeolocationOverride", params)
    if err:
        return f"设置地理位置失败: {err}"
    return f"已设置地理位置: lat={latitude}, lng={longitude}, accuracy={accuracy}m"


if __name__ == "__main__":
    # 传输模式可通过环境变量 MCP_TRANSPORT 切换：
    #   stdio            - 本地 MCP 客户端通过子进程调用（默认，向后兼容）
    #   sse              - SSE 远程模式，任意 MCP 客户端通过 http://<host>:<port>/sse 连接
    #   streamable-http  - Streamable HTTP 远程模式（较新协议）
    # SSE 模式下 host/port 通过 MCP_HOST / MCP_PORT 环境变量配置（默认 127.0.0.1:8765）。
    # 在 agent.env 中配置即可，例如：
    #   MCP_TRANSPORT=sse
    #   MCP_HOST=127.0.0.1
    #   MCP_PORT=8765
    _transport = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()
    if _transport in ("sse", "streamable-http"):
        # FastMCP 构造函数已用默认值覆盖了 FASTMCP_ 环境变量（pydantic 显式参数优先级最高），
        # 因此这里运行时直接修改 settings，使 MCP_HOST / MCP_PORT 生效
        _host = os.environ.get("MCP_HOST", "127.0.0.1")
        _port = int(os.environ.get("MCP_PORT", "8765"))
        mcp.settings.host = _host
        mcp.settings.port = _port
        _endpoint = (
            f"http://{_host}:{_port}{mcp.settings.sse_path}"
            if _transport == "sse"
            else f"http://{_host}:{_port}"
        )
        print(f"[browser_mcp] 以 {_transport} 模式启动，连接端点: {_endpoint}")
        mcp.run(transport=_transport)
    else:
        mcp.run(transport="stdio")
