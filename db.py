"""Supabase 数据访问层：认证、收藏、设置、管理员操作。

对外保持与原 SQLite 版本一致的返回值结构（元组顺序固定），
以便 app.py 的业务与界面代码不需要大改。

元组顺序：(id, title, platform, url, tags, note, created_at, author, cp)
"""

from __future__ import annotations

import csv
import io
import os
from datetime import datetime, timedelta, timezone

import streamlit as st
from supabase import create_client

CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- 配置
def _secret(name):
    try:
        value = st.secrets.get(name)
    except Exception:
        value = None
    return value or os.environ.get(name)


def _url():
    return _secret("SUPABASE_URL")


def _anon_key():
    return _secret("SUPABASE_ANON_KEY")


def configured():
    return bool(_url() and _anon_key())


def _client():
    if not configured():
        st.error(
            "缺少 SUPABASE_URL / SUPABASE_ANON_KEY。"
            "请在 Streamlit Cloud 的 Secrets 或本地 .streamlit/secrets.toml 中配置。"
        )
        st.stop()
    client = create_client(_url(), _anon_key())
    session = st.session_state.get("sb_session")
    if session:
        try:
            client.auth.set_session(session["access_token"], session["refresh_token"])
        except Exception:
            st.session_state.pop("sb_session", None)
    return client


# ---------------------------------------------------------------- 认证
def _store_session(session, user):
    st.session_state["sb_session"] = {
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
    }
    st.session_state["sb_user"] = {"id": user.id, "email": user.email}
    _load_profile(user.id)


def _load_profile(uid):
    data = (
        _client()
        .table("profiles")
        .select("id,email,username,role,is_active")
        .eq("id", uid)
        .execute()
        .data
    )
    profile = data[0] if data else {"id": uid, "role": "user", "is_active": True}
    st.session_state["sb_profile"] = profile
    return profile


def sign_up(email, password, invite_code):
    client = _client()
    result = client.auth.sign_up(
        {
            "email": email.strip(),
            "password": password,
            "options": {"data": {"invite_code": invite_code.strip()}},
        }
    )
    if result.session and result.user:
        _store_session(result.session, result.user)
    return result


def sign_in(email, password):
    client = _client()
    result = client.auth.sign_in_with_password(
        {"email": email.strip(), "password": password}
    )
    if result.session and result.user:
        _store_session(result.session, result.user)
    return result


def sign_out():
    try:
        _client().auth.sign_out()
    except Exception:
        pass
    for key in ("sb_session", "sb_user", "sb_profile"):
        st.session_state.pop(key, None)


def logged_in():
    return bool(st.session_state.get("sb_session"))


def current_uid():
    user = st.session_state.get("sb_user") or {}
    uid = user.get("id")
    if not uid:
        st.session_state.clear()
        st.warning("登录已失效，请重新登录。")
        st.stop()
    return uid


def profile():
    prof = st.session_state.get("sb_profile")
    if not prof:
        prof = _load_profile(current_uid())
    return prof


def is_admin():
    return (profile() or {}).get("role") == "admin"


def is_active():
    return (profile() or {}).get("is_active", True)


# ---------------------------------------------------------------- 收藏
def _fmt_time(value):
    if not value:
        return ""
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(CST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)[:16].replace("T", " ")


def _to_row(item):
    return (
        item["id"],
        item["title"],
        item["platform"],
        item["url"],
        item.get("tags") or "",
        item.get("note") or "",
        _fmt_time(item.get("created_at")),
        item.get("author") or "",
        item.get("cp") or "",
    )


_WORK_FIELDS = "id,title,platform,url,tags,note,created_at,author,cp"


def read_works():
    data = (
        _client()
        .table("works")
        .select(_WORK_FIELDS)
        .order("id", desc=True)
        .execute()
        .data
        or []
    )
    return [_to_row(item) for item in data]


def insert_work(title, platform, url, tags, note, author, cp):
    uid = current_uid()
    _client().table("works").insert(
        {
            "user_id": uid,
            "title": title,
            "platform": platform,
            "url": url,
            "tags": tags,
            "note": note,
            "author": author,
            "cp": cp,
        }
    ).execute()


def update_work_tags(work_id, tags, cp):
    _client().table("works").update({"tags": tags, "cp": cp}).eq("id", work_id).execute()


def delete_work(work_id):
    _client().table("works").delete().eq("id", work_id).execute()


def fill_blank_cp(cp_value):
    uid = current_uid()
    table = _client().table("works")
    table.update({"cp": cp_value}).eq("user_id", uid).is_("cp", "null").execute()
    table.update({"cp": cp_value}).eq("user_id", uid).eq("cp", "").execute()


# ---------------------------------------------------------------- 设置
def get_setting(key, fallback=""):
    data = (
        _client()
        .table("user_settings")
        .select("value")
        .eq("user_id", current_uid())
        .eq("key", key)
        .execute()
        .data
        or []
    )
    return data[0]["value"] if data else fallback


def set_setting(key, value):
    _client().table("user_settings").upsert(
        {"user_id": current_uid(), "key": key, "value": value},
        on_conflict="user_id,key",
    ).execute()


# ---------------------------------------------------------------- 管理员
def admin_list_profiles():
    data = (
        _client()
        .table("profiles")
        .select("id,email,username,role,is_active,created_at")
        .order("created_at", desc=False)
        .execute()
        .data
        or []
    )
    for item in data:
        item["created_at_fmt"] = _fmt_time(item.get("created_at"))
    return data


def admin_set_active(uid, active):
    _client().table("profiles").update({"is_active": active}).eq("id", uid).execute()


def admin_set_role(uid, role):
    _client().table("profiles").update({"role": role}).eq("id", uid).execute()


def admin_list_works(email_map=None):
    data = (
        _client()
        .table("works")
        .select(_WORK_FIELDS + ",user_id,is_seed")
        .order("user_id", desc=False)
        .order("id", desc=True)
        .execute()
        .data
        or []
    )
    emails = email_map or {}
    for item in data:
        item["owner"] = emails.get(item.get("user_id"), item.get("user_id"))
        item["created_at_fmt"] = _fmt_time(item.get("created_at"))
    return data


def admin_delete_work(work_id):
    _client().table("works").delete().eq("id", work_id).execute()


def admin_update_work(work_id, tags, cp):
    _client().table("works").update({"tags": tags, "cp": cp}).eq("id", work_id).execute()


def admin_list_invite_codes():
    data = (
        _client()
        .table("invite_codes")
        .select("code,max_uses,used,active,created_at")
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )
    for item in data:
        item["created_at_fmt"] = _fmt_time(item.get("created_at"))
    return data


def admin_add_invite_code(code, max_uses=10):
    _client().table("invite_codes").insert(
        {"code": code.strip().lower(), "max_uses": int(max_uses)}
    ).execute()


def admin_set_code_active(code, active):
    _client().table("invite_codes").update({"active": active}).eq("code", code).execute()


def admin_export_csv(profiles, works):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["用户邮箱", "作品名", "作者", "CP", "平台", "链接", "标签", "笔记", "收藏时间", "是否初始化"]
    )
    for item in works:
        writer.writerow(
            [
                item.get("owner") or "",
                item.get("title") or "",
                item.get("author") or "",
                item.get("cp") or "",
                item.get("platform") or "",
                item.get("url") or "",
                item.get("tags") or "",
                item.get("note") or "",
                item.get("created_at_fmt") or "",
                "是" if item.get("is_seed") else "否",
            ]
        )
    return output.getvalue()
