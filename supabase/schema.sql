-- ============================================================
-- BooksFor547 / Fandom Archive — Supabase 初始化 SQL
-- 使用方式：Supabase 控制台 → SQL Editor → New query → 粘贴 → Run
-- 可重复执行（幂等）
-- ============================================================

-- ------------------------------------------------------------
-- 1. 数据表
-- ------------------------------------------------------------
create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text,
  username text,
  role text not null default 'user' check (role in ('user', 'admin')),
  is_active boolean not null default true,
  created_at timestamptz not null default now()
);

create table if not exists public.works (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  title text not null,
  platform text not null default '其他',
  url text not null,
  tags text not null default '',
  note text not null default '',
  author text not null default '',
  cp text not null default '',
  is_seed boolean not null default false,
  created_at timestamptz not null default now()
);

create index if not exists works_user_id_idx on public.works (user_id);
create index if not exists works_user_created_idx on public.works (user_id, created_at desc);

create table if not exists public.user_settings (
  user_id uuid not null references auth.users(id) on delete cascade,
  key text not null,
  value text not null default '',
  updated_at timestamptz not null default now(),
  primary key (user_id, key)
);

create table if not exists public.invite_codes (
  code text primary key,
  max_uses integer not null default 10,
  used integer not null default 0,
  active boolean not null default true,
  created_at timestamptz not null default now()
);

-- ------------------------------------------------------------
-- 2. 内部辅助函数（放在 private schema，不对外暴露）
-- ------------------------------------------------------------
create schema if not exists private;

create or replace function private.is_admin()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select coalesce(
    (select p.role = 'admin' from public.profiles p where p.id = (select auth.uid())),
    false
  );
$$;

create or replace function private.is_active()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select coalesce(
    (select p.is_active from public.profiles p where p.id = (select auth.uid())),
    false
  );
$$;

revoke all on function private.is_admin() from public;
revoke all on function private.is_active() from public;
grant usage on schema private to authenticated;
grant execute on function private.is_admin() to authenticated;
grant execute on function private.is_active() to authenticated;

-- ------------------------------------------------------------
-- 3. 新用户初始化：校验邀请码 → 建档 → 复制 7 条初始化收藏
-- ------------------------------------------------------------
create or replace function public.seed_default_works(target uuid)
returns void
language sql
security definer
set search_path = ''
as $$
  insert into public.works
    (user_id, title, platform, url, tags, note, author, cp, is_seed)
  values
    (target, '本命是我哥', '小红书',
     'https://xhslink.cn/o/pRvoc3aBQe', '捡手机', '', '', '钎九', true),
    (target, '追网黄老婆只需死缠烂打', '小红书',
     'https://xhslink.cn/o/5gfUF4qTTDL', '捡手机', '', '', '钎九', true),
    (target, '童养媳', 'LOFTER',
     'https://xinjinjumin8280702.lofter.com/post/7716644d_34f51d475?incantation=rzsqvxMn0wLe',
     '', '', '', '', true),
    (target, '睡服小狐狸', 'AO3',
     'https://archiveofourown.org/works/48945748?view_adult=true', '搞笑', '', '', '钎九', true),
    (target, '猎狐行动', 'AO3',
     'https://archiveofourown.org/works/48945682', '刑侦', '刑侦', '', '钎九', true),
    (target, '青梅', 'AO3',
     'https://archiveofourown.org/works/89723571/chapters/244263366', '骨科', '真骨科', '', '钎九', true),
    (target, '尝尝鸡腿饭', '小红书',
     'https://xhslink.cn/o/9hLSFEzBhqv', '捡手机, 校园', '', '', '钎九', true);
$$;

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_code text;
  v_ok boolean;
begin
  v_code := lower(btrim(coalesce(new.raw_user_meta_data ->> 'invite_code', '')));

  select exists (
    select 1
    from public.invite_codes c
    where c.code = v_code
      and c.active
      and c.used < c.max_uses
  ) into v_ok;

  if not v_ok then
    raise exception 'INVITE_CODE_INVALID';
  end if;

  update public.invite_codes
     set used = used + 1
   where code = v_code;

  insert into public.profiles (id, email, username)
  values (
    new.id,
    new.email,
    coalesce(new.raw_user_meta_data ->> 'username', '')
  )
  on conflict (id) do nothing;

  perform public.seed_default_works(new.id);

  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
after insert on auth.users
for each row execute function public.handle_new_user();

-- ------------------------------------------------------------
-- 4. 行级安全（RLS）
-- ------------------------------------------------------------
alter table public.profiles      enable row level security;
alter table public.works         enable row level security;
alter table public.user_settings enable row level security;
alter table public.invite_codes  enable row level security;

revoke all on public.profiles      from anon, authenticated;
revoke all on public.works         from anon, authenticated;
revoke all on public.user_settings from anon, authenticated;
revoke all on public.invite_codes  from anon, authenticated;

grant select, update
  on public.profiles to authenticated;
grant select, insert, update, delete
  on public.works to authenticated;
grant select, insert, update, delete
  on public.user_settings to authenticated;
grant select, insert, update, delete
  on public.invite_codes to authenticated;

-- profiles：本人可读；管理员可读全部、可改全部（改 = 启用/停用/角色）
drop policy if exists profiles_select on public.profiles;
create policy profiles_select on public.profiles
for select to authenticated
using ( id = (select auth.uid()) or (select private.is_admin()) );

drop policy if exists profiles_update_admin on public.profiles;
create policy profiles_update_admin on public.profiles
for update to authenticated
using ( (select private.is_admin()) )
with check ( (select private.is_admin()) );

-- works：本人可读写自己的；被停用后不可读写；管理员可读写全部
drop policy if exists works_select on public.works;
create policy works_select on public.works
for select to authenticated
using (
  ( user_id = (select auth.uid()) and (select private.is_active()) )
  or (select private.is_admin())
);

drop policy if exists works_insert on public.works;
create policy works_insert on public.works
for insert to authenticated
with check (
  ( user_id = (select auth.uid()) and (select private.is_active()) )
  or (select private.is_admin())
);

drop policy if exists works_update on public.works;
create policy works_update on public.works
for update to authenticated
using (
  ( user_id = (select auth.uid()) and (select private.is_active()) )
  or (select private.is_admin())
)
with check (
  ( user_id = (select auth.uid()) and (select private.is_active()) )
  or (select private.is_admin())
);

drop policy if exists works_delete on public.works;
create policy works_delete on public.works
for delete to authenticated
using (
  ( user_id = (select auth.uid()) and (select private.is_active()) )
  or (select private.is_admin())
);

-- user_settings：本人可读写自己的；管理员可读写全部
drop policy if exists settings_select on public.user_settings;
create policy settings_select on public.user_settings
for select to authenticated
using ( user_id = (select auth.uid()) or (select private.is_admin()) );

drop policy if exists settings_insert on public.user_settings;
create policy settings_insert on public.user_settings
for insert to authenticated
with check ( user_id = (select auth.uid()) or (select private.is_admin()) );

drop policy if exists settings_update on public.user_settings;
create policy settings_update on public.user_settings
for update to authenticated
using ( user_id = (select auth.uid()) or (select private.is_admin()) )
with check ( user_id = (select auth.uid()) or (select private.is_admin()) );

drop policy if exists settings_delete on public.user_settings;
create policy settings_delete on public.user_settings
for delete to authenticated
using ( user_id = (select auth.uid()) or (select private.is_admin()) );

-- invite_codes：仅管理员可读写
drop policy if exists codes_select on public.invite_codes;
create policy codes_select on public.invite_codes
for select to authenticated
using ( (select private.is_admin()) );

drop policy if exists codes_insert on public.invite_codes;
create policy codes_insert on public.invite_codes
for insert to authenticated
with check ( (select private.is_admin()) );

drop policy if exists codes_update on public.invite_codes;
create policy codes_update on public.invite_codes
for update to authenticated
using ( (select private.is_admin()) )
with check ( (select private.is_admin()) );

drop policy if exists codes_delete on public.invite_codes;
create policy codes_delete on public.invite_codes
for delete to authenticated
using ( (select private.is_admin()) );

-- ------------------------------------------------------------
-- 5. 初始邀请码（可按需修改/新增）
-- ------------------------------------------------------------
insert into public.invite_codes (code, max_uses)
values ('booksfor547', 50)
on conflict (code) do nothing;
