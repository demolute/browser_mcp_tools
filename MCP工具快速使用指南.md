# 🚀 MCP 浏览器自动化工具快速使用指南

> **工具总数**：57 个  
> **适用场景**：Selenium 浏览器自动化操作  
> **技术实现**：Selenium + Python + MCP 协议  
> **特殊说明**：鼠标/键盘/输入工具均使用 JavaScript dispatchEvent 模拟，兼容 Chrome attach 模式

---

## 📋 目录

1. [快速开始](#1-快速开始)
2. [浏览器操作](#2-浏览器操作)
3. [窗口管理](#3-窗口管理)
4. [元素定位方式](#4-元素定位方式)
5. [元素操作](#5-元素操作)
6. [Iframe 切换](#6-iframe-切换)
7. [文件上传](#7-文件上传)
8. [等待机制](#8-等待机制)
9. [弹窗处理](#9-弹窗处理)
10. [Cookie 操作](#10-cookie-操作)
11. [鼠标操作](#11-鼠标操作)
12. [键盘操作](#12-键盘操作)
13. [常见场景示例](#13-常见场景示例)
14. [排错指南](#14-排错指南)

---

## 1. 快速开始

### 第一步：打开浏览器

```python
# 打开浏览器并访问指定网站
open_browser(intent="访问百度 https://www.baidu.com")

# 或先打开浏览器，再导航
open_browser(intent="打开浏览器")
navigate(url="https://www.baidu.com")
```

### 第二步：基本操作流程

```text
1. 打开浏览器 → 2. 导航到页面 → 3. 等待页面加载 → 
4. 定位元素 → 5. 操作元素 → 6. 验证结果
```

### 元素定位方式速查

| `by` 参数 | 说明 | 示例 |
|----------|------|------|
| `id` | 通过 id 属性定位 | `by="id", value="submit-btn"` |
| `name` | 通过 name 属性定位 | `by="name", value="username"` |
| `class_name` | 通过 class 定位 | `by="class_name", value="btn-primary"` |
| `tag_name` | 通过标签名定位 | `by="tag_name", value="button"` |
| `css_selector` | CSS 选择器 | `by="css_selector", value="#main .btn"` |
| `xpath` | XPath 表达式 | `by="xpath", value="//button[@id='submit']"` |
| `link_text` | 链接完整文本 | `by="link_text", value="点击这里"` |
| `partial_link_text` | 链接部分文本 | `by="partial_link_text", value="点击"` |

**推荐**：优先使用 `id` → `css_selector` → `xpath`

---

## 2. 浏览器操作

### 打开/关闭浏览器

```python
# 打开浏览器（会自动分析 intent 中的 URL）
open_browser(intent="打开百度")
open_browser(intent="访问 B 站 https://www.bilibili.com")

# 关闭浏览器（谨慎使用，会完全关闭 Chrome）
close_browser()
```

### 页面导航

```python
# 访问指定 URL
navigate(url="https://www.example.com")

# 后退
back()

# 前进
forward()

# 刷新
refresh()

# 获取页面基本信息
get_page_info()
# 返回：页面标题: xxx\n当前 URL: xxx

# 获取页面源代码（返回前 50000 字符）
get_page_source()
```

### 截图与滚动

```python
# 截图并保存到文件
take_screenshot(file_path="d:\\scripts\\screenshot.png")

# 仅获取截图的 base64 编码
take_screenshot()

# 滚动页面
scroll_page(direction="down", pixels=500)   # 向下滚动 500 像素
scroll_page(direction="up", pixels=300)     # 向上滚动 300 像素
scroll_page(direction="right", pixels=200)  # 向右滚动
scroll_page(direction="left", pixels=200)   # 向左滚动
```

---

## 3. 窗口管理

### 窗口大小

```python
# 最大化
maximize_window()

# 最小化
minimize_window()

# 全屏（F11 效果）
fullscreen_window()

# 设置自定义大小
set_window_size(width=1280, height=720)
```

### 多标签页操作

```python
# 打开新窗口（可选指定 URL）
open_new_window(url="https://www.example.com")
open_new_window()  # 空白页

# 查看所有窗口
get_window_handles()
# 返回：
# 窗口数量: 2
# [0] handle=CDwindow-xxx... title=页面A
# [1] handle=CDwindow-yyy... title=页面B

# 切换窗口（按索引）
switch_window(index=0)  # 切换到第一个窗口
switch_window(index=1)  # 切换到第二个窗口

# 关闭当前窗口
close_window()
```

---

## 4. 元素定位方式

### 定位方式对比

| 方式 | 优点 | 缺点 | 适用场景 |
|------|------|------|---------|
| **id** | 速度最快，唯一 | 动态 id 不可用 | 有固定 id 的元素 |
| **name** | 简单直观 | 可能重复 | 表单元素 |
| **class_name** | 快速 | 可能不唯一 | 通用样式类 |
| **css_selector** | 功能强大，推荐 | 语法需学习 | 复杂选择 |
| **xpath** | 最灵活 | 性能稍差 | CSS 无法定位时 |
| **link_text** | 链接专用 | 仅适用于 `<a>` 标签 | 超链接 |

### CSS Selector 常用写法

```python
# id 选择器
by="css_selector", value="#submit-btn"

# class 选择器
by="css_selector", value=".btn-primary"

# 属性选择器
by="css_selector", value="input[name='username']"
by="css_selector", value="button[type='submit']"

# 层级选择器
by="css_selector", value="#main-content .item-list > li"

# 伪类
by="css_selector", value="button:first-child"
```

### XPath 常用写法

```python
# 属性匹配
by="xpath", value="//button[@id='submit']"
by="xpath", value="//input[@name='username']"

# 文本匹配
by="xpath", value="//button[text()='登录']"
by="xpath", value="//a[contains(text(), '忘记密码')]"

# 层级关系
by="xpath", value="//div[@id='form']//input"
by="xpath", value="//li[@class='item']/following-sibling::li"
```

---

## 5. 元素操作

### 点击元素

```python
# 点击按钮
click_element(by="id", value="submit-btn")

# 点击链接
click_element(by="link_text", value="立即注册")

# 点击 CSS 选择器定位的元素
click_element(by="css_selector", value=".btn-login")
```

### 输入文本

```python
# 输入文本（会自动先清空输入框）
input_text(by="id", value="username-input", text="my_username")

# 使用 type_text 输入（可控是否清空）
type_text(by="id", value="comment", text="这是一条评论", clear=True)
type_text(by="id", value="comment", text=" 追加内容", clear=False)
```

### 清除输入

```python
# 清空输入框
clear_input(by="id", value="search-input")
```

### 获取元素信息

```python
# 获取元素文本
get_element_text(by="css_selector", value="h1.title")

# 获取元素属性
get_element_attribute(by="id", value="link", attribute="href")
get_element_attribute(by="css_selector", value=".avatar", attribute="src")
get_element_attribute(by="tag_name", value="input", attribute="value")
```

### 执行 JavaScript

```python
# 执行任意 JS 并返回结果
execute_script(script="return document.title")

# 修改元素样式
execute_script(script="document.getElementById('hidden').style.display='block'")

# 触发事件
execute_script(script="document.getElementById('btn').click()")

# 滚动到元素
execute_script(script="arguments[0].scrollIntoView()", 
              # 需要在脚本中使用 arguments[0] 引用元素时，请先定位再结合 click_element
              )
```

---

## 6. Iframe 切换

> ⚠️ **重要**：元素在 iframe 内时，必须先切换到对应 iframe，否则会找不到元素！

### 单层 iframe

```python
# 切换到 iframe（通过 id 或 name）
switch_to_frame(frame_id_or_name="main-frame")

# 切回主文档 
switch_to_default()
```

### 多层嵌套 iframe

```python
# 逐层切换（用逗号分隔路径）
# 例如：外层 iframe id = outer，内层 id = inner
switch_to_nested_frame(frame_path="outer,inner")

# 三层嵌套
switch_to_nested_frame(frame_path="frame1,frame2,frame3")

# 操作完成后切回主文档
switch_to_default()
```

### 常见场景：上传组件在 iframe 内

```python
# 1. 切到嵌套 iframe
switch_to_nested_frame(frame_path="vw-video-up-frame,vw-video-up-frame")

# 2. 操作 iframe 内的上传按钮
click_element(by="css_selector", value=".upload-btn")

# 3. 切回主文档
switch_to_default()
```

---

## 7. 文件上传

### 方式一：upload_file（推荐）

```python
# 指定 by/value 定位 input[type=file]
upload_file(by="css_selector", value="input[type='file']", 
           file_path="d:\\documents\\photo.jpg")

# 如果定位失败，会自动搜索 shadow DOM 内的 input[type=file]
upload_file(by="id", value="file-input", file_path="d:\\video.mp4")
```

### 方式二：upload_file_direct（跳过等待，更快速）

```python
# 直接用 CSS 选择器定位并上传
upload_file_direct(css_selector="input[type=file]", 
                  file_path="d:\\reports\\document.pdf")

# 适合隐藏的 file input（会自动使其可见）
upload_file_direct(css_selector="#upload-form input[type=file]",
                  file_path="d:\\image.png")
```

### 高级场景：多层 iframe 内的上传

```python
# 1. 切换到 iframe
switch_to_nested_frame(frame_path="outer-frame,inner-frame")

# 2. 使用 JavaScript 先将隐藏的 input 显示出来
execute_script(
    script="""
    var input = document.querySelector('#upload-area input[type=file]');
    if (input) {
        input.style.display = 'block';
        input.style.visibility = 'visible';
    }
    """
)

# 3. 上传文件
upload_file_direct(css_selector="#upload-area input[type=file]",
                  file_path="d:\\video.mp4")

# 4. 切回主文档
switch_to_default()
```

---

## 8. 等待机制

> 💡 **最佳实践**：永远使用显式等待，代替固定的 `time.sleep()`

### 隐式等待（全局）

```python
# 设置全局隐式等待 10 秒（每次查找元素都等待最多 10 秒）
set_implicit_wait(seconds=10)

# 不建议设置过长，5-10 秒合适
set_implicit_wait(seconds=5)
```

### 显式等待（推荐）

```python
# 1. 等待元素出现在 DOM 中（即使不可见也返回）
wait_for_element(by="id", value="dynamic-content", timeout=10)

# 2. 等待元素可见（display != none）
wait_for_element_visible(by="css_selector", value=".loading-overlay", timeout=5)

# 3. 等待元素可点击
wait_for_clickable(by="id", value="submit-btn", timeout=8)

# 4. 等待元素包含指定文本
wait_for_text(by="id", value="status-message", text="操作成功", timeout=10)

# 5. 等待自定义 JS 条件为真
wait_until(
    script="return document.getElementById('progress-bar').style.width === '100%'",
    timeout=15
)

# 6. 等待页面完全加载
wait_for_page_load(timeout=30)
```

### 实际场景示例

```python
# 点击按钮后等待结果出现
click_element(by="id", value="search-btn")
wait_for_element_visible(by="css_selector", value=".search-results", timeout=10)
get_element_text(by="css_selector", value=".search-results .count")

# 等待列表加载完成
wait_until(
    script="return document.querySelectorAll('#item-list li').length >= 10",
    timeout=15
)

# 等待弹窗加载文本
wait_for_text(by="id", value="confirm-dialog", text="确定要删除吗？", timeout=5)
accept_alert()
```

---

## 9. 弹窗处理

### Alert（提示框）

```python
# 获取弹窗文本
get_alert_text()
# 返回：弹窗文本: 操作成功！

# 点击确定
accept_alert()
```

### Confirm（确认框）

```python
# 获取弹窗文本
get_alert_text()

# 点击确定（接受）
accept_alert()

# 点击取消
dismiss_alert()
```

### Prompt（输入框）

```python
# 输入文本并确定
send_alert_text(text="张三")
```

---

## 10. Cookie 操作

### 获取 Cookie

```python
# 获取所有 Cookie
get_cookie()
# 返回：
# Cookie 列表:
# session_id=abc123
# user_name=zhangsan

# 获取指定 Cookie
get_cookie(name="session_id")
# 返回：Cookie 'session_id': name=session_id, value=abc123
```

### 设置 Cookie

```python
# 添加 Cookie
add_cookie(name="theme", value="dark", path="/")

# 指定域名的 Cookie
add_cookie(name="login_token", value="xyz789", domain="example.com")
```

### 删除 Cookie

```python
# 删除单个
delete_cookie(name="temp_data")

# 删除所有
delete_all_cookies()
```

---

## 11. 鼠标操作

> 💡 **技术说明**：全部使用 JavaScript `dispatchEvent` 模拟，兼容 Chrome attach 模式

### 基础鼠标操作

```python
# 双击元素
double_click(by="id", value="zoom-image")

# 右键点击（显示自定义菜单）
right_click(by="css_selector", value=".file-item")

# 鼠标悬停（显示下拉菜单 / Tooltip）
hover(by="css_selector", value=".dropdown-trigger")
```

### 拖拽操作

```python
# 拖放：源元素 → 目标元素
drag_and_drop(
    by_source="id", value_source="draggable-item",
    by_target="id", value_target="drop-zone"
)

# 按偏移量拖动
drag_and_drop_by_offset(
    by="id", value="slider-thumb",
    x_offset=100,  # 向右拖 100 像素
    y_offset=0     # Y 轴不动
)
```

### 高级鼠标操作

```python
# 按住鼠标左键不释放
click_and_hold(by="id", value="draggable-box")

# 移动鼠标（按偏移）
move_mouse_by_offset(x=50, y=0)

# 在目标位置释放鼠标
release(by="id", value="target-container")
```

---

## 12. 键盘操作

> 💡 **技术说明**：使用 JavaScript 模拟键盘事件，组合键通过 execCommand 实现

### 输入文本

```python
# 输入文本（先清空）
type_text(by="id", value="post-title", text="我的第一篇文章", clear=True)

# 追加文本
type_text(by="id", value="comment", text=" 非常棒！", clear=False)
```

### 单个按键

```python
# Enter 键
press_key(by="id", value="search-input", key="enter")

# Tab 键（切换到下一个输入框）
press_key(by="id", value="username-field", key="tab")

# Escape 键（关闭弹窗）
press_key(by="tag_name", value="body", key="escape")

# 方向键
press_key(by="id", value="game-canvas", key="up")
press_key(by="id", value="game-canvas", key="down")
press_key(by="id", value="game-canvas", key="left")
press_key(by="id", value="game-canvas", key="right")

# 其他可用按键
# backspace, delete, space, home, end, page_up, page_down
# shift, control, alt, f1 ~ f12
```

### 组合键

```python
# Ctrl + A 全选
press_keys(by="id", value="text-editor", keys="control+a")

# Ctrl + C 复制
press_keys(by="id", value="text-editor", keys="control+c")

# Ctrl + V 粘贴
press_keys(by="id", value="paste-area", keys="control+v")

# Ctrl + X 剪切
press_keys(by="id", value="text-editor", keys="control+x")

# Shift + 字符（大写）
press_keys(by="id", value="text-editor", keys="shift+a")  # 输入大写 A
```

### 高级按键控制

```python
# 按住 Shift 不释放
key_down(key="shift", by="id", value="text-area")

# 执行其他操作...（此时输入的字母会是大写）

# 释放 Shift
key_up(key="shift", by="id", value="text-area")
```

---

## 13. 常见场景示例

### 场景一：用户登录

```python
# 1. 打开登录页
open_browser(intent="访问登录页 https://example.com/login")
wait_for_page_load(timeout=15)

# 2. 输入用户名
type_text(by="id", value="username", text="my_account", clear=True)

# 3. 输入密码
type_text(by="id", value="password", text="my_password_123", clear=True)

# 4. 点击登录（先等待按钮可点击）
wait_for_clickable(by="id", value="login-btn", timeout=5)
click_element(by="id", value="login-btn")

# 5. 等待登录成功（检测跳转到首页或出现欢迎文本）
wait_for_text(by="css_selector", value=".welcome-msg", text="欢迎回来", timeout=10)
print("登录成功！")
```

### 场景二：搜索内容并验证结果

```python
# 1. 打开网站
navigate(url="https://www.example.com")

# 2. 输入搜索关键词
type_text(by="name", value="q", text="Python 教程", clear=True)

# 3. 按 Enter 搜索
press_key(by="name", value="q", key="enter")

# 4. 等待结果加载
wait_for_element(by="css_selector", value=".search-result-list", timeout=10)

# 5. 获取搜索结果数量
result_count = get_element_text(by="css_selector", value=".result-count")
print(f"搜索结果: {result_count}")
```

### 场景三：填写表单并提交

```python
# 1. 等待表单加载
wait_for_element_visible(by="id", value="registration-form", timeout=10)

# 2. 填写文本字段
type_text(by="id", value="full-name", text="张三", clear=True)
type_text(by="id", value="email", text="zhangsan@example.com", clear=True)
type_text(by="id", value="phone", text="13800138000", clear=True)

# 3. 下拉框选择（使用 JavaScript 或点击展开后选择）
execute_script(script="document.getElementById('country').value = 'CN';")
execute_script(script="document.getElementById('country').dispatchEvent(new Event('change'));")

# 4. 勾选 checkbox（点击外层自定义 span）
click_element(by="css_selector", value="span.bcc-checkbox-checkbox")

# 5. 悬停显示协议
hover(by="css_selector", value=".terms-link")

# 6. 提交表单
click_element(by="id", value="submit-btn")

# 7. 验证提交成功
wait_for_text(by="id", value="result-message", text="提交成功", timeout=15)
```

### 场景四：文件上传到 B 站

```python
# 1. 打开上传页
navigate(url="http://127.0.0.1:8080/member/platform/upload/video/frame")
wait_for_page_load(timeout=15)

# 2. 切换到嵌套 iframe（上传组件在双层 iframe 内）
switch_to_nested_frame(frame_path="vw-video-up-frame,vw-video-up-frame")

# 3. 使隐藏的 file input 可见
execute_script(script="""
    var input = document.querySelector('.upload-wrp input[type=file]');
    if (input) {
        input.style.display = 'block';
        input.style.visibility = 'visible';
        input.style.opacity = '1';
        input.scrollIntoView();
    }
""")

# 4. 上传视频
upload_file_direct(
    css_selector=".upload-wrp input[type=file]",
    file_path="d:\\脚本\\ai\\video.mp4"
)

# 5. 切回主文档填写信息
switch_to_default()
wait_for_element(by="css_selector", value=".video-basic-wrp", timeout=10)

# 6. 填写标题
type_text(
    by="css_selector",
    value="input[placeholder='请输入稿件标题']",
    text="【AI编程】自动化测试视频",
    clear=True
)

# 7. 选择分区（使用 JavaScript 模拟）
click_element(by="css_selector", value=".select-controller")
wait_for_element(by="css_selector", value=".vw-partition-option", timeout=5)
execute_script(script="document.querySelectorAll('.vw-partition-option')[2].click()")
```

### 场景五：处理弹窗

```python
# 场景 1：操作触发 alert
click_element(by="id", value="confirm-delete")
# 等待弹窗出现（通常 alert 是同步的，但仍建议用 try-except）
try:
    text = get_alert_text()
    if "确定删除" in text:
        accept_alert()
except:
    pass  # 无弹窗

# 场景 2：prompt 输入框
send_alert_text(text="张三")

# 场景 3：confirm 取消
dismiss_alert()
```

### 场景六：多标签页操作

```python
# 1. 当前在首页，打开新标签页
open_new_window(url="https://www.example.com/details")

# 2. 获取所有窗口
get_window_handles()

# 3. 切换到详情页（索引 1）
switch_window(index=1)
wait_for_page_load(timeout=10)

# 4. 获取详情页内容
title = get_element_text(by="css_selector", value="h1")

# 5. 关闭详情页，切回首页
close_window()
switch_window(index=0)
```

### 场景七：Cookie 实现免登录

```python
# 第一次：手动登录后保存 Cookie
cookies = get_cookie()
# 将 cookies 保存到文件或环境变量

# 第二次：打开网站后注入 Cookie
navigate(url="https://example.com")  # 先访问同域名
delete_all_cookies()  # 可选：清除已有

# 逐个添加 Cookie
add_cookie(name="session_id", value="保存的session值", domain="example.com")
add_cookie(name="user_token", value="保存的token值", domain="example.com")

# 刷新页面，已自动登录
refresh()
wait_for_page_load(timeout=10)
```

### 场景八：拖拽排序

```python
# 1. 获取列表项位置
# 2. 将第 3 项拖到第 1 项
drag_and_drop(
    by_source="css_selector", value_source="#list li:nth-child(3)",
    by_target="css_selector", value_target="#list li:nth-child(1)"
)

# 或者按偏移拖动
drag_and_drop_by_offset(
    by="css_selector", value="#list li:nth-child(5)",
    x_offset=0,
    y_offset=-200  # 向上拖 200 像素
)
```

### 场景九：鼠标悬停菜单

```python
# 导航栏菜单：悬停显示子菜单
hover(by="css_selector", value=".nav-item.products")

# 等待子菜单显示
wait_for_element_visible(by="css_selector", value=".sub-menu.products", timeout=3)

# 点击子菜单项
click_element(by="link_text", value="产品详情")
```

### 场景十：测试延迟加载的列表

```python
# 使用 wait_until 等待列表加载完成
wait_until(
    script="return document.querySelectorAll('#data-list .item').length >= 20",
    timeout=30
)

# 或者等待加载动画消失
wait_until(
    script="return !document.querySelector('.loading-spinner') || "
           "getComputedStyle(document.querySelector('.loading-spinner')).display === 'none'",
    timeout=15
)

# 获取列表项数量
item_count = execute_script(
    script="return document.querySelectorAll('#data-list .item').length"
)
```

---

## 14. 排错指南

### ❌ 问题 1：找不到元素

**可能原因及解决方案**：

| 原因 | 解决方案 |
|------|---------|
| 元素还没加载 | 使用 `wait_for_element` 或 `wait_for_element_visible` |
| 元素在 iframe 内 | 使用 `switch_to_frame` 或 `switch_to_nested_frame` |
| 定位方式错误 | 尝试 `css_selector` 或 `xpath` |
| 元素在新窗口中 | 使用 `switch_window` 切换 |

**调试步骤**：
```python
# 1. 用 execute_script 手动检查
execute_script(script="return document.querySelector('#my-id') ? '存在' : '不存在'")

# 2. 检查是否在 iframe 中
# 先用 switch_to_default，再检查
```

### ❌ 问题 2：点击无效

**可能原因**：
- 元素被其他元素遮挡
- 元素 disabled / 不可点击
- 自定义组件需要点击外层容器

**解决方案**：
```python
# 1. 先等待可点击
wait_for_clickable(by="id", value="btn", timeout=5)
click_element(by="id", value="btn")

# 2. 使用 JavaScript 点击
execute_script(script="document.getElementById('btn').click()")

# 3. 自定义组件点击外层（如自定义 checkbox）
click_element(by="css_selector", value="span.bcc-checkbox-checkbox")
```

### ❌ 问题 3：输入文本无效

**解决方案**：
```python
# 使用 type_text 代替 input_text（内部用 JS 模拟）
type_text(by="id", value="input-field", text="要输入的内容", clear=True)

# 手动清除后再输入
clear_input(by="id", value="input-field")
type_text(by="id", value="input-field", text="内容", clear=False)
```

### ❌ 问题 4：文件上传失败

**步骤排查**：
```python
# 1. 检查是否在 iframe 内
switch_to_nested_frame(frame_path="outer,inner")

# 2. 检查 input[type=file] 是否存在
execute_script(script="return document.querySelector('input[type=file]') ? '存在' : '不存在'")

# 3. 手动使其可见
execute_script(script="""
    var input = document.querySelector('input[type=file]');
    if (input) {
        input.style.display = 'block';
        input.style.visibility = 'visible';
    }
""")

# 4. 使用 upload_file_direct 上传
upload_file_direct(css_selector="input[type=file]", file_path="绝对路径")
```

### ❌ 问题 5：拖拽无效

**解决方案**：
```python
# 大多数情况下 drag_and_drop 已使用 JS 模拟
# 如果仍不生效，尝试：

# 方式1：click_and_hold + move + release
click_and_hold(by="id", value="source")
move_mouse_by_offset(x=200, y=0)
release(by="id", value="target")

# 方式2：直接修改 DOM 位置
execute_script(script="""
    var source = document.getElementById('source');
    var target = document.getElementById('target');
    target.appendChild(source);
""")
```

### ❌ 问题 6：Invalid Session ID

**原因**：driver 会话失效（可能是 Chrome 被手动关闭或重启）

**解决方案**：
```python
# 重新打开浏览器（会自动 attach 运行中的 Chrome）
open_browser(intent="重新连接浏览器")
```

---

## 📎 附录：工具速查表

| 分类 | 工具名称 | 快速说明 |
|------|---------|---------|
| **浏览器** | `open_browser` | 打开浏览器 |
| | `close_browser` | 关闭浏览器 |
| | `navigate` | 访问 URL |
| | `back` / `forward` | 前进/后退 |
| | `refresh` | 刷新 |
| | `get_page_info` | 获取标题和 URL |
| | `get_page_source` | 获取源代码 |
| **窗口** | `maximize_window` | 最大化 |
| | `minimize_window` | 最小化 |
| | `set_window_size` | 设置大小 |
| | `fullscreen_window` | 全屏 |
| | `get_window_handles` | 查看所有窗口 |
| | `open_new_window` | 打开新标签页 |
| | `switch_window` | 切换标签页 |
| | `close_window` | 关闭当前标签 |
| **元素** | `click_element` | 点击元素 |
| | `input_text` | 输入文本（先清空） |
| | `type_text` | 输入文本（可控清空） |
| | `clear_input` | 清空输入框 |
| | `get_element_text` | 获取元素文本 |
| | `get_element_attribute` | 获取元素属性 |
| | `execute_script` | 执行 JS |
| **Iframe** | `switch_to_frame` | 切换单层 iframe |
| | `switch_to_nested_frame` | 切换多层嵌套 iframe |
| | `switch_to_default` | 切回主文档 |
| **上传** | `upload_file` | 上传文件（推荐） |
| | `upload_file_direct` | 快速上传 |
| **等待** | `set_implicit_wait` | 隐式等待 |
| | `wait_for_element` | 等待元素在 DOM |
| | `wait_for_element_visible` | 等待元素可见 |
| | `wait_for_clickable` | 等待可点击 |
| | `wait_for_text` | 等待文本出现 |
| | `wait_until` | 等待 JS 条件 |
| | `wait_for_page_load` | 等待页面加载 |
| **弹窗** | `get_alert_text` | 获取弹窗文本 |
| | `accept_alert` | 确定 |
| | `dismiss_alert` | 取消 |
| | `send_alert_text` | Prompt 输入 |
| **Cookie** | `get_cookie` | 获取 |
| | `add_cookie` | 添加 |
| | `delete_cookie` | 删除单个 |
| | `delete_all_cookies` | 删除全部 |
| **截图滚动** | `take_screenshot` | 截图 |
| | `scroll_page` | 滚动 |
| **鼠标** | `double_click` | 双击 |
| | `right_click` | 右键 |
| | `hover` | 悬停 |
| | `drag_and_drop` | 拖放 |
| | `drag_and_drop_by_offset` | 按偏移拖动 |
| | `click_and_hold` | 按住左键 |
| | `release` | 释放鼠标 |
| | `move_mouse_by_offset` | 移动鼠标 |
| **键盘** | `press_key` | 按单个键 |
| | `press_keys` | 按组合键 |
| | `type_text` | 输入文本 |
| | `key_down` | 按住键 |
| | `key_up` | 释放键 |

---

## 🎯 最佳实践总结

1. **优先使用等待机制**：`wait_for_element_visible` > `wait_for_element` > 固定等待
2. **定位元素首选**：`id` → `css_selector` → `xpath`
3. **操作前检查上下文**：iframe 内？弹窗中？新标签页？
4. **输入操作推荐**：`type_text`（JS 模拟）优于 `input_text`（Selenium 原生）
5. **复杂组件处理**：使用 `execute_script` 或 `click_element` 点击外层容器
6. **上传文件步骤**：切 iframe → 找 input → 显示元素 → 上传

---

*文档版本：v1.0 | 最后更新：2026-08-04*
