# -*- coding: utf-8 -*-
"""
AI 每日新闻推送（GitHub Actions 云端定时版）
================================================
流程：抓取多个公开新闻源 → 按「标题 / 摘要 / 链接」排版成 HTML → 通过 PushPlus 推送到微信。

运行方式（GitHub Actions 已配置好，无需本地运行）：
    python main.py

环境变量（在 GitHub 仓库 Secrets 中配置，不写死在代码里）：
    PUSHPLUS_TOKEN    必填  PushPlus 用户 Token（实名后可获取）
    DEEPSEEK_API_KEY  可选  配置后自动生成一段「今日导读」（DeepSeek v4-flash），未配置则跳过

依赖：requests、feedparser（daily.yml 中已自动安装）
"""

import os
import sys
import re
import html as html_mod
from datetime import datetime, timezone, timedelta

import requests

try:
    import feedparser
except ImportError:  # 依赖缺失时降级提示，避免启动即崩
    feedparser = None

# ================= 配置区 =================
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()  # 可选

PUSHPLUS_URL = "https://www.pushplus.plus/send"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

BEIJING_TZ = timezone(timedelta(hours=8))  # 北京 = UTC + 8
MAX_ITEMS = 8          # 最多推送条数
HOURS_BACK = 30        # 只抓最近 30 小时内的内容（覆盖每天一次）

# 新闻源：(显示名, 类型, 地址)  类型: rss / hn / arxiv
# 说明：机器之心已无公开 RSS，故未收录；每个源独立容错，单源失败不影响其他源
SOURCES = [
    ("量子位",      "rss",   "https://www.qbitai.com/feed"),
    ("The Verge AI", "rss",  "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("Hacker News", "hn",    "https://hn.algolia.com/api/v1/search_by_date"),
    ("arXiv cs.AI", "arxiv", "https://export.arxiv.org/api/query"),
]

USER_AGENT = "Mozilla/5.0 (compatible; AI-Daily-News/1.0; +https://github.com/)"
REQ_TIMEOUT = 20  # 秒


# ================= 工具函数 =================
def now_beijing() -> str:
    """返回北京时间字符串 YYYY-MM-DD HH:MM"""
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")


def clean_text(text: str, limit: int = 120) -> str:
    """清洗文本：去掉 HTML 标签 / 多余空白 / 截断"""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)      # 去标签
    text = html_mod.unescape(text)            # 反转义 &amp; 等
    text = re.sub(r"\s+", " ", text).strip()  # 压缩空白
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


# ================= 各源抓取 =================
def fetch_rss(source, since_dt):
    """通用 RSS/Atom 抓取（feedparser 同时支持 RSS 2.0 与 Atom）"""
    if feedparser is None:
        print("[skip] feedparser 未安装，跳过 RSS 源", source[0])
        return []
    resp = requests.get(source[2], headers={"User-Agent": USER_AGENT}, timeout=REQ_TIMEOUT)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    items = []
    for entry in feed.entries:
        # 取发布时间（RSS: published/published_parsed；Atom: published/updated）
        pub = entry.get("published_parsed") or entry.get("updated_parsed")
        if pub is None:
            continue
        pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
        if pub_dt < since_dt:
            continue
        link = entry.get("link", "")
        summary = clean_text(entry.get("summary", ""))
        items.append({"source": source[0], "title": clean_text(entry.get("title", ""), 90),
                      "url": link, "summary": summary})
    return items


def fetch_hn(source, since_ts):
    """Hacker News 按时间倒序搜索 AI 相关热门 story"""
    resp = requests.get(source[2], params={
        "query": "AI",
        "tags": "story",
        "hitsPerPage": 30,
        "numericFilters": "created_at_i>%d" % since_ts,
    }, headers={"User-Agent": USER_AGENT}, timeout=REQ_TIMEOUT)
    resp.raise_for_status()
    hits = resp.json().get("hits", [])
    items = []
    for h in hits:
        title = clean_text(h.get("title", ""), 90)
        if not title:
            continue
        link = h.get("url") or "https://news.ycombinator.com/item?id=%s" % h.get("objectID", "")
        meta = []
        if h.get("points"):
            meta.append("%d 赞" % h["points"])
        if h.get("num_comments"):
            meta.append("%d 评论" % h["num_comments"])
        summary = ("HN 热帖 · " + " · ".join(meta)) if meta else "Hacker News 热帖"
        items.append({"source": source[0], "title": title, "url": link, "summary": summary})
    return items


def fetch_arxiv(source, since_dt):
    """arXiv cs.AI 最新论文（按提交时间倒序）"""
    resp = requests.get(source[2], params={
        "search_query": "cat:cs.AI",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": 20,
    }, headers={"User-Agent": USER_AGENT}, timeout=REQ_TIMEOUT)
    resp.raise_for_status()
    if feedparser is None:
        print("[skip] feedparser 未安装，跳过 arXiv 源")
        return []
    feed = feedparser.parse(resp.content)
    items = []
    for entry in feed.entries:
        pub = entry.get("published_parsed")
        if pub is None:
            continue
        pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
        if pub_dt < since_dt:
            continue
        # Atom 里 alternate 链接才是正文页
        link = ""
        for l in entry.get("links", []):
            if l.get("rel") == "alternate":
                link = l.get("href", "")
                break
        if not link:
            link = entry.get("link", "")
        items.append({"source": source[0], "title": clean_text(entry.get("title", ""), 90),
                      "url": link, "summary": clean_text(entry.get("summary", ""))})
    return items


def fetch_all():
    """遍历所有源，单源失败自动跳过（打印原因不中断）"""
    since_dt = datetime.now(timezone.utc) - timedelta(hours=HOURS_BACK)
    since_ts = int((datetime.now(timezone.utc) - timedelta(hours=HOURS_BACK)).timestamp())
    all_items = []
    for source in SOURCES:
        try:
            if source[1] == "rss":
                items = fetch_rss(source, since_dt)
            elif source[1] == "hn":
                items = fetch_hn(source, since_ts)
            else:
                items = fetch_arxiv(source, since_dt)
            print("[ok] %s: 抓取到 %d 条" % (source[0], len(items)))
            all_items.extend(items)
        except Exception as e:
            print("[warn] %s 抓取失败: %s" % (source[0], e))
    return all_items


def dedupe(items):
    """按标题去重（保留第一次出现）"""
    seen, result = set(), []
    for it in items:
        key = it["title"].lower()[:40]
        if key in seen:
            continue
        seen.add(key)
        result.append(it)
    return result


# ================= 可选：DeepSeek 今日导读 =================
def ai_guide(items):
    """用 DeepSeek v4-flash 生成 3~4 句今日导读；失败/未配置时返回 None"""
    if not DEEPSEEK_API_KEY:
        return None
    titles = "\n".join("- %s（%s）" % (it["title"], it["source"]) for it in items[:MAX_ITEMS])
    prompt = (
        "你是科技新闻编辑。下面是一批今天的 AI 相关新闻标题，请用中文写一段 3~4 句的"
        "「今日导读」：点出最值得关注的方向，语言精炼口语化，不要列点，不要用 Markdown。\n\n"
        + titles
    )
    try:
        resp = requests.post(DEEPSEEK_URL, headers={
            "Authorization": "Bearer " + DEEPSEEK_API_KEY,
            "Content-Type": "application/json",
        }, json={
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 300,
            "temperature": 0.7,
        }, timeout=40)
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        return text or None
    except Exception as e:
        print("[warn] AI 导读生成失败（将跳过）: %s" % e)
        return None


# ================= HTML 排版 =================
def build_html(items, guide):
    """把新闻列表排版成适合微信阅读的 HTML（PushPlus template=html）"""
    now = now_beijing()
    rows = []
    for i, it in enumerate(items[:MAX_ITEMS], 1):
        summary = ("<br><span style='color:#8a94a6;font-size:12px'>%s</span>" % it["summary"]) if it.get("summary") else ""
        rows.append(
            '<div style="margin:10px 0;padding:10px 12px;background:#f5f7fb;'
            'border-left:3px solid #2563eb;border-radius:6px;">'
            '<div style="font-size:14px;color:#1f2937;"><b>%d. <a href="%s" style="color:#1d4ed8;text-decoration:none;">%s</a></b></div>'
            '<div style="font-size:11px;color:#6b7280;margin-top:2px;">来源：%s</div>%s'
            '</div>' % (i, html_mod.escape(it["url"]), html_mod.escape(it["title"]),
                        html_mod.escape(it["source"]), summary)
        )
    guide_html = ""
    if guide:
        guide_html = (
            '<div style="margin:6px 0 12px;padding:10px 12px;background:#eef4ff;'
            'border-radius:8px;font-size:13px;color:#1e3a8a;line-height:1.6;">'
            '<b>📌 今日导读</b><br>%s</div>' % html_mod.escape(guide)
        )
    return (
        '<div style="font-family:\'PingFang SC\',\'Microsoft YaHei\',sans-serif;max-width:600px;">'
        '<div style="font-size:12px;color:#9ca3af;">AI 每日简报 · %s</div>'
        '%s'
        '%s'
        '<div style="margin-top:12px;font-size:11px;color:#9ca3af;">共 %d 条 · 自动抓取推送</div>'
        '</div>' % (now, guide_html, "".join(rows), min(len(items), MAX_ITEMS))
    )


# ================= 推送 =================
def send(title, content):
    """调用 PushPlus /send 接口（template=html）"""
    if not PUSHPLUS_TOKEN:
        print("错误：未设置 PUSHPLUS_TOKEN（GitHub Secrets 缺失或为空）")
        return False
    payload = {
        "token": PUSHPLUS_TOKEN,
        "title": title,
        "content": content,
        "template": "html",
    }
    try:
        resp = requests.post(PUSHPLUS_URL, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        # 注意：code=200 仅代表服务端收到请求（异步发送），不代表已送达微信
        print("PushPlus 响应: code=%s msg=%s 流水号=%s" % (
            result.get("code"), result.get("msg"), result.get("data", "")))
        return result.get("code") == 200
    except Exception as e:
        print("推送异常：%s" % e)
        return False


# ================= 主流程 =================
def main():
    print("== 开始执行每日 AI 新闻任务 ==")
    print("时间（北京）：%s" % now_beijing())

    if not PUSHPLUS_TOKEN:
        print("错误：PUSHPLUS_TOKEN 未配置，请先在 GitHub Secrets 添加")
        sys.exit(1)

    print("-- 抓取新闻源 --")
    items = dedupe(fetch_all())
    if not items:
        print("错误：所有新闻源均未取到内容（可能网络问题或源变更）")
        sys.exit(1)
    print("去重后共 %d 条，将推送前 %d 条" % (len(items), min(len(items), MAX_ITEMS)))

    print("-- 生成导读（可选）--")
    guide = ai_guide(items)

    print("-- 排版并推送 --")
    content = build_html(items, guide)
    title = "AI 每日简报 %s" % now_beijing()[:10]
    if send(title, content):
        print("== 完成：已提交推送（异步送达微信，约数秒~1 分钟）==")
    else:
        print("== 失败：推送未成功 ==")
        sys.exit(1)


if __name__ == "__main__":
    main()
