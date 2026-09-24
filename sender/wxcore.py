# -*- coding: utf-8 -*-
"""wxcore.py —— 微信控件树桥接核心（读/写共用的底座）

职责
----
1. **找到真正的 UI 窗口**。判据：子控件里含 ``MMUIRenderSubWindow*`` 或 ``mmui::``。
   ⚠️ **绝不按标题挑窗口** —— 上游库 `WeChatUIA._wechat_hwnds()` 就是按标题
   （只认 '微信'/'Weixin'）挑，在本机会选中 262x296 的 Qt 空壳（hwnd=524832），
   而真正承载 UI 的是 hwnd=67958（标题 '11-13'）。结果：gate 字节写得对、校验却
   永远 False，库一直报「热激活失败」。本模块用正确判据绕开它。

2. **热激活 + 自愈**。微信重启/重登后 gate 字节归零，控件树退回 Qt 空壳，
   本模块负责重新置位。写法与上游库一致：**写前读原值、校验失败即回滚**。

3. **只读原语**：`read_sessions()` / `read_current_messages()`。

安全边界
--------
* 除 gate 字节外，本模块**不做任何写动作**（不点击、不输入、不发送）
* **不构造 WeChatDB**、不解密数据库、不读进程内存提密钥
* STOP 急停文件存在时，调用方应停止一切动作
"""
from __future__ import annotations

import ctypes
import os
import re
import time
from ctypes import wintypes

import uiautomation as auto
from wechatauto import uia_driver as ud

# ---------------------------------------------------------------- 常量

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020

WECHAT_WIN_PREFIXES = ("Qt51514", "MMUIRenderSubWindow", "mmui::")
UI_CHILD_PREFIXES = ("MMUIRenderSubWindow", "mmui::")

SPI_GETSCREENREADER = 0x0046
SPI_SETSCREENREADER = 0x0047
SPIF_SENDCHANGE = 0x0002

MSG_CLASS_TYPE = {
    "mmui::ChatTextItemView": "text",
    "mmui::ChatBubbleItemView": "bubble",
    "mmui::ChatBubbleReferItemView": "refer",
    "mmui::ChatItemView": "time",
    "mmui::ChatVoiceItemView": "voice",
    "mmui::ChatPersonalCardItemView": "card",
    "mmui::ChatImageItemView": "image",
    "mmui::ChatVideoItemView": "video",
    "mmui::ChatFileItemView": "file",
    "mmui::ChatSystemItemView": "system",
}

NAME_HINTS = (("[动画表情", "emoji"), ("[图片]", "image"), ("图片", "image"),
              ("[视频]", "video"), ("视频", "video"), ("[文件]", "file"),
              ("[链接]", "link"), ("位置", "location"),
              ("[语音]", "voice"), ("语音通话", "voip"))

_TIME_RE = re.compile(
    r"^(\d{1,2}:\d{2}|星期[一二三四五六日天]|昨天|前天|"
    r"\d{1,2}/\d{1,2}|\d{4}/\d{1,2}/\d{1,2}|上午|下午|中午|晚上)$")
_UNREAD_RE = re.compile(r"^\[(\d+)条\]\s*(.*)$")


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ensure_dpi_aware():
    """设进程 DPI 感知，保证 **UIA 矩形**与**截图坐标**在同一坐标系。

    本机是 150% 缩放（系统 DPI=144，物理 2560x1600）。不设的话
    UIA 给物理像素、截图按虚拟坐标，两者会整体偏移 —— 实测裁到的是任务栏。
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE
        return True
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return True
    except Exception:
        return False


def work_area():
    """主屏工作区（排除任务栏）。取不到返回 None。"""
    try:
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        r = RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            return (r.left, r.top, r.right, r.bottom)
    except Exception:
        pass
    return None


def last_input_idle_sec():
    """距用户上一次键盘/鼠标输入过了多少秒。

    用来避开「主人正在用电脑」的时机 —— 自动化会真实移动鼠标并点击，
    主人正在操作时抢鼠标看起来就像"鼠标抽风"，必须让路。
    """
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT),
                        ("dwTime", wintypes.DWORD)]
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return 999.0
        ticks = ctypes.windll.kernel32.GetTickCount()
        return max(0.0, (ticks - lii.dwTime) / 1000.0)
    except Exception:
        return 999.0


def cursor_pos():
    """当前鼠标位置（用于动完放回原处）。"""
    try:
        import win32api
        return win32api.GetCursorPos()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 「真人空闲」判定：必须扣掉自动化自己制造的那次输入
#
# 🔴 2026-09-23 事故根因（主人报"俩白名单同时发消息，回了一个另一个就不回了，跟宕机一样"）：
# `GetLastInputInfo` 把**自动化自己合成的鼠标点击/滚轮**也算作"用户输入"。
# 于是形成自锁死循环：
#   采集轮点鼠标去读会话 → 空闲归零 → 紧接着的回复轮判定"主人在用电脑" → 跳过回复
#   → 下轮采集又点 → 又归零 → **永远不回**（日志里连续 9 轮都是"主人在用电脑"）。
# 修法：每次自己合成输入后打一个时间戳，判定时把它扣掉。
# ---------------------------------------------------------------------------
_SELF_INPUT_TS = 0.0
_SELF_IDLE_BASE = 0.0        # 我们开始动手之前，真人已经空闲了多久
_BURST_GAP = 3.0             # 相邻两次自己动手间隔小于这个值 → 算同一轮


def begin_self_input():
    """在**生成任何输入之前**调用：把「动手前的真人空闲」记成基准。

    ⚠️ 这一步不能省！`mark_self_input()` 是动手**之后**打的，那时系统报的空闲
    已经是**我们自己刚才那次点击/滚轮**造成的 0 秒 —— 拿它当基准，
    基准就永远是 0，于是每轮都误判"真人刚动过"、回复全被跳过
    （实测日志里每轮都是「真人空闲仅 1.0s」，而主人根本没碰鼠标）。
    """
    global _SELF_IDLE_BASE, _SELF_INPUT_TS
    now = time.time()
    if _SELF_INPUT_TS and (now - _SELF_INPUT_TS) < _BURST_GAP:
        return                      # 还在同一轮"自己动手"里，基准不重置
    _SELF_IDLE_BASE = last_input_idle_sec()
    _SELF_INPUT_TS = now            # 先占位，避免嵌套调用时重复取基准


def mark_self_input():
    """在**生成输入之后**调用：刷新"我们自己最近动手"的时间戳。"""
    global _SELF_INPUT_TS
    _SELF_INPUT_TS = time.time()


def user_idle_sec():
    """**真人**的真·空闲秒数（已扣掉自动化自己制造的那次输入）。

    设 `i` = 系统报的空闲秒数，`m` = 距我们自己上次动手的秒数：
    * `i < m - 0.3` → 最近那次输入**比我们动手还新** → 是真人干的 → 返回 `i`
    * 否则 → 最近那次输入**就是我们自己** → 返回「动手前的真人空闲 + 距我们动手的时间」

    前提：每次自己动手**前后**都要分别调 `begin_self_input()` / `mark_self_input()`。
    """
    i = last_input_idle_sec()
    if not _SELF_INPUT_TS:
        return i
    m = time.time() - _SELF_INPUT_TS
    if i < m - 0.3:
        return i
    return _SELF_IDLE_BASE + m


def set_cursor_pos(pos):
    try:
        import win32api
        win32api.SetCursorPos((int(pos[0]), int(pos[1])))
        return True
    except Exception:
        return False


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- 通用工具

def find_first(root, pred, max_nodes=6000, max_depth=40):
    """在控件树里找第一个满足 pred 的控件（深度优先）。"""
    stack = [(root, 0)]
    n = 0
    while stack:
        ctrl, depth = stack.pop()
        if depth > max_depth or n > max_nodes:
            continue
        n += 1
        try:
            if pred(ctrl):
                return ctrl
        except Exception:
            pass
        try:
            for k in ctrl.GetChildren():
                stack.append((k, depth + 1))
        except Exception:
            pass
    return None


def runtime_id(ctrl):
    """控件的 RuntimeId 字符串（用于消息去重）。取不到就返回空串。"""
    for attr in ("runtimeid",):
        try:
            v = getattr(ctrl, attr)
            if v:
                return str(v)
        except Exception:
            pass
    try:
        rid = ctrl.GetRuntimeId()
        if rid:
            return "-".join(str(x) for x in rid)
    except Exception:
        pass
    return ""


def parse_session_cell(raw, aid=""):
    """把会话列表 cell 的 Name 拆成结构化字段。

    实测格式（`\n` 分隔，除首行外顺序可变、可缺）::

        会话名
        已置顶
        [未读条数] [发送者: 消息预览]
        时间
        消息免打扰
    """
    raw = (raw or "").strip("\n")
    parts = [p.strip() for p in raw.split("\n")]
    parts = [p for p in parts if p != ""]
    if not parts:
        return {"name": "", "pinned": False, "muted": False, "unread": 0,
                "time": "", "preview": "", "aid": aid, "raw": raw}

    name = parts[0]
    rest = parts[1:]

    muted = "消息免打扰" in rest
    rest = [p for p in rest if p != "消息免打扰"]
    pinned = "已置顶" in rest
    rest = [p for p in rest if p != "已置顶"]

    unread = 0
    body = []
    for p in rest:
        m = _UNREAD_RE.match(p)
        if m:
            unread = int(m.group(1))
            tail = m.group(2).strip()
            if tail:
                body.append(tail)
        else:
            body.append(p)

    t = ""
    if body and _TIME_RE.match(body[-1]):
        t = body.pop()

    return {"name": name, "pinned": pinned, "muted": muted, "unread": unread,
            "time": t, "preview": " ".join(body).strip(), "aid": aid, "raw": raw}


def classify_message(cls, name):
    t = MSG_CLASS_TYPE.get(cls or "")
    if t:
        return t
    for hint, kind in NAME_HINTS:
        if (name or "").startswith(hint):
            return kind
    return "unknown"


# ---------------------------------------------------------------- 桥接主体

class WeChatBridge:
    """微信控件树桥接。用法::

        br = WeChatBridge(config)
        ok, why = br.ensure_ready()
        sessions = br.read_sessions()
        messages = br.read_current_messages()
        br.close()
    """

    def __init__(self, config=None, verbose=True):
        self.cfg = config or {}
        self.verbose = verbose
        self.hwnd = None
        self._root = None
        self._driver = None
        self.verified_rva = None
        self._screen_reader_before = None
        self.window_desc = ""
        ensure_dpi_aware()

    # ------------------------------------------------------------ 日志
    def _log(self, msg):
        if self.verbose:
            log(msg)

    # ------------------------------------------------------------ 驱动器
    def driver(self):
        if self._driver is None:
            self._driver = ud.WeChatUIA()
        return self._driver

    # ------------------------------------------------------------ 窗口
    def _weixin_pids(self):
        try:
            import psutil
        except Exception:
            return set()
        out = set()
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if (p.info["name"] or "").lower() == "weixin.exe":
                    out.add(p.info["pid"])
            except Exception:
                pass
        return out

    def list_windows(self):
        """列出微信全部顶层 Qt/渲染窗口（含隐藏）。不按可见性过滤 —— 托盘态也要能找到。"""
        try:
            import win32gui
            import win32process
        except Exception:
            return []
        pids = self._weixin_pids()
        out = []

        def cb(h, _):
            try:
                _tid, pid = win32process.GetWindowThreadProcessId(h)
                if pids and pid not in pids:
                    return True
                cls = win32gui.GetClassName(h) or ""
                if not cls.startswith(WECHAT_WIN_PREFIXES):
                    return True
                l, t, r, b = win32gui.GetWindowRect(h)
                out.append({"hwnd": h, "pid": pid, "cls": cls,
                            "title": win32gui.GetWindowText(h) or "",
                            "visible": bool(win32gui.IsWindowVisible(h)),
                            "w": r - l, "h": b - t})
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            pass
        out.sort(key=lambda w: -(w["w"] * w["h"]))
        return out

    def pick_ui_hwnd(self):
        """挑出真正的 UI 窗口。

        **首选**：子控件里含 `MMUIRenderSubWindow*` / `mmui::` 的那个（最可靠，别按标题挑）。

        ⚠️ **必须有兜底**（2026-09-23 实测踩到，服务直接起不来）：
        微信重启或闲置后 gate 字节会归零、控件树退回 **Qt 空壳**，
        那时上面那条**永远匹配不上** → `ensure_ready` 报「找不到微信 UI 窗口」
        → **走不到 activate()**，形成死循环：
            要激活得先认出窗口，要认出窗口得先激活。

        兜底规则：WeChat 进程里、类名像 Qt 主窗口（含 `QWindowIcon`）、
        排除托盘/提示/输入法/电源消息窗，且尺寸够大。
        """
        wins = self.list_windows()

        # ---- ① 首选：已经物化的 mmui 树 ----
        for w in wins:
            try:
                ctrl = auto.ControlFromHandle(w["hwnd"])
            except Exception:
                continue
            if ctrl is None:
                continue
            try:
                kids = ctrl.GetChildren()
            except Exception:
                kids = []
            if any((getattr(k, "ClassName", "") or "").startswith(UI_CHILD_PREFIXES)
                   for k in kids):
                self.window_desc = ("hwnd=%d pid=%d class=%s title=%r %dx%d "
                                    "[含 MMUIRenderSubWindow/mmui 子控件]" % (
                                        w["hwnd"], w["pid"], w["cls"], w["title"],
                                        w["w"], w["h"]))
                return w["hwnd"]

        # ---- ② 兜底：没物化时按类名挑主窗口，交给 activate() 去物化 ----
        best = None
        for w in wins:
            cls = w.get("cls") or ""
            if not cls.startswith("Qt") or "QWindowIcon" not in cls:
                continue
            if any(x in cls for x in ("Tool", "Tray", "Message", "Power", "IME")):
                continue
            if (w.get("w") or 0) < 400 or (w.get("h") or 0) < 400:
                continue
            if best is None or (w["w"] * w["h"]) > (best["w"] * best["h"]):
                best = w
        if best is not None:
            self.window_desc = ("hwnd=%d pid=%d class=%s title=%r %dx%d "
                                "[未物化·按 Qt 主窗口兜底选中，待激活]" % (
                                    best["hwnd"], best["pid"], best["cls"],
                                    best["title"], best["w"], best["h"]))
            return best["hwnd"]
        return None

    def mmui_present(self, hwnd=None, timeout=1.0):
        return ud.WeChatUIA._mmui_present(hwnd or self.hwnd, timeout=timeout)

    def root(self, refresh=False):
        if self._root is None or refresh:
            self._root = auto.ControlFromHandle(self.hwnd)
        return self._root

    # ------------------------------------------------------------ 系统读屏标志
    @staticmethod
    def get_screen_reader():
        try:
            v = ctypes.c_int(0)
            ok = ctypes.windll.user32.SystemParametersInfoW(
                SPI_GETSCREENREADER, 0, ctypes.byref(v), 0)
            return bool(v.value) if ok else None
        except Exception:
            return None

    @staticmethod
    def set_screen_reader(enable):
        try:
            return bool(ctypes.windll.user32.SystemParametersInfoW(
                SPI_SETSCREENREADER, 1 if enable else 0, None, SPIF_SENDCHANGE))
        except Exception:
            return False

    # ------------------------------------------------------------ 热激活
    def activate(self, timeout=8.0):
        """置位 gate 字节使控件树物化。返回 (ok, detail)。

        只用上游库的**扫描/读写原语**，但校验目标是**我们自己锁定的窗口**。
        """
        hwnd = self.hwnd or self.pick_ui_hwnd()
        if hwnd is None:
            return False, "找不到微信 UI 窗口"
        self.hwnd = hwnd
        if self.mmui_present(hwnd):
            return True, "控件树已就绪"

        if self.cfg.get("set_screen_reader", True):
            self._screen_reader_before = self.get_screen_reader()
            self.set_screen_reader(True)

        drv = self.driver()
        pid = ud.WeChatUIA._pid_from_hwnd(hwnd)
        if not pid:
            return False, "拿不到窗口所属 PID"
        mod = drv._weixin_dll_module(pid)
        if not mod:
            return False, "未找到 Weixin.dll"
        base, _size, dll_path = mod
        cands = drv._qaccessible_candidate_rvas(dll_path)
        if not cands:
            return False, "不支持的 Weixin.dll：%s" % dll_path

        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        access = (PROCESS_QUERY_INFORMATION | PROCESS_VM_READ |
                  PROCESS_VM_WRITE | PROCESS_VM_OPERATION)
        handle = k32.OpenProcess(access, False, pid)
        if not handle:
            return False, ("OpenProcess 失败 err=%d —— 多为权限受限（需要 Full access）"
                           % k32.GetLastError())
        try:
            deadline = time.time() + max(2.0, timeout)
            while time.time() < deadline:
                for rva in cands[:4]:
                    addr = int(base) + int(rva)
                    cur = drv._read_process_byte(handle, addr)
                    if cur is None:
                        continue
                    wrote = False
                    if cur != 1:
                        if not drv._write_process_byte(handle, addr, 1):
                            continue
                        wrote = True
                    if self.mmui_present(hwnd, timeout=1.2):
                        self.verified_rva = int(rva)
                        try:
                            ud._gate_cache_put(ud._dll_identity(dll_path),
                                               verified=int(rva))
                        except Exception:
                            pass
                        return True, "热激活成功：Weixin.dll+0x%x" % rva
                    if wrote:                      # 候选不对：回滚，继续试下一个
                        drv._write_process_byte(handle, addr, cur)
                time.sleep(0.3)
            return False, "候选 %d 个均未使控件树物化（DLL=%s）" % (
                len(cands[:4]), os.path.basename(dll_path))
        finally:
            k32.CloseHandle(handle)

    # ------------------------------------------------------------ 就绪
    def ensure_ready(self, activate=True):
        hwnd = self.pick_ui_hwnd()
        if hwnd is None:
            wins = self.list_windows()
            detail = ("找不到微信 UI 窗口。"
                      "候选窗口 %d 个；请确认微信已启动并已登录（托盘态也可以）" % len(wins))
            return False, detail
        self.hwnd = hwnd
        self._root = None
        self._log("  UI 窗口：%s" % self.window_desc)

        if self.mmui_present(hwnd):
            return True, "控件树已就绪（无需激活）"
        if not activate:
            return False, "控件树未物化，且本次禁用了激活（--no-activate）"

        t = time.time()
        ok, why = self.activate()
        self._log("  热激活：%s（%.1fs）" % (why, time.time() - t))
        if not ok:
            return False, why
        self._root = None
        return True, why

    # ------------------------------------------------------------ 读取：会话列表
    def _session_table(self):
        root = self.root()
        if root is None:
            return None
        return find_first(root, lambda c: (getattr(c, "AutomationId", "") or "") == "session_list")

    def session_cells(self):
        """返回会话列表的 cell 控件列表（未解析）。"""
        tbl = self._session_table()
        if tbl is None:
            return []
        try:
            return list(tbl.GetChildren())
        except Exception:
            return []

    @staticmethod
    def _cell_match(c, name):
        try:
            aid = getattr(c, "AutomationId", "") or ""
            cn = (c.Name or "").split("\n")[0].strip()
        except Exception:
            return False
        return (aid == "session_item_" + name or cn == name
                or (name and (name in cn or cn in name)))

    def _locate_session_cell(self, name, max_scrolls=14):
        """在会话列表里找目标 cell，找不到就**边滚边找**。

        ⚠️ 会话列表也是**虚拟列表**：滚出视口的会话**根本不在控件树里**。
        实测踩过 —— 前面几个会话处理后列表滚下去了，
        后面的 `open_session('服务号')` 就报「会话列表里没找到」。
        """
        tbl = self._session_table()
        if tbl is None:
            return None, None
        for c in self.session_cells():
            if self._cell_match(c, name):
                return c, tbl
        # 先滚回顶部，再一屏一屏往下找
        try:
            for _ in range(14):
                tbl.WheelUp()
            time.sleep(0.35)
        except Exception:
            pass
        for _ in range(max_scrolls):
            for c in self.session_cells():
                if self._cell_match(c, name):
                    return c, tbl
            try:
                tbl.WheelDown()
            except Exception:
                break
            time.sleep(0.28)
        return None, tbl

    def scroll_session_list_top(self, times=16):
        """把会话列表滚回顶部。

        ⚠️ 会话列表是**虚拟列表**：滚出视口的会话**根本不在控件树里**。
        采集轮去读下面的群会把列表滚下去，紧接着的回复轮就"看不到"上面的
        白名单会话（实测：`文件传输助手` / `好友D` 双双读不到 → 目标 0 个）。
        """
        tbl = self._session_table()
        if tbl is None:
            return False
        begin_self_input()
        try:
            for _ in range(int(times)):
                tbl.WheelUp()
            time.sleep(0.4)
            return True
        except Exception as e:
            self._log("  （滚回顶部失败：%s）" % e)
            return False
        finally:
            mark_self_input()

    def go_back(self):
        """若停在**子面板**里，点左上角「返回」箭头回到主会话列表。

        ⚠️ 实战教训（主人 2026-09-23 亲自指点）：
        **「服务号」「公众号」「折叠的聊天」这类是聚合入口，点进去是一个面板、不是聊天**，
        里面本来就只有那几条，误点进去后**主会话列表会"消失"**，看起来像采集坏了。

        ⚠️ 更坑的是：`root()` 只能拿到 2 个直接子控件，**走不到 MMUIRenderSubWindow 那棵树**，
        所以靠控件名找「返回」按钮**根本找不到**（人家试过，`go_back` 一直是瞎的）。
        改用**几何位置点击**：返回箭头固定在会话列表左上方
        ——「列表左边界 +31，列表顶边界 -27」（实测 101+31=132, 173-27=146 命中）。
        """
        tbl = self._session_table()
        if tbl is None:
            return False
        try:
            nm = (tbl.Name or "").strip()
            r = tbl.BoundingRectangle
        except Exception:
            return False
        if nm in ("会话", ""):        # 已经在主列表（主列表的 Name 是「会话」）
            return False
        self._log("  （当前在子面板 %r → 点「返回」复位）" % nm)
        begin_self_input()
        try:
            import win32api
            import win32gui
            pt = win32gui.ClientToScreen(self.hwnd, (0, 0))
            x, y = pt[0] + r.left + 31, pt[1] + r.top - 27
            win32api.SetCursorPos((x, y))
            time.sleep(0.25)
            u32 = ctypes.windll.user32
            u32.mouse_event(0x0002, 0, 0, 0, 0)      # 左键按下
            time.sleep(0.06)
            u32.mouse_event(0x0004, 0, 0, 0, 0)      # 左键抬起
            time.sleep(0.9)
            t2 = self._session_table()
            ok = t2 is not None and (t2.Name or "").strip() == "会话"
            self._log("  （返回%s）" % ("成功" if ok else "后仍不在主列表"))
            return ok
        except Exception as e:
            self._log("  （返回失败：%s）" % e)
            return False
        finally:
            mark_self_input()

    def open_session(self, name, exact=False):
        """打开会话。外面包一层：**动手前记基准、动手后打标记**，
        否则空闲判定会把自己刚才的点击当成真人在操作（自锁事故）。"""
        begin_self_input()
        try:
            return self._open_session(name, exact)
        finally:
            mark_self_input()

    def _open_session(self, name, exact=False):
        """按会话名**点开**会话（UI 动作）。

        比上游的 `open_chat`(搜索框打字) 可靠得多 —— 会话列表每个 cell 都带
        `aid='session_item_<会话名>'`，可以直接精确定位。实测上游的搜索式切换
        在本机**失败**（含 emoji 的群名，34 秒后放弃）。

        返回 (ok, detail)。
        """
        if not name:
            return False, "会话名为空"
        # 聚合入口（服务号/公众号那种）点进去是面板不是聊天 —— 直接跳过
        for s in (self.cfg.get("skip_sessions") or []):
            if s and (name == s or name.startswith(s)):
                return False, "%r 是聚合入口（不是聊天会话），已跳过" % name
        # ⚠️ 如果还停在上一次误进的子面板里，**先复位** —— 否则"主会话列表消失"，
        #    后面所有会话都找不到（实测被这个坑了整整一轮）。
        self.go_back()
        # 已经在这个会话里就不用动
        if self._chat_name_matches(name):
            return True, "已经在目标会话"
        # ⚠️ 找 cell 可能要滚动，而**滚轮只对前台窗口生效** ——
        #    不先置前，滚的是别的窗口，等于白滚（实测踩过）。
        try:
            import win32gui
            if win32gui.GetForegroundWindow() != self.hwnd:
                self.restore_window()
                time.sleep(0.3)
        except Exception:
            pass
        hit, _tbl = self._locate_session_cell(name)
        if hit is None:
            return False, "会话列表里没找到 %r（滚遍了也没找到）" % name

        # ---- 点击 + 校验，最多重试 3 轮（会话列表会随新消息重排，cell 会重建）----
        last_err = ""
        for attempt in range(1, 4):
            # ⚠️ 点击前**必须**让微信在最前台：Click() 是真实鼠标点击，
            #    窗口被别的程序挡住时，点击会落到挡住它的那个窗口上（实测踩过）。
            try:
                import win32gui
                if win32gui.GetForegroundWindow() != self.hwnd:
                    self.restore_window()
                    time.sleep(0.3)
            except Exception as e:
                self._log("  （置前失败：%s）" % e)
            try:
                from wechatauto import uia as wxuia
                tbl = self._session_table()
                # 每轮都重新取一次 cell：微信收新消息时列表会重排，旧句柄可能失效
                cells = self.session_cells()
                target = None
                for c in cells:
                    if self._cell_match(c, name):
                        target = c
                        break
                if target is None:
                    # 可能滚出视口了（会话列表也是虚拟列表）→ 边滚边找
                    target, tbl2 = self._locate_session_cell(name)
                    if tbl2 is not None:
                        tbl = tbl2
                if target is None:
                    last_err = "会话列表里暂时找不到 %r" % name
                    time.sleep(0.4)
                    continue

                # 滚进可视区（uiautomation 2.0.29 的 Control 没有 ScrollIntoView，
                # 用上游的 RollIntoView —— 它靠 WheelUp/WheelDown 实现）
                try:
                    tr = tbl.BoundingRectangle
                    cr = target.BoundingRectangle
                    if cr.top < tr.top or cr.bottom > tr.bottom:
                        wxuia.RollIntoView(tbl, target)
                        time.sleep(0.35)
                except Exception as e:
                    self._log("  （滚动入视口失败，继续尝试直接点击：%s）" % e)

                # ⚠️ 滚动之后列表还在惯性滑动 —— 必须等它停稳、再重新读一次 cell，
                #    否则 Click() 用的是滚动前算出的坐标，会点到**别的会话**上
                #    （实测：点「某公司群」结果打开了「2026向往传媒临汾IT交流群」）。
                time.sleep(0.45)
                for c in self.session_cells():
                    if self._cell_match(c, name):
                        target = c
                        break

                # ⚠️ 必须用默认的 simulateMove=True！实测 simulateMove=False 时
                #    微信的 Qt 界面**完全不响应**（点了等于没点）。
                target.Click(waitTime=0.05)
            except Exception as e:
                last_err = "点击失败：%s: %s" % (type(e).__name__, e)
                continue

            # 等会话名跟上（同时看输入框 Name 和标题栏 label 两个来源）
            deadline = time.time() + 4.0
            while time.time() < deadline:
                if self._chat_name_matches(name):
                    return True, "已切到 %r" % name
                time.sleep(0.25)
            last_err = "点了但会话名没跟上"
            self._log("  （第 %d 次点击未生效，重试）" % attempt)
            time.sleep(0.5)

        # 兜底：可能误进了子面板（服务号/公众号那种）→ 点左上角「返回」复位
        if self.go_back():
            self._log("  （已点「返回」复位到会话列表）")
        return False, last_err or "切换失败"

    def read_sessions(self):
        root = self.root()
        if root is None:
            return []
        tbl = find_first(root, lambda c: (getattr(c, "AutomationId", "") or "") == "session_list")
        if tbl is None:
            return []
        out = []
        try:
            cells = tbl.GetChildren()
        except Exception:
            return []
        for cell in cells:
            try:
                out.append(parse_session_cell(cell.Name or "",
                                              getattr(cell, "AutomationId", "") or ""))
            except Exception:
                continue
        return out

    # ------------------------------------------------------------ 读取：当前会话
    def current_chat_title(self):
        root = self.root()
        if root is None:
            return ""
        try:
            c = find_first(root, lambda c: (getattr(c, "AutomationId", "") or "")
                           .endswith("current_chat_name_label"))
            return (c.Name or "") if c is not None else ""
        except Exception:
            return ""

    def _msg_container(self):
        root = self.root()
        if root is None:
            return None, None
        mv = find_first(root, lambda c: (c.ClassName or "") == "mmui::MessageView")
        if mv is None:
            return None, None
        # ⚠️ 消息不在 MessageView 的直接子节点，而在 RecyclerListView 里（多一层）
        rlv = find_first(mv, lambda c: (c.ClassName or "") == "mmui::RecyclerListView") or mv
        return mv, rlv

    # ------------------------------------------------------------ 方向判定
    def _clip(self, rect, viewport=None):
        """把矩形裁到「消息视口 ∩ 工作区（排除任务栏）∩ 屏幕」。"""
        l, t, r, b = (int(x) for x in rect)
        if viewport:
            l, t = max(l, int(viewport[0])), max(t, int(viewport[1]))
            r, b = min(r, int(viewport[2])), min(b, int(viewport[3]))
        wa = work_area()
        if wa:
            l, t = max(l, wa[0]), max(t, wa[1])
            r, b = min(r, wa[2]), min(b, wa[3])
        return (l, t, r, b)

    def detect_side(self, rect, viewport=None, thr=28, min_cov=0.02):
        """判断这条消息是「自己」还是「对方」发的 —— 靠气泡重心在左还是右。

        控件树里消息行是**叶子节点且满宽**（实测 834px，左右边距都是 0），
        拿不到发送者字段，所以只能靠像素。自己发的靠右（绿底）、对方发的靠左。

        返回 dict: {side: right|left|unknown, conf, coverage, detail}
        """
        out = {"side": "unknown", "conf": 0.0, "coverage": 0.0, "detail": ""}
        try:
            from PIL import ImageGrab
        except Exception as e:
            out["detail"] = "缺少 Pillow：%s" % e
            return out
        box = self._clip(rect, viewport)
        if box[2] - box[0] < 12 or box[3] - box[1] < 8:
            out["detail"] = "有效矩形太小 %r" % (box,)
            return out
        try:
            import numpy as np
        except Exception:
            np = None
        try:
            img = ImageGrab.grab(bbox=box, all_screens=True)
        except Exception as e:
            out["detail"] = "截图失败：%s" % e
            return out
        if np is None:
            out["detail"] = "缺少 numpy"
            return out
        a = np.asarray(img.convert("RGB"), dtype=np.int16)
        h, w, _ = a.shape
        # 背景色 = 出现最多的颜色（量化到 8 级降噪）
        q = (a // 8).reshape(-1, 3)
        cols, counts = np.unique(q, axis=0, return_counts=True)
        bg = cols[counts.argmax()] * 8 + 4
        mask = np.abs(a - bg).max(axis=2) > thr
        cov = float(mask.sum()) / float(mask.size)
        out["coverage"] = round(cov, 4)
        if mask.sum() < 40 or cov < min_cov:
            out["detail"] = "非背景像素太少（cov=%.3f）" % cov
            return out
        xs = np.tile(np.arange(w), (h, 1))[mask]
        mean_x = float(xs.mean())
        cx = w / 2.0
        out["side"] = "right" if mean_x > cx else "left"
        out["conf"] = round(abs(mean_x - cx) / cx, 3)
        out["detail"] = "w=%d bg=%s mean_x=%.1f center=%.1f cov=%.3f" % (
            w, tuple(int(x) for x in bg), mean_x, cx, cov)
        return out

    def read_current_messages(self, limit=60, with_side=False):
        """读**当前已打开会话**的消息。只读，不切换会话、不点击。

        with_side=True 时额外判断每条消息的发送方（自己/对方），
        需要截图 —— 代价是**必须先让微信窗口置前**（否则拍到的是别的窗口）。
        """
        mv, rlv = self._msg_container()
        if rlv is None:
            return []
        viewport = None
        try:
            r = mv.BoundingRectangle
            viewport = (r.left, r.top, r.right, r.bottom)
        except Exception:
            pass
        try:
            rows = rlv.GetChildren()
        except Exception:
            return []

        out = []
        for r in rows:
            try:
                cls = r.ClassName or ""
                name = (r.Name or "").strip()
                rect = r.BoundingRectangle
                box = [rect.left, rect.top, rect.right, rect.bottom]
            except Exception:
                continue
            rec = {
                "type": classify_message(cls, name),
                "class": cls,
                "text": name,
                "aid": getattr(r, "AutomationId", "") or "",
                "runtime_id": runtime_id(r),
                "rect": box,
                "side": None,
                "side_conf": None,
            }
            if with_side and rec["type"] != "time":
                d = self.detect_side(box, viewport=viewport)
                rec["side"] = d["side"]
                rec["side_conf"] = d["conf"]
                rec["side_detail"] = d["detail"]
            out.append(rec)
        if limit and len(out) > limit:
            out = out[-limit:]
        return out

    # ------------------------------------------------------------ 收尾
    def close(self):
        """还原系统读屏标志（若本次是自己置位的）。"""
        if self._screen_reader_before is not None:
            cur = self.get_screen_reader()
            if cur is not None and cur != self._screen_reader_before:
                self.set_screen_reader(self._screen_reader_before)
                self._log("  已还原 SPI_SETSCREENREADER -> %s" % self._screen_reader_before)
            self._screen_reader_before = None

    # ------------------------------------------------------------ 发送侧支持
    def is_minimized(self):
        try:
            import win32gui
            return bool(win32gui.IsIconic(self.hwnd))
        except Exception:
            return False

    def restore_window(self):
        """把主窗口从最小化/托盘态还原并置前。

        发送必须在窗口**真实可见且在屏幕上**时进行 —— `_paste_into` 会真实点击
        输入框，窗口若停在 -32000 的托盘坐标，点击会落到虚空里。
        注意：只还原+置前，**不像上游 GUI 层那样去最小化别的窗口**。
        """
        try:
            import win32con
            import win32gui
        except Exception as e:
            return False, "缺少 win32gui：%s" % e
        h = self.hwnd
        try:
            if win32gui.IsIconic(h):
                win32gui.ShowWindow(h, win32con.SW_RESTORE)
            win32gui.ShowWindow(h, win32con.SW_SHOW)
            ud.WeChatUIA._force_foreground(h)
        except Exception as e:
            return False, "还原窗口失败：%s: %s" % (type(e).__name__, e)
        deadline = time.time() + 4.0
        while time.time() < deadline:
            try:
                if not win32gui.IsIconic(h):
                    l, t, r, b = win32gui.GetWindowRect(h)
                    if l > -30000 and t > -30000:
                        return True, "已还原并置前（rect=%d,%d,%d,%d）" % (l, t, r, b)
            except Exception:
                pass
            time.sleep(0.3)
        return False, "窗口仍在最小化/托盘坐标"

    def pinned_driver(self):
        """返回一个**窗口指针已钉死到正确 hwnd** 的 WeChatUIA。

        上游 `WeChatUIA` 自己找窗口会选中 262x296 的 Qt 空壳（内置浏览器窗），
        于是发送必然失败。这里把 `_win` 直接钉住，并把 `ensure_window` 短路，
        让它的 `send_text` 直接作用在正确的窗口上。
        """
        drv = self.driver()
        ctrl = auto.ControlFromHandle(self.hwnd)
        if ctrl is None:
            return None
        drv._win = ctrl
        drv._find_main = lambda: ctrl
        drv.ensure_window = lambda *a, **k: True
        try:
            auto.InitializeUIAutomationInCurrentThread()
        except Exception:
            pass
        return drv

    def current_chat(self, drv=None):
        """当前打开会话的名称。

        ⚠️ 每次都**重新锚定** `_win`：微信切换会话/重建控件树后，
        之前抓到的 Control 可能已经失效（表现为突然返回 None）。
        同时取两个来源（聊天输入框的 Name、标题栏的 current_chat_name_label），
        任一可用即返回。
        """
        drv = drv or self.driver()
        try:
            ctrl = auto.ControlFromHandle(self.hwnd)
            if ctrl is not None:
                drv._win = ctrl
                drv._find_main = lambda: ctrl
                drv.ensure_window = lambda *a, **k: True
        except Exception:
            pass
        try:
            n = drv.current_chat()
            if n:
                return n
        except Exception:
            pass
        t = self.current_chat_title()
        return t or None

    def _chat_name_matches(self, want):
        """当前会话名是否等于/包含 want（两个来源都试）。"""
        if not want:
            return True
        cands = []
        try:
            d = self.driver()
            n = d.current_chat()
            if n:
                cands.append(n)
        except Exception:
            pass
        t = self.current_chat_title()
        if t:
            cands.append(t)
        for c in cands:
            if c == want or want in c or c in want:
                return True
        return False

    # ------------------------------------------------------------ 保险的发送
    def send_text_safe(self, text, to=None, readback=True, readback_timeout=8.0,
                       restore_clipboard=True, log=print):
        """外面包一层：发送会合成键鼠输入，**动手前记基准、动手后打标记**（否则自锁）。"""
        begin_self_input()
        try:
            return self._send_text_safe(text, to=to, readback=readback,
                                        readback_timeout=readback_timeout,
                                        restore_clipboard=restore_clipboard, log=log)
        finally:
            mark_self_input()

    def _send_text_safe(self, text, to=None, readback=True, readback_timeout=8.0,
                        restore_clipboard=True, log=print):
        """完整的一次发送：切会话 → 置前 → 存剪贴板 → 发送 → 还原剪贴板 → 回读。

        只做「发一条文本」这一件事；白名单/脱敏/速率/审计都不在这里 ——
        那些在 `guard.py`，由调用方决定。返回 dict。
        """
        res = {"ok": False, "detail": "", "switched": None, "readback": None,
               "readback_rid": None, "chat_before": None, "chat_after": None}
        if not text or not text.strip():
            res["detail"] = "内容为空，什么都没做"
            return res
        try:
            drv = self.pinned_driver()
            if drv is None:
                res["detail"] = "拿不到钉住窗口的驱动器"
                return res
            res["chat_before"] = self.current_chat(drv)

            if to and res["chat_before"] != to:
                ok, why = self.open_session(to)
                res["switched"] = ok
                log("  ➡️ 切换会话 %r：%s" % (to, why))
                if not ok:
                    res["detail"] = "切换会话失败：%s" % why
                    return res

            ok, why = self.restore_window()
            if not ok:
                res["detail"] = "窗口还原失败：%s" % why
                return res

            saved = None
            if restore_clipboard:
                try:
                    import pyperclip
                    saved = pyperclip.paste()
                except Exception:
                    saved = None
            try:
                sent = bool(drv.send_text(text))
            finally:
                if restore_clipboard and saved is not None:
                    try:
                        import pyperclip
                        pyperclip.copy(saved)
                    except Exception:
                        pass
            res["ok"] = sent
            res["detail"] = "发送成功" if sent else "send_text 返回 False"
            if not sent:
                return res

            if readback:
                deadline = time.time() + max(1.0, readback_timeout)
                key = text.strip()[:20]
                found = False
                while time.time() < deadline:
                    try:
                        for m in self.read_current_messages(limit=15):
                            if key and key in (m.get("text") or ""):
                                found = True
                                # 记下这条的 runtime_id —— 采集模块靠它区分
                                # 「鲸鱼娘发的」和「主人亲手打的」（两者在微信里长得一样）
                                res["readback_rid"] = m.get("runtime_id")
                                break
                    except Exception:
                        pass
                    if found:
                        break
                    time.sleep(0.8)
                res["readback"] = found
                res["detail"] = "发送成功且已回读确认" if found else "发送成功但回读未确认"
            try:
                res["chat_after"] = self.current_chat(drv)
            except Exception:
                pass
            return res
        except Exception as e:
            res["detail"] = "异常：%s: %s" % (type(e).__name__, e)
            return res
