# -*- coding: utf-8 -*-
"""
AI 每日新闻推送 v2（GitHub Actions 云端定时版）
================================================
流程：抓取国内外稳定新闻源 → 严格按「昨天 0:00-24:00（北京时间）」过滤
      → 去重（原始源优先）→ DeepSeek 一次批量分类+简短总结
      → 生成移动端 HTML（目录+详情两级结构，适配手机）
      → QQ 邮箱发送 HTML 附件（失败自动重试 3 次）
      → 仍失败则 PushPlus 推送微信消息兜底。

运行方式（GitHub Actions 已配置好，无需本地运行）：
    python main.py            # 完整流程
    python main.py --selftest # 本地自检：用模拟数据生成 HTML，不联网不发送

环境变量（在 GitHub 仓库 Secrets 中配置，不写死在代码里）：
    MAIL_TO           必填  收件邮箱（你的 QQ 邮箱）
    SMTP_USER         必填  发信 QQ 邮箱
    SMTP_AUTH_CODE    必填  QQ 邮箱 SMTP 授权码（16 位）
    PUSHPLUS_TOKEN    必填  PushPlus Token（仅邮件失败时兜底用）
    DEEPSEEK_API_KEY  必填  用于批量分类+总结（缺失时自动降级为规则分类+摘要截断）
    GITHUB_TOKEN      可选  GitHub Actions 自动提供，用于提高 GitHub API 调用限额

依赖：requests、feedparser（daily.yml 中已自动安装）
"""

import os
import sys
import re
import json
import time
import html as html_mod
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formataddr
from datetime import datetime, timezone, timedelta
from argparse import ArgumentParser

try:
    import requests
except ImportError:  # 本地无依赖时降级；云端 Actions 已安装，不影响正式运行
    requests = None

try:
    import feedparser
except ImportError:  # 依赖缺失时降级提示，避免启动即崩
    feedparser = None

# ================= 配置区 =================
MAIL_TO = os.environ.get("MAIL_TO", "").strip()
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_AUTH_CODE = os.environ.get("SMTP_AUTH_CODE", "").strip()
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465  # SSL
PUSHPLUS_URL = "https://www.pushplus.plus/send"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

BEIJING_TZ = timezone(timedelta(hours=8))  # 北京 = UTC + 8
VERSION = "2.2"          # 版本号：日志开头会打印，用于确认上传的是最新版
MAX_PER_CATEGORY = 4    # 每类最多条数
MAX_TOTAL = 16          # 每天最多总条数

# 新闻源：(显示名, 类型, 地址, 优先级)  类型: rss / hn / github
# 优先级：数字越小越接近原始发布源（去重时优先保留）
# 说明：机器之心/新智元无公开 RSS 且反爬严格，故不收录；Twitter/X 无免费 API，故不收录
SOURCES = [
    ("GitHub",      "github", "https://api.github.com/search/repositories", 1),
    ("YouTube",     "rss",    "https://www.youtube.com/feeds/videos.xml?channel_id=UC_x5XG1OV2P6uZZ5FSM9Ttw", 2),   # Google Developers（实测可用）
    ("量子位",       "rss",    "https://www.qbitai.com/feed", 3),
    ("36氪",        "rss",    "https://36kr.com/feed", 3),
    ("The Verge",   "rss",    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", 4),
    ("Hacker News", "hn",     "https://hn.algolia.com/api/v1/search_by_date", 5),
]

USER_AGENT = "Mozilla/5.0 (compatible; AI-Daily-News/2.0; +https://github.com/)"
REQ_TIMEOUT = 20  # 秒

# 分类体系（4 类，纯技术向；财经/法律/融资等明确不收）
CATEGORIES = ["模型发布", "技术开源", "产品应用", "公司动态"]

# 36氪是全站 feed，仅保留标题/摘要含 AI 关键词的条目
AI_KEYWORDS = [
    "ai", "人工智能", "大模型", "机器学习", "深度学习", "神经网络",
    "openai", "chatgpt", "gpt", "gemini", "claude", "llama", "deepseek",
    "qwen", "kimi", "文心", "通义", "豆包", "智谱", "讯飞",
    "copilot", "agent", "智能体", "机器人", "自动驾驶", "hugging face",
    "vllm", "langchain", "模型", "算法",
]

# 降级分类：强关键词优先（命中直接定类），再按分数兜底
STRONG_KEYWORDS = {
    "技术开源": ["开源", "github", "vllm", "langchain", "hugging face", "仓库", "代码库", "框架"],
    "模型发布": ["新模型", "发布.*模型", "评测", "mmlu", "benchmark", "得分", "推理模型"],
    "产品应用": ["集成", "上线", "推出", "新功能", "侧边栏", "app", "功能"],
    "公司动态": ["路线图", "宣布", "收购", "战略", "财报", "计划"],
}

# 降级分类用的关键词规则（DeepSeek 不可用时的兜底）
RULE_KEYWORDS = {
    "模型发布": ["模型", "llm", "gpt", "claude", "gemini", "llama", "mistral",
                 "qwen", "deepseek", "发布.*(版本|模型|v[0-9])", "评测", "得分",
                 "mmlu", "swe-bench", "benchmark", "分数"],
    "技术开源": ["开源", "github", "代码库", "仓库", "框架", "工具链", "vllm",
                 "langchain", "hugging face", "发布.*(工具|框架|库)", "open source"],
    "产品应用": ["产品", "上线", "新功能", "功能", "应用", "集成", "copilot",
                 "app", "桌面", "手机", "推出", "feature"],
    "公司动态": ["公司", "宣布", "计划", "路线图", "收购", "合作", "战略",
                 "openai", "meta", "google", "nvidia", "英伟达", "微软", "年报"],
}


# ================= 工具函数 =================
def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def fmt(dt, pattern: str = "%Y-%m-%d %H:%M") -> str:
    if dt is None:  # 字段缺失保护
        return "—"
    return dt.astimezone(BEIJING_TZ).strftime(pattern)


def clean_text(text: str, limit: int = 200) -> str:
    """清洗文本：去 HTML 标签 / 反转义 / 压缩空白 / 截断"""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def normalize_title(title: str) -> str:
    """标题归一化，用于去重比较"""
    t = title.lower()
    t = re.sub(r"[^\w\u4e00-\u9fff]", "", t)  # 只留字母数字中文
    return t[:40]


def yesterday_window():
    """返回昨天 0:00-24:00（北京时间）的时间窗口 [start, end]"""
    today = now_beijing().date()
    y = today - timedelta(days=1)
    start = datetime(y.year, y.month, y.day, 0, 0, 0, tzinfo=BEIJING_TZ)
    end = datetime(y.year, y.month, y.day, 23, 59, 59, 999999, tzinfo=BEIJING_TZ)
    return start, end, y


# ================= 各源抓取 =================
def fetch_rss(source, start, end):
    """通用 RSS/Atom 抓取（feedparser 同时支持 RSS 2.0 与 Atom）"""
    name, _, url, prio = source
    if feedparser is None:
        print("[skip] feedparser 未安装，跳过 RSS 源", name)
        return []
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQ_TIMEOUT)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    items = []
    for entry in feed.entries:
        pub = entry.get("published_parsed") or entry.get("updated_parsed")
        if pub is None:
            continue
        pub_dt = datetime(*pub[:6], tzinfo=timezone.utc).astimezone(BEIJING_TZ)
        if not (start <= pub_dt <= end):
            continue
        title = clean_text(entry.get("title", ""), 120)
        if not title:
            continue
        # 36氪全站 feed 过滤：只保留 AI 相关条目
        if name == "36氪":
            blob = (title + " " + clean_text(entry.get("summary", ""), 300)).lower()
            if not any(k in blob for k in AI_KEYWORDS):
                continue
        link = entry.get("link", "")
        summary = clean_text(entry.get("summary", ""), 300)
        items.append({"source": name, "title": title, "url": link,
                      "summary": summary, "pub_dt": pub_dt, "prio": prio,
                      "site": url.split("/")[2] if url else ""})
    return items


def fetch_hn(source, start, end):
    """Hacker News 按时间范围搜索 AI 相关 story（按赞数排序）"""
    name, _, url, prio = source
    start_ts = int(start.astimezone(timezone.utc).timestamp())
    end_ts = int(end.astimezone(timezone.utc).timestamp())
    resp = requests.get(url, params={
        "query": "AI",
        "tags": "story",
        "hitsPerPage": 40,
        "numericFilters": "created_at_i>%d,created_at_i<%d" % (start_ts, end_ts),
    }, headers={"User-Agent": USER_AGENT}, timeout=REQ_TIMEOUT)
    resp.raise_for_status()
    hits = resp.json().get("hits", [])
    items = []
    for h in hits:
        title = clean_text(h.get("title", ""), 120)
        if not title:
            continue
        ts = h.get("created_at_i", 0)
        pub_dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(BEIJING_TZ)
        if not (start <= pub_dt <= end):
            continue
        link = h.get("url") or "https://news.ycombinator.com/item?id=%s" % h.get("objectID", "")
        meta = []
        if h.get("points"):
            meta.append("%d 赞" % h["points"])
        if h.get("num_comments"):
            meta.append("%d 评论" % h["num_comments"])
        summary = ("HN 热帖 · " + " · ".join(meta)) if meta else "Hacker News 热帖"
        items.append({"source": name, "title": title, "url": link,
                      "summary": summary, "pub_dt": pub_dt, "prio": prio,
                      "site": "news.ycombinator.com"})
    return items


def fetch_github(source, start, end):
    """GitHub Search API：昨天创建/更新的 AI 相关仓库，按 star 排序"""
    name, _, url, prio = source
    start_utc = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc = end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = "Bearer " + GITHUB_TOKEN
    resp = requests.get(url, params={
        "q": 'ai created:>=%s created:<=%s' % (start_utc, end_utc),
        "sort": "stars",
        "order": "desc",
        "per_page": 20,
    }, headers=headers, timeout=REQ_TIMEOUT)
    resp.raise_for_status()
    repos = resp.json().get("items", [])
    items = []
    for r in repos:
        full_name = r.get("full_name", "")
        if not full_name:
            continue
        try:
            pub_dt = datetime.fromisoformat(r.get("created_at", "").replace("Z", "+00:00")).astimezone(BEIJING_TZ)
        except ValueError:
            continue
        if not (start <= pub_dt <= end):
            continue
        desc = clean_text(r.get("description", ""), 300)
        summary = "GitHub 新项目 · ★ %d 星" % r.get("stargazers_count", 0)
        if desc:
            summary += " · " + desc
        items.append({"source": name, "title": clean_text(full_name, 120),
                      "url": r.get("html_url", "https://github.com"), "summary": summary,
                      "pub_dt": pub_dt, "prio": prio, "site": "github.com"})
    return items


def fetch_all():
    """遍历所有源，单源失败自动跳过（打印原因不中断）"""
    start, end, _ = yesterday_window()
    all_items = []
    for source in SOURCES:
        try:
            if source[1] == "rss":
                items = fetch_rss(source, start, end)
            elif source[1] == "hn":
                items = fetch_hn(source, start, end)
            else:
                items = fetch_github(source, start, end)
            print("[ok] %s: 抓取到 %d 条" % (source[0], len(items)))
            all_items.extend(items)
        except Exception as e:
            print("[warn] %s 抓取失败: %s" % (source[0], e))
    return all_items


# ================= 去重（原始源优先） =================
def dedupe(items):
    """按标题归一化去重；保留优先级最小（最接近原始源）的条目"""
    seen, result = {}, []
    for it in sorted(items, key=lambda x: x["prio"]):
        key = normalize_title(it["title"])
        if key in seen:
            continue
        seen[key] = it
        result.append(it)
    return result


# ================= 分类 + 总结（DeepSeek 一次批量调用） =================
def classify_with_rules(title, summary):
    """降级方案：强关键词命中直接定类，否则按关键词分数取最高"""
    blob = (title + " " + summary).lower()
    for cat, kws in STRONG_KEYWORDS.items():
        if any(re.search(k, blob) for k in kws):
            return cat
    scores = {}
    for cat, kws in RULE_KEYWORDS.items():
        s = 0
        for kw in kws:
            if re.search(kw, blob):
                s += 2 if kw in RULE_KEYWORDS[cat][:4] else 1
        scores[cat] = s
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "产品应用"


def _pack_item(it, category, summary):
    """把分类+总结结果与原始字段（时间/来源/链接）合并，供 HTML 使用"""
    return {"title": it["title"], "category": category, "summary": summary,
            "pub_dt": it.get("pub_dt"), "source": it.get("source"),
            "url": it.get("url"), "site": it.get("site")}


def ai_classify_summarize(items):
    """DeepSeek 一次批量：为所有新闻分类 + 生成 2-3 句总结。返回 [{title,category,summary}]。
    失败/未配置时降级：规则分类 + 原文摘要截断。"""
    results = []
    if DEEPSEEK_API_KEY and items:
        listing = "\n".join(
            "%d. 标题：%s | 摘要：%s | 来源：%s" % (i + 1, it["title"], it["summary"], it["source"])
            for i, it in enumerate(items))
        prompt = (
            "你是 AI 科技新闻编辑。以下是昨天发布的 %d 条 AI 新闻，请逐条完成两件事：\n"
            "1. 分类（category），只能从这四个中选一个：\n"
            "   - 模型发布：新模型/新版本发布、能力提升、评测分数\n"
            "   - 技术开源：开源项目、代码库、框架工具\n"
            "   - 产品应用：AI 新产品/新功能落地、实际使用\n"
            "   - 公司动态：公司技术路线、发布计划、战略决策\n"
            "2. 用 2-3 句中文简短总结（summary），客观陈述事实，不要发表观点、不要加评价。\n"
            "严格按 JSON 数组返回，不要输出其他任何文字：\n"
            '[{"title":"新闻标题(原文)","category":"类别","summary":"总结"}] \n\n'
            "新闻列表：\n" + listing
        ) % len(items)
        for attempt in (1, 2):
            try:
                resp = requests.post(DEEPSEEK_URL, headers={
                    "Authorization": "Bearer " + DEEPSEEK_API_KEY,
                    "Content-Type": "application/json",
                }, json={
                    "model": "deepseek-v4-flash",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 4000,
                    "temperature": 0.3,
                    "response_format": {"type": "json_object"},
                }, timeout=90)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"].strip()
                content = re.sub(r"^```(json)?|```$", "", content, flags=re.M).strip()
                data = json.loads(content)
                if not isinstance(data, list):
                    data = data.get("news", data.get("data", []))
                # 与新闻标题做模糊匹配，保证顺序与数量一致
                for it in items:
                    match = next((d for d in data
                                  if normalize_title(str(d.get("title", "")))
                                  == normalize_title(it["title"])), None)
                    if match:
                        cat = match.get("category", "").strip()
                        cat = cat if cat in CATEGORIES else classify_with_rules(it["title"], it["summary"])
                        results.append(_pack_item(it, cat, clean_text(match.get("summary", ""), 300)))
                    else:
                        results.append(_pack_item(it, classify_with_rules(it["title"], it["summary"]),
                                                  clean_text(it["summary"], 120) or it["title"]))
                if len(results) == len(items):
                    print("[ok] DeepSeek 批量分类+总结成功（%d 条）" % len(results))
                    return results
                results = []
                print("[warn] DeepSeek 返回数量不匹配，重试（第 %d 次）" % attempt)
            except Exception as e:
                results = []
                detail = ""
                try:
                    detail = " | HTTP %s | body: %s" % (resp.status_code, resp.text[:200])
                except Exception:
                    pass
                print("[warn] DeepSeek 调用失败（第 %d 次）: %s%s" % (attempt, e, detail))
                time.sleep(3)
    # 降级：规则分类 + 摘要截断
    print("[warn] 使用降级方案：规则分类 + 原文摘要截断")
    for it in items:
        results.append(_pack_item(it, classify_with_rules(it["title"], it["summary"]),
                                  clean_text(it["summary"], 120) or it["title"]))
    return results


# ================= HTML 生成（目录+详情两级结构，移动端适配） =================
def build_html(results, report_date):
    """把分类后的新闻生成单文件 HTML：顶部要点+分类按钮 / 目录区 / 详情区"""
    # 按分类分组，每类限流
    grouped = {c: [] for c in CATEGORIES}
    for r in results:
        if r["category"] in grouped and len(grouped[r["category"]]) < MAX_PER_CATEGORY:
            grouped[r["category"]].append(r)
    ordered = [(c, grouped[c]) for c in CATEGORIES if grouped[c]]
    total = sum(len(v) for _, v in ordered)

    # 顶部一句话要点
    brief_parts = "、".join("%s %d 条" % (CATEGORY_EMOJI[c], len(v)) for c, v in ordered)
    brief = "今日共 %d 条精选：%s。" % (total, brief_parts)

    esc = html_mod.escape

    # 分类按钮
    cat_btns = "".join(
        '<a class="cat-btn" href="#toc-%s">%s %s <span class="cnt">%d</span></a>'
        % (c, CATEGORY_EMOJI[c], c, len(v)) for c, v in ordered)

    # 目录区
    toc_blocks = []
    news_id = 0
    for c, v in ordered:
        rows = []
        for r in v:
            news_id += 1
            r["_id"] = news_id
            rows.append(
                '<a class="toc-item" href="#news-%d">'
                '<span class="toc-no">%d</span>'
                '<span class="toc-body">'
                '<span class="toc-title">%s</span>'
                '<span class="toc-sum">%s</span>'
                '</span><span class="toc-arrow">›</span></a>'
                % (news_id, news_id, esc(r["title"]), esc(r["summary"][:40])))
        toc_blocks.append(
            '<div class="toc-group" id="toc-%s">'
            '<div class="toc-group-title"><span class="emoji">%s</span> %s <span class="cnt">%d 条</span></div>'
            '%s</div>' % (c, CATEGORY_EMOJI[c], c, len(v), "".join(rows)))

    # 详情区
    detail_sections = []
    for c, v in ordered:
        cards = []
        for r in v:
            meta = ('<span class="src">%s</span>'
                    '<span>🕐 发布时间：%s</span>'
                    '<span>🌐 网站：%s</span>'
                    % (esc(r.get("source", "")), fmt(r.get("pub_dt")),
                       esc(r.get("site", "") or "—")))
            cards.append(
                '<article class="news-card" id="news-%d">'
                '<a class="back-toc" href="#toc">↑ 返回目录</a>'
                '<h3><a href="%s">%s</a></h3>'
                '<div class="news-meta">%s</div>'
                '<p class="news-detail">%s</p>'
                '<div class="news-links"><a href="%s">🔗 原文链接</a></div>'
                '</article>'
                % (r["_id"], esc(r.get("url", "#")), esc(r["title"]),
                   meta, esc(r["summary"]), esc(r.get("url", "#"))))
        detail_sections.append(
            '<div class="detail-section" id="detail-%s">'
            '<div class="detail-sec-title"><span class="emoji">%s</span> %s</div>'
            '<div class="detail-sec-desc">%s</div>%s</div>'
            % (c, CATEGORY_EMOJI[c], c, CATEGORY_DESC[c], "".join(cards)))

    date_cn = "%d年%d月%d日" % (report_date.year, report_date.month, report_date.day)
    weekday_cn = "一二三四五六日"[report_date.weekday()]

    return (HTML_TEMPLATE
            % {"date": date_cn, "weekday": weekday_cn, "brief": brief,
               "cat_btns": cat_btns, "toc_blocks": "".join(toc_blocks),
               "detail_sections": "".join(detail_sections),
               "total": total, "yesterday": "%d-%02d-%02d" % (report_date.year, report_date.month, report_date.day)})


CATEGORY_EMOJI = {"模型发布": "🧠", "技术开源": "🔓", "产品应用": "🛠️", "公司动态": "🏢"}
CATEGORY_DESC = {
    "模型发布": "新模型 / 新版本 / 评测分数",
    "技术开源": "开源项目 / 代码库 / 工具链",
    "产品应用": "AI 做到的事 / 新功能落地",
    "公司动态": "大厂发布 / 技术路线 / 关键决策",
}

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>AI 日报 · %(date)s</title>
<style>
:root{--bg:#f2f4f8;--card:#fff;--primary:#2563eb;--primary-deep:#1e40af;--text:#1f2937;--text-sub:#6b7280;--text-light:#9ca3af;--line:#e5e7eb;--radius:14px;}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent;}
html{scroll-behavior:smooth;}
body{font-family:-apple-system,BlinkMacSystemFont,'MiSans','HarmonyOS Sans','PingFang SC','Microsoft YaHei',sans-serif;background:var(--bg);color:var(--text);max-width:480px;margin:0 auto;font-size:15px;line-height:1.6;padding-bottom:64px;}
a{color:var(--primary);text-decoration:none;}
.hero{background:linear-gradient(135deg,#1e3a8a 0%%,#2563eb 55%%,#3b82f6 100%%);color:#fff;padding:28px 20px 20px;border-radius:0 0 22px 22px;}
.hero .date{font-size:12px;opacity:.85;letter-spacing:1px;}
.hero h1{font-size:22px;font-weight:700;margin:4px 0 10px;}
.hero .brief{font-size:13px;opacity:.95;line-height:1.5;background:rgba(255,255,255,.14);border-radius:10px;padding:8px 12px;}
.cat-bar{position:sticky;top:0;z-index:50;display:flex;gap:8px;overflow-x:auto;padding:10px 16px;background:rgba(242,244,248,.92);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);scrollbar-width:none;}
.cat-bar::-webkit-scrollbar{display:none;}
.cat-btn{flex:0 0 auto;display:inline-flex;align-items:center;gap:4px;padding:7px 14px;border-radius:999px;background:#fff;border:1px solid var(--line);color:var(--text-sub);font-size:13px;font-weight:500;white-space:nowrap;transition:all .15s;}
.cat-btn:active{transform:scale(.95);}
.cat-btn .cnt{background:#eef2ff;color:var(--primary);border-radius:999px;font-size:11px;padding:0 6px;line-height:18px;}
.toc{padding:18px 16px 4px;scroll-margin-top:56px;}
.toc-head{display:flex;align-items:baseline;gap:8px;margin-bottom:12px;}
.toc-head .title{font-size:17px;font-weight:700;}
.toc-head .hint{font-size:11px;color:var(--text-light);}
.toc-group{margin-bottom:16px;}
.toc-group-title{display:flex;align-items:center;gap:6px;font-size:14px;font-weight:700;margin:14px 0 8px;scroll-margin-top:60px;}
.toc-group-title .emoji{font-size:15px;}
.toc-group-title .cnt{font-size:11px;font-weight:400;color:var(--text-light);}
.toc-item{display:flex;align-items:center;gap:10px;background:var(--card);border-radius:12px;padding:11px 14px;margin-bottom:8px;box-shadow:0 1px 2px rgba(17,24,39,.05);}
.toc-item:active{background:#f5f8ff;}
.toc-no{flex:0 0 auto;width:24px;height:24px;border-radius:7px;background:#eef2ff;color:var(--primary);font-size:12px;font-weight:700;display:flex;align-items:center;justify-content:center;}
.toc-body{flex:1;min-width:0;}
.toc-title{display:block;font-size:14px;font-weight:600;color:var(--text);line-height:1.4;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.toc-sum{display:block;font-size:12px;color:var(--text-light);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px;}
.toc-arrow{color:var(--text-light);font-size:13px;flex:0 0 auto;}
.detail{padding:10px 16px 6px;}
.detail-section{scroll-margin-top:60px;margin-bottom:8px;}
.detail-sec-title{display:flex;align-items:center;gap:8px;font-size:16px;font-weight:700;padding:18px 2px 4px;}
.detail-sec-title .emoji{font-size:17px;}
.detail-sec-desc{font-size:12px;color:var(--text-light);padding:0 2px 10px;}
.news-card{background:var(--card);border-radius:var(--radius);padding:16px;margin-bottom:14px;box-shadow:0 1px 3px rgba(17,24,39,.06);border-left:3px solid var(--primary);scroll-margin-top:64px;}
.news-card .back-toc{display:inline-block;font-size:11px;color:var(--text-light);margin-bottom:8px;}
.news-card h3{font-size:16px;font-weight:700;line-height:1.45;}
.news-card h3 a{color:var(--text);}
.news-meta{display:flex;flex-wrap:wrap;align-items:center;gap:6px;font-size:12px;color:var(--text-light);margin:10px 0 8px;}
.news-meta .src{color:#fff;background:var(--primary);border-radius:5px;padding:2px 8px;font-size:11px;}
.news-detail{font-size:14px;color:var(--text-sub);line-height:1.75;}
.news-links{margin-top:12px;display:flex;flex-wrap:wrap;gap:8px;}
.news-links a{font-size:12px;color:var(--primary-deep);background:#eef2ff;border-radius:8px;padding:5px 11px;display:inline-flex;align-items:center;gap:3px;}
.foot{margin-top:20px;padding:16px 20px;text-align:center;font-size:11px;color:var(--text-light);}
.to-top{position:fixed;right:18px;bottom:24px;width:42px;height:42px;border-radius:50%%;background:var(--primary);color:#fff;display:flex;align-items:center;justify-content:center;font-size:18px;box-shadow:0 4px 12px rgba(37,99,235,.35);opacity:.92;}
</style>
</head>
<body>
<header class="hero">
<div class="date">%(date)s · 星期%(weekday)s · 昨日精选</div>
<h1>🤖 AI 日报</h1>
<div class="brief">📌 %(brief)s</div>
</header>
<nav class="cat-bar">%(cat_btns)s</nav>
<main>
<section class="toc" id="toc">
<div class="toc-head"><span class="title">📋 今日目录</span><span class="hint">点击新闻标题查看详细内容 ↓</span></div>
%(toc_blocks)s
</section>
<section class="detail">
%(detail_sections)s
</section>
</main>
<footer class="foot">AI 日报 · 自动抓取生成 · 仅供技术学习交流<br>共 %(total)d 条 · 数据范围：%(yesterday)s（北京时间）</footer>
<a class="to-top" href="#toc" title="回到目录">↑</a>
</body>
</html>
"""


# ================= 邮件发送（QQ SMTP SSL + HTML 附件） =================
def build_email_msg(html_content, subject, filename):
    """构造带 HTML 附件的邮件（不发送）"""
    msg = MIMEMultipart()
    # From 头：显示名与地址分离编码，确保 QQ SMTP 能解析出合法地址
    msg["From"] = formataddr((str(Header("AI 日报", "utf-8")), SMTP_USER))
    msg["To"] = MAIL_TO
    msg["Subject"] = Header(subject, "utf-8")
    body = MIMEText("今日 AI 日报已生成，请打开附件查看（适配手机）。\n如附件无法显示，请直接回复本邮件反馈。", "plain", "utf-8")
    msg.attach(body)
    part = MIMEText(html_content, "html", "utf-8")
    part.add_header("Content-Disposition", "attachment", filename=("utf-8", "", filename))
    msg.attach(part)
    return msg


def send_email(html_content, subject, filename, retries=3, interval=60):
    """发送带附件的邮件；失败重试 retries 次，间隔 interval 秒"""
    msg = build_email_msg(html_content, subject, filename)
    for attempt in range(1, retries + 1):
        try:
            smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
            smtp.login(SMTP_USER, SMTP_AUTH_CODE)
            smtp.sendmail(SMTP_USER, [MAIL_TO], msg.as_string())
            smtp.quit()
            print("[ok] 邮件已发送到 %s（第 %d 次尝试）" % (MAIL_TO, attempt))
            return True
        except Exception as e:
            print("[warn] 邮件发送失败（第 %d/%d 次）: %s" % (attempt, retries, e))
            if attempt < retries:
                print("  等待 %d 秒后重试……" % interval)
                time.sleep(interval)
    return False


# ================= PushPlus 兜底（邮件失败时） =================
def send_pushplus(title, html_content):
    """把 HTML 内容内嵌进微信消息推送（template=html）"""
    if not PUSHPLUS_TOKEN:
        print("[warn] 未配置 PUSHPLUS_TOKEN，兜底推送无法执行")
        return False
    payload = {"token": PUSHPLUS_TOKEN, "title": title, "content": html_content, "template": "html"}
    try:
        resp = requests.post(PUSHPLUS_URL, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        ok = result.get("code") == 200
        print("PushPlus 兜底: code=%s msg=%s" % (result.get("code"), result.get("msg")))
        return ok
    except Exception as e:
        print("[warn] PushPlus 兜底失败: %s" % e)
        return False


# ================= 自检模式（本地验证用，不联网不发送） =================
SAMPLE_ITEMS = [
    {"source": "DeepSeek 官方", "title": "DeepSeek 发布 DeepSeek-V4-R1 推理模型，MATH-500 准确率突破 94%",
     "url": "https://deepseek.com", "summary": "新一代推理模型主打长思维链，推理成本较 V3 下降 30%，API 已开放。",
     "pub_dt": datetime(2026, 6, 4, 22, 15, tzinfo=BEIJING_TZ), "prio": 1, "site": "deepseek.com"},
    {"source": "GitHub", "title": "vLLM 发布 0.9 版本：推理吞吐提升 2.3 倍",
     "url": "https://github.com/vllm-project/vllm", "summary": "引入连续批处理 2.0，正式支持 FP8 量化，star 突破 10 万。",
     "pub_dt": datetime(2026, 6, 4, 21, 0, tzinfo=BEIJING_TZ), "prio": 1, "site": "github.com"},
    {"source": "The Verge", "title": "Gemini 深度集成进 Chrome 侧边栏：划词即问",
     "url": "https://theverge.com", "summary": "任意网页可呼出 Gemini 侧边栏，支持划词解释、网页一键总结。",
     "pub_dt": datetime(2026, 6, 4, 10, 30, tzinfo=BEIJING_TZ), "prio": 4, "site": "theverge.com"},
    {"source": "量子位", "title": "OpenAI 披露 GPT-5.2 路线图：年底开放 API",
     "url": "https://qbitai.com", "summary": "三阶段发布计划，主打多模态推理 Agent，强化工具调用与记忆能力。",
     "pub_dt": datetime(2026, 6, 4, 23, 5, tzinfo=BEIJING_TZ), "prio": 3, "site": "qbitai.com"},
    {"source": "Hacker News", "title": "Hugging Face 开源 SmolLM3：可跑在手机上的 3B 边缘模型",
     "url": "https://news.ycombinator.com", "summary": "量化后仅 1.2GB，手机端推理 45 tokens/s。",
     "pub_dt": datetime(2026, 6, 4, 11, 45, tzinfo=BEIJING_TZ), "prio": 5, "site": "news.ycombinator.com"},
]


def run_selftest():
    """用模拟数据生成 HTML 并构造邮件，验证不联网也能跑通"""
    print("== 自检模式：模拟数据生成 HTML（不联网不发送）==")
    _, _, report_date = yesterday_window()
    results = [{"title": it["title"], "category": classify_with_rules(it["title"], it["summary"]),
                "summary": it["summary"], "pub_dt": it["pub_dt"],
                "source": it["source"], "url": it["url"], "site": it["site"]}
               for it in SAMPLE_ITEMS]
    html_content = build_html(results, report_date)
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "selftest.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print("[ok] 已生成 %s（%.1f KB）" % (out_path, len(html_content) / 1024))
    # 锚点完整性检查
    ids = set(re.findall(r'id="(news-\d+|toc-[a-z]+|toc)"', html_content))
    hrefs = set(re.findall(r'href="#(news-\d+|toc-[a-z]+|toc)"', html_content))
    missing = hrefs - ids
    print("[check] 锚点定义 %d 个，引用 %d 个，缺失: %s" % (len(ids), len(hrefs), missing or "无"))
    # 邮件构造检查（不发送）
    if SMTP_USER and MAIL_TO:
        msg = build_email_msg(html_content, "AI 日报 自检", "AI日报_自检.html")
        print("[check] 邮件消息构造成功，总大小 %.1f KB" % (len(msg.as_string()) / 1024))
    else:
        print("[check] 未配置 SMTP_USER/MAIL_TO，跳过邮件构造检查")
    return 0 if not missing else 1


# ================= 主流程 =================
def main():
    parser = ArgumentParser()
    parser.add_argument("--selftest", action="store_true", help="本地自检（模拟数据，不联网不发送）")
    args = parser.parse_args()

    if args.selftest:
        sys.exit(run_selftest())

    print("== 开始执行每日 AI 新闻任务（v%s） ==" % VERSION)
    print("时间（北京）：%s" % fmt(now_beijing()))

    if not (MAIL_TO and SMTP_USER and SMTP_AUTH_CODE):
        print("错误：MAIL_TO / SMTP_USER / SMTP_AUTH_CODE 未配置完整，请检查 GitHub Secrets")
        sys.exit(1)

    start, end, report_date = yesterday_window()
    print("数据范围：%s 00:00 ~ 23:59（北京时间）" % report_date)

    print("-- 抓取新闻源 --")
    items = dedupe(fetch_all())
    if not items:
        print("错误：所有新闻源均未取到内容（可能网络问题或源变更）")
        sys.exit(1)
    print("去重后共 %d 条" % len(items))

    print("-- DeepSeek 批量分类 + 总结 --")
    results = ai_classify_summarize(items)
    # 每类限流
    capped = []
    counts = {c: 0 for c in CATEGORIES}
    for r in results:
        if counts.get(r["category"], 99) >= MAX_PER_CATEGORY:
            continue
        counts[r["category"]] = counts.get(r["category"], 0) + 1
        capped.append(r)
        if len(capped) >= MAX_TOTAL:
            break
    results = capped
    print("最终 %d 条（%s）" % (len(results),
          "，".join("%s:%d" % (c, counts[c]) for c in CATEGORIES if counts[c])))

    print("-- 生成 HTML --")
    html_content = build_html(results, report_date)
    filename = "AI日报_%04d-%02d-%02d.html" % (report_date.year, report_date.month, report_date.day)
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, filename), "w", encoding="utf-8") as f:
        f.write(html_content)
    print("[ok] HTML 已生成：%s（%.1f KB）" % (filename, len(html_content) / 1024))

    print("-- 发送邮件（失败自动重试 3 次） --")
    subject = "AI 日报 %s" % report_date
    if send_email(html_content, subject, filename):
        print("== 完成：日报已发送到 QQ 邮箱 ==")
        sys.exit(0)

    print("-- 邮件失败，尝试 PushPlus 微信兜底 --")
    if send_pushplus(subject + "（邮件失败·微信兜底）", html_content):
        print("== 完成：已通过微信兜底送达 ==")
        sys.exit(0)
    print("== 失败：邮件与微信兜底均未成功 ==")
    sys.exit(1)


if __name__ == "__main__":
    main()
