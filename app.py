"""Fandom Archive V2.9 | CP 标签也支持历史多选与新增。

升级说明：CP 由单值升级为多值（仍存储于原 cp TEXT 列），
独立于作品类型标签；历史 CP 自动成为选项，旧收藏无损兼容。
启动：python -m streamlit run app.py
"""

import csv
from collections import Counter
import html
import io
import ipaddress
import json
import socket
import urllib.error
import urllib.request
from html.parser import HTMLParser
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlparse

import streamlit as st

import db

# 汉字标题按拼音排序；若尚未安装，页面会提示运行更新包中的安装脚本。
try:
    from pypinyin import lazy_pinyin
except ImportError:
    lazy_pinyin = None



st.set_page_config(
    page_title="我的同人收藏馆 · Fandom Archive",
    page_icon="🌷",
    layout="wide",
    initial_sidebar_state="collapsed",
)

PLATFORMS = ["LOFTER", "AO3", "小红书", "微博", "抖音", "其他"]

# 与页面上的来源筛选保持相同顺序；这里也是默认分组展示顺序。
PLATFORM_INDEX = {name: index for index, name in enumerate(PLATFORMS)}
DEFAULT_SORT = "按平台分类 · 标题拼音 A–Z"


def title_pinyin_key(title):
    """中文转为拼音全拼，英文/数字按字符排序；忽略标题前的装饰标点。"""
    name = re.sub(r"^[^\w]+", "", str(title or "").strip())
    if lazy_pinyin is None:
        return name.casefold()
    return "".join(lazy_pinyin(name)).casefold()


def platform_title_key(work):
    # id 用于同名时提供稳定排序，不影响原始收藏数据和时间。
    return (
        PLATFORM_INDEX.get(work[2], len(PLATFORMS)),
        title_pinyin_key(work[1]),
        (work[1] or "").casefold(),
        work[0],
    )


def sorted_by_platform_title(works):
    return sorted(works, key=platform_title_key)

PLATFORM_ICONS = {
    "LOFTER": "🪷",
    "AO3": "📖",
    "小红书": "🍓",
    "微博": "💬",
    "抖音": "🎞️",
    "其他": "💗",
}
PLATFORM_STYLES = {
    "LOFTER": ("#FCEAF2", "#F8DEE9"),
    "AO3": ("#FCE7E9", "#F8EDE6"),
    "小红书": ("#FFF0EF", "#FDE6EB"),
    "微博": ("#FFF1E8", "#FCE7E5"),
    "抖音": ("#F1E9FB", "#FAEAF2"),
    "其他": ("#FCECF4", "#F0EDFB"),
}


def esc(value):
    return html.escape(str(value or ""), quote=True)


def get_setting(key, fallback=""):
    return db.get_setting(key, fallback)


def set_setting(key, value):
    db.set_setting(key, value)


def read_works():
    return db.read_works()


def clean_cp(value):
    """统一单个 CP 名称（不改动作品类型标签）。"""
    return " ".join(str(value or "").strip().split())[:80]


def parse_cps(raw):
    """兼容旧版单一 CP 和新版用逗号、顿号、换行分隔的多 CP 字符串。"""
    result, seen = [], set()
    for item in parse_tags(raw):
        cp = clean_cp(item)
        if cp and cp.casefold() not in seen:
            result.append(cp)
            seen.add(cp.casefold())
    return result


def normalize_selected_cps(selected, known_cps):
    """复用已有 CP 写法，并对新选项去重；适用于新增、修改和常用 CP。"""
    canonical = {cp.casefold(): cp for cp in known_cps}
    result, seen = [], set()
    for item in selected or []:
        for cp in parse_cps(item):
            key = cp.casefold()
            if key not in seen:
                result.append(canonical.get(key, cp))
                seen.add(key)
    return result


def cp_catalog(works):
    """每个 CP 对应的作品数量；同一篇作品可属于多个 CP 分类。"""
    names, count = {}, Counter()
    for work in works:
        for cp in parse_cps(work[8]):
            normalized = cp.casefold()
            names.setdefault(normalized, cp)
            count[normalized] += 1
    return sorted(((names[key], count[key]) for key in count), key=lambda x: (-x[1], x[0].casefold()))


def work_has_cp(work, cp):
    return cp == "全部 CP" or cp.casefold() in {item.casefold() for item in parse_cps(work[8])}


def valid_url(url):
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def extract_url(text):
    """提取网页链接、手机复制的整段分享文案，也支持漏写协议的常见平台链接。"""
    content = str(text or "").replace("\u200b", "").replace("\ufeff", "")
    trailing = ".,!?，。；;！？，、)]}）】>\"'’”"

    # 保留分享链接里的查询参数（例如小红书的 xsec_token），便于正常跳转。
    match = re.search(r"https?://[^\s<>\"'，。；;、）)】\]\}]+", content, flags=re.I)
    if match:
        return match.group(0).rstrip(trailing)

    # 有些手机复制出的分享链接不带 https://，仅针对已知平台补全协议。
    known = (
        r"xhslink\.(?:com|cn)|xiaohongshu\.com|rednote\.com|"
        r"lofter\.com|archiveofourown\.org|ao3\.org|"
        r"weibo\.(?:com|cn)|douyin\.com|iesdouyin\.com"
    )
    match = re.search(
        rf"(?<![\w@])((?:[a-z0-9-]+\.)*(?:{known})(?:/[^\s<>\"'，。；;、）)】\]\}}]*)?(?:\?[^\s<>\"'，。；;、）)】\]\}}]*)?)",
        content,
        flags=re.I,
    )
    return "https://" + match.group(1).rstrip(trailing) if match else ""


def is_domain(host, domain):
    """匹配真实主域和子域，避免 example-lofter.com 误判。"""
    return host == domain or host.endswith("." + domain)


def infer_platform(url):
    """根据域名自动识别平台，不向原平台发出请求。"""
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if is_domain(host, "lofter.com"):
        return "LOFTER"
    if is_domain(host, "archiveofourown.org") or is_domain(host, "ao3.org"):
        return "AO3"
    if any(is_domain(host, domain) for domain in (
        "xiaohongshu.com", "xhslink.com", "xhslink.cn", "rednote.com"
    )):
        return "小红书"
    if any(is_domain(host, domain) for domain in ("weibo.com", "weibo.cn", "t.cn")):
        return "微博"
    if any(is_domain(host, domain) for domain in ("douyin.com", "iesdouyin.com")):
        return "抖音"
    return None


def canonical_url_key(url):
    """生成只用于查重的链接指纹，不修改实际存储的原始作品链接。

    能识别部分同一作品的不同 URL、末尾斜杠和常见跟踪参数；
    不请求短链接跳转，故不同短链与长链可能无法判为同一篇。
    """
    url = extract_url(url)
    if not valid_url(url):
        return ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    path = unquote(parsed.path or "/").rstrip("/") or "/"
    platform = infer_platform(url)

    # 同一篇 AO3 作品的章节地址属于同一 works ID。
    match = re.match(r"^/works/(\d+)(?:/|$)", path, re.I)
    if platform == "AO3" and match:
        return f"ao3:work:{match.group(1)}"

    if platform == "小红书" and is_domain(host, "xiaohongshu.com"):
        match = re.match(r"^/(?:explore|discovery/item)/(\w+)(?:/|$)", path, re.I)
        if match:
            return f"xhs:item:{match.group(1).lower()}"

    if platform == "抖音":
        match = re.match(r"^/(?:share/)?video/(\d+)(?:/|$)", path, re.I)
        if match:
            return f"douyin:video:{match.group(1)}"

    ignored = {
        "source", "from", "ref", "refer", "spm", "share_source", "share_from",
        "share_id", "timestamp", "xsec_source", "xsec_token", "share_channel",
    }
    params = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in ignored and not k.lower().startswith("utm_")
    ]
    query = urlencode(sorted(params))
    # 子域对于 LOFTER 作者页很关键，这里不随意去掉。
    if platform == "微博" and host == "m.weibo.cn":
        host = "weibo.cn"
    return f"{host}{path}?{query}" if query else f"{host}{path}"


def find_duplicate(url, works):
    key = canonical_url_key(url)
    if not key:
        return None
    for work in works:
        if canonical_url_key(work[3]) == key:
            return work
    return None


def parse_tags(raw):
    """拆分中英文逗号、顿号、分号、换行和竖线，去重并保持输入顺序。

    旧版存储在 works.tags 的字符串无需迁移，也能直接归入分类。
    例如「捡手机、搞笑，HE」会被识别成三个独立标签。
    """
    items = re.split(r"[,，、;；\n|｜]+", str(raw or ""))
    result, seen = [], set()
    for item in items:
        tag = item.strip().lstrip("#＃").strip()
        if not tag:
            continue
        key = tag.casefold()
        if key not in seen:
            result.append(tag)
            seen.add(key)
    return result


def tag_catalog(works):
    """返回 (显示标签, 作品数) 列表，每篇作品在同一标签下只计算一次。"""
    counts = Counter()
    labels = {}
    for work in works:
        for tag in parse_tags(work[4]):
            key = tag.casefold()
            counts[key] += 1
            labels.setdefault(key, tag)
    return sorted(
        ((labels[k], value) for k, value in counts.items()),
        key=lambda pair: (-pair[1], pair[0].casefold()),
    )


def work_has_tag(work, tag):
    return tag == "全部分类" or tag.casefold() in {
        item.casefold() for item in parse_tags(work[4])
    }


def normalize_selected_tags(selected, known_tags):
    """保存多选标签：合并新旧选项、大小写去重，尽量沿用已有标签名称。"""
    known = {tag.casefold(): tag for tag in known_tags}
    normalized, seen = [], set()
    for raw in selected or []:
        for tag in parse_tags(raw):
            key = tag.casefold()
            if key not in seen:
                normalized.append(known.get(key, tag))
                seen.add(key)
    return normalized



# 自动作者识别：仅查询用户主动粘贴的公开作品页；不使用账号、Cookies 或反爬手段。
# 只访问已知的作品平台域名，限制跳转、响应大小与网络时间；任何失败都允许手填。
PUBLIC_DOMAINS = (
    "lofter.com", "archiveofourown.org", "ao3.org",
    "xiaohongshu.com", "xhslink.com", "xhslink.cn", "rednote.com",
    "weibo.com", "weibo.cn", "t.cn", "douyin.com", "iesdouyin.com",
)


def safe_author(value):
    """清理作者字段，拒绝过长、网址、站点名或明显无意义的内容。"""
    value = html.unescape(str(value or "")).strip().strip("@＠")
    # 某些分享文案以类似「3.14 昵称发布了…」开头；数字是分享提示，并非昵称。
    value = re.sub(r"^\d+(?:\.\d+)?\s+", "", value)
    value = re.sub(r"\s+", " ", value)
    if not value or len(value) > 70 or "http://" in value or "https://" in value:
        return ""
    if value.casefold() in {
        "lofter", "小红书", "抖音", "微博", "ao3", "archive of our own",
        "rednote", "weibo", "douyin", "unknown", "administrator",
    }:
        return ""
    if any(ord(c) < 32 for c in value):
        return ""
    return value


def author_from_share_text(raw_text, platform):
    """只解析较明确的分享语句，不把作品标题误认为作者。"""
    text = str(raw_text or "")[:500]
    if platform == "小红书":
        patterns = [
            r"(?:^|[\n\r])\s*([^\n\r，,。！!：:]{1,30}?)\s*发布了一篇小红书笔记",
            r"(?:^|[\n\r])\s*([^\n\r，,。！!：:]{1,30}?)\s*在小红书发布了",
        ]
    elif platform == "抖音":
        patterns = [r"看看【([^【】]{1,30}?)的作品】"]
    else:
        patterns = []
    for p in patterns:
        match = re.search(p, text)
        if match:
            candidate = safe_author(match.group(1))
            if candidate:
                return candidate
    return ""


class PublicMetadataParser(HTMLParser):
    """只读取页面公开的标准作者元信息、JSON-LD 和 AO3 作者栏。"""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.author_links = []
        self.in_author_link = False
        self.author_link_text = []
        self.in_jsonld = False
        self.jsonld_chunks = []
        self.jsonld_documents = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "meta":
            key = (d.get("name") or d.get("property") or "").lower()
            val = d.get("content") or ""
            if key and val:
                self.meta.setdefault(key, val)
        if tag == "a" and "author" in (d.get("rel") or "").lower().split():
            self.in_author_link = True
            self.author_link_text = []
        if tag == "script" and (d.get("type") or "").lower().split(";")[0].strip() == "application/ld+json":
            self.in_jsonld = True
            self.jsonld_chunks = []

    def handle_data(self, data):
        if self.in_author_link:
            self.author_link_text.append(data)
        if self.in_jsonld:
            self.jsonld_chunks.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.in_author_link:
            name = safe_author("".join(self.author_link_text))
            if name:
                self.author_links.append(name)
            self.in_author_link = False
        if tag == "script" and self.in_jsonld:
            self.jsonld_documents.append("".join(self.jsonld_chunks))
            self.in_jsonld = False


def jsonld_author(node):
    if isinstance(node, list):
        for item in node:
            a = jsonld_author(item)
            if a:
                return a
    elif isinstance(node, dict):
        for key in ("author", "creator"):
            entry = node.get(key)
            if isinstance(entry, str):
                name = safe_author(entry)
                if name:
                    return name
            elif isinstance(entry, dict):
                name = safe_author(entry.get("name"))
                if name:
                    return name
            elif isinstance(entry, list):
                authors = [safe_author(a.get("name") if isinstance(a, dict) else a) for a in entry]
                authors = [a for a in authors if a]
                if authors:
                    return "、".join(authors)[:70]
        for key in ("@graph", "mainEntity"):
            if key in node:
                a = jsonld_author(node[key])
                if a:
                    return a
    return ""


def parse_author_from_html(html_text, platform):
    p = PublicMetadataParser()
    try:
        p.feed(html_text)
    except (ValueError, AssertionError):
        pass
    # AO3 的作品页作者栏往往使用 <a rel="author">；有多个作者时一起保留。
    if platform == "AO3" and p.author_links:
        return "、".join(dict.fromkeys(p.author_links))[:70]
    for field in ("author", "article:author", "og:article:author", "twitter:creator", "parsely-author"):
        val = safe_author(p.meta.get(field, ""))
        if val:
            return val
    for raw in p.jsonld_documents:
        try:
            name = jsonld_author(json.loads(raw))
        except (ValueError, TypeError, RecursionError):
            continue
        if name:
            return name
    return ""


def validate_remote_url(url):
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or parts.username or parts.password:
        raise ValueError("不支持的链接")
    host = (parts.hostname or "").lower().rstrip(".")
    if not any(is_domain(host, domain) for domain in PUBLIC_DOMAINS):
        raise ValueError("只支持已知平台的公开网页")
    if parts.port not in (None, 80, 443):
        raise ValueError("不支持自定义端口")
    # 阻止平台跳转至内网地址；不保证绕过所有 DNS 重绑定攻击，故不提供自定义域名抓取。
    resolved = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    if not resolved or any(not ipaddress.ip_address(info[4][0]).is_global for info in resolved):
        raise ValueError("不能访问非公网地址")


class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 4
    max_repeats = 2
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_remote_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@st.cache_data(ttl=1800, max_entries=200, show_spinner=False)
def fetch_public_author(url, platform):
    """返回 (作者, 简短状态)。如果网站限制访问，不尝试破解登录或验证码。"""
    try:
        validate_remote_url(url)
        opener = urllib.request.build_opener(RestrictedRedirect())
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; PersonalBookmarkArchive/2.8)",
                "Accept": "text/html,application/xhtml+xml",
            },
            method="GET",
        )
        with opener.open(request, timeout=4) as response:
            if response.info().get_content_type() not in ("text/html", "application/xhtml+xml"):
                return "", "网页没有提供可读的作者信息"
            raw = response.read(850_000)
            charset = response.info().get_content_charset() or "utf-8"
            try:
                body = raw.decode(charset, errors="replace")
            except LookupError:
                body = raw.decode("utf-8", errors="replace")
        result = parse_author_from_html(body, platform)
        if result:
            return result, "公开网页元数据"
        return "", "网页未公开作者信息，或内容需要登录后才能查看"
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError, UnicodeError, RuntimeError) as ex:
        return "", "网站限制访问或网络暂时不可用（可手动填写）"


def on_new_link_change():
    # 回调在页面重新绘制之前触发，可以安全清理上一次链接的自动填写结果。
    for key in ("new_work_author", "new_work_author_origin"):
        st.session_state.pop(key, None)


def _friendly_error(ex):
    text = str(ex)
    mapping = [
        ("INVITE_CODE_INVALID", "邀请码无效或已用完，请向管理员索取新的邀请码。"),
        ("User already registered", "这个邮箱已经注册过了，请直接登录。"),
        ("Invalid login credentials", "邮箱或密码不正确。"),
        ("Email not confirmed", "邮箱尚未确认，请联系管理员。"),
        ("Password should be at least", "密码长度至少 6 位。"),
        ("New password should be different", "新密码不能与旧密码相同。"),
    ]
    for needle, message in mapping:
        if needle.lower() in text.lower():
            return message
    return text


if not db.configured():
    st.error("缺少 Supabase 配置：请设置 SUPABASE_URL 与 SUPABASE_ANON_KEY。")
    st.stop()

if not db.logged_in():
    st.markdown(
        '<div class="hero"><div class="hero-eyebrow">✦ FANDOM ARCHIVE</div>'
        '<div class="hero-title">🌷 我的同人收藏馆</div>'
        '<p class="hero-subtitle">请先登录。注册需要管理员发放的邀请码。</p></div>',
        unsafe_allow_html=True,
    )
    tab_login, tab_signup = st.tabs(["🔑 登录", "💌 使用邀请码注册"])

    with tab_login:
        with st.form("login_form"):
            login_email = st.text_input("邮箱", key="login_email")
            login_password = st.text_input("密码", type="password", key="login_password")
            login_submit = st.form_submit_button(
                "登录", type="primary", use_container_width=True
            )
        if login_submit:
            if not login_email.strip() or not login_password:
                st.warning("请填写邮箱和密码。")
            else:
                try:
                    db.sign_in(login_email, login_password)
                    st.rerun()
                except Exception as ex:
                    st.error(_friendly_error(ex))

    with tab_signup:
        with st.form("signup_form"):
            invite_code = st.text_input("邀请码 *", key="signup_invite")
            signup_email = st.text_input("邮箱 *", key="signup_email")
            signup_password = st.text_input(
                "密码 *（至少 6 位）", type="password", key="signup_password"
            )
            signup_confirm = st.text_input(
                "确认密码 *", type="password", key="signup_confirm"
            )
            signup_submit = st.form_submit_button(
                "注册并登录", type="primary", use_container_width=True
            )
        if signup_submit:
            if not invite_code.strip():
                st.warning("请填写邀请码。")
            elif not signup_email.strip():
                st.warning("请填写邮箱。")
            elif len(signup_password) < 6:
                st.warning("密码至少 6 位。")
            elif signup_password != signup_confirm:
                st.warning("两次输入的密码不一致。")
            else:
                try:
                    db.sign_up(signup_email, signup_password, invite_code)
                    if db.logged_in():
                        st.rerun()
                    else:
                        st.success("注册成功，请直接登录。")
                except Exception as ex:
                    st.error(_friendly_error(ex))
    st.stop()

if not db.is_active():
    st.error("该账号已被管理员停用。如有疑问请联系管理员。")
    if st.button("退出登录"):
        db.sign_out()
        st.rerun()
    st.stop()

with st.sidebar:
    st.markdown("### 🌷 我的收藏馆")
    st.caption(f"👤 {st.session_state.get('sb_user', {}).get('email', '')}")
    if db.is_admin():
        st.page_link("pages/admin.py", label="🛠️ 管理后台", icon="🛠️")
    if st.button("退出登录", use_container_width=True):
        db.sign_out()
        st.rerun()

st.markdown(
    """
    <style>
    :root { color-scheme: light; }
    .stApp { background: #fffdfd; color: #352d38; }
    [data-testid="stHeader"] { background: rgba(255,253,253,.92); }
    .block-container { max-width: 1160px; padding-top: 2.0rem; padding-bottom: 4rem; }
    h1, h2, h3 { color: #392e3a; letter-spacing: -.025em; }
    div[role="radiogroup"] { background:#fff5f9; padding:.35rem; border-radius:13px; }
    div[role="radiogroup"] label { border-radius:10px; }
    div[data-testid="stVerticalBlockBorderWrapper"] > div:has(> div[data-testid="stVerticalBlock"]) {
      border-color: #f4e4ec;
    }
    [data-testid="stForm"] {
      background: white !important;
      border: 1px solid #f3dfe8 !important;
      border-radius: 20px !important;
      padding: 1.25rem !important;
      box-shadow: 0 8px 25px rgba(135,65,95,.045);
    }
    .stTextInput input, .stTextArea textarea,
    div[data-baseweb="select"] > div {
      border-color: #efdfe6 !important;
      border-radius: 11px !important;
      background-color: #fff !important;
    }
    .stTextInput input:focus, .stTextArea textarea:focus {
      border-color: #d96f9c !important;
    }
    button[kind="primary"], [data-testid="stFormSubmitButton"] button[kind="primary"] {
      background: #c85787 !important; border-color: #c85787 !important;
      color: #fff !important; border-radius: 11px !important;
      font-weight: 650 !important;
    }
    button[kind="primary"]:hover { background: #ae3b6c !important; border-color: #ae3b6c !important; }
    [data-testid="stLinkButton"] a {
      background: #fff1f6 !important; border-color: #f5d8e5 !important;
      color: #a5396b !important; border-radius: 10px !important;
      font-weight: 600 !important;
    }
    [data-testid="stLinkButton"] a:hover { background: #ffe2ec !important; }
    div[data-testid="stButton"] button:not([kind="primary"]),
    div[data-testid="stDownloadButton"] button {
      border-radius: 10px !important; border-color: #ecdde5 !important;
    }
    .hero {
      background: linear-gradient(107deg, #ffffff 9%, #fff5f9 62%, #ffe8f0 100%);
      border: 1px solid #f7e5ed; border-radius: 24px;
      padding: 2rem 2.25rem 2.2rem; margin: 0 0 1.1rem;
      position: relative; overflow: hidden;
    }
    .hero::before {
      content: ''; position: absolute; right: -55px; top: -110px;
      height: 285px; width: 285px; border-radius: 50%;
      border: 33px solid rgba(236,130,169,.10);
    }
    .hero::after {
      content: '✿'; position:absolute; right:80px; bottom:10px;
      font-size:76px; color:rgba(206,98,139,.14); transform: rotate(-17deg);
    }
    .hero-eyebrow { font-size: .77rem; font-weight: 750; letter-spacing:.21em; color:#b6537e; }
    .hero-title { font-size: clamp(2rem,4vw,3.1rem); line-height:1.3; font-weight: 780;
      margin: .5rem 0 .5rem; color: #3f303d; letter-spacing: -.05em; }
    .hero-subtitle { font-size: .99rem; color: #836f7e; margin:0; line-height:1.8; }
    .stat {
      background: #fff; border: 1px solid #f5e6ed; border-radius: 17px;
      padding: .9rem 1.2rem; min-height: 98px;
      box-shadow: 0 4px 20px rgba(151,79,111,.035);
    }
    .stat-label { color: #967e8d; font-size:.83rem; }
    .stat-value { color:#9e3f6d; font-size: 1.68rem; font-weight:780; margin-top: .15rem; }
    .section-heading {font-size:1.25rem; font-weight:750; color:#422f3c;
      margin:1.25rem 0 .3rem;}
    .section-sub {color:#9b8592; font-size:.86rem; margin-bottom:1rem;}
    .cover {
      min-height: 98px; border-radius: 12px; display:flex; align-items:center;
      justify-content:space-between; padding: 1rem 1.25rem; margin-bottom: 1.05rem;
      overflow:hidden; position:relative;
    }
    .cover::after { content:'✳'; position:absolute; right:17%; top:-22px;
      font-size:90px; opacity:.11; color:#9b315c; transform:rotate(14deg); }
    .cover-icon {font-size:2.0rem; z-index:1;}
    .cover-label { color:#8d5170; font-size:.8rem; font-weight:750; z-index:1;
      background:rgba(255,255,255,.69); padding:.35rem .7rem; border-radius:50px; }
    .work-title {font-size:1.18rem; line-height:1.45; font-weight:750; color:#402e3b;
      overflow-wrap:anywhere; margin:.2rem 0 .35rem;}
    .work-date {color:#a28c99; font-size:.76rem; margin-bottom:.9rem;}
    .work-author { color:#9b5978; font-size:.87rem; margin:.25rem 0 .5rem; }
    .work-cp { display: inline-block; font-size:.83rem; color:#b24677;
      background:#fff1f7; border:1px solid #f5d9e5; border-radius:20px;
      padding:.2rem .65rem; margin:.25rem 0 .6rem; }
    .platform-group-title {margin:1.35rem 0 .8rem; padding:.8rem 1rem;
      background:linear-gradient(100deg,#fff2f8 10%,#fffdfd 95%);
      border-left:4px solid #dc8faf; border-radius:12px; color:#8e3e66;
      font-size:1.06rem; font-weight:760;}
    .platform-group-count {font-size:.78rem; margin-left:.7rem; color:#ad8498; font-weight:500;}
    .tag-section {border:1px solid #f3e3ec; border-radius:17px; padding:1rem 1.1rem;
      background: linear-gradient(130deg, #fff 30%, #fff8fb 100%); margin:1rem 0 1.1rem;}
    .tag-section-title {font-size:1.04rem; font-weight:740; color:#7d3f61;}
    .tag-section-sub {font-size:.82rem; color:#9c7f90; margin:.25rem 0 .85rem;}
    .tags {margin: .55rem 0; min-height: 22px;}
    .tag {display:inline-block; padding:.2rem .62rem; margin:0 .3rem .36rem 0;
      border-radius:100px; background:#fff0f6; color:#ad507b; font-size:.78rem;}
    .work-note {background:#fffbfd; border-left: 3px solid #e8afc7;
      padding:.6rem .85rem; border-radius:0 9px 9px 0;
      font-size:.88rem; line-height:1.6; color:#7a6876; overflow-wrap:anywhere;}
    .empty {text-align:center; background: #fff9fc; border:1px dashed #ecc6d7;
      border-radius:20px; padding:2.8rem 1rem; margin:1.1rem 0;}
    .empty-icon {font-size:2.6rem; margin-bottom:.5rem;}
    .empty-title {font-size:1.17rem; font-weight:750; color:#7c4662;}
    .empty-desc {color:#a58d9a; font-size:.9rem; margin-top:.4rem;}
    .footnote {color:#a8929e; font-size:.78rem; line-height:1.7;
      padding-top:1.4rem; border-top:1px solid #f8e7ef; margin-top:2rem;}
    /* 手机屏幕：继续保持白底粉色点缀，并为触屏操作留出空间。 */
    @media (max-width: 720px) {
      .block-container { padding: .8rem .85rem 3rem !important; }
      .hero { padding: 1.25rem 1.05rem 1.35rem; border-radius: 18px; }
      .hero-title { font-size: clamp(1.65rem, 7vw, 2.15rem); line-height:1.3; }
      .hero-eyebrow { font-size: .61rem; letter-spacing: .12em; }
      .hero-subtitle { font-size: .86rem; max-width: 94%; line-height: 1.6; }
      .hero::after { right: 12px; bottom: 10px; font-size: 40px; }
      .hero::before { width:180px; height:180px; right:-100px; top:-100px; }
      .stat { padding: .7rem .65rem; min-height: 75px; border-radius: 12px; }
      .stat-label { font-size: .7rem; }
      .stat-value { font-size: 1.25rem; }
      .cover { min-height: 72px; padding: .75rem .8rem; margin-bottom: .75rem; }
      .work-title { font-size: 1.05rem; }
      .work-note { font-size: .83rem; }
      .section-heading { font-size: 1.14rem; }
      [data-testid="stForm"] { padding: .85rem !important; border-radius: 15px !important; }
      .stTextInput input, .stTextArea textarea { font-size: 16px !important; }
      button { min-height: 42px; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="hero">
      <div class="hero-eyebrow">✦ FANDOM ARCHIVE · MY LITTLE LIBRARY</div>
      <div class="hero-title">🌷 我的同人收藏馆</div>
      <p class="hero-subtitle">收藏每一份心动，把散落在各个平台的喜欢，悄悄珍藏起来。</p>
    </div>
    """,
    unsafe_allow_html=True,
)

all_works = read_works()
categories = tag_catalog(all_works)
cp_counts = cp_catalog(all_works)
cp_names = [name for name, _ in cp_counts]
for previously_saved in parse_cps(get_setting("default_cp")):
    if previously_saved.casefold() not in {name.casefold() for name in cp_names}:
        cp_names.append(previously_saved)
counts = [len(all_works), len({w[2] for w in all_works}), len(categories)]
cols = st.columns(3, gap="medium")
for col, label, value in zip(cols, ["💌 全部收藏", "🪄 收藏平台", "🏷️ 标签分类"], counts):
    with col:
        st.markdown(
            f'<div class="stat"><div class="stat-label">{label}</div>'
            f'<div class="stat-value">{value}</div></div>',
            unsafe_allow_html=True,
        )

if st.session_state.pop("delete_notice", False):
    st.success("已删除这条收藏记录。")
if st.session_state.get("edit_tag_notice"):
    st.success(f"💞 已更新《{st.session_state.pop('edit_tag_notice')}》的 CP 与分类标签。")

view = st.radio(
    "切换页面", ["🌸 我的作品馆", "＋ 添加新收藏"], horizontal=True,
    label_visibility="collapsed", key="current_page",
)

if view == "🌸 我的作品馆":
    st.markdown('<div class="section-heading">珍藏的作品</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-sub">想重温时，它们就在这里。</div>', unsafe_allow_html=True)

    st.markdown(
        '<div class="tag-section"><div class="tag-section-title">🏷️ 按标签分类</div>'
        '<div class="tag-section-sub">根据你收藏时填写的标签自动生成分类；一篇作品可以同时属于多个分类。</div></div>',
        unsafe_allow_html=True,
    )
    category_names = [label for label, _ in categories]
    tag_options = ["全部分类"] + category_names
    # 编辑或删除最后一条某分类的作品后，恢复「全部分类」，避免控件出现无效选项。
    if st.session_state.get("selected_tag") not in tag_options:
        st.session_state["selected_tag"] = "全部分类"

    # 手机可直接点常用标签；所有标签都能在下方下拉列表中选择。
    quick_tags = ["全部分类"] + category_names[:5]
    if categories:
        quick_cols = st.columns(3, gap="small")
        frequencies = dict(categories)
        for index, tag in enumerate(quick_tags):
            with quick_cols[index % 3]:
                label = "全部作品" if tag == "全部分类" else f"{tag} · {frequencies[tag]}"
                if st.button(
                    label, key=f"quick_category_{index}",
                    type="primary" if st.session_state["selected_tag"] == tag else "secondary",
                    use_container_width=True,
                ):
                    st.session_state["selected_tag"] = tag
                    st.rerun()
    else:
        st.caption("还没有分类。添加作品时填写「捡手机、温情、搞笑」等标签，分类会自动出现在这里。")

    # CP 也像作品类型标签一样自动成为分类入口。
    if cp_counts:
        st.markdown("**💞 按 CP 分类**")
        available_cps = ["全部 CP"] + [name for name, _ in cp_counts]
        if st.session_state.get("selected_cp") not in available_cps:
            st.session_state["selected_cp"] = "全部 CP"
        quick_cp_cols = st.columns(3, gap="small")
        for index, name in enumerate(available_cps[:6]):
            with quick_cp_cols[index % 3]:
                cp_label = "全部 CP" if name == "全部 CP" else f"{name} · {dict(cp_counts)[name]}"
                if st.button(
                    cp_label, key=f"quick_cp_{index}",
                    type="primary" if st.session_state["selected_cp"] == name else "secondary",
                    use_container_width=True,
                ):
                    st.session_state["selected_cp"] = name
                    st.rerun()

    category_col, cp_col = st.columns(2, gap="medium")
    with category_col:
        selected_tag = st.selectbox(
            "按分类查看", tag_options, key="selected_tag",
            format_func=lambda name: "全部分类" if name == "全部分类"
                else f"{name}（{dict(categories)[name]} 篇）",
        )
    with cp_col:
        cp_options = ["全部 CP"] + [name for name, _ in cp_counts]
        if st.session_state.get("selected_cp") not in cp_options:
            st.session_state["selected_cp"] = "全部 CP"
        selected_cp = st.selectbox("按 CP 查看", cp_options, key="selected_cp",
                                   format_func=lambda name: name if name == "全部 CP" else f"{name}（{dict(cp_counts)[name]} 篇）")
    platform_col, order_col = st.columns(2, gap="medium")
    with platform_col:
        selected_platform = st.selectbox("来源平台", ["全部平台"] + PLATFORMS)
    with order_col:
        sort_method = st.selectbox("排列顺序", [DEFAULT_SORT, "最新收藏", "最早收藏", "标题拼音 A–Z"])
    keyword = st.text_input(
        "搜索作品", placeholder="🔎 搜索标题 / CP / 标签 / 作者 / 收藏笔记", key="search"
    )

    filtered = []
    for work in all_works:
        work_id, title, platform, url, tags, note, created_at, author, cp = work
        searchable = f"{title} {platform} {tags} {note} {author} {cp}".casefold()
        if keyword.strip().casefold() not in searchable:
            continue
        if selected_platform != "全部平台" and platform != selected_platform:
            continue
        if not work_has_cp(work, selected_cp):
            continue
        if not work_has_tag(work, selected_tag):
            continue
        filtered.append(work)

    if sort_method == DEFAULT_SORT:
        filtered = sorted_by_platform_title(filtered)
    elif sort_method == "最早收藏":
        filtered.reverse()
    elif sort_method == "标题拼音 A–Z":
        filtered.sort(key=lambda row: (title_pinyin_key(row[1]), (row[1] or "").casefold(), row[0]))

    if lazy_pinyin is None and sort_method in (DEFAULT_SORT, "标题拼音 A–Z"):
        st.warning("尚未安装拼音排序组件 pypinyin。英文排序可用，但中文拼音排序可能不准确；请运行更新包中的『安装拼音排序.bat』。")

    st.caption(
        f"共珍藏 {len(all_works)} 篇 · {len(cp_counts)} 种 CP · {len(categories)} 种标签分类 · "
        f"当前显示 {len(filtered)} 篇"
        + (f" · 分类：{selected_tag}" if selected_tag != "全部分类" else "")
    )

    if not filtered:
        if all_works:
            heading, desc = "没有找到这类作品", "换个关键词、分类或平台试试看吧。"
        else:
            heading, desc = "你的收藏馆还是空的", "点击「添加新收藏」，存下第一份心动吧。"
        st.markdown(
            f'<div class="empty"><div class="empty-icon">🧸</div>'
            f'<div class="empty-title">{heading}</div>'
            f'<div class="empty-desc">{desc}</div></div>',
            unsafe_allow_html=True,
        )
    else:
        # 默认按平台分组，并在各组内按标题拼音 A–Z 排列。
        # 切换到时间排序时，仍可以按全部收藏的时间顺序查看。
        if sort_method == DEFAULT_SORT:
            groups = [
                (platform, [work for work in filtered if work[2] == platform])
                for platform in PLATFORMS
            ]
            groups = [(platform, works) for platform, works in groups if works]
        else:
            groups = [(None, filtered)]

        for group_platform, group_items in groups:
            if group_platform is not None:
                group_icon = PLATFORM_ICONS.get(group_platform, "💗")
                st.markdown(
                    f'<div class="platform-group-title">{group_icon} {esc(group_platform)}'
                    f'<span class="platform-group-count">{len(group_items)} 篇 · 按标题拼音 A–Z</span></div>',
                    unsafe_allow_html=True,
                )
            for start in range(0, len(group_items), 2):
                work_cols = st.columns(2, gap="large")
                for col, work in zip(work_cols, group_items[start : start + 2]):
                    work_id, title, platform, url, tags, note, created_at, author, cp = work
                    with col:
                        with st.container(border=True):
                            bg_a, bg_b = PLATFORM_STYLES.get(platform, PLATFORM_STYLES["其他"])
                            icon = PLATFORM_ICONS.get(platform, "💗")
                            st.markdown(
                                f'<div class="cover" style="background:linear-gradient(115deg,{bg_a},{bg_b});">'
                                f'<span class="cover-icon">{icon}</span>'
                                f'<span class="cover-label">{esc(platform)}</span>'
                                f'</div><div class="work-title">{esc(title)}</div>'
                                f'<div class="work-date">收藏于 {esc(created_at)}</div>'
                                + (f'<div class="work-author">✍️ 作者：{esc(author)}</div>' if author else '')
                                + (''.join(f'<span class="work-cp">💞 {esc(one_cp)}</span> ' for one_cp in parse_cps(cp))),
                                unsafe_allow_html=True,
                            )
                            work_tags = parse_tags(tags)
                            if work_tags:
                                pills = "".join(f'<span class="tag"># {esc(tag)}</span>' for tag in work_tags[:12])
                                st.markdown(f'<div class="tags">{pills}</div>', unsafe_allow_html=True)
                            if note:
                                st.markdown(
                                    f'<div class="work-note">💭 {esc(note).replace(chr(10), "<br>")}</div>',
                                    unsafe_allow_html=True,
                                )
                            with st.expander("✏️ 修改 CP / 分类标签"):
                                with st.form(f"edit_tag_form_{work_id}"):
                                    # 升级后可能带有旧的 text_input 字符串状态，先转换成多选列表。
                                    edit_cp_key = f"edit_cp_{work_id}"
                                    if isinstance(st.session_state.get(edit_cp_key), str):
                                        st.session_state[edit_cp_key] = parse_cps(st.session_state[edit_cp_key])
                                    edited_cps = st.multiselect(
                                        "💞 CP 标签（可多选，也可新增）",
                                        options=cp_names,
                                        default=parse_cps(cp),
                                        accept_new_options=True,
                                        placeholder="选择已有 CP，或输入新 CP 后按回车",
                                        key=edit_cp_key,
                                    )
                                    edited_tags = st.multiselect(
                                        "🏷️ 选择或新增作品类型标签",
                                        options=category_names,
                                        default=work_tags,
                                        accept_new_options=True,
                                        placeholder="选择已有标签，或输入新标签后按回车",
                                        key=f"edit_tags_{work_id}",
                                    )
                                    st.caption("可同时选择多个标签；新标签输入后按回车确认。")
                                    if st.form_submit_button("保存 CP 和标签", use_container_width=True):
                                        normalized_tags = ", ".join(normalize_selected_tags(edited_tags, category_names))
                                        normalized_cps = ", ".join(normalize_selected_cps(edited_cps, cp_names))
                                        db.update_work_tags(work_id, normalized_tags, normalized_cps)
                                        st.session_state["edit_tag_notice"] = title
                                        st.rerun()
                            st.write("")
                            if valid_url(url):
                                left, right = st.columns([1.55, 1])
                                with left:
                                    st.link_button("↗ 阅读原作品", url, use_container_width=True)
                                with right:
                                    if st.button("🗑️ 删除", key=f"delete_{work_id}", use_container_width=True):
                                        st.session_state[f"confirm_{work_id}"] = True
                                        st.rerun()
                            else:
                                st.warning("此条记录的链接格式不正确。")
                                if st.button("🗑️ 删除", key=f"delete_{work_id}"):
                                    st.session_state[f"confirm_{work_id}"] = True
                                    st.rerun()

                            if st.session_state.get(f"confirm_{work_id}", False):
                                st.warning("确定删除这条收藏吗？删除后无法撤销。")
                                yes, no = st.columns(2)
                                with yes:
                                    if st.button("确认删除", key=f"yes_{work_id}", type="primary", use_container_width=True):
                                        db.delete_work(work_id)
                                        st.session_state.pop(f"confirm_{work_id}", None)
                                        st.session_state["delete_notice"] = True
                                        st.rerun()
                                with no:
                                    if st.button("取消", key=f"no_{work_id}", use_container_width=True):
                                        st.session_state.pop(f"confirm_{work_id}", None)
                                        st.rerun()

    if all_works:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["作品名", "作者", "CP", "平台", "链接", "标签", "笔记", "收藏时间"])
        for work_id, title, platform, url, tags, note, created_at, author, cp in sorted_by_platform_title(all_works):
            writer.writerow([title, author, cp, platform, url, tags, note, created_at])
        st.download_button(
            "📥 导出全部收藏（CSV 备份）",
            data=output.getvalue().encode("utf-8-sig"),
            file_name="我的同人收藏备份.csv",
            mime="text/csv",
        )

else:
    st.markdown('<div class="section-heading">添加一份新的心动 💌</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-sub">粘贴链接自动识别平台；CP 与作品类型标签都可以从历史选项多选，也可以新增。</div>', unsafe_allow_html=True)

    # 常用 CP 同样是可多选的标签列表。存入现有 settings 表，不必重建数据库。
    default_cp = get_setting("default_cp")
    default_cps = parse_cps(default_cp)
    if isinstance(st.session_state.get("setting_default_cp"), str):
        st.session_state["setting_default_cp"] = parse_cps(st.session_state["setting_default_cp"])
    with st.expander("⚙️ 设置常用 CP（只需设置一次）"):
        new_default_cps = st.multiselect(
            "我的常用 CP 标签（可多选）",
            options=cp_names,
            default=default_cps,
            accept_new_options=True,
            placeholder="选择历史 CP，或输入新 CP 后按回车",
            key="setting_default_cp",
        )
        if st.button("保存常用 CP", key="save_default_cp", use_container_width=True):
            saved_cps = normalize_selected_cps(new_default_cps, cp_names)
            set_setting("default_cp", ", ".join(saved_cps))
            st.session_state["new_work_cp"] = saved_cps
            st.session_state["default_cp_notice"] = True
            st.rerun()
        st.caption("选中的常用 CP 会在新增收藏时自动勾选；单篇作品仍可更改，也可以留空。")
        empty_cp_count = sum(not parse_cps(work[8]) for work in all_works)
        if default_cps and empty_cp_count:
            st.info(f"现有 {empty_cp_count} 篇旧收藏尚未填写独立 CP。旧标签会原样保留。")
            confirmed = st.checkbox(
                "我确认要把以上未填写 CP 的旧收藏归为这个常用 CP",
                key="confirm_fill_old_cp",
            )
            if st.button(
                "💞 给旧收藏补充常用 CP（只补空白）",
                key="fill_old_cp", disabled=not confirmed, use_container_width=True,
            ):
                db.fill_blank_cp(default_cp)
                st.session_state["fill_old_cp_notice"] = True
                st.rerun()
    if st.session_state.pop("fill_old_cp_notice", False):
        st.success("💞 已补充旧收藏的空白 CP，原标签、作者、链接均未修改。")
    if st.session_state.pop("default_cp_notice", False):
        st.success("💞 常用 CP 标签已保存！后续新收藏默认选中，也可以针对单篇修改。")

    # 保存成功后立即在本页清空输入。不要在输入控件已经创建后修改 Session State。
    if st.session_state.pop("clear_add_work", False):
        for key in (
            "new_work_url", "new_work_title", "new_work_note",
            "new_work_author", "new_work_author_origin",
        ):
            st.session_state[key] = ""
        # 多选标签必须重置为空列表，而不是旧版输入框使用的空字符串。
        st.session_state["new_work_tags"] = []
        st.session_state["new_work_cp"] = parse_cps(get_setting("default_cp"))
        st.session_state["override_platform"] = False
        st.session_state.pop("manual_platform", None)
    if st.session_state.pop("save_notice", False):
        st.success("💗 收藏成功！输入框已经清空，可以直接粘贴下一篇作品。")

    # 从旧版热更新时，旧 text_input 的 Session State 可能仍是字符串，转成多选所需的列表。
    if "new_work_cp" not in st.session_state:
        st.session_state["new_work_cp"] = parse_cps(get_setting("default_cp"))
    if isinstance(st.session_state.get("new_work_cp"), str):
        st.session_state["new_work_cp"] = parse_cps(st.session_state["new_work_cp"])
    previous_tags = st.session_state.get("new_work_tags")
    if isinstance(previous_tags, str):
        st.session_state["new_work_tags"] = parse_tags(previous_tags)

    with st.container(border=True):
        raw_url = st.text_input(
            "🔗 作品链接 *",
            placeholder="粘贴小红书、AO3、LOFTER、微博、抖音链接或整段分享文案",
            key="new_work_url", on_change=on_new_link_change,
        )
        clean_url = extract_url(raw_url)
        detected = infer_platform(clean_url) if clean_url else None

        if not raw_url.strip():
            st.caption("💡 先粘贴链接，下方会自动显示来源平台。")
        elif not clean_url or not valid_url(clean_url):
            st.warning("没有找到有效的 http:// 或 https:// 链接，请检查粘贴内容。")
        elif detected:
            st.success(f"{PLATFORM_ICONS.get(detected, '💗')} 已自动识别：{detected}")
        else:
            st.info("🔎 暂未识别来源，将按「其他」保存；如有需要可手动选择平台。")

        if clean_url and raw_url.strip() != clean_url:
            st.caption("已从分享文案中提取作品链接，保存时只会保存链接本身。")

        # 发现重复收藏就不重复请求网站，减少手机端等待。
        duplicate = find_duplicate(clean_url, all_works) if clean_url else None
        # 只在第一次识别某个链接时请求一次；Streamlit 缓存可避免重复加载。
        # 自动识别作者是辅助填充，用户仍然可以修改结果。
        author_found, author_source = "", ""
        if clean_url and valid_url(clean_url) and detected and not duplicate:
            author_found = author_from_share_text(raw_url, detected)
            if author_found:
                author_source = "分享文案"
            else:
                with st.spinner("正在尝试读取公开作者信息…"):
                    author_found, author_source = fetch_public_author(clean_url, detected)
            if author_found and not st.session_state.get("new_work_author", "").strip():
                st.session_state["new_work_author"] = author_found
                st.session_state["new_work_author_origin"] = author_source
        elif not st.session_state.get("new_work_author"):
            author_source = "未识别来源，支持手动填写"

        manual = st.checkbox("手动修改来源平台", key="override_platform")
        if manual:
            platform = st.selectbox("选择来源平台", PLATFORMS, key="manual_platform")
        else:
            platform = detected or "其他"

        left, right = st.columns([2, 1], gap="medium")
        with left:
            title = st.text_input(
                "作品名称 *", placeholder="给这篇作品起一个好记的名字", key="new_work_title"
            )
        with right:
            st.text_input("识别结果", value=platform, disabled=True)

        author = st.text_input(
            "✍️ 作者昵称（选填 · 自动识别失败时可手填）",
            placeholder="例如：作者在原平台显示的昵称",
            key="new_work_author",
        )
        if st.session_state.get("new_work_author_origin") and author:
            st.caption(f"✅ 作者来自{st.session_state['new_work_author_origin']}，请核对是否正确；可以直接修改。")
        elif clean_url and detected and not author:
            st.caption(f"💡 未能可靠识别作者（{author_source or '暂无公开信息'}）。手动填写即可，不影响收藏。")

        selected_cps = st.multiselect(
            "💞 CP 标签（可多选，也可新增）",
            options=cp_names,
            accept_new_options=True,
            placeholder="点选以前收藏过的 CP，或输入新 CP 后按回车",
            key="new_work_cp",
        )
        if get_setting("default_cp"):
            st.caption("💗 已自动选中你设置的常用 CP；这篇作品不同的话可取消或添加其他 CP。")
        else:
            st.caption("💗 收藏过的 CP 会自动出现在选项里，下次直接点击即可。")

        # 下拉选项来自已收藏作品：用户添加过的标签可以直接重复选择.
        # accept_new_options=True 允许输入全新标签后按回车创建，保存后会进入历史选项。
        tags = st.multiselect(
            "🏷️ 作品类型标签（可多选，也可新增）",
            options=[name for name, _ in categories],
            accept_new_options=True,
            placeholder="选择已有标签，或输入新标签后按回车",
            key="new_work_tags",
        )
        st.caption("💗 点击选择多个已有标签；想加新类型，直接输入名称并按回车。保存后下次就能直接选。")
        note = st.text_area(
            "我的私藏笔记（选填）",
            placeholder="为什么喜欢、印象深刻的情节、想重温的章节……",
            height=110,
            key="new_work_note",
        )

        if duplicate:
            st.warning(f"💌 重复收藏提醒：你已经保存过《{duplicate[1]}》（{duplicate[2]}）。")
            if valid_url(duplicate[3]):
                st.link_button("↗ 打开已收藏的作品", duplicate[3])
            st.caption("为避免重复，当前链接无法再次保存。可以到「我的作品馆」查找原收藏。")
        elif clean_url and valid_url(clean_url):
            st.caption("✅ 暂未发现重复链接，可以收藏。不同短链接与原作品长链接不一定能相互识别。")

        submitted = st.button(
            "💗 收进收藏馆",
            type="primary",
            use_container_width=True,
            disabled=bool(duplicate),
        )

    if submitted:
        title = title.strip()
        if not title:
            st.warning("请先填写作品名称。")
        elif not clean_url or not valid_url(clean_url):
            st.warning("请填写完整有效的作品链接。")
        else:
            # 保存前再次检查数据库，避免页面显示后发生的重复提交。
            latest = read_works()
            repeated = find_duplicate(clean_url, latest)
            if repeated:
                st.warning(f"💌 这条作品已经收藏过：《{repeated[1]}》，没有重复保存。")
            else:
                db.insert_work(
                    title,
                    platform,
                    clean_url,
                    ", ".join(normalize_selected_tags(tags, [name for name, _ in categories])),
                    note.strip(),
                    author.strip(),
                    ", ".join(normalize_selected_cps(selected_cps, cp_names)),
                )
                st.session_state["save_notice"] = True
                st.session_state["clear_add_work"] = True
                st.rerun()

st.markdown(
    '<div class="footnote">🌷 这是你的私人收藏馆。作品链接、分类和笔记保存在云端数据库（Supabase）中，'
    '只有你自己能看到；请定期用「导出 CSV」做备份。'
    '<br>🔐 账号由管理员通过邀请码发放；若忘记密码或需要更多邀请码，请联系管理员。'
    '<br>📱 手机与电脑都可访问同一个网址，数据实时同步到云端。'
    '<br>💞 CP 标签也支持历史多选、新增和常用 CP 默认勾选；与作品类型标签独立。旧 CP 数据会被正常识别。'
    '<br>🏷️ 作品类型标签可多选历史选项，也能输入新标签；一篇作品可属于多个分类。'
    '<br>作者自动识别只读取公开网页信息，部分网站可能拒绝访问，仍可手动填写；短链接与原链接不一定能互相查重。</div>',
    unsafe_allow_html=True,
)
