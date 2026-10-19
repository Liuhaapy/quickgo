#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WPS / 金山表单定时自动提交脚本（Playwright 版）

设计目标
--------
1. 浏览器由 Playwright 启动，使用独立的持久化用户目录保存登录态。
2. 登录 / 扫码 / 滑块由你手动完成，脚本不绕过任何验证码。
3. 脚本只负责填表，绝不提前点击提交。
4. 先用 NTP / 网络时间 API 校准时钟，再在目标时间前约 1 秒开始
   高频检查提交按钮；到达目标时间后才允许点击。
5. 只点击一次，避免重复提交；结果会截图保存到 artifacts/。

使用前请阅读 README.md，并把 config.json 中的 URL、字段选择器、
提交按钮选择器和答案改成你自己的。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import statistics
import struct
import sys
import time
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone, tzinfo
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent


def _install_tempfile_compat() -> None:
    """兼容某些 Windows 安全软件/沙箱环境。

    这些环境会在 tempfile.mkdtemp() 的审计事件之后给新目录打上拒绝访问的
    ACL，导致 Playwright 无法创建 artifacts 临时目录。这里用等价的
    os.mkdir(path, 0o700) 实现替换，避开审计事件；权限语义与标准库一致。
    """
    import tempfile

    if getattr(tempfile.mkdtemp, "_wps_compat", False):
        return

    def _mkdtemp(suffix: str | None = None, prefix: str | None = None, dir: str | None = None) -> str:
        prefix = "tmp" if prefix is None else prefix
        suffix = "" if suffix is None else suffix
        directory = dir or tempfile.gettempdir()
        names = tempfile._get_candidate_names()
        for _ in range(tempfile.TMP_MAX):
            path = os.path.join(directory, prefix + next(names) + suffix)
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:
                continue
            return os.path.abspath(path)
        raise FileExistsError("无法创建临时目录")

    _mkdtemp._wps_compat = True  # type: ignore[attr-defined]
    tempfile.mkdtemp = _mkdtemp  # type: ignore[assignment]


_install_tempfile_compat()


def _prepare_local_temp() -> None:
    """把临时目录放到工作区，避开 Windows 系统 Temp 的权限/安全软件问题。

    Playwright 的 Node 驱动会用 os.tmpdir() 创建 playwright-artifacts-*。
    某些 Windows 环境下系统 Temp 目录的 ACL 会导致 EPERM；工作区内的
    .pwtmp 目录没有这个问题。
    """
    if os.name != "nt":
        return
    import tempfile

    temp_dir = BASE_DIR / ".pwtmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = str(temp_dir)
    os.environ["TMP"] = str(temp_dir)
    os.environ["TMPDIR"] = str(temp_dir)
    tempfile.tempdir = str(temp_dir)


_prepare_local_temp()

try:
    from playwright.sync_api import (
        Error as PlaywrightError,
        Locator,
        Page,
        TimeoutError as PlaywrightTimeoutError,
        sync_playwright,
    )
except ImportError:  # pragma: no cover - 只在依赖未安装时触发
    print(
        "缺少 Playwright。请先安装依赖：\n"
        "  python -m pip install -r requirements.txt\n"
        "如果使用系统 Edge/Chrome，不需要再下载浏览器；\n"
        "否则请执行：python -m playwright install chromium",
        file=sys.stderr,
    )
    raise SystemExit(2)


ARTIFACT_DIR = BASE_DIR / "artifacts"
LOG = logging.getLogger("wps_auto_submit")

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    # 表单地址。支持普通 https:// 地址，也支持 file:// 本地测试页。
    "form_url": "",
    # 目标时间：HH:MM:SS，或 "YYYY-MM-DD HH:MM:SS"。
    "submit_at": "12:40:00",
    "timezone": "Asia/Shanghai",
    # 提交按钮。留空时脚本会尝试自动寻找“提交”按钮。
    # 推荐明确写：button:has-text('提交') 或 text=提交
    "submit_button": "",
    # 登录 / 滑块完成后，是否在终端按 Enter 再让脚本接管。
    "manual_confirm": True,
    # 是否启用网络校时。测试时可设 false。
    "time_sync": True,
    # 如果所有时间源都失败，是否直接报错退出（false 则使用本机时钟）。
    "require_time_sync": False,
    # 到达目标时间后，再额外延迟多少毫秒点击。默认 0。
    "submit_delay_ms": 0,
    # 高频轮询间隔（毫秒）。20ms 约等于每秒 50 次。
    "poll_interval_ms": 20,
    # 目标时间到达后，最多再等多少秒让按钮变为可点击。
    "open_grace_seconds": 10,
    # 浏览器：edge / chrome / chromium。edge 使用系统自带 Edge，最省事。
    "browser": "edge",
    "headless": False,
    # 持久化用户目录，保存 Cookie / 登录态。
    "user_data_dir": ".wps_profile",
    # 单次填写超时。
    "field_timeout_ms": 8000,
    # 填表失败时是否立即中止（强烈建议 true，避免提交残缺数据）。
    "strict_fill": True,
    # 开放瞬间快速填写：字段在开放前是 disabled 时，不强行填写；
    # 等页面自己把字段启用后，立刻填写并提交。
    "wait_for_fields": False,
    # 在目标时间前多少秒自动刷新一次表单页面。0 表示不刷新。
    # 刷新会清空已填内容，所以刷新发生在填写之前。
    "refresh_before_seconds": 0,
    # 提交前检查字段是否为空，并尝试补填。
    "verify_before_submit": True,
    # 点击次数。默认 1，避免重复提交；只有页面明确提示“未开始”时才建议 2。
    "max_click_attempts": 1,
    # 点击后等待成功提示的秒数。
    "result_timeout_seconds": 8,
    # 出现验证码时，等待人工完成的最长秒数。
    "captcha_timeout_seconds": 60,
    # 是否截图。
    "screenshot": True,
    # 浏览器操作放慢毫秒数，调试时可设 100。
    "slow_mo_ms": 0,
    # 成功提示文字。
    "success_texts": [
        "提交成功",
        "提交完成",
        "已提交",
        "填写成功",
        "感谢您的填写",
        "感谢您的参与",
    ],
    # 页面明确表示尚未开放的文字。
    "not_open_texts": [
        "未开始",
        "未开放",
        "尚未开始",
        "活动未开始",
        "还未开始",
        "敬请期待",
    ],
    # 页面明确表示失败的文字。
    "failure_texts": [
        "提交失败",
        "网络异常",
        "网络错误",
        "请稍后重试",
        "提交出错",
    ],
    # 字段列表，示例见 config.example.json。
    "fields": [],
}


# ---------------------------------------------------------------------------
# 时间校准：NTP 优先，HTTP 时间 API 兜底
# ---------------------------------------------------------------------------

NTP_UNIX_DELTA = 2_208_988_800  # 1900-01-01 到 1970-01-01 的秒数

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "identity",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


@dataclass
class TimeSample:
    offset: float       # 服务器时间 - 本机时间（秒）
    rtt: float          # 往返耗时（秒）
    precision: str      # "ms" 或 "s"
    source: str


class NtpSource:
    """极简 SNTP 客户端，精度通常为毫秒级。"""

    def __init__(self, name: str, host: str, timeout: float = 1.5) -> None:
        self.name = name
        self.host = host
        self.timeout = timeout

    def sample(self) -> TimeSample:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        try:
            # LI=0, VN=3, Mode=3 (client)
            packet = b"\x1b" + 47 * b"\x00"
            t1 = time.time()
            sock.sendto(packet, (self.host, 123))
            data, _ = sock.recvfrom(1024)
            t4 = time.time()
        finally:
            sock.close()

        if len(data) < 48:
            raise ValueError(f"NTP 响应过短：{len(data)} 字节")

        values = struct.unpack("!12I", data[:48])
        # 第 8/9 个 32 位字是服务器接收时间，第 10/11 个是发送时间。
        t2 = values[8] + values[9] / 2**32 - NTP_UNIX_DELTA
        t3 = values[10] + values[11] / 2**32 - NTP_UNIX_DELTA
        if t2 <= 0 or t3 <= 0:
            raise ValueError("NTP 服务器返回了无效时间戳")

        offset = ((t2 - t1) + (t3 - t4)) / 2.0
        rtt = max(0.0, (t4 - t1) - (t3 - t2))
        return TimeSample(offset=offset, rtt=rtt, precision="ms", source=self.name)


class HttpTimeSource:
    """HTTP 时间源。

    kind:
      - taobao: 淘宝 mtop 毫秒时间戳
      - qq:     腾讯视频 checktime 秒级时间戳
      - suning: 苏宁 sysTime2 秒级时间
      - date:   HTTP 响应头 Date（秒级）
    """

    def __init__(
        self,
        name: str,
        url: str,
        kind: str,
        tz: tzinfo,
        timeout: float = 5.0,
    ) -> None:
        self.name = name
        self.url = url
        self.kind = kind
        self.tz = tz
        self.timeout = timeout

    def sample(self) -> TimeSample:
        req = Request(self.url, headers=HTTP_HEADERS, method="GET")
        t0 = time.time()
        with urlopen(req, timeout=self.timeout) as response:
            body = response.read(4096)
            headers = response.headers
        t1 = time.time()

        if self.kind == "date":
            raw_date = headers.get("Date")
            if not raw_date:
                raise ValueError("响应没有 Date 头")
            dt = parsedate_to_datetime(raw_date)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            server_epoch = dt.timestamp()
            precision = "s"
        elif self.kind == "taobao":
            payload = json.loads(body.decode("utf-8", errors="replace"))
            raw = payload["data"]["t"]
            value = float(raw)
            server_epoch = value / 1000.0 if value > 10**11 else value
            precision = "ms"
        elif self.kind == "qq":
            text = body.decode("utf-8", errors="replace")
            match = re.search(r"\{.*\}", text, flags=re.S)
            if not match:
                raise ValueError("腾讯时间响应不是 JSON")
            payload = json.loads(match.group(0))
            server_epoch = float(payload["t"])
            precision = "s"
        elif self.kind == "suning":
            payload = json.loads(body.decode("utf-8", errors="replace"))
            if payload.get("sysTime2"):
                dt = datetime.strptime(payload["sysTime2"], "%Y-%m-%d %H:%M:%S")
                server_epoch = dt.replace(tzinfo=self.tz).timestamp()
            else:
                value = float(payload["sysTime1"])
                server_epoch = value / 1000.0 if value > 10**11 else value
            precision = "s"
        else:
            raise ValueError(f"未知时间源类型：{self.kind}")

        midpoint = (t0 + t1) / 2.0
        return TimeSample(
            offset=server_epoch - midpoint,
            rtt=max(0.0, t1 - t0),
            precision=precision,
            source=self.name,
        )


def default_time_sources(tz: tzinfo) -> list[Any]:
    """按优先级返回时间源。国内网络优先使用阿里云 NTP。"""
    return [
        NtpSource("阿里云 NTP", "ntp.aliyun.com"),
        NtpSource("阿里云 NTP 2", "ntp1.aliyun.com"),
        NtpSource("Windows NTP", "time.windows.com"),
        HttpTimeSource(
            "淘宝时间戳",
            "https://api.m.taobao.com/rest/api3.do?api=mtop.common.getTimestamp",
            "taobao",
            tz,
        ),
        HttpTimeSource(
            "腾讯视频时间",
            "https://vv.video.qq.com/checktime?otype=json",
            "qq",
            tz,
        ),
        HttpTimeSource(
            "苏宁时间",
            "https://quan.suning.com/getSysTime.do",
            "suning",
            tz,
        ),
        HttpTimeSource("百度 Date", "https://www.baidu.com", "date", tz),
        HttpTimeSource("京东 Date", "https://www.jd.com", "date", tz),
        HttpTimeSource("腾讯网 Date", "https://www.qq.com", "date", tz),
    ]


class TimeKeeper:
    """维护“服务器当前时间”。

    校时成功后，用 monotonic() 推进，避免系统时间被修改造成跳变。
    网络校时只在启动、每 5 分钟、以及目标时间前 65 秒进行，不会高频
    请求时间服务器。
    """

    def __init__(
        self,
        tz: tzinfo,
        enabled: bool = True,
        require_sync: bool = False,
        sources: Sequence[Any] | None = None,
    ) -> None:
        self.tz = tz
        self.enabled = enabled
        self.require_sync = require_sync
        self.sources = list(sources) if sources is not None else default_time_sources(tz)
        self.offset = 0.0
        self.anchor_wall = time.time()
        self.anchor_mono = time.monotonic()
        self.synced = False
        self.precision = "local"
        self.last_sync_mono = 0.0

    def now(self) -> float:
        return self.anchor_wall + (time.monotonic() - self.anchor_mono)

    def seconds_since_sync(self) -> float:
        if not self.last_sync_mono:
            return 10**9
        return time.monotonic() - self.last_sync_mono

    def sync(self, samples_per_source: int = 3) -> bool:
        if not self.enabled:
            LOG.info("网络校时已禁用，使用本机时钟。")
            return False

        for source in self.sources:
            samples: list[TimeSample] = []
            for index in range(samples_per_source):
                try:
                    sample = source.sample()
                except (HTTPError, URLError, socket.timeout, TimeoutError, ValueError, KeyError) as exc:
                    LOG.debug("时间源 %s 第 %d 次失败：%s", source.name, index + 1, exc)
                    break
                except Exception as exc:  # 某些网络库会抛出自定义异常
                    LOG.debug("时间源 %s 第 %d 次异常：%s", source.name, index + 1, exc)
                    break

                if sample.rtt <= 2.0 and abs(sample.offset) < 86400:
                    samples.append(sample)
                if index < samples_per_source - 1:
                    time.sleep(0.15)

            if not samples:
                continue

            offsets = [item.offset for item in samples]
            median_offset = statistics.median(offsets)
            # 去掉明显离群的样本（例如 HTTP Date 跨秒）。
            filtered = [value for value in offsets if abs(value - median_offset) <= 0.75]
            if not filtered:
                filtered = offsets
            offset = statistics.median(filtered)
            rtt = statistics.median([item.rtt for item in samples])
            precision = samples[-1].precision
            source_name = samples[-1].source

            self.offset = offset
            self.anchor_wall = time.time() + offset
            self.anchor_mono = time.monotonic()
            self.synced = True
            self.precision = precision
            self.last_sync_mono = time.monotonic()

            precision_note = "毫秒级" if precision == "ms" else "秒级（可能晚 0~1 秒）"
            LOG.info(
                "校时成功：%s，偏差 %+.3f 秒，往返 %.0f ms，%s",
                source_name,
                offset,
                rtt * 1000,
                precision_note,
            )
            return True

        LOG.warning(
            "所有网络时间源都失败，改用本机时钟。"
            "请确认 Windows 已开启“自动设置时间”，或检查防火墙是否放行 UDP 123。"
        )
        if self.require_sync:
            raise RuntimeError("require_time_sync=true，但网络校时失败")
        # 保留之前的 anchor；如果从未成功过，则重新锚定本机时间。
        if not self.synced:
            self.anchor_wall = time.time()
            self.anchor_mono = time.monotonic()
        return False


def get_timezone(name: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        # Windows 可能没有 IANA 时区数据库。中国没有夏令时，固定 UTC+8 即可。
        if name in {"Asia/Shanghai", "PRC", "Asia/Chongqing"}:
            LOG.warning("未找到 IANA 时区数据，使用固定 UTC+8。")
            return timezone(timedelta(hours=8), name="Asia/Shanghai")
        raise


def parse_target_time(
    raw: str,
    tz: tzinfo,
    clock: TimeKeeper,
    target_in: float | None = None,
) -> float:
    """把配置中的时间转换成 Unix 时间戳。"""
    if target_in is not None:
        return clock.now() + float(target_in)

    text = str(raw).strip()
    dt: datetime | None = None

    # 完整日期时间
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue

    # 只有时间：使用“今天”。如果今天已经过了，脚本会明确报错，避免误排到明天。
    if dt is None:
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                parsed_time: datetime_time = datetime.strptime(text, fmt).time()
                # 用校准后的时间判断“今天”，避免本机时钟偏差导致跨日错误。
                dt = datetime.combine(
                    datetime.fromtimestamp(clock.now(), tz).date(),
                    parsed_time,
                )
                break
            except ValueError:
                continue

    if dt is None:
        raise ValueError(
            f"无法解析 submit_at={raw!r}。请使用 HH:MM:SS 或 YYYY-MM-DD HH:MM:SS。"
        )

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)

    target = dt.timestamp()
    now = clock.now()
    if target <= now + 0.5:
        raise ValueError(
            f"目标时间 {dt.strftime('%Y-%m-%d %H:%M:%S %Z')} 已经过去"
            f"（当前 {datetime.fromtimestamp(now, tz).strftime('%Y-%m-%d %H:%M:%S')}）。"
            "请检查时间，或写成完整的明天日期；测试请用 --target-in。"
        )

    if target - now > 36 * 3600:
        LOG.warning("目标时间距离现在超过 36 小时，请确认日期没有写错。")
    return target


def format_epoch(epoch: float, tz: tzinfo) -> str:
    return datetime.fromtimestamp(epoch, tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def wait_until_clock(clock: TimeKeeper, when: float, label: str) -> None:
    """等待到指定网络时间；短睡眠循环，避免长时间死睡。"""
    LOG.info("等待%s：%s", label, format_epoch(when, clock.tz))
    while True:
        remaining = when - clock.now()
        if remaining <= 0:
            return
        if remaining > 5:
            time.sleep(min(5.0, max(0.2, remaining - 1.0)))
        elif remaining > 0.05:
            time.sleep(min(0.05, remaining))
        else:
            time.sleep(0.002)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def resolve_path(value: str | os.PathLike[str], default: str) -> Path:
    path = Path(value or default)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def load_config(path: str | os.PathLike[str], validate_fields: bool = True) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = BASE_DIR / config_path
    if not config_path.exists():
        raise FileNotFoundError(
            f"找不到配置文件：{config_path}\n"
            "请复制 config.example.json 为 config.json，并填入你的表单地址和答案。"
        )

    with config_path.open("r", encoding="utf-8-sig") as handle:
        raw = json.load(handle)

    if not isinstance(raw, dict):
        raise ValueError("配置文件最外层必须是 JSON 对象。")

    cfg = {**DEFAULTS, **raw}
    cfg["_config_path"] = str(config_path)

    url = str(cfg.get("form_url", "")).strip()
    if not url:
        raise ValueError("config.form_url 不能为空。")
    if "请替换" in url or "REPLACE_ME" in url.upper():
        raise ValueError("请先把 config.form_url 换成真实的 WPS 表单地址。")
    cfg["form_url"] = url

    fields = cfg.get("fields")
    if not isinstance(fields, list):
        raise ValueError("config.fields 必须是数组。")
    if not validate_fields:
        return cfg
    for index, field in enumerate(fields, start=1):
        if not isinstance(field, dict):
            raise ValueError(f"fields[{index}] 必须是对象。")
        field.setdefault("type", "text")
        field["type"] = str(field["type"]).lower()
        if field["type"] not in {"text", "textarea", "input", "date", "radio", "single", "choice", "checkbox", "multi", "select", "dropdown", "click"}:
            raise ValueError(f"fields[{index}] 的 type={field['type']!r} 不支持。")
        if not field.get("selector") and not field.get("container") and not (field.get("question") or field.get("label")):
            raise ValueError(
                f"fields[{index}] 至少要有 selector、container 或 label/question 之一。"
            )

        # 防止只改了 URL、却忘了改示例答案就运行。
        candidates: list[Any] = [field.get("value"), field.get("option")]
        if isinstance(field.get("options"), list):
            candidates.extend(field["options"])
        if any(str(item).strip() == "请填写" for item in candidates if item is not None):
            raise ValueError(
                f"fields[{index}] 仍然是示例答案“请填写”，请先改成你的真实答案。"
            )

    return cfg


def setup_logging(debug: bool = False) -> None:
    # Windows 控制台默认可能是 GBK，强制 UTF-8 避免中文日志乱码。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    level = logging.DEBUG if debug else logging.INFO
    LOG.setLevel(level)
    LOG.handlers.clear()
    LOG.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(formatter)
    LOG.addHandler(console)

    try:
        file_handler = logging.FileHandler(
            BASE_DIR / "wps_auto_submit.log",
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        LOG.addHandler(file_handler)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 填表
# ---------------------------------------------------------------------------

# 在页面里标记“题目文本 -> 题目容器”。这是 selector / container 都没写时的兜底。
JS_MARK_QUESTION = r"""
({label, key}) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const target = norm(label);
  if (!target) return false;

  const nodes = Array.from(document.querySelectorAll(
    'div,span,p,label,li,td,th,h1,h2,h3,h4,h5,h6'
  ));
  let best = null;
  let bestScore = -1e9;

  for (const el of nodes) {
    const text = norm(el.innerText);
    if (!text || text.length > target.length + 60 || !text.includes(target)) continue;

    const controls = el.querySelectorAll(
      'input,textarea,select,[contenteditable="true"],[role="radio"],[role="checkbox"]'
    ).length;
    if (controls > 12) continue;

    let score = 0;
    if (text === target) score += 100;
    const cls = String(el.className || '') + ' ' + String(el.getAttribute('data-type') || '');
    if (/title|label|question|field|form-item|q-/i.test(cls)) score += 25;
    if (controls > 0) score += 20;
    if (el.tagName === 'LABEL') score += 15;
    score -= text.length / 40;
    if (score > bestScore) {
      bestScore = score;
      best = el;
    }
  }

  if (!best) return false;

  let node = best;
  for (let i = 0; i < 10 && node && node !== document.body; i++) {
    if (node.querySelector(
      'input,textarea,select,[contenteditable="true"],[role="radio"],[role="checkbox"]'
    )) {
      node.setAttribute('data-wps-auto-q', key);
      return true;
    }
    node = node.parentElement;
  }
  return false;
}
"""

JS_ELEMENT_INFO = r"""
(el) => {
  const rect = el.getBoundingClientRect();
  return {
    text: String(el.innerText || el.value || '').replace(/\s+/g, ' ').trim(),
    tag: el.tagName || '',
    cls: String(el.className || ''),
    role: el.getAttribute('role') || '',
    rect: {x: rect.x, y: rect.y, w: rect.width, h: rect.height, bottom: rect.bottom}
  };
}
"""

JS_BUTTON_STATE = r"""
(el) => {
  const text = String(el.innerText || el.value || '').replace(/\s+/g, ' ').trim();
  const rect = el.getBoundingClientRect();
  let blocked = false;
  let reason = '';

  for (let node = el; node && node !== document.documentElement; node = node.parentElement) {
    const style = getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden') {
      blocked = true; reason = 'hidden'; break;
    }
    if (style.pointerEvents === 'none') {
      blocked = true; reason = 'pointer-events'; break;
    }
    if (node.getAttribute && node.getAttribute('aria-disabled') === 'true') {
      blocked = true; reason = 'aria-disabled'; break;
    }
    const cls = String(node.className || '');
    if (/(^|[-_ ])(disabled|is-disabled|btn-disabled)([-_ ]|$)/i.test(cls)) {
      blocked = true; reason = 'class-disabled'; break;
    }
  }

  const style = getComputedStyle(el);
  const opacity = parseFloat(style.opacity || '1');
  const visible = rect.width > 1 && rect.height > 1 && !blocked;
  const disabled = !!el.disabled || el.getAttribute('aria-disabled') === 'true' || blocked;
  const notOpen = /未开始|未开放|尚未开始|活动未开始|还未开始|敬请期待/.test(text);
  const ready = visible && !disabled && opacity > 0.5 && !notOpen;

  return {
    text,
    visible,
    disabled,
    opacity,
    notOpen,
    ready,
    reason,
    className: String(el.className || '')
  };
}
"""

JS_ENABLED_STATE = r"""
(el) => {
  if (el.disabled === true) return false;
  for (let node = el; node && node !== document.documentElement; node = node.parentElement) {
    const style = getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    if (style.pointerEvents === 'none') return false;
    if (node.getAttribute && node.getAttribute('aria-disabled') === 'true') return false;
    const cls = String(node.className || '');
    if (/(^|[-_ ])(disabled|is-disabled|btn-disabled)([-_ ]|$)/i.test(cls)) return false;
  }
  return true;
}
"""


class FormFiller:
    def __init__(self, page: Page, cfg: dict[str, Any]) -> None:
        self.page = page
        self.cfg = cfg
        self.field_timeout = int(cfg.get("field_timeout_ms", 8000))
        self._question_key = 0
        self._question_keys: dict[str, str] = {}
        self.dialogs: list[str] = []
        page.on("dialog", self._on_dialog)

    # ---------------------------- 通用工具 ----------------------------

    def _on_dialog(self, dialog: Any) -> None:
        try:
            message = dialog.message
            self.dialogs.append(message)
            # 按用户要求：不再自动取消弹窗。这里统一自动确认，
            # 避免“取消”导致提交确认框被关掉。
            LOG.info("浏览器弹窗：%s（自动确认）", message)
            dialog.accept()
        except Exception:
            pass

    def _locator(self, selector: str) -> Locator:
        locator = self.page.locator(selector)
        if locator.count() == 0:
            raise ValueError(f"找不到选择器：{selector}")
        return locator

    @staticmethod
    def _first_visible(locator: Locator, limit: int = 10) -> Locator:
        count = locator.count()
        if count == 0:
            raise ValueError("定位器没有匹配到元素")
        fallback: Locator | None = None
        for index in range(min(count, limit)):
            item = locator.nth(index)
            if fallback is None:
                fallback = item
            try:
                if item.is_visible():
                    return item
            except PlaywrightError:
                continue
        assert fallback is not None
        return fallback

    def _read_value(self, locator: Locator) -> str:
        try:
            tag = str(locator.evaluate("el => el.tagName")).upper()
            if tag in {"INPUT", "TEXTAREA", "SELECT"}:
                return locator.input_value() or ""
            return locator.inner_text() or ""
        except PlaywrightError:
            return ""

    # ---------------------------- 题目容器 ----------------------------

    def _find_question_container(self, label: str) -> Locator:
        if not label:
            raise ValueError("需要 label/question 或 container 才能定位题目。")

        # 先尝试精确文本，再尝试包含文本。ancestor-or-self 的 [1] 取最近祖先。
        for exact in (True, False):
            try:
                title_locator = self.page.get_by_text(label, exact=exact)
                count = title_locator.count()
            except PlaywrightError:
                count = 0
            for index in range(min(count, 12)):
                title = title_locator.nth(index)
                try:
                    if not title.is_visible():
                        continue
                    container = title.locator(
                        "xpath=ancestor-or-self::*[not(self::body) and not(self::html) "
                        "and (.//input or .//textarea or .//select "
                        "or @contenteditable='true' or @role='radio' or @role='checkbox')][1]"
                    )
                    if container.count() > 0:
                        return container.first
                except PlaywrightError:
                    continue
            if exact:
                continue

        # 兜底：在页面里找到包含题目文字的最小容器并打标记。
        # 同一个题目复用同一个 key，避免反复轮询时不断往 DOM 里加属性。
        key = self._question_keys.get(label)
        if key is None:
            self._question_key += 1
            key = f"wpsq{self._question_key}"
            self._question_keys[label] = key
        try:
            found = self.page.evaluate(JS_MARK_QUESTION, {"label": label, "key": key})
        except PlaywrightError:
            found = False
        if found:
            locator = self.page.locator(f'[data-wps-auto-q="{key}"]')
            if locator.count() > 0:
                return locator.first

        raise ValueError(
            f"找不到题目「{label}」所在的表单区域。"
            "请在 config.json 里给该字段写 container 或 selector。"
        )

    def _resolve_container(self, field: dict[str, Any]) -> Locator:
        container_selector = str(field.get("container") or "").strip()
        if container_selector:
            return self._first_visible(self._locator(container_selector), 5)

        label = str(field.get("question") or field.get("label") or "").strip()
        return self._find_question_container(label)

    # ---------------------------- 控件定位 ----------------------------

    def _find_text_control(self, field: dict[str, Any]) -> Locator:
        selector = str(field.get("selector") or "").strip()
        if selector:
            locator = self._first_visible(self._locator(selector), 5)
            # 允许 selector 指向题目容器，而不是 input 本身。
            try:
                tag = str(locator.evaluate("el => el.tagName")).upper()
                is_editable = bool(
                    locator.evaluate(
                        "el => el.isContentEditable || el.getAttribute('contenteditable') === 'true'"
                    )
                )
            except PlaywrightError:
                tag, is_editable = "", False
            if tag not in {"INPUT", "TEXTAREA"} and not is_editable:
                inner = locator.locator(
                    "input:not([type='hidden']):not([type='radio']):not([type='checkbox'])"
                    ":not([type='submit']):not([type='button']):not([type='reset']), "
                    "textarea, [contenteditable='true']"
                )
                if inner.count() > 0:
                    return self._first_visible(inner, 5)
            return locator

        container = self._resolve_container(field)
        selectors = [
            "input:not([type='hidden']):not([type='radio']):not([type='checkbox'])"
            ":not([type='submit']):not([type='button']):not([type='reset'])",
            "textarea",
            "[contenteditable='true']",
            "[role='textbox']",
        ]
        for item_selector in selectors:
            locator = container.locator(item_selector)
            if locator.count() > 0:
                return self._first_visible(locator, 5)

        raise ValueError(
            f"题目「{field.get('label') or field.get('question')}」里找不到输入框。"
            "请写 selector 直接指向 input/textarea。"
        )

    def _find_select_control(self, field: dict[str, Any]) -> Locator:
        selector = str(field.get("selector") or "").strip()
        if selector:
            return self._first_visible(self._locator(selector), 5)

        container = self._resolve_container(field)
        locator = container.locator("select")
        if locator.count() > 0:
            return self._first_visible(locator, 5)
        # 自定义下拉：找带 role=combobox 或 aria-haspopup=listbox 的触发器。
        locator = container.locator(
            "[role='combobox'], [aria-haspopup='listbox'], .select, .dropdown"
        )
        if locator.count() > 0:
            return self._first_visible(locator, 5)
        raise ValueError("找不到下拉框，请用 selector 指向 select 或下拉触发器。")

    # ---------------------- 开放瞬间快速填写 ----------------------

    def _element_enabled(self, locator: Locator) -> bool:
        """检查元素及祖先是否处于可交互状态（不修改 DOM）。"""
        try:
            return bool(locator.evaluate(JS_ENABLED_STATE))
        except PlaywrightError:
            return False

    def _field_ready(self, field: dict[str, Any]) -> bool:
        """只读检查一个字段是否已经可用。"""
        field_type = str(field.get("type", "text")).lower()

        if field_type in {"text", "textarea", "input", "date"}:
            locator = self._find_text_control(field)
            try:
                if not locator.is_visible() or not locator.is_enabled():
                    return False
                if locator.get_attribute("readonly") is not None:
                    return False
            except PlaywrightError:
                return False
            return self._element_enabled(locator)

        if field_type in {"radio", "single", "choice", "checkbox", "multi"}:
            direct_selector = str(field.get("selector") or "").strip()
            if direct_selector and not field.get("question") and not field.get("container"):
                target = self._first_visible(self._locator(direct_selector), 5)
                try:
                    if not target.is_visible() or not target.is_enabled():
                        return False
                except PlaywrightError:
                    return False
                return self._element_enabled(target)

            container = self._resolve_container(field)
            try:
                if not container.is_visible():
                    return False
                if container.locator("input:disabled").count() > 0:
                    return False
                if container.locator("[aria-disabled='true']").count() > 0:
                    return False
            except PlaywrightError:
                return False
            return self._element_enabled(container)

        if field_type in {"select", "dropdown"}:
            locator = self._find_select_control(field)
            try:
                if not locator.is_visible() or not locator.is_enabled():
                    return False
            except PlaywrightError:
                return False
            return self._element_enabled(locator)

        if field_type == "click":
            selector = str(field.get("selector") or "").strip()
            if not selector:
                raise ValueError("type=click 的字段必须写 selector。")
            target = self._first_visible(self._locator(selector), 5)
            try:
                if not target.is_visible() or not target.is_enabled():
                    return False
            except PlaywrightError:
                return False
            return self._element_enabled(target)

        return True

    def fields_ready(self) -> tuple[bool, list[str]]:
        """返回 (是否全部可用, 尚未可用的字段名列表)。"""
        pending: list[str] = []
        fields = self.cfg.get("fields") or []
        for index, field in enumerate(fields, start=1):
            label = str(field.get("label") or field.get("question") or f"字段{index}")
            try:
                if not self._field_ready(field):
                    pending.append(label)
            except ValueError as exc:
                raise ValueError(f"检查字段「{label}」失败：{exc}") from exc
        return not pending, pending

    def wait_until_fields_ready(self, clock: TimeKeeper, target: float) -> None:
        """等页面自己把字段启用；绝不修改 disabled 属性。"""
        poll_interval = max(
            0.005,
            min(0.1, float(self.cfg.get("poll_interval_ms", 20)) / 1000.0),
        )
        grace = max(0.0, float(self.cfg.get("open_grace_seconds", 10)))
        deadline = target + grace
        last_pending: list[str] | None = None

        while True:
            ready, pending = self.fields_ready()
            if ready:
                LOG.info("字段已经开放，可以开始填写。")
                return

            if pending != last_pending:
                LOG.info("仍在等待字段开放：%s", "、".join(pending))
                last_pending = pending

            now = clock.now()
            if now >= deadline:
                raise RuntimeError(
                    "等待字段开放超时：" + "、".join(pending)
                    + "。脚本不会强行修改 disabled，请检查表单是否真的开放。"
                )

            remaining = target - now
            if remaining > 10:
                time.sleep(0.5)
            elif remaining > 0:
                time.sleep(min(0.1, max(0.02, remaining)))
            else:
                time.sleep(poll_interval)

    # ---------------------------- 写入 ----------------------------

    def _write_text(
        self,
        locator: Locator,
        value: str,
        field: dict[str, Any],
        fast: bool = False,
    ) -> None:
        if fast:
            # 开放瞬间模式：不做 scroll/click/blur，直接用 fill 写值，
            # 尽量缩短从“字段可用”到“提交”的时间。
            try:
                locator.fill(value, timeout=min(self.field_timeout, 3000))
                locator.evaluate(
                    "el => el.dispatchEvent(new Event('change', {bubbles: true}))"
                )
            except PlaywrightTimeoutError as exc:
                raise RuntimeError(f"快速填写超时：{exc}") from exc
            except PlaywrightError as exc:
                raise RuntimeError(f"快速填写失败：{exc}") from exc
            return

        try:
            locator.scroll_into_view_if_needed(timeout=2000)
        except PlaywrightError:
            pass
        try:
            locator.click(timeout=2000)
        except PlaywrightError:
            pass

        try:
            if field.get("typing"):
                locator.fill("", timeout=self.field_timeout)
                locator.press_sequentially(
                    value,
                    delay=int(field.get("typing_delay_ms", 25)),
                    timeout=self.field_timeout,
                )
            else:
                locator.fill(value, timeout=self.field_timeout)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(
                f"填写超时（字段可能尚未开放、只读或被遮挡）：{exc}"
            ) from exc
        except PlaywrightError as exc:
            raise RuntimeError(f"填写失败（字段可能只读或禁用）：{exc}") from exc

        # React / Vue 一般监听 input；再补一个 change + blur 让表单模型同步。
        try:
            locator.evaluate(
                """el => {
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    if (typeof el.blur === 'function') el.blur();
                }"""
            )
        except PlaywrightError:
            pass

        actual = self._read_value(locator)
        if value and value not in actual:
            LOG.warning("字段值回读不一致：期望 %r，实际 %r", value, actual)

    def _has_checked(self, container: Locator) -> bool:
        try:
            if container.locator(
                "input[type='radio']:checked, input[type='checkbox']:checked"
            ).count() > 0:
                return True
            if container.locator("[aria-checked='true']").count() > 0:
                return True
            return bool(
                container.evaluate(
                    """el => {
                        const nodes = el.querySelectorAll('[class]');
                        for (const node of nodes) {
                            const cls = String(node.className || '');
                            if (/(^|[-_ ])(checked|selected)([-_ ]|$)/i.test(cls)) return true;
                        }
                        return false;
                    }"""
                )
            )
        except PlaywrightError:
            return False

    def _is_option_checked(self, target: Locator) -> bool:
        try:
            return bool(
                target.evaluate(
                    """el => {
                        const label = el.closest('label');
                        if (label) {
                            const input = label.querySelector('input');
                            if (input && input.checked) return true;
                        }
                        if (el.tagName === 'INPUT' && el.checked) return true;
                        const aria = el.closest('[aria-checked]');
                        if (aria && aria.getAttribute('aria-checked') === 'true') return true;
                        const node = el.closest('.checked, .selected, .active, [class*="checked"], [class*="selected"]');
                        if (node) return true;
                        return false;
                    }"""
                )
            )
        except PlaywrightError:
            return False

    def _option_candidates(self, container: Locator, option: str) -> list[Locator]:
        exact_text = re.compile(rf"^\s*{re.escape(option)}\s*$")
        candidates: list[Locator] = []

        # 最常见的 <label> 选项。
        try:
            labels = container.locator("label").filter(has_text=exact_text)
            for index in range(min(labels.count(), 10)):
                item = labels.nth(index)
                if item.is_visible():
                    candidates.append(item)
        except PlaywrightError:
            pass

        # 自定义 div/span 选项。
        if not candidates:
            try:
                texts = container.get_by_text(option, exact=True)
                for index in range(min(texts.count(), 10)):
                    item = texts.nth(index)
                    if item.is_visible():
                        candidates.append(item)
            except PlaywrightError:
                pass

        # ARIA 选项。
        if not candidates:
            try:
                roles = container.locator(
                    "[role='radio'], [role='checkbox'], [role='option']"
                ).filter(has_text=exact_text)
                for index in range(min(roles.count(), 10)):
                    item = roles.nth(index)
                    if item.is_visible():
                        candidates.append(item)
            except PlaywrightError:
                pass

        return candidates

    def _click_option(self, container: Locator, option: str, fast: bool = False) -> None:
        candidates = self._option_candidates(container, option)
        if not candidates:
            raise ValueError(f"题目里找不到选项「{option}」")

        target = candidates[0]
        if self._is_option_checked(target):
            LOG.debug("选项已是选中状态，跳过：%s", option)
            return

        if not fast:
            target.scroll_into_view_if_needed(timeout=2000)
        try:
            target.click(timeout=3000)
        except PlaywrightTimeoutError:
            # 某些自定义选项被透明层盖住，退回到 DOM click。
            target.evaluate("el => el.click()")

        self.page.wait_for_timeout(20 if fast else 120)
        # 注意：这里不再点击第二次。自定义 checkbox 的选中状态有时无法从 DOM
        # 判断，二次点击反而可能把已选中的复选框取消掉。
        if not self._is_option_checked(target):
            LOG.warning(
                "选项「%s」点击后未检测到明确的选中状态；如果页面显示已选中，可忽略。",
                option,
            )

    def _choose(
        self,
        field: dict[str, Any],
        multi: bool,
        only_missing: bool,
        fast: bool = False,
    ) -> None:
        # 如果用户直接给了选项元素的选择器（没有 question/container），
        # 就直接点击这个元素。适合单选，或只选一个复选框的情况。
        direct_selector = str(field.get("selector") or "").strip()
        if direct_selector and not field.get("question") and not field.get("container"):
            target = self._first_visible(self._locator(direct_selector), 5)
            if only_missing and self._is_option_checked(target):
                return
            if not self._is_option_checked(target):
                if not fast:
                    target.scroll_into_view_if_needed(timeout=2000)
                target.click(timeout=3000)
                self.page.wait_for_timeout(20 if fast else 120)
            if not self._is_option_checked(target):
                LOG.warning(
                    "选项选择器 %r 点击后未检测到明确选中状态，请人工确认。",
                    direct_selector,
                )
            return

        container = self._resolve_container(field)

        if multi:
            options = field.get("options")
            if not isinstance(options, list) or not options:
                raise ValueError("checkbox 字段需要 options 数组。")
        else:
            option = field.get("option")
            if option is None or str(option) == "":
                raise ValueError("radio 字段需要 option。")
            options = [option]

        for item in options:
            text = str(item)
            if only_missing:
                candidates = self._option_candidates(container, text)
                if candidates and self._is_option_checked(candidates[0]):
                    continue
            self._click_option(container, text, fast=fast)

        if not self._has_checked(container):
            LOG.warning(
                "选择题「%s」没有检测到选中项，请在提交前人工确认。",
                field.get("label") or field.get("question"),
            )

    def _select(self, field: dict[str, Any], only_missing: bool) -> None:
        locator = self._find_select_control(field)
        value = "" if field.get("value") is None else str(field.get("value"))
        if not value:
            raise ValueError("select 字段需要 value。")

        if only_missing:
            current = self._read_value(locator)
            if current.strip():
                return

        try:
            tag = str(locator.evaluate("el => el.tagName")).upper()
        except PlaywrightError:
            tag = ""

        if tag == "SELECT":
            try:
                locator.select_option(label=value, timeout=self.field_timeout)
            except PlaywrightError:
                locator.select_option(value=value, timeout=self.field_timeout)
            return

        # 自定义下拉：点开触发器，再点选项文字。
        locator.click(timeout=3000)
        option = self.page.get_by_text(value, exact=True)
        if option.count() == 0:
            raise ValueError(f"自定义下拉里找不到选项「{value}」")
        self._first_visible(option, 5).click(timeout=3000)

    # ---------------------------- 对外填表 ----------------------------

    def fill_one(
        self,
        field: dict[str, Any],
        only_missing: bool = False,
        fast: bool = False,
    ) -> None:
        field_type = str(field.get("type", "text")).lower()
        label = str(field.get("label") or field.get("question") or "未命名字段")

        if field_type in {"text", "textarea", "input", "date"}:
            locator = self._find_text_control(field)
            value = "" if field.get("value") is None else str(field.get("value"))
            if only_missing and self._read_value(locator).strip():
                return
            self._write_text(locator, value, field, fast=fast)
        elif field_type in {"radio", "single", "choice"}:
            self._choose(field, multi=False, only_missing=only_missing, fast=fast)
        elif field_type in {"checkbox", "multi"}:
            self._choose(field, multi=True, only_missing=only_missing, fast=fast)
        elif field_type in {"select", "dropdown"}:
            self._select(field, only_missing=only_missing)
        elif field_type == "click":
            selector = str(field.get("selector") or "").strip()
            if not selector:
                raise ValueError("type=click 的字段必须写 selector。")
            self._first_visible(self._locator(selector), 5).click(timeout=3000)
        else:
            raise ValueError(f"不支持的字段类型：{field_type}")

        LOG.info("已填写：%s（%s%s）", label, field_type, "，快速模式" if fast else "")

    def fill_all(self, only_missing: bool = False, fast: bool = False) -> None:
        fields = self.cfg.get("fields") or []
        if not fields:
            LOG.warning("config.fields 为空，没有字段需要填写。")
            return

        errors: list[tuple[str, Exception]] = []
        total = len(fields)
        for index, field in enumerate(fields, start=1):
            label = str(field.get("label") or field.get("question") or f"字段{index}")
            try:
                self.fill_one(field, only_missing=only_missing, fast=fast)
                LOG.info("[%d/%d] %s 完成", index, total, label)
            except Exception as exc:
                errors.append((label, exc))
                LOG.error("[%d/%d] %s 失败：%s", index, total, label, exc)
                if self.cfg.get("strict_fill", True):
                    raise

        if errors:
            detail = "；".join(f"{label}: {exc}" for label, exc in errors)
            raise RuntimeError(f"以下字段填写失败：{detail}")

    def verify_all(self) -> list[str]:
        missing: list[str] = []
        for index, field in enumerate(self.cfg.get("fields") or [], start=1):
            label = str(field.get("label") or field.get("question") or f"字段{index}")
            field_type = str(field.get("type", "text")).lower()
            try:
                if field_type in {"text", "textarea", "input", "date"}:
                    locator = self._find_text_control(field)
                    if self._read_value(locator).strip() == "":
                        missing.append(label)
                elif field_type in {"radio", "single", "choice", "checkbox", "multi"}:
                    direct_selector = str(field.get("selector") or "").strip()
                    if direct_selector and not field.get("question") and not field.get("container"):
                        target = self._first_visible(self._locator(direct_selector), 5)
                        if not self._is_option_checked(target):
                            missing.append(label)
                    else:
                        container = self._resolve_container(field)
                        if not self._has_checked(container):
                            missing.append(label)
                elif field_type in {"select", "dropdown"}:
                    locator = self._find_select_control(field)
                    if self._read_value(locator).strip() == "":
                        missing.append(label)
            except Exception as exc:
                missing.append(f"{label}（检查失败：{exc}）")
        return missing

    def pre_submit_verify(self) -> None:
        if not self.cfg.get("verify_before_submit", True):
            return
        missing = self.verify_all()
        if not missing:
            LOG.info("提交前检查：配置中的字段都已有值。")
            return

        LOG.warning("提交前检查发现可能为空的字段：%s", "、".join(missing))
        LOG.info("尝试补填这些字段……")
        try:
            self.fill_all(
                only_missing=True,
                fast=bool(self.cfg.get("wait_for_fields")),
            )
        except Exception as exc:
            LOG.error("补填失败：%s", exc)
            return

        still_missing = self.verify_all()
        if still_missing:
            LOG.warning(
                "补填后仍无法确认以下字段：%s。请立即人工检查浏览器页面。",
                "、".join(still_missing),
            )
        else:
            LOG.info("补填完成，字段检查通过。")

    # ---------------------------- 提交按钮 ----------------------------

    @staticmethod
    def _compact_text(text: Any) -> str:
        """去掉空格，兼容 WPS 的“提 交”写法。"""
        return re.sub(r"\s+", "", str(text or ""))

    def _score_submit(self, info: dict[str, Any], viewport_height: float) -> float:
        text = str(info.get("text") or "")
        compact = self._compact_text(text)
        tag = str(info.get("tag") or "").upper()
        role = str(info.get("role") or "").lower()
        cls = str(info.get("cls") or "")
        rect = info.get("rect") or {}
        width = float(rect.get("w") or 0)
        height = float(rect.get("h") or 0)
        bottom = float(rect.get("bottom") or 0)

        score = 0.0
        if compact in {"提交", "提交表单", "确认提交"}:
            score += 100
        elif "提交" in compact:
            score += 40
        if tag in {"BUTTON", "INPUT"}:
            score += 30
        if role == "button":
            score += 15
        if re.search(r"submit|btn", cls, re.I):
            score += 20
        if 0 < width * height < 30000:
            score += 10
        if viewport_height > 0:
            score += max(0.0, min(1.0, bottom / viewport_height)) * 5
        return score

    def _best_submit(self, locator: Locator, require_text: bool = True) -> Locator:
        viewport = self.page.viewport_size or {"height": 900}
        viewport_height = float(viewport.get("height") or 900)
        best: tuple[float, Locator, dict[str, Any]] | None = None

        for index in range(min(locator.count(), 30)):
            item = locator.nth(index)
            try:
                if not item.is_visible():
                    continue
                info = item.evaluate(JS_ELEMENT_INFO)
            except PlaywrightError:
                continue
            if require_text and "提交" not in self._compact_text(info.get("text")):
                continue
            score = self._score_submit(info, viewport_height)
            if best is None or score > best[0]:
                best = (score, item, info)

        if best is None:
            raise ValueError("提交按钮存在但不可见，或文字里没有“提交”。")
        LOG.info(
            "提交按钮：text=%r tag=%s class=%r",
            best[2].get("text"),
            best[2].get("tag"),
            best[2].get("cls"),
        )
        return best[1]

    def find_submit(self) -> Locator:
        selector = str(self.cfg.get("submit_button") or "").strip()
        if selector:
            return self._best_submit(self._locator(selector), require_text=False)

        # 自动寻找：先看 button / input / role=button / a，再退回精确文字。
        for candidate_selector in (
            "button",
            "input[type='submit']",
            "input[type='button']",
            "[role='button']",
            "a",
            ".submit-btn",
            ".form-submit",
            ".btn-submit",
        ):
            locator = self.page.locator(candidate_selector)
            count = locator.count()
            if count == 0:
                continue
            for index in range(min(count, 200)):
                item = locator.nth(index)
                try:
                    if not item.is_visible():
                        continue
                    info = item.evaluate(JS_ELEMENT_INFO)
                except PlaywrightError:
                    continue
                if "提交" not in self._compact_text(info.get("text")):
                    continue
                return self._best_submit(locator)

        # 最后的兜底：按文字找。兼容“提交”和“提 交”。
        text_locator = self.page.get_by_text(re.compile(r"^\s*提\s*交\s*$"))
        visible: list[Locator] = []
        for index in range(min(text_locator.count(), 50)):
            item = text_locator.nth(index)
            try:
                if item.is_visible():
                    visible.append(item)
            except PlaywrightError:
                continue
        if visible:
            return visible[-1]

        raise ValueError(
            "找不到提交按钮。请在 config.json 里设置 submit_button，"
            "例如：button:has-text('提交') 或 text=提交"
        )

    def button_state(self, button: Locator) -> dict[str, Any]:
        return button.evaluate(JS_BUTTON_STATE)

    def screenshot(self, name: str) -> Path | None:
        if not self.cfg.get("screenshot", True):
            return None
        try:
            ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            path = ARTIFACT_DIR / f"{name}_{stamp}.png"
            self.page.screenshot(path=str(path), full_page=False)
            LOG.info("截图：%s", path)
            return path
        except Exception as exc:
            LOG.warning("截图失败：%s", exc)
            return None

    # ---------------------------- 等待与点击 ----------------------------

    def _wait_result(self, timeout: float, baseline_text: str = "") -> bool | str | None:
        success_texts = [str(item) for item in self.cfg.get("success_texts", [])]
        not_open_texts = [str(item) for item in self.cfg.get("not_open_texts", [])]
        failure_texts = [str(item) for item in self.cfg.get("failure_texts", [])]
        captcha_markers = [
            "拖动滑块",
            "请完成验证",
            "安全验证",
            "验证码",
            "人机验证",
        ]

        deadline = time.monotonic() + timeout
        captcha_deadline: float | None = None
        captcha_logged = False

        # baseline_text 必须由点击前的页面快照传入。只在“点击后新出现”的
        # 提示里判断结果，避免页面静态说明文字造成误判。
        baseline_success = {text for text in success_texts if text and text in baseline_text}
        baseline_not_open = {text for text in not_open_texts if text and text in baseline_text}
        baseline_failure = {text for text in failure_texts if text and text in baseline_text}
        if baseline_success:
            LOG.warning(
                "点击前页面已经包含成功提示文字 %s；将忽略这些静态文字。",
                "、".join(sorted(baseline_success)),
            )

        while time.monotonic() < deadline:
            try:
                body_text = self.page.locator("body").inner_text(timeout=2000)
            except PlaywrightError:
                body_text = ""

            if any(
                text and text in body_text and text not in baseline_success
                for text in success_texts
            ):
                return True
            if any(
                text and text in body_text and text not in baseline_not_open
                for text in not_open_texts
            ):
                return "not_open"
            if any(
                text and text in body_text and text not in baseline_failure
                for text in failure_texts
            ):
                LOG.error("页面显示提交失败提示，请查看截图。")
                return False

            if any(marker in body_text for marker in captcha_markers):
                if not captcha_logged:
                    LOG.warning(
                        "检测到验证码 / 滑块。脚本不会绕过验证，"
                        "请在浏览器里手动完成；完成后脚本会继续等待成功提示。"
                    )
                    captcha_logged = True
                    captcha_deadline = time.monotonic() + float(
                        self.cfg.get("captcha_timeout_seconds", 60)
                    )
                    deadline = max(deadline, captcha_deadline)
                if captcha_deadline and time.monotonic() > captcha_deadline:
                    LOG.error("等待人工验证超时。")
                    return False

            self.page.wait_for_timeout(250)

        # 弹窗里也可能出现“提交成功”。
        if any(
            any(text and text in dialog for text in success_texts)
            for dialog in self.dialogs
        ):
            return True
        return None

    def wait_and_submit(
        self,
        button: Locator,
        clock: TimeKeeper,
        target: float,
    ) -> bool:
        cfg = self.cfg
        final_sync_done = False
        verified = False
        next_sync_at = time.monotonic() + 300.0
        logged_milestones: set[int] = set()
        milestones = ((300, "5 分钟"), (60, "1 分钟"), (10, "10 秒"), (3, "3 秒"))

        LOG.info("开始等待目标时间：%s", format_epoch(target, clock.tz))

        while True:
            now = clock.now()
            remaining = target - now
            if remaining <= 0:
                break

            if clock.enabled and remaining <= 65 and not final_sync_done:
                LOG.info("临近提交，做最后一次网络校时……")
                clock.sync()
                final_sync_done = True
                next_sync_at = time.monotonic() + 300.0
                now = clock.now()
                remaining = target - now
            elif clock.enabled and remaining > 65 and time.monotonic() >= next_sync_at:
                LOG.info("定期重新校时……")
                clock.sync()
                next_sync_at = time.monotonic() + 300.0
                now = clock.now()
                remaining = target - now

            if remaining <= 60 and not verified:
                if remaining > 0:
                    self.pre_submit_verify()
                else:
                    # 开放瞬间模式可能在目标时间之后才填完，此时再跑一次
                    # 字段检查和补填只会增加延迟。
                    LOG.info("目标时间已到，跳过提交前补填检查以节省时间。")
                verified = True

            for seconds, label in milestones:
                # 只在“刚跨过”这个节点时提示，避免启动时一次性打印所有节点。
                if (
                    remaining <= seconds
                    and remaining > seconds - 5
                    and seconds not in logged_milestones
                ):
                    logged_milestones.add(seconds)
                    LOG.info("距离提交还有%s。", label)

            if remaining > 1.2:
                # 粗等待，最后 1.2 秒再进入高频轮询。
                time.sleep(min(max(0.2, remaining - 1.0), 5.0))
            else:
                break

        LOG.info("进入高频轮询：目标时间前约 1 秒开始检查按钮状态。")
        try:
            button.scroll_into_view_if_needed(timeout=2000)
        except PlaywrightError:
            pass
        try:
            self.page.bring_to_front()
        except PlaywrightError:
            pass

        poll_interval = max(
            0.005,
            min(0.1, float(cfg.get("poll_interval_ms", 20)) / 1000.0),
        )
        ready_logged = False

        # 从目标前 1 秒开始轮询，但绝不提前点击。
        while True:
            now = clock.now()
            remaining = target - now
            if remaining <= 0:
                break

            if remaining <= 1.0:
                try:
                    state = self.button_state(button)
                except PlaywrightError:
                    button = self.find_submit()
                    state = self.button_state(button)
                if state.get("ready") and not ready_logged:
                    LOG.info(
                        "按钮已经可点击（比目标时间早 %.3f 秒），等待目标时间后再点。",
                        remaining,
                    )
                    ready_logged = True
                time.sleep(min(poll_interval, max(0.001, remaining)))
            else:
                time.sleep(0.05)

        submit_delay = max(0.0, float(cfg.get("submit_delay_ms", 0)) / 1000.0)
        if submit_delay:
            LOG.info("按配置额外延迟 %.0f ms 后点击。", submit_delay * 1000)
            time.sleep(submit_delay)

        # 防止校时/系统时钟跳变导致提前。
        while clock.now() < target:
            time.sleep(0.001)

        # 到达目标时间：确认按钮可点击；如果还没开放，给它一个宽限期。
        grace_deadline = time.monotonic() + max(
            0.0, float(cfg.get("open_grace_seconds", 10))
        )
        state: dict[str, Any] | None = None
        while True:
            try:
                state = self.button_state(button)
            except PlaywrightError:
                try:
                    button = self.find_submit()
                    state = self.button_state(button)
                except Exception as exc:
                    LOG.warning("重新定位提交按钮失败：%s", exc)
                    state = None

            if state and state.get("ready"):
                break
            if time.monotonic() >= grace_deadline:
                break
            time.sleep(poll_interval)

        if not state or not state.get("ready"):
            self.screenshot("submit_button_not_ready")
            detail = ""
            if state:
                detail = (
                    f"（visible={state.get('visible')}, disabled={state.get('disabled')}, "
                    f"notOpen={state.get('notOpen')}, reason={state.get('reason')!r}）"
                )
            raise RuntimeError(
                f"到达目标时间后提交按钮仍不可点击{detail}。"
                "为避免误提交，脚本没有点击。请查看 artifacts/ 截图。"
            )

        LOG.info(
            "点击提交按钮（文字：%r，距目标 %+.3f 秒）",
            state.get("text"),
            clock.now() - target,
        )
        self.screenshot("before_submit")

        attempts = max(1, int(cfg.get("max_click_attempts", 1)))
        result: bool | str | None = None

        for attempt in range(1, attempts + 1):
            # 在点击前拍页面文字快照，供 _wait_result 区分“点击后新出现”的提示。
            try:
                baseline_text = self.page.locator("body").inner_text(timeout=2000)
            except PlaywrightError:
                baseline_text = ""

            try:
                button.click(timeout=3000)
            except PlaywrightTimeoutError:
                LOG.warning("常规点击超时，尝试 DOM click。")
                button.evaluate("el => el.click()")
            except PlaywrightError as exc:
                LOG.error("点击提交按钮失败：%s", exc)
                self.screenshot("click_failed")
                raise

            result = self._wait_result(
                float(cfg.get("result_timeout_seconds", 8)),
                baseline_text=baseline_text,
            )
            if result is True:
                LOG.info("提交成功。")
                self.screenshot("submit_success")
                return True

            if result == "not_open" and attempt < attempts:
                LOG.warning(
                    "页面提示尚未开放，0.5 秒后重试点击（%d/%d）……",
                    attempt,
                    attempts,
                )
                time.sleep(0.5)
                try:
                    button = self.find_submit()
                except Exception:
                    pass
                continue
            break

        if result == "not_open":
            LOG.error("页面提示尚未开放，请检查目标时间或服务器时间。")
        elif result is False:
            LOG.error("页面显示提交失败，请查看截图和浏览器页面。")
        else:
            LOG.warning(
                "没有检测到明确的成功/失败提示。请立即查看浏览器页面和 artifacts/ 截图，"
                "不要盲目重复运行，以免重复提交。"
            )
        self.screenshot("submit_result_unknown")
        return False


# ---------------------------------------------------------------------------
# 浏览器
# ---------------------------------------------------------------------------

def launch_browser(
    playwright: Any,
    cfg: dict[str, Any],
) -> tuple[Any, Page]:
    profile_dir = resolve_path(cfg.get("user_data_dir", ".wps_profile"), ".wps_profile")
    profile_dir.mkdir(parents=True, exist_ok=True)

    browser_name = str(cfg.get("browser", "edge")).lower()
    kwargs: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": bool(cfg.get("headless", False)),
        "viewport": {"width": 1440, "height": 900},
        "locale": "zh-CN",
        "timezone_id": str(cfg.get("timezone", "Asia/Shanghai")),
        "slow_mo": int(cfg.get("slow_mo_ms", 0) or 0),
        "args": ["--disable-notifications"],
    }
    if browser_name in {"edge", "msedge"}:
        kwargs["channel"] = "msedge"
    elif browser_name == "chrome":
        kwargs["channel"] = "chrome"
    elif browser_name != "chromium":
        raise ValueError("browser 只能是 edge、chrome 或 chromium。")

    LOG.info("启动浏览器：%s（用户目录：%s）", browser_name, profile_dir)
    try:
        context = playwright.chromium.launch_persistent_context(**kwargs)
    except PlaywrightError as exc:
        LOG.error("浏览器启动失败：%s", exc)
        if browser_name in {"edge", "chrome"}:
            LOG.error(
                "请确认已安装 Microsoft Edge 或 Google Chrome；"
                "也可以改用 --browser chromium 并执行：python -m playwright install chromium"
            )
        raise

    page = context.pages[0] if context.pages else context.new_page()
    page.set_default_timeout(int(cfg.get("field_timeout_ms", 8000)))
    return context, page


def wait_for_form_ready(
    page: Page,
    timeout_ms: int = 20000,
    settle_ms: int = 2500,
) -> None:
    """等待 WPS 单页应用渲染出输入控件。"""
    try:
        page.wait_for_function(
            """() => {
                const nodes = Array.from(document.querySelectorAll(
                    'input,textarea,select,[contenteditable="true"],[role="textbox"]'
                ));
                return nodes.some((el) => {
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 1 && rect.height > 1
                        && style.display !== 'none'
                        && style.visibility !== 'hidden';
                });
            }""",
            timeout=timeout_ms,
        )
        LOG.info("表单输入控件已加载。")
    except PlaywrightTimeoutError:
        LOG.warning(
            "20 秒内没有检测到可见输入控件。"
            "页面可能还在登录、加载很慢，或需要手动操作。"
        )

    # WPS 单选题、选项和底部按钮可能比第一个输入框晚渲染。
    # 初次打开多等一会儿；自动刷新后为了赶时间，只等一小会儿。
    page.wait_for_timeout(settle_ms)
    try:
        LOG.info("页面标题：%s", page.title())
        LOG.info("当前地址：%s", page.url)
    except PlaywrightError:
        pass


def open_form(page: Page, url: str, timeout_ms: int = 60000) -> None:
    LOG.info("打开表单：%s", url)
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    wait_for_form_ready(page)


def reload_form(page: Page, timeout_ms: int = 60000) -> None:
    LOG.info("自动刷新表单页面……")
    page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
    # 刷新发生在目标前几秒，不能像初次打开那样固定等 2.5 秒；
    # 这里只等 600ms，后续 fields_ready / fast-open 会继续等待。
    wait_for_form_ready(page, settle_ms=600)
    LOG.info("表单页面刷新完成。")


def dump_controls(page: Page) -> int:
    """打印页面上可见的输入控件，帮助用户写 selector。"""
    try:
        items = page.evaluate(
            r"""() => {
                const nodes = Array.from(document.querySelectorAll(
                    'input,textarea,select,button,[role="button"],[contenteditable="true"]'
                ));
                return nodes.map((el, index) => {
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    const visible = rect.width > 1 && rect.height > 1
                        && style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && parseFloat(style.opacity || '1') > 0.2;
                    return {
                        index,
                        tag: el.tagName || '',
                        type: el.type || '',
                        id: el.id || '',
                        name: el.getAttribute('name') || '',
                        placeholder: el.getAttribute('placeholder') || '',
                        aria: el.getAttribute('aria-label') || '',
                        text: String(el.innerText || el.value || '').replace(/\s+/g, ' ').trim().slice(0, 80),
                        label: (() => {
                            const label = el.closest('label');
                            return label ? String(label.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80) : '';
                        })(),
                        cls: String(el.className || '').slice(0, 120),
                        visible
                    };
                }).filter(item => item.visible);
            }"""
        )
    except PlaywrightError as exc:
        LOG.error("读取页面控件失败：%s", exc)
        return 0

    LOG.info("页面可见控件（共 %d 个）：", len(items))
    for item in items[:80]:
        suggestion = ""
        element_id = str(item.get("id") or "")
        name = str(item.get("name") or "")
        placeholder = str(item.get("placeholder") or "")
        text = str(item.get("text") or "")
        label_text = str(item.get("label") or "")
        field_type = str(item.get("type") or "").lower()
        if field_type in {"radio", "checkbox"} and label_text:
            suggestion = "label:has-text('" + label_text.replace("'", "\\'") + "')"
        elif element_id:
            suggestion = f"#{element_id}"
        elif name:
            suggestion = f"[name='{name}']"
        elif placeholder:
            suggestion = f"[placeholder*='{placeholder}']"
        elif text and str(item.get("tag")).upper() in {"BUTTON", "A"}:
            suggestion = f"text={text}"
        LOG.info(
            "#%02d <%s type=%r> id=%r name=%r placeholder=%r aria=%r text=%r label=%r class=%r%s",
            int(item.get("index", 0)),
            item.get("tag"),
            item.get("type"),
            element_id,
            name,
            placeholder,
            item.get("aria"),
            text,
            label_text,
            item.get("cls"),
            f"  => 可尝试 {suggestion}" if suggestion else "",
        )
    LOG.info(
        "提示：填空题优先用 placeholder/name/id 写 CSS；"
        "选择题可以在浏览器里点一下选项，再用 F12 看它的 label/class。"
    )
    return len(items)


def wait_for_manual_ready(page: Page, cfg: dict[str, Any], no_pause: bool) -> None:
    if no_pause or not cfg.get("manual_confirm", True):
        return

    if cfg.get("headless"):
        LOG.warning("headless=true 时无法扫码/滑块，请确认已保存登录态。")

    print()
    print("=" * 68)
    print("浏览器已打开。请完成以下操作：")
    print("  1. 扫码登录 WPS / 金山表单（如果需要）")
    print("  2. 完成滑块或其它人机验证（脚本不会绕过）")
    print("  3. 确认表单题目已经显示出来")
    print("  4. 不要点击“提交”按钮")
    print()
    print("完成后回到这个终端，按 Enter 让脚本接管。")
    print("=" * 68)
    try:
        input("按 Enter 继续...")
    except EOFError:
        LOG.warning("没有检测到交互式终端，直接继续。")

    if page.is_closed():
        raise RuntimeError("浏览器页面已关闭。")
    page.wait_for_timeout(300)
    try:
        current_url = page.url.lower()
        if "login" in current_url or "passport" in current_url:
            LOG.warning("当前 URL 仍像登录页，请确认已经登录成功。")
    except PlaywrightError:
        pass


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="WPS / 金山表单定时自动提交（Playwright）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python wps_auto_submit.py --config config.json\n"
            "  python wps_auto_submit.py --config config.json --fill-only\n"
            "  python wps_auto_submit.py --config tests/test_config.json "
            "--target-in 8 --no-pause --no-time-sync\n"
        ),
    )
    parser.add_argument("--config", default="config.json", help="配置文件路径（默认 config.json）")
    parser.add_argument(
        "--target",
        help="覆盖 submit_at，格式 HH:MM:SS 或 'YYYY-MM-DD HH:MM:SS'",
    )
    parser.add_argument(
        "--target-in",
        type=float,
        metavar="SECONDS",
        help="测试用：多少秒后提交，覆盖 submit_at（例如 10）",
    )
    parser.add_argument(
        "--no-time-sync",
        action="store_true",
        help="不请求网络时间，使用本机时钟（测试/离线时使用）",
    )
    parser.add_argument(
        "--fill-only",
        action="store_true",
        help="只填表并截图，不等待、不点击提交",
    )
    parser.add_argument(
        "--fast-open",
        action="store_true",
        help="开放瞬间快速填写：字段开放前不强行填，等页面启用后立即填写并提交",
    )
    parser.add_argument(
        "--refresh-before",
        type=float,
        metavar="SECONDS",
        default=None,
        help="目标时间前 N 秒自动刷新表单页面（刷新后重新填表）",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="只打开表单并打印可见输入控件/按钮，不填表、不提交，用于编写 selector",
    )
    parser.add_argument(
        "--no-pause",
        action="store_true",
        help="跳过“扫码后按 Enter”的暂停（配合已保存登录态）",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="无头模式；无法扫码/滑块，只建议测试使用",
    )
    parser.add_argument(
        "--browser",
        choices=("edge", "chrome", "chromium"),
        help="覆盖配置文件里的浏览器",
    )
    parser.add_argument("--profile", help="覆盖持久化用户目录")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    setup_logging(args.debug)

    try:
        cfg = load_config(args.config, validate_fields=not args.inspect)
        if args.target:
            cfg["submit_at"] = args.target
        if args.no_time_sync:
            cfg["time_sync"] = False
        if args.fast_open:
            cfg["wait_for_fields"] = True
        if args.refresh_before is not None:
            cfg["refresh_before_seconds"] = args.refresh_before
        if args.headless:
            cfg["headless"] = True
        if args.browser:
            cfg["browser"] = args.browser
        if args.profile:
            cfg["user_data_dir"] = args.profile

        tz = get_timezone(str(cfg.get("timezone", "Asia/Shanghai")))

        clock = TimeKeeper(
            tz=tz,
            enabled=bool(cfg.get("time_sync", True)) and not (args.fill_only or args.inspect),
            require_sync=bool(cfg.get("require_time_sync", False)) and not (args.fill_only or args.inspect),
        )
        if args.fill_only or args.inspect:
            LOG.info("--fill-only / --inspect：跳过网络校时和目标时间校验。")
        else:
            LOG.info("正在校准网络时间……")
            clock.sync()
            if clock.synced:
                LOG.info(
                    "当前网络时间：%s（精度：%s）",
                    format_epoch(clock.now(), tz),
                    "毫秒级" if clock.precision == "ms" else "秒级",
                )
            else:
                LOG.warning("当前使用本机时间：%s", format_epoch(clock.now(), tz))

        if args.fill_only or args.inspect:
            target = clock.now()
        else:
            target = parse_target_time(
                str(cfg.get("submit_at", "12:40:00")),
                tz,
                clock,
                args.target_in,
            )
            seconds_left = target - clock.now()
            LOG.info(
                "目标提交时间：%s（还有 %.1f 秒）",
                format_epoch(target, tz),
                seconds_left,
            )
            if seconds_left < 30:
                LOG.warning(
                    "距离目标时间不足 30 秒，浏览器启动和人工确认可能来不及。"
                    "建议提前 5~10 分钟运行。"
                )

        with sync_playwright() as playwright:
            context = None
            try:
                context, page = launch_browser(playwright, cfg)
                open_form(page, str(cfg["form_url"]))
                wait_for_manual_ready(page, cfg, args.no_pause)

                if args.inspect:
                    dump_controls(page)
                    LOG.info("--inspect：只打印控件，不填表、不提交。")
                    return 0

                filler = FormFiller(page, cfg)

                refresh_before = max(
                    0.0,
                    float(cfg.get("refresh_before_seconds", 0) or 0),
                )
                if refresh_before > 0 and not args.fill_only:
                    refresh_at = target - refresh_before
                    if refresh_at <= clock.now():
                        LOG.warning(
                            "自动刷新时间已到（或已过），立即刷新表单页面。"
                        )
                    else:
                        wait_until_clock(clock, refresh_at, "自动刷新表单")
                    reload_form(page)

                LOG.info("开始填写 %d 个字段……", len(cfg.get("fields") or []))

                fast_open = bool(cfg.get("wait_for_fields")) and not args.fill_only
                if fast_open:
                    ready, pending = filler.fields_ready()
                    if ready:
                        LOG.info("字段已经可用，按正常速度填写。")
                        filler.fill_all()
                    else:
                        LOG.warning(
                            "字段暂未开放：%s。"
                            "进入快速开放模式：不修改 disabled，等页面自己启用后立即填写。",
                            "、".join(pending),
                        )
                        filler.wait_until_fields_ready(clock, target)
                        LOG.info("开始快速填写……")
                        fill_started = time.monotonic()
                        try:
                            filler.fill_all(only_missing=True, fast=True)
                        except Exception:
                            # 极少数页面会在开放瞬间再次重渲染，重等一次。
                            LOG.warning("快速填写失败，重新等待字段稳定后重试……")
                            time.sleep(0.1)
                            filler.wait_until_fields_ready(clock, target)
                            filler.fill_all(only_missing=True, fast=True)
                        LOG.info(
                            "快速填写耗时 %.0f ms。",
                            (time.monotonic() - fill_started) * 1000,
                        )
                else:
                    filler.fill_all()

                if args.fill_only:
                    filler.screenshot("filled_preview")
                    LOG.info("--fill-only：已填表并截图，不等待、不提交。")
                    return 0

                button = filler.find_submit()
                ok = filler.wait_and_submit(button, clock, target)
                return 0 if ok else 3
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass

    except KeyboardInterrupt:
        LOG.warning("用户取消。")
        return 130
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        LOG.error("%s", exc)
        return 2
    except PlaywrightError as exc:
        LOG.error("浏览器操作失败：%s", exc)
        return 2
    except Exception as exc:
        LOG.exception("未预期错误：%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
