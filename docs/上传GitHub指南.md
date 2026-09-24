# 上传 GitHub 指南

> 这个项目里全是隐私（**聊天记录、API key、好友手机号、微信解密密钥**）。
> 直接 `git push` = 把主人的全部聊天记录公开发布。**必须按下面的顺序来。**

---

## 第 0 步：先生成「干净副本」（最重要的一步）

```powershell
cd <项目目录>
& '.\.venv\Scripts\python.exe' tools\make_public.py
```

它会：
1. 只挑**代码和文档**（27 个文件，约 180 KB）
2. **剔除**全部数据：`corpus/turns`（真实对话）、`plain/snapshot`（解密库）、`keys/`、
   `state/`（好友名单）、`logs/`（发出去的原话）、`config.json`、`secrets.json`
3. **自动脱敏**：把代码/文档里的 `wxid_xxx`、`<微信数据目录> 换成占位符
4. **敏感串扫描**：扫 API key 形态 / 手机号 / wxid / 本机路径 —— **不干净会拒绝并报错**

输出目录：`<工作目录>

> ⚠️ **永远不要**在 `<项目目录> 里直接 `git init`。
> 那个目录里有解密后的完整微信数据库。

---

## 第 1 步：上传前自检（三分钟，别省）

```powershell
cd <工作目录>

git init -b main
git add -A

# ① 看看到底会提交什么（应该只有 .py / .md / .cmd / .json 示例）
git diff --cached --stat

# ② 再搜一遍敏感词（双保险）
git grep -nE "wxid_[A-Za-z0-9]{10,}|1[3-9][0-9]{9}|sk-[A-Za-z0-9]{20,}" -- . 

# ③ 确认 .gitignore 生效（不该出现任何 .db / secrets.json）
git status --short --ignored | Select-String "db$|secrets.json"
```

**②和③应该都没有输出。有输出就停下来，告诉人家（AI 助手）一起看。**

---

## 第 2 步：本地提交

```powershell
git commit -m "init: 微信 AI 分身（人设蒸馏 + 记忆库 + 红线过滤）"
```

---

## 第 3 步：在 GitHub 建仓库

1. 打开 https://github.com/new
2. Repository name：`wechat-persona-agent`（或你喜欢的）
3. **Public**（想分享就选公开）
4. ⚠️ **不要**勾 "Add a README file" / ".gitignore" / "license"（本地已经有了，勾了会冲突）
5. Create repository

---

## 第 4 步：推上去

```powershell
git remote add origin https://github.com/<你的用户名>/wechat-persona-agent.git
git push -u origin main
```

首次 push 会要求登录。推荐用 **Personal Access Token**（GitHub 已不支持密码）：
Settings → Developer settings → Personal access tokens → Fine-grained token → 勾 `repo` 权限。

---

## 第 5 步：完善仓库

| 建议 | 说明 |
|---|---|
| **加 LICENSE** | 想让人自由用 → MIT；想免责 → 加一段免责声明（模板见下） |
| **加仓库描述** | `读取自己的微信聊天记录，蒸馏人设与记忆，用大模型以本人身份回复好友` |
| **加 Topics** | `wechat` `llm` `persona` `sqlcipher` `automation` |
| **别开 Actions** | 没什么可跑的 |

**建议的免责声明**（放 README 末尾或 LICENSE 里）：

```
本项目仅供个人学习研究。客户端自动化与数据库读取可能违反微信服务条款，
账号存在风险；以本人身份对外发言的社交后果由使用者自行承担。
禁止用于冒充他人、读取他人聊天记录等用途。
```

---

## 🚨 万一已经传上去了才发现泄露

**顺序不能错**：

1. **先去作废密钥**（DeepSeek 平台删掉那个 API key）—— 这是唯一真正止损的动作，
   因为 git 历史删了也可能已被爬走
2. 删文件 + 提交：`git rm --cached secrets.json && git commit -m "remove secret"`
   → **注意：历史里还在**，只是最新版没了
3. 彻底清历史（会改写所有 commit id）：
   ```powershell
   pip install git-filter-repo
   git filter-repo --path secrets.json --path plain/ --path corpus/turns/ --invert-paths
   git push --force
   ```
4. 如果仓库是 Public 且已经放了一会儿 —— 就当密钥已泄露处理，**换 key**。

---

## 一句话总结

```
① python tools/make_public.py   ← 生成干净副本（自动脱敏 + 扫描）
② cd 副本 && git init && git add -A
③ git diff --cached --stat      ← 亲眼看一遍
④ 建仓库 → push
```

**永远不要在 `<项目目录> 里 git init。**
