# 🤖 AI 每日新闻推送（PushPlus 版）

每天北京时间 **08:00** 自动抓取 AI 新闻 → 排版成 **HTML** → 通过 **PushPlus 推送到你的微信**。
全程云端运行（GitHub Actions），电脑无需开机，成本 **¥0/月**。

```
ai-daily-news/
├── main.py                     # 抓取 + 排版 + 推送脚本（完整可运行）
└── .github/workflows/daily.yml # 定时任务配置
```

---

## 你需要做什么（照做即可，全程不用写代码）

### 第 1 步：注册 PushPlus 并拿到 Token（约 10 分钟）

1. 打开 https://www.pushplus.plus ，用**微信扫码登录**，首次需关注「pushplus 推送加」公众号（不关注收不到消息）
2. 按提示完成**实名认证**（2024-08-01 起未实名无法调用发送接口）
3. 进入「个人中心」，复制你的 **Token**（一长串字符）

### 第 2 步：创建 GitHub 仓库（约 5 分钟）

1. 打开 https://github.com ，右上角 **+** → **New repository**
2. 仓库名随意（如 `ai-daily-news`），可见性选 **Public**（Actions 免费分钟数无限制）或 **Private**（免费档每月 2000 分钟，本任务只用约 30 分钟，也够）
3. 点 **Create repository**

### 第 3 步：上传这两个文件（约 5 分钟）

在仓库页面点 **Add file** → **Upload files**，把本文件夹里的 `main.py` 和 `.github/workflows/daily.yml` 拖进去上传（保持目录结构：`.github/workflows/daily.yml`），点 **Commit changes**。

### 第 4 步：设置 Token（GitHub Secrets，约 2 分钟）

1. 打开直达链接（换成你的用户名和仓库名）：
   `https://github.com/你的用户名/你的仓库名/settings/secrets/actions`
2. 点绿色 **New repository secret**
3. Name 填：`PUSHPLUS_TOKEN`
4. Secret 粘贴第 1 步复制的 Token
5. 点 **Add secret**

### 第 5 步：手动测试一次（约 2 分钟）

1. 进入仓库 **Actions** 页，首次需点 **I understand my workflows, go ahead and enable them**
2. 左侧选「每日AI新闻推送」→ 右侧 **Run workflow** → **Run workflow**
3. 等 20~60 秒运行结束，日志绿色通过
4. **看微信是否收到「AI 每日简报」**——收到即整套打通，之后每天 08:00 自动推送

---

## 可选：加一段「AI 今日导读」（DeepSeek 生成，约 ¥0.2/月）

1. 打开 https://platform.deepseek.com 注册并充值（最低 ¥10，够用很久）
2. 创建 API Key
3. 按第 4 步再加一个 Secret：Name 填 `DEEPSEEK_API_KEY`，Secret 粘贴 API Key
4. 下次推送时，简报顶部会自动多出一段 3~4 句的今日导读

> 不配置此 Key 完全不影响运行（代码会自动跳过导读）。

## 想改推送时间？

编辑 `.github/workflows/daily.yml` 里的这行（GitHub 用 UTC，北京时间 = UTC + 8）：

| 想要的北京时间 | 填入的 cron |
|----------------|-------------|
| 07:00 | `0 23 * * *` |
| 08:00（默认） | `0 0 * * *` |
| 09:00 | `0 1 * * *` |
| 21:00 | `0 13 * * *` |

改完 Commit，下次按新时间执行。

---

## 常见问题

| 问题 | 解决 |
|------|------|
| 微信收不到 | ① 是否关注了「推送加」公众号；② Token 是否配对；③ 是否完成实名；④ 先手动 Run workflow 看日志是否报错 |
| Actions 运行失败（红色） | 点进失败记录看日志：常见是 `PUSHPLUS_TOKEN` 没设/设错、网络超时 |
| 日志显示 code=200 但没收到 | PushPlus 是异步发送，等 1~2 分钟；仍无则去 PushPlus 官网看推送日志 |
| 每天没触发 | 确认两个文件在默认分支（main）上、Actions 已启用 |
| 新闻内容为空 | 个别源失效不影响整体（代码已容错）；全部为空说明网络问题，看日志 |

## 新闻来源（已验证可用）

- **量子位**（中文 AI 媒体）· RSS
- **The Verge AI**（国际科技媒体）· RSS
- **Hacker News**（全球极客社区 AI 热帖）· API
- **arXiv cs.AI**（AI 论文）· API

> 说明：机器之心已无公开 RSS，故未收录。想换/加源，改 `main.py` 顶部的 `SOURCES` 列表即可。
