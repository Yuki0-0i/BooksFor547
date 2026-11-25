"""管理后台：用户管理、全站收藏管理、邀请码、导出。"""

import streamlit as st

import db

st.set_page_config(
    page_title="管理后台 · Fandom Archive",
    page_icon="🛠️",
    layout="wide",
)

if not db.configured():
    st.error("缺少 Supabase 配置：请设置 SUPABASE_URL 与 SUPABASE_ANON_KEY。")
    st.stop()

if not db.logged_in():
    st.warning("请先在主页登录，再进入管理后台。")
    st.page_link("app.py", label="← 返回主页")
    st.stop()

if not db.is_admin():
    st.error("仅管理员可以访问此页面。")
    st.page_link("app.py", label="← 返回主页")
    st.stop()

me = st.session_state.get("sb_user", {})

st.title("🛠️ 管理后台")
st.caption("管理用户与收藏、发放邀请码、导出全站数据。")

profiles = db.admin_list_profiles()
email_map = {item["id"]: item.get("email") or item["id"] for item in profiles}
works = db.admin_list_works(email_map)

col1, col2, col3, col4 = st.columns(4)
col1.metric("用户数", len(profiles))
col2.metric("收藏总数", len(works))
col3.metric("已停用账号", sum(1 for p in profiles if not p.get("is_active")))
col4.metric("管理员数", sum(1 for p in profiles if p.get("role") == "admin"))

tab_users, tab_works, tab_codes, tab_export = st.tabs(
    ["👥 用户", "📚 全部收藏", "🎟️ 邀请码", "📥 导出"]
)

# ------------------------------------------------------------------ 用户
with tab_users:
    if not profiles:
        st.info("还没有用户。让第一位用户用邀请码注册后，这里就会出现。")
    for item in profiles:
        uid = item["id"]
        is_self = uid == me.get("id")
        with st.container(border=True):
            row = st.columns([3.2, 1.2, 1.2, 2])
            with row[0]:
                st.markdown(f"**{item.get('email') or '（无邮箱）'}**")
                st.caption(f"注册于 {item.get('created_at_fmt') or '未知'}")
            with row[1]:
                st.caption("角色")
                st.write("🛠️ 管理员" if item.get("role") == "admin" else "👤 普通用户")
            with row[2]:
                st.caption("状态")
                st.write("✅ 正常" if item.get("is_active") else "🚫 已停用")
            with row[3]:
                if is_self:
                    st.caption("这是你自己，不能停用或降级。")
                else:
                    new_active = st.checkbox(
                        "启用账号", value=bool(item.get("is_active")), key=f"active_{uid}"
                    )
                    if new_active != bool(item.get("is_active")):
                        db.admin_set_active(uid, new_active)
                        st.rerun()

                    new_role = st.selectbox(
                        "角色",
                        ["user", "admin"],
                        index=0 if item.get("role") != "admin" else 1,
                        key=f"role_{uid}",
                        format_func=lambda r: "普通用户" if r == "user" else "管理员",
                    )
                    if new_role != item.get("role"):
                        db.admin_set_role(uid, new_role)
                        st.rerun()

    if st.button("🔄 刷新用户列表"):
        st.rerun()

# ------------------------------------------------------------------ 收藏
with tab_works:
    st.caption("可以删除任意用户的作品，或修改其 CP / 标签。")
    if not works:
        st.info("暂时没有任何收藏。")
    keyword = st.text_input("按标题 / 邮箱 / 标签搜索", key="admin_work_search")
    key = keyword.strip().casefold()

    shown = 0
    for item in works:
        text = " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("owner") or ""),
                str(item.get("tags") or ""),
                str(item.get("cp") or ""),
            ]
        ).casefold()
        if key and key not in text:
            continue
        shown += 1
        with st.container(border=True):
            st.markdown(f"**{item.get('title')}**")
            st.caption(
                f"👤 {item.get('owner')} · {item.get('platform')} · "
                f"收藏于 {item.get('created_at_fmt')}"
                + (" · 初始化数据" if item.get("is_seed") else "")
            )
            if item.get("tags"):
                st.caption(f"🏷️ {item.get('tags')}")
            if item.get("cp"):
                st.caption(f"💞 {item.get('cp')}")
            if item.get("url"):
                st.link_button("↗ 打开原作品", item["url"])

            with st.expander("编辑 CP / 标签"):
                with st.form(f"admin_edit_{item['id']}"):
                    new_tags = st.text_input(
                        "标签（逗号分隔）", value=item.get("tags") or ""
                    )
                    new_cp = st.text_input("CP（逗号分隔）", value=item.get("cp") or "")
                    saved = st.form_submit_button("保存")
                if saved:
                    db.admin_update_work(item["id"], new_tags.strip(), new_cp.strip())
                    st.rerun()

            if st.button("🗑️ 删除这条收藏", key=f"admin_del_{item['id']}"):
                db.admin_delete_work(item["id"])
                st.rerun()

    st.caption(f"当前显示 {shown} / {len(works)} 条")

# ------------------------------------------------------------------ 邀请码
with tab_codes:
    st.caption("注册时必须填写有效邀请码。每个邀请码有可用次数。")
    with st.form("add_code_form"):
        cols = st.columns([2, 1])
        with cols[0]:
            new_code = st.text_input("新邀请码", placeholder="例如 booksfor547")
        with cols[1]:
            max_uses = st.number_input("可用次数", min_value=1, max_value=10000, value=10)
        added = st.form_submit_button("添加邀请码", use_container_width=True)
    if added:
        if not new_code.strip():
            st.warning("邀请码不能为空。")
        else:
            try:
                db.admin_add_invite_code(new_code, max_uses)
                st.success(f"已添加邀请码：{new_code.strip().lower()}")
                st.rerun()
            except Exception as ex:
                st.error(f"添加失败（可能已存在）：{ex}")

    codes = db.admin_list_invite_codes()
    if not codes:
        st.info("还没有邀请码。")
    for code in codes:
        with st.container(border=True):
            row = st.columns([2.2, 1.4, 1.2, 1.6])
            with row[0]:
                st.markdown(f"**`{code['code']}`**")
                st.caption(f"创建于 {code.get('created_at_fmt') or '未知'}")
            with row[1]:
                st.caption("已用 / 上限")
                st.write(f"{code.get('used', 0)} / {code.get('max_uses')}")
            with row[2]:
                st.caption("状态")
                st.write("✅ 可用" if code.get("active") else "🚫 已停用")
            with row[3]:
                new_state = st.checkbox(
                    "启用", value=bool(code.get("active")), key=f"code_{code['code']}"
                )
                if new_state != bool(code.get("active")):
                    db.admin_set_code_active(code["code"], new_state)
                    st.rerun()

# ------------------------------------------------------------------ 导出
with tab_export:
    st.caption("导出全站收藏为 CSV（含所属用户邮箱）。")
    csv_text = db.admin_export_csv(profiles, works)
    st.download_button(
        "📥 下载全站收藏 CSV",
        data=csv_text.encode("utf-8-sig"),
        file_name="booksfor547_all_works.csv",
        mime="text/csv",
    )
    st.code(
        "用户邮箱,作品名,作者,CP,平台,链接,标签,笔记,收藏时间,是否初始化",
        language="text",
    )

st.divider()
st.page_link("app.py", label="← 返回我的收藏馆")
