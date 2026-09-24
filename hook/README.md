# 发送腿 B（hook 注入）· 空心针验证记录

> 日期：2026-09-23 晚 ｜ 目标进程：`Weixin.exe` **4.1.15.9**（pid 20872，202 MB `Weixin.dll`）
> 目的：用**最小代价**问清三个未知量，再决定要不要投入逆向工程。

---

## 一、结论：三个问题全部通过 ✅

| 问题 | 结果 | 证据 |
|---|---|---|
| **能不能注入** | ✅ **能，而且不需要提权** | agent 在 pid=20872 内跑起来，读到 143 个模块、`Weixin.dll` base=`0x7ffda7710000`、头两字节 `77 90`（`MZ`） |
| **Defender 拦不拦** | ✅ 没拦 | 注入成功、无报毒、无阻断 |
| **微信会不会崩/掉线** | ✅ 毫发无损 | 5 个进程 PID 与启动时间**都没变**；真 UI 窗口 `'11-13'` 仍在；无登录/扫码页；`message_0.db-wal` **52 秒前仍在写入** → 登录态、实时收消息 |
| 卸载干净 | ✅ | `unload` + `detach` 正常，链接里无残留 |

**⚠️ 重要更正**：之前人家根据 `OpenProcess(PROCESS_VM_WRITE)` 返回 **err=5** 就断言"注入不可能、必须提权"——
**这个判断是错的**。实测非提权状态下 frida 注入一次成功。

原因：`VM_WRITE` 这个**单独的访问掩码**被拒，不代表 frida 的注入路径拿不到写权限。
frida 走的是它自己的 winjector 助手链路，跟我们手工 `OpenProcess` 的掩码不是一回事。
**教训：这种"能不能"的问题，只有实测说了算，不能靠单点探针推断。**

---

## 二、frida 在本机的实际脾气（都踩过了）

| 现象 | 说明 | 处理 |
|---|---|---|
| `device.enumerate_processes()` **恒返回 0 个** | frida 报 `winjector.vala:87: Error setting ACLs (SetNamedSecurityInfo returned 0x00000000)` | **弃用它**，pid 自己用 toolhelp 拿（`inject.py: find_pids_ctypes()`） |
| `attach` 一个 202 MB 的进程要 **~19 秒** | 首次注入要提取/加载 agent | 超时给足（`--timeout 25`） |
| 沙箱会把 uv 缓存挡在工作区外 | `os error 5` | 设 `UV_CACHE_DIR` 指到工作区内 |
| **frida 本身能跑通** | 自注入（attach 自己）完整成功 | 见 `frida_selftest.py` |

---

## 三、文件

| 文件 | 作用 |
|---|---|
| `hollow_needle.js` | ⭐ 空心针 agent：**零 hook、零写入**，只做只读自报 |
| `inject.py` | 注入器：找 pid（ctypes 回退）→ attach → load → 收报告 → unload → detach → 存活自检 |
| `run_hollow.cmd` | 启动器（**全 ASCII**）；当前**不需要**提权也能跑 |
| `frida_selftest.py` | frida 自检：设备/进程枚举/自注入，用来判断 frida 本身是否可用 |
| `health.py` | 微信健康检查：顶层窗口 + 数据库写入活跃度（判断有没有崩/掉线） |
| `hollow_needle.log` | 首次成功注入的原始日志 |

用法：

```powershell
cd "<项目目录>"
& .\.venv\Scripts\python.exe hook\inject.py          # 自动找 pid
& .\.venv\Scripts\python.exe hook\inject.py --pid 20872 --timeout 25
& .\.venv\Scripts\python.exe hook\health.py          # 微信健康检查
& .\.venv\Scripts\python.exe hook\frida_selftest.py  # frida 自检
```

---

## 四、下一步（还没做）

空心针只证明"**能进去**"。真正的 B 还差**找到发送函数**：

1. **先摸清方法，别自己硬逆**
   社区有 4.1.x 的先例 —— [WechatHook4.1.10.27](https://github.com/mosheng20205/WechatHook4.1.10.27)、
   [wx-basic-bridge](https://github.com/ssq123123/wx-basic-bridge)（4.1.9.23 DLL 注入桥）、
   [看雪那篇带 frida 脚本的 4.0 hook](https://bbs.kanxue.com/thread-289764-2.htm)。
   学它们的**定位手法**（特征码 / 调用栈回溯），再为 4.1.15.9 重采锚点。
   **绝不下载运行别人的成品 exe/DLL。**
2. **动态发现**：hook 一个稳定的低层落点（如消息入库的那次写入），
   然后**手动**从微信发一条消息，抓调用栈 → 从栈里认出发送函数。
   ⭐ 测试一律发「文件传输助手」（发给自己，零社交风险）。
3. **风控与检测**：这次没触发，不代表长期不触发。每次注入后都要跑 `health.py`。

---

## 五、红线（B 路线专用）

1. **agent 只做只读**：不写内存、不改数据、不碰消息，除非明确在做发送实验
2. **发送实验只打「文件传输助手」**，绝不拿真好友做试验
3. **每次注入前后跑 `health.py`**：确认微信没崩、没掉线
4. **绝不用别人的成品注入工具/hook**
5. **一旦微信出现异常（崩溃/强制重登）→ 立即停手**，把 hook 全部撤掉，回到只读+草稿
6. 记住 **B 的独有风险**：agent 和聊天数据同进程，脚本有 bug 可能损坏正在写的库

> ✅ 现在本仓的 `.venv` 已装齐（`frida` + `cryptography` + `zstandard` + 发送器依赖），
> 直接用 `<项目目录> 即可（旧项目已于 2026-09-24 删除）。
