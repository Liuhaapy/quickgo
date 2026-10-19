# WPS / 金山表单定时自动提交

这是一个基于 **Python + Playwright** 的表单助手：

- 浏览器由 Playwright 启动，使用独立用户目录保存登录态。
- 你手动扫码登录、完成滑块验证；脚本不绕过验证码。
- 脚本先填表，**不会提前点提交**。
- 用 NTP（阿里云等）和 HTTP 时间 API 校准时钟，目标时间前约 1 秒开始高频检查按钮。
- 到达目标时间后才点击，默认只点一次，避免重复提交。
- 可选 `--fast-open`：字段在开放前是 `disabled` 时，不强行填写，等页面自己启用后快速填写并提交。
- 结果、失败现场都会截图到 `artifacts/`。

> 请只在你有权提交的表单上使用，并遵守表单发布方的规则。不要用它绕过访问控制、验证码或频繁刷接口。定时提交本身可能影响公平性，请先确认活动规则允许。

> **电脑小白请看：[新手教程.md](新手教程.md)**，按里面的数字顺序双击 `.bat` 文件即可。

---

## 1. 文件说明

| 文件 | 作用 |
|---|---|
| `wps_auto_submit.py` | 主脚本 |
| `config.json` | 你的实际配置（已经生成，需要修改） |
| `config.example.json` | 配置示例 |
| `requirements.txt` | Python 依赖 |
| `run.ps1` | Windows PowerShell 一键运行脚本 |
| `run.bat` | 可双击运行的批处理入口（内部调用 `run.ps1`） |
| `tests/mock_form.html` | 本地模拟表单，用于演练 |
| `tests/test_config.json` | 本地模拟表单的测试配置 |
| `tests/test_locked_config.json` | 字段在开放前被 `disabled` 的测试配置（配合 `--fast-open`） |
| `wps_auto_submit.log` | 运行日志（自动生成） |
| `artifacts/` | 截图目录（自动生成） |
| `.wps_profile/` | 浏览器登录态（自动生成，不要删） |

---

## 0. 电脑小白：按顺序双击这几个文件

已经为你准备了带编号的批处理文件，不用打开命令行：

| 双击这个文件 | 作用 |
|---|---|
| `0-恢复配置.bat` | 如果 `config.json` 改坏了，用备份恢复 |
| `1-修改配置.bat` | 用记事本打开 `config.json`，改表单地址和答案 |
| `2-查看页面控件.bat` | 打开表单，打印输入框/选项/按钮，帮你写 selector |
| `3-只填表不提交.bat` | 演练：只填表，绝不提交 |
| `4-正式提交.bat` | 正式运行：等到目标时间再点提交；自动带 `--fast-open` |
| `5-测试本地表单.bat` | 不联网的演示，验证安装是否正常 |

### 第 1 步：改配置

双击 `1-修改配置.bat`，会弹出记事本。第一次打开时会自动把原配置备份为 `config.backup.json`。重点改：

- `form_url`：浏览器地址栏里的 WPS 表单链接；
- `submit_at`：例如 `"12:40:00"`；
- `fields` 里的 `value` / `option` / `options`：你的答案；
- `submit_button` 可以先留空，找不到按钮时再填。

改完按 `Ctrl + S` 保存，关闭记事本。

注意 JSON 格式：

- 文字要用英文双引号包起来，例如 `"张三"`；
- 每一项之间用英文逗号分隔；
- 不要删除大括号 `{}` 和方括号 `[]`；
- 最后一项后面不要多写逗号。

如果保存后发现 JSON 格式坏了、脚本报“Expecting...”之类错误，双击 `0-恢复配置.bat`，会恢复成第一次打开时的 `config.backup.json`。

### 第 2 步：先本地测试安装

双击 `5-测试本地表单.bat`。

它会自动打开一个本地模拟表单，等 4 秒后填写并提交。最后窗口显示：

```text
提交成功。
Done. Press any key to close this window.
```

看到这个就说明环境没问题。按任意键关闭。

### 第 3 步：查看真实表单的控件

双击 `2-查看页面控件.bat`。

1. Edge 会打开你的 WPS 表单；
2. 如果需要登录，先扫码登录、完成滑块；
3. 确认表单题目已经显示；
4. 回到黑色命令行窗口，按一次 `Enter`；
5. 窗口会打印每个输入框、选项、按钮的 `id`、`name`、`placeholder`、`label`、`class`，并给出“可尝试”的 selector；
6. 看完按任意键关闭。

把打印出来的 selector 填到 `config.json` 对应字段的 `selector` 里即可。不会填也没关系，把打印内容发给我。

### 第 4 步：只填表，不提交

双击 `3-只填表不提交.bat`。

1. Edge 打开表单；
2. 扫码登录、完成滑块；
3. 回到黑色窗口按 `Enter`；
4. 脚本自动填表；
5. 填完后会截图到 `artifacts` 文件夹，然后退出，**不会提交**。

打开 `artifacts` 里最新的截图，检查每个答案是否填对。如果不对，回到第 1 步改 `config.json`。

### 第 5 步：正式运行

双击 `4-正式提交.bat`。

1. Edge 打开表单；
2. 扫码登录、完成滑块；
3. 回到黑色窗口按 `Enter`；
4. 脚本开始填表/等待；
5. **之后不要碰鼠标键盘**，让它自己等到 12:40:00 点击提交；
6. 看到“提交成功”后，截图会保存在 `artifacts`。

如果表单在开放前把输入框禁用了，`4-正式提交.bat` 已经自动带 `--fast-open`：它会等页面自己启用字段后再快速填写，不会强行改 `disabled`。

### 出问题先看哪里

- 黑色窗口里的红色/黄色文字：会直接说明哪个字段失败；
- `artifacts` 文件夹：有失败现场截图；
- `wps_auto_submit.log`：完整日志；
- 如果提示 `请先把 config.form_url 换成真实的 WPS 表单地址`，说明第 1 步还没改地址。

---

## 2. 安装依赖

需要 **Python 3.10+**（本机是 3.12）。

### 2.1 标准安装（推荐）

在项目目录打开 PowerShell：

```powershell
cd "C:\Users\lenovo\Documents\deepseek-harness\default-workspace"

py -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install -r requirements.txt
```

如果 PowerShell 禁止执行激活脚本，可以只对当前窗口放开：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### 2.2 浏览器：优先用系统 Edge

脚本默认 `"browser": "edge"`，直接使用 Windows 自带的 Microsoft Edge，**不需要再下载 Chromium**。

如果你的机器没有 Edge，或想用 Playwright 自带 Chromium：

```powershell
python -m playwright install chromium
```

然后把 `config.json` 改成：

```json
"browser": "chromium"
```

### 2.3 本工作区已经装好的离线依赖

这个工作区里已经有一个 `.deps/` 目录（Playwright + greenlet + pyee + tzdata），以及 `.venv/`。`.venv\Lib\site-packages\wps_deps.pth` 已经把 `.deps` 加入模块搜索路径，所以直接用 `.venv\Scripts\python.exe` 也能 `import playwright`。

如果标准 `pip install` 在你的环境里因为临时目录权限失败，可以直接：

```powershell
.\run.ps1
```

或者双击 `run.bat`。

`run.ps1` 会自动优先使用 `.venv` 里的 Python，并把 `.deps` 加入 `PYTHONPATH`；`.venv` 在这里主要作为解释器，依赖实际从 `.deps` 加载。在普通电脑上仍然建议用 2.1 的标准安装。

---

## 3. 需要你手动替换的变量

打开 `config.json`，重点改这几项：

### 3.1 表单地址

```json
"form_url": "https://f.wps.cn/g/你的表单ID/"
```

脚本检测到 `请替换` 或 `REPLACE_ME` 会直接报错，避免误运行。

### 3.2 提交时间

```json
"submit_at": "12:40:00"
```

支持两种写法：

- `"12:40:00"`：今天 12:40:00；如果今天这个时间已经过了，脚本会报错，不会自动排到明天。
- `"2026-10-20 12:40:00"`：指定日期。

时区默认 `Asia/Shanghai`。

### 3.3 提交按钮

```json
"submit_button": ""
```

留空时脚本会自动找文字包含“提交”的按钮。WPS 表单的按钮结构可能变，如果自动找不到，就手动写选择器：

```json
"submit_button": "button:has-text('提交')"
```

或者：

```json
"submit_button": "text=提交"
```

或者用 XPath：

```json
"submit_button": "xpath=//button[contains(., '提交')]"
```

如果按钮在开放前文字是“未开放”，开放后才变成“提交”，**建议直接写按钮的 CSS 选择器**，例如：

```json
"submit_button": "button.submit-btn"
```

这样脚本在开放前也能定位到它，检查它的 `disabled` 状态。

### 3.4 字段列表

每个字段是一个对象。`type` 支持：

| type | 用途 | 必填 |
|---|---|---|
| `text` / `textarea` / `input` | 文本框、多行文本 | `selector` + `value`，或 `question` + `value` |
| `radio` / `single` | 单选 | `option`，以及 `question`/`container`/`selector` |
| `checkbox` / `multi` | 多选 | `options` 数组，以及 `question`/`container` |
| `select` / `dropdown` | 下拉框 | `selector` + `value`，或 `question` + `value` |
| `click` | 点某个自定义控件 | `selector` |

#### 填空题示例

```json
{
  "label": "姓名",
  "type": "text",
  "selector": "input[placeholder*='姓名']",
  "value": "张三"
}
```

如果输入框没有合适的 placeholder，可以用题目文字自动找：

```json
{
  "label": "姓名",
  "type": "text",
  "question": "姓名",
  "value": "张三"
}
```

脚本会找到“姓名”这个题目文字，再向上找最近的、包含输入框的题目容器。

#### 选择题示例（按题目 + 选项文字）

```json
{
  "label": "班级",
  "type": "radio",
  "question": "班级",
  "option": "一班"
}
```

```json
{
  "label": "兴趣",
  "type": "checkbox",
  "question": "兴趣",
  "options": ["阅读", "跑步"]
}
```

这是最不依赖 WPS 内部 class 的写法，推荐优先用。

#### 选择题示例（直接写选项选择器）

```json
{
  "label": "班级",
  "type": "radio",
  "selector": "label:has-text('一班')"
}
```

注意：这种写法只适合单选，或只选一个复选框。多选请用 `options` 数组。

#### 下拉框示例

```json
{
  "label": "学院",
  "type": "select",
  "selector": "select[name='college']",
  "value": "计算机学院"
}
```

#### 自定义点击控件

```json
{
  "label": "同意条款",
  "type": "click",
  "selector": ".agreement-checkbox"
}
```

### 3.5 怎么找 CSS / XPath / Playwright 选择器

1. 在浏览器里按 `F12` 打开开发者工具。
2. 点左上角的“选择元素”图标，再点表单里的输入框或按钮。
3. 在 Elements 面板里右键选中的元素：
   - `Copy` → `Copy selector`：得到 CSS 选择器。
   - `Copy` → `Copy XPath`：得到 XPath。
4. 把 CSS 选择器直接填到 `selector`。
5. 如果复制的是 XPath，前面加 `xpath=`：

```json
"selector": "xpath=//input[@placeholder='姓名']"
```

Playwright 还支持这些方便写法：

```text
text=提交
button:has-text("提交")
input[placeholder*="姓名"]
label:has-text("一班")
```

如果不想手动翻 F12，可以先用：

```powershell
python wps_auto_submit.py --config config.json --inspect --no-pause
```

它会打开表单，等登录完成后，把页面上可见的输入框、单选项、复选项、按钮及其 `id`、`name`、`placeholder`、`label`、`class` 打印出来，并给出一个可尝试的 selector。这个模式不会填表，也不会提交，所以 `fields` 可以先不改；但 `form_url` 要先改成真实地址。

### 3.6 题目容器定位不准怎么办

WPS 表单的 DOM 可能嵌套很深。如果 `question` 自动定位到了错误的区域，可以手动指定 `container`：

```json
{
  "label": "班级",
  "type": "radio",
  "container": "xpath=//div[contains(@class,'form-item')][.//*[contains(text(),'班级')]]",
  "option": "一班"
}
```

`container` 应该是一个“题目块”，里面同时包含题目文字和选项。

### 3.7 其它常用配置

```json
{
  "poll_interval_ms": 20,
  "open_grace_seconds": 10,
  "submit_delay_ms": 0,
  "max_click_attempts": 1,
  "strict_fill": true,
  "wait_for_fields": false,
  "refresh_before_seconds": 0,
  "verify_before_submit": true,
  "screenshot": true,
  "slow_mo_ms": 0
}
```

- `poll_interval_ms`：目标前 1 秒开始检查按钮的间隔，默认 20ms。
- `open_grace_seconds`：到点后按钮还没开放时，最多再等几秒。
- `submit_delay_ms`：到点后再延迟多少毫秒点击，默认 0。
- `max_click_attempts`：默认 1，避免重复提交。只有页面明确提示“未开始”时才建议改 2。
- `strict_fill`：填表失败是否立即中止，建议保持 `true`。
- `wait_for_fields`：字段开放前不强行填，等页面启用后快速填写；也可以用 `--fast-open` 开启。
- `refresh_before_seconds`：目标前多少秒自动刷新表单，0 表示不刷新；`4-正式提交.bat` 默认用 1 秒。
- `verify_before_submit`：提交前检查字段是否为空并尝试补填。
- `screenshot`：是否保存截图。
- `slow_mo_ms`：Playwright 每步操作放慢的毫秒数，调试时可设 100。

如果 WPS 对 `fill()` 比较敏感，可以给文本字段加逐字输入：

```json
{
  "label": "姓名",
  "type": "text",
  "selector": "input[placeholder*='姓名']",
  "value": "张三",
  "typing": true,
  "typing_delay_ms": 30
}
```

`typing: true` 会用 `press_sequentially` 逐字输入，速度慢一些，但更像人工操作。

---

## 4. 运行流程

### 4.1 正式运行

```powershell
python wps_auto_submit.py --config config.json
```

或：

```powershell
.\run.ps1
```

也可以直接双击 `run.bat`。如果字段在开放前是 `disabled`，用：

```powershell
.\run.ps1 --fast-open
```

流程：

1. 脚本校准网络时间。
2. 启动 Edge，打开表单。
3. 终端提示你扫码登录 / 完成滑块 / 确认表单已显示。
4. 你回到终端按 `Enter`。
5. 脚本自动填表，并在终端打印每个字段的结果。
6. 脚本进入等待，目标时间前约 1 秒开始高频检查提交按钮。
7. 到达 12:40:00 后点击提交。
8. 检测成功提示，截图到 `artifacts/`。

**建议提前 5~10 分钟运行**，不要等到 12:39:50 才启动。浏览器启动、人工扫码都需要时间。

### 4.2 只填表，不提交（强烈建议先演练）

```powershell
python wps_auto_submit.py --config config.json --fill-only
```

它会打开浏览器、等你登录、填表、截图，然后退出，**不会等待，也不会点提交**。这个模式会跳过网络校时和目标时间校验，所以即使 12:40 已经过了也能用来检查选择器。先用这个模式确认每个字段都填对了。

### 4.3 测试定时点击（本地模拟表单）

```powershell
python wps_auto_submit.py `
  --config tests/test_config.json `
  --target-in 8 `
  --no-pause `
  --no-time-sync `
  --headless
```

`--target-in 8` 表示 8 秒后提交，用来验证“填表 → 等待 → 到点点击 → 检测成功”的完整流程。模拟表单的提交按钮会在页面加载 4 秒后开放。

### 4.4 其它参数

| 参数 | 作用 |
|---|---|
| `--target "2026-10-20 12:40:00"` | 覆盖配置里的时间 |
| `--target-in 10` | 测试用：10 秒后提交 |
| `--no-time-sync` | 不请求网络时间，使用本机时钟 |
| `--fill-only` | 只填表，不等待、不提交 |
| `--fast-open` | 字段开放前不强行填，等页面启用后快速填写并提交 |
| `--refresh-before 1` | 目标前 1 秒自动刷新一次表单页面 |
| `--inspect` | 只打印页面控件和 selector 建议，不填表、不提交 |
| `--no-pause` | 跳过“按 Enter 继续” |
| `--headless` | 无头模式，无法扫码，只建议测试 |
| `--browser edge/chrome/chromium` | 覆盖浏览器 |
| `--profile 路径` | 覆盖登录态目录 |
| `--debug` | 输出调试日志 |

### 4.5 字段在 12:40 前是 disabled：`--fast-open`

如果表单在开放前把输入框本身设为 `disabled`，普通模式无法提前填写。这时用：

```powershell
.\run.ps1 --fast-open
```

或者在 `config.json` 里设：

```json
"wait_for_fields": true
```

这个模式的行为：

1. 先只读检查所有配置字段是否可用。
2. 如果字段还是 `disabled`，脚本**不会修改 `disabled`、不会用 JS 注入值**，只会等待。
3. 目标前用 0.5 秒间隔检查；临近目标时用 20ms 间隔检查。
4. 页面自己把字段启用后，立即用快速模式填写：跳过滚动、点击、失焦等步骤，直接 `fill`。
5. 填完后仍然走原来的“到点才允许点击”逻辑。

本地锁定表单的实测结果：

- 字段在页面加载 4 秒后才解除 `disabled`；
- 脚本检测到开放后，用约 **300ms** 填完 5 个字段；
- 如果目标时间还没到，就继续等；如果目标时间已经过了，就立即点击提交。

测试命令：

```powershell
python wps_auto_submit.py `
  --config tests/test_locked_config.json `
  --target-in 10 `
  --no-pause `
  --headless `
  --fast-open
```

这个模式不能把“12:40 才开放”变成“12:39 就能填”。它只是把开放后的操作压缩到最快，不绕过页面的开放状态。

### 4.6 目标前自动刷新表单

`4-正式提交.bat` 已经内置自动刷新。文件里第一行有效设置是：

```bat
set REFRESH_BEFORE=1
```

意思是：在目标提交时间前 **1 秒**自动刷新一次表单页面，然后重新填写、等待到点、提交。

刷新发生在**填表之前**，所以刷新会清空页面上已有的内容，然后脚本会按 `config.json` 重新填写。不要在刷新后手动输入，脚本会接管。

如果 WPS 页面加载比较慢，提交时间明显晚于目标时间，可以把 `4-正式提交.bat` 里的 `1` 改成 `2` 或 `3`。如果不想自动刷新，改成 `0`。

手动运行等价命令：

```powershell
.\run.ps1 --fast-open --refresh-before 1
```

注意：刷新会重新加载页面，如果登录态刚好过期，需要在刷新后的页面重新扫码登录。建议提前几分钟运行，不要卡到 12:39:59 才开始。

---

## 5. 时间控制是怎么做的

1. 启动时优先用 **NTP**：
   - `ntp.aliyun.com`
   - `ntp1.aliyun.com`
   - `time.windows.com`
2. NTP 不可用时，退回 HTTP 时间源：
   - 淘宝 mtop 毫秒时间戳
   - 腾讯视频 `checktime`
   - 苏宁 `getSysTime`
   - 百度 / 京东 / 腾讯网响应头 `Date`
3. 对每个源取 3 个样本，去掉离群值，取中位数，按往返时延做中点修正。
4. 校时成功后用 `time.monotonic()` 推进，**不会用 `time.sleep()` 死等几个小时**。
5. 每隔 5 分钟重新校时；目标前 65 秒做最后一次校时。
6. 目标前约 1 秒开始每 20ms 检查按钮的 `disabled`、`aria-disabled`、`pointer-events`、`opacity`、文字等状态。
7. 即使按钮提前变亮，也会等到目标时间才点。
8. 到达目标时间后，如果按钮还没开放，最多再等 `open_grace_seconds` 秒；仍不可点击就放弃并截图，不会盲点。

> 如果只成功使用了 HTTP `Date` 这种秒级时间源，精度大约 ±1 秒，可能晚 0~1 秒。想要更准，请确保 NTP 的 UDP 123 端口没被防火墙拦截。

如果担心本机时钟和服务器仍有细微差异，可以设置：

```json
"submit_delay_ms": 200
```

表示目标时间后再等 200ms 点击。默认 0。

---

## 6. 常见问题

### 6.1 找不到提交按钮

先在 `--fill-only` 模式里确认页面已经加载完。然后手动设置：

```json
"submit_button": "button:has-text('提交')"
```

如果按钮是 `div`：

```json
"submit_button": "div.submit-btn"
```

如果还是找不到，用 F12 复制按钮的 CSS 选择器。

### 6.2 找不到输入框 / 填错字段

优先给字段写 `selector`。如果 WPS 的 placeholder 会变，用题目文字定位：

```json
{
  "label": "姓名",
  "type": "text",
  "question": "姓名",
  "value": "张三"
}
```

定位不准时加 `container`。

### 6.3 选择题点不上

WPS 的选项可能是 `label`，也可能是 `div`。先试：

```json
{
  "type": "radio",
  "question": "班级",
  "option": "一班"
}
```

不行就复制选项元素的 CSS 选择器：

```json
{
  "type": "radio",
  "selector": "label:has-text('一班')"
}
```

### 6.4 提示“字段可能尚未开放 / 只读”

如果表单在 12:40 之前把输入框也禁用了，Playwright 无法 `fill`。脚本不会去改 `disabled` 或注入值。用 `--fast-open`：它会等页面自己启用字段后，立即快速填写并提交。开放前仍然是填不了的，这是发布方的限制。

### 6.5 登录态过期

脚本使用 `.wps_profile/` 持久化目录。第一次扫码后，下次通常不用再扫。但 WPS 可能使登录态过期；正式运行前先用 `--fill-only` 打开一次确认。

### 6.6 出现滑块 / 验证码

脚本不会绕过。它会在检测到验证码时暂停等待，最多等 `captcha_timeout_seconds` 秒，让你在浏览器里手动完成。定时提交场景下这可能会错过时间，所以请提前登录并完成验证。

### 6.7 时间校准失败

- 检查 Windows 是否开启“自动设置时间”。
- 检查防火墙是否放行 UDP 123（NTP）。
- 脚本会自动退回 HTTP 时间源。
- 测试时可以加 `--no-time-sync`。

### 6.8 点击后没有检测到成功提示

看 `artifacts/` 里最新截图，以及浏览器页面。**不要直接重复运行**，先确认上一次是否已经提交成功，避免重复提交。

### 6.9 电脑休眠 / 锁屏

Windows 休眠会暂停浏览器和脚本。正式运行前：

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0
```

运行结束后可以改回来。

### 6.10 浏览器原生弹窗

按你的要求，脚本已经删除了“自动取消弹窗”的逻辑。现在浏览器原生 `alert` / `confirm` 弹窗会统一**自动确认**，日志里显示“自动确认”，不会再出现“自动取消”。

如果某个弹窗必须点“取消”，告诉我，我再单独加规则。

---

## 7. 安全与合规提醒

- 脚本不破解验证码、不伪造登录、不绕过访问控制。
- 不要高频请求 WPS 或时间服务器；脚本的时间同步是低频的，高频轮询只发生在本地浏览器 DOM 上。
- 不要同时运行多个实例，避免重复提交。
- 如果表单规则明确禁止自动化，请不要使用本脚本。
- 因使用本脚本造成的重复提交、账号风控或规则问题，需要你自己承担。
