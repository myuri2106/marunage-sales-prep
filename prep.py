"""商談準備シート自動作成ツール（丸投げコネクト営業用）

商談相手のホームページURLを渡すと、会社概要・事業内容・採用・ニュースなどの
ページを自動で巡回し、商談前に必要な情報を1つのフォルダにまとめます。

使い方:
    python prep.py https://example.co.jp
    python prep.py https://example.co.jp --max-pages 15 --no-ai
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.robotparser
from dataclasses import dataclass, field, asdict
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
SERVICE_FILE = BASE_DIR / "config" / "service.md"

USER_AGENT = "Mozilla/5.0 (compatible; SalesPrepBot/1.0)"
REQUEST_INTERVAL_SEC = 1.0  # 相手サイトに負荷をかけないための待ち時間
MAX_PDF_BYTES = 20 * 1024 * 1024

# 優先して巡回するページ（リンク文字列・URLに含まれるキーワード → 分類）
PAGE_CATEGORIES = {
    "会社概要": ["会社概要", "企業情報", "会社情報", "会社案内", "企業概要", "about", "company", "corporate", "profile", "outline", "overview"],
    "代表挨拶・理念": ["代表挨拶", "メッセージ", "ごあいさつ", "挨拶", "理念", "ビジョン", "message", "philosophy", "vision", "mission"],
    "沿革": ["沿革", "歴史", "history"],
    "事業内容": ["事業内容", "事業紹介", "サービス", "製品", "商品", "ソリューション", "business", "service", "product", "solution", "works", "実績", "事例", "case"],
    "採用": ["採用", "求人", "リクルート", "キャリア", "recruit", "career", "jobs"],
    "ニュース": ["ニュース", "お知らせ", "新着", "トピックス", "プレス", "news", "topics", "info", "press", "release"],
    "IR": ["ir", "投資家", "決算", "investor"],
    "拠点・アクセス": ["アクセス", "拠点", "所在地", "事業所", "access", "location", "office"],
    "お問い合わせ": ["お問い合わせ", "問い合わせ", "contact", "inquiry"],
}

# 会社概要表から拾う項目
PROFILE_KEYS = {
    "会社名": ["会社名", "商号", "社名", "法人名", "名称"],
    "電話番号": ["電話", "tel", "TEL"],
    "代表者": ["代表者", "代表取締役", "代表", "社長"],
    "設立": ["設立", "創業", "創立"],
    "資本金": ["資本金"],
    "従業員数": ["従業員", "社員数", "スタッフ数", "人数"],
    "売上高": ["売上", "年商"],
    "所在地": ["所在地", "本社", "住所", "本店"],
    "事業内容": ["事業内容", "事業概要", "業務内容", "営業品目"],
    "取引先": ["取引先", "主要取引先", "主な取引先"],
    "許認可・資格": ["許可", "登録", "認証", "資格", "免許"],
}

SNS_DOMAINS = {
    "X(Twitter)": ["twitter.com", "x.com"],
    "Facebook": ["facebook.com"],
    "Instagram": ["instagram.com"],
    "YouTube": ["youtube.com", "youtu.be"],
    "LinkedIn": ["linkedin.com"],
    "LINE": ["line.me", "lin.ee"],
    "note": ["note.com"],
    "TikTok": ["tiktok.com"],
}

PHONE_RE = re.compile(r"0\d{1,4}[-‐ー−(（]\d{1,4}[-‐ー−)）]\d{3,4}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
DATE_RE = re.compile(r"(20\d{2})[./年-](\d{1,2})[./月-](\d{1,2})日?")


@dataclass
class Page:
    url: str
    category: str
    title: str = ""
    description: str = ""
    text: str = ""


@dataclass
class Report:
    start_url: str
    fetched_at: str
    pages: list[Page] = field(default_factory=list)
    profile: dict[str, str] = field(default_factory=dict)
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    sns: dict[str, str] = field(default_factory=dict)
    news: list[str] = field(default_factory=list)
    pdfs: list[dict[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class Fetcher:
    """robots.txt を守りつつ、間隔をあけてページを取得する。"""

    def __init__(self, start_url: str):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"})
        self.robots = urllib.robotparser.RobotFileParser()
        parsed = urlparse(start_url)
        self.robots.set_url(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
        try:
            resp = self.session.get(self.robots.url, timeout=10)
            self.robots.parse(resp.text.splitlines() if resp.status_code == 200 else [])
        except requests.RequestException:
            self.robots.parse([])
        self._last = 0.0

    def allowed(self, url: str) -> bool:
        return self.robots.can_fetch(USER_AGENT, url)

    def get(self, url: str, **kwargs) -> requests.Response:
        wait = REQUEST_INTERVAL_SEC - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        resp = self.session.get(url, timeout=20, **kwargs)
        resp.raise_for_status()
        return resp


def decode_html(resp: requests.Response) -> str:
    # Shift_JIS / EUC-JP のサイトで文字化けしないように推定エンコーディングを使う
    if resp.encoding is None or resp.encoding.lower() in ("iso-8859-1", "ascii"):
        resp.encoding = resp.apparent_encoding
    return resp.text


def normalize_url(url: str) -> str:
    return urldefrag(url)[0].rstrip("/") or url


def same_site(url: str, root: str) -> bool:
    a, b = urlparse(url).netloc.lower(), urlparse(root).netloc.lower()
    return a.removeprefix("www.") == b.removeprefix("www.")


def categorize(link_text: str, url: str) -> str | None:
    path = urlparse(url).path.lower()
    text = link_text.lower()
    for category, words in PAGE_CATEGORIES.items():
        for w in words:
            wl = w.lower()
            # 英字の短い語（ir, info など）はパスの区切り単位でのみ一致させる
            if wl.isascii() and len(wl) <= 4:
                if re.search(rf"(^|[/_\-.]){re.escape(wl)}([/_\-.]|$)", path) or text == wl:
                    return category
            elif wl in text or (wl.isascii() and wl in path):
                return category
    return None


def page_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    main = soup.find("main") or soup.find(id=re.compile("content|main", re.I)) or soup.body or soup
    lines = (line.strip() for line in main.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


def extract_profile(soup: BeautifulSoup) -> dict[str, str]:
    """会社概要によくある表（th/td, dt/dd）から項目を拾う。"""
    pairs: list[tuple[str, str]] = []
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) >= 2:
            pairs.append((cells[0].get_text(" ", strip=True), cells[1].get_text(" ", strip=True)))
    for dt_tag in soup.find_all("dt"):
        dd = dt_tag.find_next_sibling("dd")
        if dd:
            pairs.append((dt_tag.get_text(" ", strip=True), dd.get_text(" ", strip=True)))

    profile: dict[str, str] = {}
    for label, value in pairs:
        if not value or len(label) > 20:
            continue
        for key, words in PROFILE_KEYS.items():
            if key not in profile and any(w in label for w in words):
                profile[key] = value[:300]
                break
    return profile


def extract_news(text: str, limit: int = 10) -> list[str]:
    items = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = DATE_RE.search(line)
        if not m:
            continue
        rest = DATE_RE.sub("", line).strip(" 　:：-|")
        if len(rest) < 4 and i + 1 < len(lines):
            rest = lines[i + 1].strip()
        if rest:
            items.append(f"{m.group(1)}/{int(m.group(2)):02d}/{int(m.group(3)):02d} {rest[:80]}")
    # 新しい順・重複除去
    return sorted(dict.fromkeys(items), reverse=True)[:limit]


def crawl(start_url: str, max_pages: int, download_pdfs: bool, out_dir: Path) -> Report:
    report = Report(start_url=start_url, fetched_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    fetcher = Fetcher(start_url)
    queue: list[tuple[str, str]] = [(normalize_url(start_url), "トップ")]
    seen: set[str] = set()
    pdf_links: dict[str, str] = {}
    category_counts: dict[str, int] = {}

    while queue and len(report.pages) < max_pages:
        url, category = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        if not fetcher.allowed(url):
            report.skipped.append(f"{url}（robots.txtで禁止）")
            continue
        try:
            resp = fetcher.get(url)
        except requests.RequestException as e:
            report.skipped.append(f"{url}（取得失敗: {e.__class__.__name__}）")
            continue
        if "html" not in resp.headers.get("Content-Type", "html"):
            continue

        print(f"  [{category}] {url}")
        soup = BeautifulSoup(decode_html(resp), "html.parser")

        # リンク収集は本文抽出（タグ削除）より先に行う
        for a in soup.find_all("a", href=True):
            href = normalize_url(urljoin(resp.url, a["href"]))
            if not href.startswith(("http://", "https://")):
                continue
            label = a.get_text(" ", strip=True)
            for sns, domains in SNS_DOMAINS.items():
                if sns not in report.sns and any(d in urlparse(href).netloc for d in domains):
                    report.sns[sns] = href
            if not same_site(href, start_url):
                continue
            if href.lower().endswith(".pdf"):
                pdf_links.setdefault(href, label or Path(urlparse(href).path).name)
                continue
            cat = categorize(label, href)
            # 同じ分類のページを取りすぎないよう、1分類あたり3ページまで
            if cat and href not in seen and category_counts.get(cat, 0) < 3:
                category_counts[cat] = category_counts.get(cat, 0) + 1
                queue.append((href, cat))

        profile = extract_profile(soup)
        for k, v in profile.items():
            report.profile.setdefault(k, v)

        title = soup.title.get_text(strip=True) if soup.title else ""
        meta = soup.find("meta", attrs={"name": "description"})
        text = page_text(soup)
        report.pages.append(Page(url=resp.url, category=category, title=title,
                                 description=meta.get("content", "").strip() if meta else "", text=text))

        report.phones.extend(p for p in PHONE_RE.findall(text) if p not in report.phones)
        report.emails.extend(e for e in EMAIL_RE.findall(text) if e not in report.emails)
        if category in ("トップ", "ニュース"):
            report.news.extend(n for n in extract_news(text) if n not in report.news)

    report.news = sorted(report.news, reverse=True)[:10]

    if download_pdfs and pdf_links:
        pdf_dir = out_dir / "pdf"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        for href, label in list(pdf_links.items())[:10]:
            entry = {"title": label, "url": href, "file": ""}
            if fetcher.allowed(href):
                try:
                    resp = fetcher.get(href, stream=True)
                    size = int(resp.headers.get("Content-Length") or 0)
                    if size <= MAX_PDF_BYTES:
                        name = re.sub(r"[^\w.\-]", "_", Path(urlparse(href).path).name) or "document.pdf"
                        path = pdf_dir / name
                        path.write_bytes(resp.content[:MAX_PDF_BYTES])
                        entry["file"] = f"pdf/{name}"
                        print(f"  [PDF] {href}")
                except requests.RequestException:
                    pass
            report.pdfs.append(entry)
    else:
        report.pdfs = [{"title": t, "url": u, "file": ""} for u, t in pdf_links.items()]

    return report


def company_name(report: Report) -> str:
    if report.profile.get("会社名"):
        return report.profile["会社名"]
    title = report.pages[0].title if report.pages else ""
    return re.split(r"[|｜\-–—:：]", title)[0].strip() or urlparse(report.start_url).netloc


AI_SYSTEM_PROMPT = """あなたは法人営業のリサーチ担当です。
商談相手企業のホームページから集めた情報と、自社サービスの説明をもとに、
営業担当者が商談前に読む「商談準備メモ」を日本語のMarkdownで作成してください。

構成:
## 企業の要約（3〜5行）
## 事業・ビジネスモデルの理解
## 最近の動き（ニュース・採用から読み取れること）
## 想定される課題・ニーズ（根拠となるHP上の記述も併記）
## 自社サービスの提案の切り口（優先度順に3つ）
## ヒアリング質問リスト（10問程度）
## 想定される反論と切り返し
## 商談時の注意点

ホームページに書かれていないことは推測であると明記し、事実と区別してください。"""


def ai_analysis(report: Report, service_text: str) -> str | None:
    try:
        import anthropic
    except ImportError:
        print("  ※ anthropic パッケージが未インストールのため、AI分析はスキップしました")
        return None

    sections = [f"### [{p.category}] {p.title}\nURL: {p.url}\n{p.description}\n{p.text[:6000]}" for p in report.pages]
    user_content = (
        f"# 自社サービス\n{service_text}\n\n"
        f"# 商談相手のHPから抽出した会社概要\n{json.dumps(report.profile, ensure_ascii=False, indent=2)}\n\n"
        f"# 最近のニュース\n" + "\n".join(report.news) + "\n\n"
        f"# 各ページの本文\n" + "\n\n".join(sections)
    )

    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model="claude-opus-5-5",
            max_tokens=16000,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=AI_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
    except anthropic.AuthenticationError:
        print("  ※ APIキーが無効です。.env の ANTHROPIC_API_KEY を確認してください")
        return None
    except anthropic.APIConnectionError:
        print("  ※ Claude APIに接続できませんでした。AI分析はスキップしました")
        return None
    except anthropic.APIStatusError as e:
        print(f"  ※ Claude APIエラー（{e.status_code}）: AI分析はスキップしました")
        return None

    if response.stop_reason == "refusal":
        print("  ※ AI分析を生成できませんでした（refusal）")
        return None
    return "\n".join(b.text for b in response.content if b.type == "text")


def render_markdown(report: Report, ai_text: str | None) -> str:
    name = company_name(report)
    lines = [
        f"# 商談準備シート：{name}",
        "",
        f"- 対象URL: {report.start_url}",
        f"- 作成日時: {report.fetched_at}",
        f"- 取得ページ数: {len(report.pages)}",
        "",
        "## 会社概要（HPから自動抽出）",
        "",
        "| 項目 | 内容 |",
        "|---|---|",
    ]
    for key in PROFILE_KEYS:
        value = report.profile.get(key, "（未取得）").replace("|", "／").replace("\n", " ")
        lines.append(f"| {key} | {value} |")

    lines += ["", "## 連絡先", ""]
    lines.append(f"- 電話: {', '.join(report.phones[:5]) or '（未取得）'}")
    lines.append(f"- メール: {', '.join(report.emails[:5]) or '（未取得）'}")
    contact = next((p.url for p in report.pages if p.category == "お問い合わせ"), None)
    if contact:
        lines.append(f"- 問い合わせフォーム: {contact}")

    if report.sns:
        lines += ["", "## SNS", ""]
        lines += [f"- {k}: {v}" for k, v in report.sns.items()]

    if report.pages and report.pages[0].description:
        lines += ["", "## サイトの説明文（meta description）", "", f"> {report.pages[0].description}"]

    lines += ["", "## 最近のニュース・お知らせ", ""]
    lines += [f"- {n}" for n in report.news] or ["- （未取得）"]

    lines += ["", "## 取得した資料（PDF）", ""]
    if report.pdfs:
        for p in report.pdfs:
            link = f"[{p['title']}]({p['file']})" if p["file"] else f"{p['title']}"
            lines.append(f"- {link} — {p['url']}")
    else:
        lines.append("- （PDF資料は見つかりませんでした）")

    lines += ["", "## 参照したページ", ""]
    lines += [f"- [{p.category}] [{p.title or p.url}]({p.url})" for p in report.pages]

    lines += ["", "---", "", "# AIによる商談準備メモ", ""]
    lines.append(ai_text or "（AI分析なし。`.env` に ANTHROPIC_API_KEY を設定すると、課題仮説・提案の切り口・ヒアリング質問が自動で追加されます）")

    lines += ["", "---", "", "## 商談メモ（手書き用）", "", "- 先方参加者:", "- ヒアリング内容:", "- 次回アクション:", ""]
    if report.skipped:
        lines += ["<details><summary>取得できなかったページ</summary>", ""]
        lines += [f"- {s}" for s in report.skipped]
        lines += ["", "</details>", ""]
    return "\n".join(lines)


def load_dotenv() -> None:
    env = BASE_DIR / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> int:
    parser = argparse.ArgumentParser(description="商談相手のHPから商談準備シートを自動作成します")
    parser.add_argument("url", help="商談相手のホームページURL")
    parser.add_argument("--max-pages", type=int, default=12, help="巡回する最大ページ数（既定: 12）")
    parser.add_argument("--no-pdf", action="store_true", help="PDF資料をダウンロードしない")
    parser.add_argument("--no-ai", action="store_true", help="Claudeによる分析を行わない")
    args = parser.parse_args()

    url = args.url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    load_dotenv()
    domain = urlparse(url).netloc.replace(":", "_")
    out_dir = OUTPUT_DIR / f"{dt.date.today():%Y%m%d}_{domain}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"▶ {url} を巡回しています...")
    report = crawl(url, args.max_pages, not args.no_pdf, out_dir)
    if not report.pages:
        print("✖ ページを取得できませんでした。URLを確認してください。")
        for s in report.skipped:
            print(f"  - {s}")
        return 1

    ai_text = None
    use_ai = not args.no_ai and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if use_ai:
        print("▶ Claudeで商談準備メモを作成しています...")
        service_text = SERVICE_FILE.read_text(encoding="utf-8") if SERVICE_FILE.exists() else ""
        ai_text = ai_analysis(report, service_text)

    sheet = out_dir / "商談準備シート.md"
    sheet.write_text(render_markdown(report, ai_text), encoding="utf-8")
    (out_dir / "pages.md").write_text(
        "\n\n---\n\n".join(f"# [{p.category}] {p.title}\n{p.url}\n\n{p.text}" for p in report.pages),
        encoding="utf-8",
    )
    (out_dir / "data.json").write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"✔ 完了: {sheet}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
