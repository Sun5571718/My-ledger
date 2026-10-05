# -*- coding: utf-8 -*-
"""
💰 客户欠款台账 · 最终版（Supabase 云端 · v9）
=========================================================
本版相对 v8 的两处升级（都是为了部署到公网）：
  ① 🔒 访问密码：打开页面要先输密码，别人拿到网址也看不到客户信息
     · 密码写在 .streamlit/secrets.toml 的 APP_PASSWORD 里（不写进代码）
     · 支持用网址参数记住（?k=密码），方便存到手机主屏幕
  ② 📷 照片改存云端 Storage（桶名 photos）
     · 不然部署后照片存在服务器临时磁盘，一重启就没了
     · USE_CLOUD = False 时仍然存本机 photos/ 文件夹

开关：
    USE_CLOUD = True   云端（默认）
    USE_CLOUD = False  本地 CSV + 本机照片

运行：python -m streamlit run app.py
"""

from __future__ import annotations

import io
import re
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import json
import zipfile

try:
    import altair as alt
except ImportError:
    alt = None

st.set_page_config(page_title="客户欠款台账", page_icon="💰", layout="wide")


# =====================================================================
# 0. 全局设置
# =====================================================================
USE_CLOUD = True

TABLE = "ledger"                 # 数据表
BUCKET = "photos"                # 存照片的桶
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_FILE = DATA_DIR / "ledger.csv"
PHOTO_DIR = BASE_DIR / "photos"
MAX_PHOTOS = 6

DB_TO_CN = {
    "customer": "客户名称",
    "debt_amount": "欠款金额",
    "paid_amount": "已收金额",
    "debt_date": "欠款日期",
    "last_paid": "最后收款时间",
    "note": "备注",
    "place": "客户位置",
    "photos": "照片",
}

ID_COL = "id"
TEXT_FIELDS = ["客户名称", "备注", "客户位置", "照片"]
MONEY_FIELDS = ["欠款金额", "已收金额"]
DATE_FIELDS = ["欠款日期", "最后收款时间"]
FIELDS = ["客户名称", "欠款金额", "已收金额", "欠款日期", "最后收款时间", "备注", "客户位置", "照片"]
EDITABLE_FIELDS = ["客户名称", "欠款金额", "已收金额", "欠款日期", "最后收款时间", "备注", "客户位置"]


class CloudError(RuntimeError):
    """云端出问题时抛这个，界面负责显示成人话。"""


# =====================================================================
# 1. 数据层 A：Supabase（数据库 + 照片存储）
# =====================================================================
def get_secret(name: str, default: str = "") -> str:
    try:
        value = st.secrets.get(name, default)
    except Exception:
        return default
    return str(value) if value else default


@st.cache_resource(show_spinner=False)
def _make_client(url: str, key: str):
    from supabase import create_client
    return create_client(url, key)


def sb_client():
    url = get_secret("SUPABASE_URL")
    key = get_secret("SUPABASE_ANON_KEY")
    if not url or not key:
        raise CloudError("没找到 SUPABASE_URL / SUPABASE_ANON_KEY，"
                         "请检查 .streamlit/secrets.toml 的文件夹名、文件名和内容")
    try:
        from supabase import create_client  # noqa: F401
    except ImportError as exc:
        raise CloudError("还没安装 supabase 库，请先运行：pip install supabase") from exc
    try:
        return _make_client(url, key)
    except Exception as exc:
        raise CloudError(f"连接 Supabase 失败：{exc}") from exc


def load_cloud() -> pd.DataFrame:
    client = sb_client()
    try:
        res = client.table(TABLE).select("*").order("id").execute()
    except Exception as exc:
        raise CloudError(f"读取云端数据失败：{exc}") from exc
    rows = res.data or []
    if not rows:
        return empty_df()
    return normalize(pd.DataFrame(rows).rename(columns=DB_TO_CN))


def _iso_or_none(value):
    return None if pd.isna(value) else pd.Timestamp(value).strftime("%Y-%m-%d")


def _to_db_record(row) -> dict:
    return {
        "customer": str(row["客户名称"]).strip(),
        "debt_amount": to_float(row["欠款金额"]),
        "paid_amount": to_float(row["已收金额"]),
        "debt_date": _iso_or_none(row["欠款日期"]),
        "last_paid": _iso_or_none(row["最后收款时间"]),
        "note": "" if pd.isna(row["备注"]) else str(row["备注"]),
        "place": "" if pd.isna(row["客户位置"]) else str(row["客户位置"]),
        "photos": "" if pd.isna(row["照片"]) else str(row["照片"]),
    }


def save_cloud(df: pd.DataFrame) -> None:
    """① 先记云端 id → ② 新行 insert / 老行 upsert → ③ 删掉云端多出来的行。"""
    client = sb_client()
    df = normalize(df)

    try:
        res = client.table(TABLE).select("id").execute()
        db_ids = {int(r["id"]) for r in (res.data or [])}
    except Exception as exc:
        raise CloudError(f"读取云端 id 失败：{exc}") from exc

    inserts, updates, keep = [], [], set()
    for _, row in df.iterrows():
        rid = int(row[ID_COL])
        record = _to_db_record(row)
        if rid > 0:
            keep.add(rid)
            updates.append(dict(record, id=rid))
        else:
            inserts.append(record)

    try:
        if inserts:
            client.table(TABLE).insert(inserts).execute()
        if updates:
            client.table(TABLE).upsert(updates, on_conflict="id").execute()
        gone = sorted(db_ids - keep)
        if gone:
            client.table(TABLE).delete().in_("id", gone).execute()
    except Exception as exc:
        raise CloudError(f"写入云端失败：{exc}") from exc


# =====================================================================
# 2. 数据层 B：本地 CSV（USE_CLOUD = False 时用）
# =====================================================================
def load_csv() -> pd.DataFrame:
    if not DATA_FILE.exists():
        return empty_df()
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            df = normalize(pd.read_csv(DATA_FILE, encoding=enc))
            df[ID_COL] = range(1, len(df) + 1)
            return df
        except Exception:
            continue
    return empty_df()


def save_csv(df: pd.DataFrame) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = with_unpaid(normalize(df))[FIELDS + ["未付金额"]].copy()
    for col in DATE_FIELDS:
        out[col] = pd.to_datetime(out[col], errors="coerce").dt.strftime("%Y-%m-%d")
    out.to_csv(DATA_FILE, index=False, encoding="utf-8-sig")


# =====================================================================
# 3. 公共数据工具
# =====================================================================
def empty_df() -> pd.DataFrame:
    return pd.DataFrame({
        ID_COL: pd.Series(dtype="int64"),
        "客户名称": pd.Series(dtype="object"),
        "欠款金额": pd.Series(dtype="float64"),
        "已收金额": pd.Series(dtype="float64"),
        "欠款日期": pd.Series(dtype="datetime64[ns]"),
        "最后收款时间": pd.Series(dtype="datetime64[ns]"),
        "备注": pd.Series(dtype="object"),
        "客户位置": pd.Series(dtype="object"),
        "照片": pd.Series(dtype="object"),
    })


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if ID_COL not in df.columns:
        df[ID_COL] = 0
    for col in FIELDS:
        if col not in df.columns:
            df[col] = pd.NA
    df = df[[ID_COL] + FIELDS]
    df[ID_COL] = pd.to_numeric(df[ID_COL], errors="coerce").fillna(0).astype("int64")
    for col in TEXT_FIELDS:
        df[col] = df[col].fillna("").astype(str).str.strip()
    for col in MONEY_FIELDS:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0).round(2)
    for col in DATE_FIELDS:
        df[col] = pd.to_datetime(df[col], errors="coerce")
    df = df[df["客户名称"] != ""]
    return df.reset_index(drop=True)


def load_data() -> pd.DataFrame:
    if USE_CLOUD:
        try:
            st.session_state["cloud_error"] = ""
            return load_cloud()
        except CloudError as exc:
            st.session_state["cloud_error"] = str(exc)
            return empty_df()
    return load_csv()


def save_data(df: pd.DataFrame) -> bool:
    if USE_CLOUD:
        try:
            st.session_state["cloud_error"] = ""
            save_cloud(df)
            return True
        except CloudError as exc:
            st.session_state["cloud_error"] = str(exc)
            return False
    save_csv(df)
    return True


def read_csv_bytes(raw: bytes) -> pd.DataFrame | None:
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            df = normalize(pd.read_csv(io.BytesIO(raw), encoding=enc))
            df[ID_COL] = 0
            return df
        except Exception:
            continue
    return None


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    out = with_unpaid(df)[FIELDS + ["未付金额"]].copy()
    for col in DATE_FIELDS:
        out[col] = pd.to_datetime(out[col], errors="coerce").dt.strftime("%Y-%m-%d")
    # 云端模式：多导出一列"照片链接" —— 以后翻备份，点链接就能看到当时的照片
    if USE_CLOUD and "照片" in out.columns:
        def _links(raw):
            urls = []
            for _n in split_photos(raw):
                _u = photo_src(_n)
                if _u:
                    urls.append(str(_u))
            return "  ".join(urls)
        out["照片链接"] = out["照片"].apply(_links)
    return out.to_csv(index=False).encode("utf-8-sig")


def with_unpaid(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["未付金额"] = (out["欠款金额"] - out["已收金额"]).round(2)
    return out


def to_float(x) -> float:
    try:
        if pd.isna(x):
            return 0.0
        return float(x)
    except Exception:
        return 0.0


def unpaid_days(when) -> float:
    if pd.isna(when):
        return float("nan")
    return float(max((pd.Timestamp(date.today()) - pd.Timestamp(when)).days, 0))


def days_text(row) -> str:
    if to_float(row["未付金额"]) <= 0:
        return "已结清"
    days = unpaid_days(row["欠款日期"])
    if pd.isna(days):
        return "日期不详"
    return f"{int(days)} 天"


# 那个早就没用的「客户位置」列，现在拿来存这位客户的往来流水（不用改数据库）
LOG_COL = "客户位置"


def load_log(row) -> list:
    """读出一位客户的流水：一串 {"d": 日期, "t": 欠/收, "v": 金额}"""
    raw = str(row.get(LOG_COL, "") or "").strip()
    if not raw.startswith("["):
        return []
    try:
        items = json.loads(raw)
    except Exception:
        return []
    return items if isinstance(items, list) else []


def dump_log(items: list) -> str:
    return json.dumps(items, ensure_ascii=False) if items else ""


def add_log(row, kind: str, amount: float, when=None) -> str:
    """往流水里加一条：kind 是「欠」或「收」"""
    items = load_log(row)
    items.append({"d": str(when if when is not None else date.today()),
                  "t": kind, "v": round(float(amount), 2)})
    return dump_log(items)


def money_short(v) -> str:
    """手机上一眼能看懂的金额：1 万及以上用「万」（保留 1 位小数），1 万以下显示元。"""
    v = to_float(v)
    if abs(v) >= 10000:
        return f"¥{v / 10000:,.2f}万"      # 1 万以上：用"万"，保留两位小数
    return f"¥{v:,.2f}"                     # 1 万以下：原样，保留两位小数


def days_badge(row) -> str:
    """未回款天数配个颜色：绿=30天内，黄=31~60，橙=61~90，红=90天以上。"""
    if to_float(row["未付金额"]) <= 0:
        return "✅"                      # 已结清
    d = unpaid_days(row["欠款日期"])
    if pd.isna(d):
        return "⚪"
    d = int(d)
    if d <= 30:
        return "🟢"
    if d <= 60:
        return "🟡"
    if d <= 90:
        return "🟠"
    return "🔴"


def mask_name(name: str) -> str:
    text = str(name)
    if len(text) <= 1:
        return text
    return text[0] + "*" * (len(text) - 1)


def merge_edits(ledger: pd.DataFrame, base: pd.DataFrame, edited: pd.DataFrame,
                ids: list, apply_delete: bool = False):
    """按 id 找行合并。先改后删。"""
    result = ledger.copy()
    pos_of = {int(v): i for i, v in enumerate(result[ID_COL])}

    for i in range(len(base)):
        rid = int(ids[i])
        if rid not in pos_of:
            continue
        p = pos_of[rid]
        row = edited.iloc[i]
        name = str(row["客户名称"]).strip()
        if not name:
            continue
        result.at[p, "客户名称"] = name
        for col in EDITABLE_FIELDS:
            if col == "客户名称" or col not in edited.columns:
                continue
            value = row[col]
            if col in MONEY_FIELDS:
                result.at[p, col] = to_float(value)
            elif col in DATE_FIELDS:
                result.at[p, col] = pd.to_datetime(value) if pd.notna(value) else pd.NaT
            else:
                result.at[p, col] = "" if pd.isna(value) else str(value).strip()

    deleted = 0
    if apply_delete and "删除" in edited.columns:
        drop_ids = {int(ids[i]) for i in range(len(base)) if bool(edited.iloc[i]["删除"])}
        if drop_ids:
            before = len(result)
            result = result[~result[ID_COL].isin(drop_ids)]
            deleted = before - len(result)

    return result.reset_index(drop=True), deleted


def signature(df: pd.DataFrame) -> list:
    out = df.copy()
    for col in DATE_FIELDS:
        out[col] = pd.to_datetime(out[col], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
    return ["|".join(str(r[c]) for c in FIELDS) for _, r in out.iterrows()]


# ---------------- 照片：云端 / 本地，两套后端同一个接口 ----------------
def safe_name(text: str, limit: int = 16) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", str(text)).strip("_")
    return (cleaned or "客户")[:limit]


def _mime_of(suffix: str) -> str:
    s = suffix.lower()
    if s in (".png",):
        return "image/png"
    if s in (".webp",):
        return "image/webp"
    return "image/jpeg"


def shrink_image(data: bytes, suffix: str):
    """
    ⚡ 提速关键：手机拍的照片常有 3~5MB，上传要等很久。
    先压缩到最长边 1600 像素、JPEG 质量 80，通常只剩 300KB 左右，快 10 倍。
    压不了就原样返回，绝不因为压缩失败而存不上。
    """
    if len(data) < 200_000:          # 本来就小，不动它
        return data, suffix
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > 1280:                 # 最长边压到 1280 像素（手机上够清楚）
            scale = 1280 / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=72, optimize=True)
        small = out.getvalue()
        if len(small) >= len(data):          # 万一压完反而更大，就用原来的
            return data, suffix
        return small, ".jpg"
    except Exception:
        return data, suffix


def save_photo(customer: str, data: bytes, suffix: str = ".jpg") -> str:
    """存一张照片：云端模式传进 Storage，本地模式写进 photos/。返回文件名。"""
    data, suffix = shrink_image(data, suffix)
    suffix = suffix if suffix.startswith(".") else "." + suffix
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]

    if USE_CLOUD:
        # ⚠️ 云端存储的文件名只能用英文/数字/下划线这类字符，
        #    用中文名（张老板_xxx.png）会报 InvalidKey。
        #    所以云端用随机文件名 —— 照片属于哪个客户，记在数据库的 photos 列里。
        filename = f"p_{stamp}_{secrets.token_hex(3)}{suffix.lower()}"
        client = sb_client()
        try:
            client.storage.from_(BUCKET).upload(
                filename, data, {"content-type": _mime_of(suffix), "upsert": "true"})
        except Exception as exc:
            raise CloudError(f"照片上传失败：{exc}") from exc
    else:
        filename = f"{safe_name(customer)}_{stamp}{suffix.lower()}"
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        (PHOTO_DIR / filename).write_bytes(data)

    return filename


def photo_src(filename: str):
    """给 st.image 用的地址：云端给网址，本地给文件路径；都没有就 None。"""
    if not filename:
        return None
    if USE_CLOUD:
        try:
            return sb_client().storage.from_(BUCKET).get_public_url(str(filename))
        except Exception:
            return None
    path = PHOTO_DIR / str(filename)
    return str(path) if path.exists() else None


def delete_photo_file(filename: str) -> None:
    """删掉照片文件本身（云端的从桶里删，本地的从文件夹删）。"""
    if not filename:
        return
    if USE_CLOUD:
        try:
            sb_client().storage.from_(BUCKET).remove([str(filename)])
        except Exception:
            pass
    else:
        try:
            (PHOTO_DIR / str(filename)).unlink()
        except Exception:
            pass


def split_photos(value) -> list:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    return [x.strip() for x in str(value).split("|") if x.strip()]


def join_photos(items: list) -> str:
    return "|".join([str(x) for x in items if str(x).strip()])


def sample_data() -> pd.DataFrame:
    today = date.today()
    rows = [
        {"客户名称": "张老板", "欠款金额": 12000.00, "已收金额": 5000.00,
         "欠款日期": today - timedelta(days=12), "最后收款时间": today - timedelta(days=5),
         "备注": "首批货款", "客户位置": "城南建材市场 3 号门市", "照片": ""},
        {"客户名称": "李经理", "欠款金额": 8600.50, "已收金额": 0.00,
         "欠款日期": today - timedelta(days=45), "最后收款时间": None,
         "备注": "上月余款", "客户位置": "开发区物流园 B 区", "照片": ""},
        {"客户名称": "李四", "欠款金额": 4200.00, "已收金额": 200.00,
         "欠款日期": today - timedelta(days=25), "最后收款时间": today - timedelta(days=10),
         "备注": "五金件", "客户位置": "城北五金城 12 号", "照片": ""},
        {"客户名称": "王姐", "欠款金额": 3000.00, "已收金额": 3000.00,
         "欠款日期": today - timedelta(days=70), "最后收款时间": today - timedelta(days=3),
         "备注": "已结清", "客户位置": "", "照片": ""},
        {"客户名称": "赵总", "欠款金额": 25000.00, "已收金额": 8000.00,
         "欠款日期": today - timedelta(days=120), "最后收款时间": today - timedelta(days=30),
         "备注": "设备尾款", "客户位置": "县城工业园 7 号厂房", "照片": ""},
        {"客户名称": "陈师傅", "欠款金额": 1500.00, "已收金额": 0.00,
         "欠款日期": today - timedelta(days=8), "最后收款时间": None,
         "备注": "零星欠款", "客户位置": "", "照片": ""},
    ]
    return normalize(pd.DataFrame(rows))


# =====================================================================
# 3.5 🔒 访问密码（部署到公网后，防止别人打开看到客户信息）
# =====================================================================
APP_PASSWORD = get_secret("APP_PASSWORD")

if APP_PASSWORD:
    try:
        remembered = st.query_params.get("k", "")
    except Exception:
        remembered = ""
    if "auth" not in st.session_state:
        st.session_state["auth"] = (remembered == APP_PASSWORD)

    if not st.session_state["auth"]:
        st.title("🔒 客户欠款台账")
        st.caption("请输入访问密码")
        pw = st.text_input("密码", type="password", key="pw_input")
        if st.button("进入", type="primary"):
            if pw == APP_PASSWORD:
                st.session_state["auth"] = True
                try:
                    st.query_params["k"] = pw          # 写进网址，方便存到手机主屏幕
                except Exception:
                    pass
                st.rerun()
            else:
                st.error("密码不对，再试试")
        st.stop()          # 没通过就不再往下执行，数据一个字都不会读


# =====================================================================
# 4. 界面层
# =====================================================================
if "ledger" not in st.session_state:
    st.session_state["ledger"] = load_data()
if "editor_key" not in st.session_state:
    st.session_state["editor_key"] = 0
if "photo_key" not in st.session_state:
    st.session_state["photo_key"] = 0
if "pending_search" not in st.session_state:
    st.session_state["pending_search"] = ""
if "show_add" not in st.session_state:
    st.session_state["show_add"] = False        # 默认收起：不点「➕ 添加客户」就不展开
if "mask_names" not in st.session_state:
    st.session_state["mask_names"] = False
if "clear_search" not in st.session_state:
    st.session_state["clear_search"] = False
if "cloud_error" not in st.session_state:
    st.session_state["cloud_error"] = ""
if "flash" not in st.session_state:
    st.session_state["flash"] = ""


def set_flash(msg: str) -> None:
    st.session_state["flash"] = msg


def update_ledger(df: pd.DataFrame, msg: str = "", force_reload: bool = False) -> None:
    """
    ⚡ 提速关键：只有「新增的行」才需要从云端重新拉一遍（为了拿云端发的 id）；
    普通编辑（改金额、备注…）直接本地更新，省掉一次跨洋往返，点起来跟手很多。
    收钱/欠款这类要立刻看到新数字的，传 force_reload=True，保证和云端一致。
    """
    df = normalize(df)
    need_reload = force_reload or (USE_CLOUD and bool((df[ID_COL] == 0).any()))
    if save_data(df):
        st.session_state["ledger"] = load_data() if need_reload else df
    else:
        st.session_state["ledger"] = df
        set_flash("⚠️ 云端保存失败，改动暂时只在本地")
    st.session_state["editor_key"] += 1
    if msg:
        set_flash(msg)


COLUMN_CONFIG = {
    "客户名称": st.column_config.TextColumn("客户名称"),
    "欠款金额": st.column_config.NumberColumn("欠款金额", min_value=0.0, step=100.0, format="%.2f"),
    "已收金额": st.column_config.NumberColumn("已收金额", min_value=0.0, step=100.0, format="%.2f"),
    "未付金额": st.column_config.NumberColumn("未付", disabled=True, format="%.2f"),
    "未回款天数": st.column_config.TextColumn("未回款天数", disabled=True, help="从欠款日期算到今天"),
    "欠款日期": st.column_config.DateColumn("欠款日期", format="YYYY-MM-DD"),
    "最后收款时间": st.column_config.DateColumn("最后收款", format="YYYY-MM-DD"),
    "备注": st.column_config.TextColumn("备注"),
    "删除": st.column_config.CheckboxColumn("删除", help="勾选后点下面「删除勾选客户」"),
}

# ---------------------------------------------------------------- 侧边栏
with st.sidebar:
    st.markdown("**⚙️ 设置与备份**")

    if USE_CLOUD:
        if st.session_state["cloud_error"]:
            st.error("☁️ 云端有问题\n\n" + st.session_state["cloud_error"])
        else:
            st.success("☁️ 已连接云端（手机电脑同一份数据）")
    else:
        st.info("💾 本地模式：data/ledger.csv")

    st.divider()
    st.markdown("**📥 导入 CSV**")
    up_file = st.file_uploader("选择 CSV 文件", type=["csv"], key="import_uploader")
    mode = st.radio("导入方式", ["追加到现有数据", "覆盖全部数据"], key="import_mode")
    if st.button("开始导入"):
        if up_file is None:
            st.warning("请先选择一个 CSV 文件")
        else:
            df_new = read_csv_bytes(up_file.getvalue())
            if df_new is None:
                st.error("读取失败：请确认是 CSV 文件（推荐 UTF-8 编码）")
            elif df_new.empty:
                st.warning("文件里没有有效数据（必须包含「客户名称」列）")
            else:
                if mode == "追加到现有数据":
                    df_new = pd.concat([st.session_state["ledger"], df_new], ignore_index=True)
                update_ledger(df_new, f"✅ 导入成功，当前共 {len(normalize(df_new))} 条数据")
                st.rerun()

    st.divider()
    st.markdown("**📤 导出 CSV**")
    st.download_button(
        "下载备份文件",
        data=to_csv_bytes(st.session_state["ledger"]),
        file_name=f"欠款台账_{date.today().strftime('%Y%m%d')}.csv",
        mime="text/csv",
    )
    st.caption("编码 UTF-8-BOM，Excel 不乱码。照片在云端，不在 CSV 里。")

    st.divider()
    st.markdown("**📦 完整备份（数据表 + 照片）**")
    _led = st.session_state["ledger"]
    _n_ph = int(sum(len(split_photos(x)) for x in _led["照片"])) if "照片" in _led else 0
    st.caption(f"当前：{len(_led)} 位客户、{_n_ph} 张照片"
               f"（打包后约 {_n_ph * 0.13:.0f} MB）")
    if _n_ph > 250:
        st.warning("照片较多，打包要等一会儿，手机下载也慢 ✗ 建议在**电脑上**点 ✓")

    if st.button("📦 开始打包", key="make_backup"):
        _buf = io.BytesIO()
        _ok = 0
        _fail = 0
        with st.spinner(f"正在打包 {_n_ph} 张照片，请稍等…"):
            try:
                with zipfile.ZipFile(_buf, "w", zipfile.ZIP_DEFLATED) as _zf:
                    _zf.writestr(f"欠款台账_{date.today().strftime('%Y%m%d')}.csv",
                                 to_csv_bytes(_led))
                    _map = ["客户名称,照片序号,包内文件名,云端原文件名,照片链接"]
                    for _rec in _led.to_dict("records"):
                        _nm = safe_name(str(_rec.get("客户名称") or "客户"))
                        for _i, _fn in enumerate(split_photos(_rec.get("照片")), 1):
                            _name = str(_fn)
                            try:
                                _blob = sb_client().storage.from_(BUCKET).download(_name)
                            except Exception:
                                _fail += 1
                                continue
                            _ext = ("." + _name.rsplit(".", 1)[-1]) if "." in _name else ".jpg"
                            _inzip = f"照片/{_nm}_{_i}{_ext}"
                            _zf.writestr(_inzip, _blob)
                            _url = photo_src(_name) or ""
                            _cust = str(_rec.get("客户名称") or "").replace(",", " ")
                            _map.append(f"{_cust},{_i},{_inzip},{_name},{_url}")
                            _ok += 1
                    # 对照表：哪一家的第几张照片 = 包里的哪个文件（一一对应 ✓）
                    _zf.writestr("照片对照表.csv",
                                 ("\n".join(_map)).encode("utf-8-sig"))
            except Exception as exc:
                st.session_state["backup_zip"] = b""
                st.error(f"打包失败：{exc}")
            else:
                st.session_state["backup_zip"] = _buf.getvalue()
                st.session_state["backup_info"] = (_ok, _fail)

    if st.session_state.get("backup_zip"):
        _ok, _fail = st.session_state.get("backup_info", (0, 0))
        _mb = len(st.session_state["backup_zip"]) / 1024 / 1024
        st.download_button(
            f"⬇️ 下载备份包（{_mb:.1f} MB）",
            data=st.session_state["backup_zip"],
            file_name=f"欠款台账完整备份_{date.today().strftime('%Y%m%d')}.zip",
            mime="application/zip",
            key="dl_backup",
        )
        st.caption(f"包里：数据表 CSV + 照片对照表 + **{_ok} 张照片**"
                   + (f"（{_fail} 张没下下来）" if _fail else "")
                   + " ✓ 存到网盘就是完整备份 ✓")

    st.divider()
    st.markdown("**📥 从备份包恢复**")
    st.caption("把之前下载的「完整备份 zip」传回来 → 数据和照片一起恢复 ✓")
    _rzip = st.file_uploader("选择备份包 (.zip)", type=["zip"], key="restore_zip")
    st.checkbox("我确认：恢复会覆盖现在的全部数据", key="restore_ok")
    if st.button("📥 开始恢复", key="restore_go"):
        if _rzip is None:
            st.warning("请先选择备份包（.zip）")
        elif not st.session_state["restore_ok"]:
            st.warning("请先勾选「我确认」")
        else:
            try:
                with zipfile.ZipFile(io.BytesIO(_rzip.getvalue())) as _zf:
                    _zl = _zf.namelist()
                    _main = [n for n in _zl
                             if n.lower().endswith(".csv") and "对照" not in n]
                    if not _main:
                        st.error("备份包里没找到数据表（.csv）")
                    else:
                        _rdf = read_csv_bytes(_zf.read(_main[0]))
                        if _rdf is None or _rdf.empty:
                            st.error("备份包里的数据表读不出来（或没有有效数据）")
                        else:
                            # 照片对照表：(客户名, 第几张) → 包里的路径
                            _cmap = {}
                            _mn = [n for n in _zl if "对照" in n and n.endswith(".csv")]
                            if _mn:
                                _mdf = pd.read_csv(io.BytesIO(_zf.read(_mn[0])),
                                                   encoding="utf-8-sig")
                                for _, _mr2 in _mdf.iterrows():
                                    try:
                                        _cmap[(str(_mr2["客户名称"]).strip(),
                                               int(_mr2["照片序号"]))] = str(_mr2["包内文件名"])
                                    except Exception:
                                        continue

                            _okn, _badn = 0, 0
                            _rdf = _rdf.copy()
                            _rdf["照片"] = ""
                            with st.spinner(f"正在恢复 {len(_rdf)} 条数据、重新上传照片…"):
                                for _i2, _row2 in _rdf.iterrows():
                                    _cn2 = str(_row2["客户名称"]).strip()
                                    _got = []
                                    _k2 = 1
                                    while (_cn2, _k2) in _cmap:
                                        _path2 = _cmap[(_cn2, _k2)]
                                        try:
                                            _blob2 = _zf.read(_path2)
                                        except Exception:
                                            _badn += 1
                                            _k2 += 1
                                            continue
                                        _ext2 = ("." + _path2.rsplit(".", 1)[-1]
                                                 if "." in _path2 else ".jpg")
                                        _got.append(save_photo(_cn2, _blob2, _ext2))
                                        _okn += 1
                                        _k2 += 1
                                    _rdf.at[_i2, "照片"] = join_photos(_got)
                            _rdf[ID_COL] = 0        # 全部当新行插入，云端旧行会被自动清掉
                            update_ledger(
                                _rdf,
                                f"✅ 已恢复 {len(_rdf)} 条数据、{_okn} 张照片"
                                + (f"（{_badn} 张在包里缺失）" if _badn else ""),
                                force_reload=True)
                            st.rerun()
            except Exception as _rex:
                st.error(f"恢复失败：{_rex}")

    st.divider()
    st.markdown("**🧪 测试数据**")
    if st.button("载入 6 条示例数据"):
        update_ledger(sample_data(), "✅ 已载入示例数据（替换了原有数据）")
        st.rerun()

    st.divider()
    st.caption("⚠️ 请把浏览器的「网页翻译」关掉 —— 页面本来就是中文，"
               "翻译会把它拆坏并报 removeChild 错误")
    st.divider()
    st.markdown("**🗑️ 清空数据**")
    confirm = st.checkbox("我确认清空全部数据（不可恢复）", key="confirm_clear")
    if st.button("清空全部数据"):
        if confirm:
            update_ledger(empty_df(), "已清空全部数据")
            st.rerun()
        else:
            st.warning("请先勾选上面的确认框")


# ---------------------------------------------------------------- 主区域
st.markdown('<div id="top"></div>', unsafe_allow_html=True)
st.markdown("#### 💰 客户欠款台账")
st.caption("版本 v90")

# ==== 界面微调：藏掉 Streamlit 痕迹 / 压缩留白 / 并排控件不换行 ====
st.markdown(
    """
    <style>
    /* "📈 统计"小按钮：小一号，紧跟在"年收"后面，不抢戏 */
    .st-key-go_stats { margin-top: 0.1rem !important; }
    .st-key-go_stats button {
        font-size: 0.68rem !important;
        padding: 0.05rem 0.4rem !important;
        min-height: 1.6rem !important;
        line-height: 1.2 !important;
        border-radius: 0.5rem !important;
    }

    /* 藏掉输入框下面那行英文 "Press Enter to submit form"（我们用中文提示代替） */
    [data-testid="InputInstructions"] { display: none !important; }

    /* 照片上传框：手机上一行放不下，就改成上下两行排，文字才不会被挤成竖排 */
    [data-testid="stFileUploaderDropzone"] {
        flex-direction: column !important;
        align-items: center !important;
        gap: 0.5rem !important;
    }
    [data-testid="stFileUploaderDropzone"] > button { width: 100% !important; }

    [data-testid="stFileUploaderDropzoneInstructions"] span { display: none; }
    [data-testid="stFileUploaderDropzoneInstructions"] small { display: none; }
    [data-testid="stFileUploaderDropzoneInstructions"] > div::after {
        content: "点这里选择文件";
        font-size: 0.85rem;
        white-space: nowrap;
    }
    [data-testid="stFileUploaderDropzone"] button span { display: none; }
    [data-testid="stFileUploaderDropzone"] button::after {
        content: "选择文件";
        white-space: nowrap;
    }

    [data-testid="stToolbar"] { display: none !important; }
    [data-testid="stDecoration"] { display: none !important; }
    [data-testid="stStatusWidget"] { display: none !important; }
    [data-testid="stAppDeployButton"] { display: none !important; }
    #MainMenu { display: none !important; }
    footer { display: none !important; }
    header[data-testid="stHeader"] { background: transparent !important; }

    .block-container { padding-top: 0.8rem !important; padding-bottom: 1rem !important; }

    /* 并排控件不许换行（不然手机上「确定」会掉到第二行），同时允许它们收窄，避免文字被截断 */
    div[data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; align-items: flex-end; }
    div[data-testid="stHorizontalBlock"] > div { min-width: 0 !important; }

    /* 搜索框视觉上短一点（不影响「确定」那一列） */
    .st-key-search_box input { max-width: 150px; }
    </style>
    """,
    unsafe_allow_html=True,
)

if st.session_state["flash"]:
    st.success(st.session_state["flash"])
    st.session_state["flash"] = ""

ledger = st.session_state["ledger"]
data = with_unpaid(ledger)

if "page" not in st.session_state:
    st.session_state["page"] = "list"
if "current_id" not in st.session_state:
    st.session_state["current_id"] = -1
if "editing_id" not in st.session_state:
    st.session_state["editing_id"] = -1
if "del_pending" not in st.session_state:
    st.session_state["del_pending"] = -1
if "scroll_mark" not in st.session_state:
    st.session_state["scroll_mark"] = "list"
if "money_kind" not in st.session_state:
    st.session_state["money_kind"] = "pay"

# =====================================================================
# 记账页（点卡片上的「💰 收钱」「➕ 欠款」进来）
# =====================================================================
if st.session_state["page"] == "money" and st.session_state["current_id"] in ledger.index:
    _mid = st.session_state["current_id"]
    _mr = ledger.loc[_mid]
    _mn = str(_mr["客户名称"])
    _is_pay = st.session_state["money_kind"] == "pay"
    _m_owed = to_float(_mr["欠款金额"]) - to_float(_mr["已收金额"])

    if st.button("← 返回客户列表", key="money_back"):
        st.session_state["page"] = "list"
        st.rerun()

    st.markdown(f"### {'💰 记一笔收款' if _is_pay else '➕ 记一笔欠款'}")
    st.markdown(f"**{_mn}**")
    st.caption(f"当前：欠 ¥{to_float(_mr['欠款金额']):,.2f}　"
               f"已收 ¥{to_float(_mr['已收金额']):,.2f}　未付 ¥{_m_owed:,.2f}")

    with st.form("money_form"):
        _mv = st.number_input("这次收了多少？" if _is_pay else "这次又欠了多少？",
                              min_value=0.0, step=100.0, format="%.2f", value=None)
        _mc1, _mc2 = st.columns(2)
        _mok = _mc1.form_submit_button("✅ 确定", type="primary")
        _mno = _mc2.form_submit_button("取消")

    if _mok:
        if _mv is None or _mv <= 0:
            st.warning("请填写金额")
        else:
            _new = ledger.copy()
            if _is_pay:
                _new.loc[_mid, "已收金额"] = to_float(_mr["已收金额"]) + _mv
                _new.loc[_mid, "最后收款时间"] = pd.Timestamp(date.today())
                _new.loc[_mid, LOG_COL] = add_log(_mr, "收", _mv)
                _msg = f"✅ 「{_mn}」已收 ¥{_mv:,.2f}"
            else:
                _new.loc[_mid, "欠款金额"] = to_float(_mr["欠款金额"]) + _mv
                _new.loc[_mid, LOG_COL] = add_log(_mr, "欠", _mv)
                _msg = f"✅ 「{_mn}」又欠 ¥{_mv:,.2f}"
            update_ledger(_new, _msg, force_reload=True)
            # 回到这家客户的详情页（列表会按欠款重排，客户会"跑掉"，详情页不会）
            st.session_state["page"] = "detail"
            st.rerun()
    if _mno:
        st.session_state["page"] = "detail"
        st.rerun()

    # 自动把光标放进金额输入框（键盘直接弹出来，省一次点击）
    components.html(
        """<script>
        (function(){
          try {
            var d = window.parent.document;
            var el = d.querySelector('section.main input[type="number"]')
                  || d.querySelector('[data-testid="stMain"] input[type="number"]')
                  || d.querySelector('input[type="number"]');
            if (el) { el.focus(); }
          } catch (e) {}
        })();
        </script>""",
        height=0,
    )

    st.stop()

# =====================================================================
# 确认删除页（点卡片右上角 🗑️ 进来）
# =====================================================================
if st.session_state["page"] == "del" and st.session_state["current_id"] in ledger.index:
    _did = st.session_state["current_id"]
    _dn = str(ledger.loc[_did, "客户名称"])

    if st.button("← 返回客户列表", key="delpage_back"):
        st.session_state["page"] = "list"
        st.rerun()

    st.markdown("### 🗑️ 删除客户")
    st.markdown(f"**{_dn}**")
    st.warning("删掉就找不回来了 ✓ 确定要删吗？")
    _dc1, _dc2 = st.columns(2)
    if _dc1.button("⚠️ 确认删除", type="primary", key="delpage_yes"):
        update_ledger(ledger[ledger.index != _did], f"🗑️ 已删除「{_dn}」",
                      force_reload=True)
        st.session_state["page"] = "list"
        st.session_state["clear_search"] = True      # 清空搜索，回到干净首页
        st.session_state["page_no"] = 1
        st.rerun()
    if _dc2.button("取消", key="delpage_no"):
        st.session_state["page"] = "list"
        st.rerun()

    st.stop()

# =====================================================================
# 详情页（点开客户卡片进来的，一屏搞定这个客户的所有事）
# =====================================================================
cur = st.session_state["current_id"]
if st.session_state["page"] == "detail" and cur in ledger.index:
    row = ledger.loc[cur]
    cname = str(row["客户名称"])
    days = days_text(with_unpaid(pd.DataFrame([row])).iloc[0])
    unpaid = to_float(row["欠款金额"]) - to_float(row["已收金额"])

    # 刚进这个页面时，自动滚到最上面（页面内点按钮不会再滚，不打扰操作）

    if st.button("← 返回客户列表"):
        st.session_state["page"] = "list"
        st.session_state["editing_id"] = -1
        st.session_state["del_pending"] = -1
        st.rerun()

    st.markdown(f"### 👤 {cname}")
    last_txt = ("没记录" if pd.isna(row["最后收款时间"])
                else pd.Timestamp(row["最后收款时间"]).strftime("%Y-%m-%d"))
    st.markdown(f"⏰ **{days}**未回款　｜　欠 ¥{to_float(row['欠款金额']):,.2f}"
                f"　已收 ¥{to_float(row['已收金额']):,.2f}　未付 **¥{unpaid:,.2f}**")
    st.caption(f"上次收款：{last_txt}")

    # 直接在这家客户这里记账（点错了不用回列表找）
    _qb1, _qb2 = st.columns(2)
    if _qb1.button("💰 收钱", key=f"dt_pay_{cur}", type="primary"):
        st.session_state["page"] = "money"
        st.session_state["money_kind"] = "pay"
        st.rerun()
    if _qb2.button("➕ 欠款", key=f"dt_debt_{cur}", type="primary"):
        st.session_state["page"] = "money"
        st.session_state["money_kind"] = "debt"
        st.rerun()
    st.divider()

    # ---------- 照片 ----------
    photos = split_photos(row["照片"])
    st.markdown(f"**📷 照片（{len(photos)} / {MAX_PHOTOS}）**")

    if len(photos) >= MAX_PHOTOS:
        st.info(f"已经有 {MAX_PHOTOS} 张了，想换先删掉一张。")
    else:
        room = MAX_PHOTOS - len(photos)
        ups = st.file_uploader("选择照片", type=["jpg", "jpeg", "png", "webp"],
                               accept_multiple_files=True, label_visibility="collapsed",
                               key=f"up_{cur}")
        pending_photos = []
        for item in (ups or []):
            pending_photos.append((item.getvalue(), "." + item.name.rsplit(".", 1)[-1]))
        _sig = "|".join(f"{_f.name}:{_f.size}" for _f in (ups or []))
        _sig_key = f"photo_sig_{cur}"
        if _sig_key not in st.session_state:
            st.session_state[_sig_key] = ""
        if _sig and st.session_state[_sig_key] == _sig:
            st.info("上面这几张已经保存过了 ✓ 要再传新的，先点它右边的 ✕ 把它们清掉，再重新选 ✓")
        if len(pending_photos) > room:
            st.warning(f"最多还能加 {room} 张，只存前 {room} 张。")
            pending_photos = pending_photos[:room]
        if st.button(f"💾 保存照片（还能加 {room} 张）", disabled=not pending_photos,
                     type="primary"):
            _before = sum(len(_b) for _b, _s in pending_photos)
            try:
                with st.spinner(f"上传中…（{len(pending_photos)} 张）"):
                    saved = [save_photo(cname, blob, sfx) for blob, sfx in pending_photos]
                    _after = sum(
                        len(_b) for _b, _s in
                        [shrink_image(_b, _s) for _b, _s in pending_photos])
            except CloudError as exc:
                st.error(str(exc))
            else:
                new = ledger.copy()
                new.loc[cur, "照片"] = join_photos(photos + saved)
                st.session_state[_sig_key] = _sig
                _saved_txt = (f"（{_before / 1024 / 1024:.1f}MB → "
                              f"{_after / 1024:.0f}KB）" if _before > _after else "")
                update_ledger(new, f"✅ 已保存 {len(saved)} 张{_saved_txt}")
                st.session_state["page"] = "list"
                st.rerun()

    # ② 再看已有的照片（可以逐张删）
    if photos:
        st.caption("已有的照片（点每张下面的「删除」就删那张）")
        cols = st.columns(3)
        for i, filename in enumerate(photos):
            with cols[i % 3]:
                src_img = photo_src(filename)
                if src_img:
                    st.image(src_img, width=110)
                else:
                    st.caption("⚠️ 照片看不见")
                # 用"照片文件名"当按钮编号：删掉一张后，别的按钮编号不会错位
                if st.button("删除", key=f"dphoto_{cur}_{filename}"):
                    rest = [x for j, x in enumerate(photos) if j != i]
                    delete_photo_file(filename)
                    new = ledger.copy()
                    new.loc[cur, "照片"] = join_photos(rest)
                    update_ledger(new)          # 删除不弹提示，安静地删掉就行
                    # 删完直接回客户列表：不在详情页原地改界面，避开节点错乱
                    st.session_state["page"] = "list"
                    st.rerun()
    else:
        st.caption("还没有照片，点上面的框选一张（手机可以直接拍照）")

    st.divider()

    # ---------- 改金额（一打开就能改，不用先点按钮） ----------
    st.markdown("**✏️ 编辑客户**　（数字框留空 = 不改）")
    with st.form(f"edit_form_{cur}"):
        new_name = st.text_input("客户名称", value=cname)
        debt = st.number_input(f"欠款金额(元)　当前 ¥{to_float(row['欠款金额']):,.2f}",
                               min_value=0.0, step=100.0, format="%.2f", value=None)
        paid = st.number_input(f"已收金额(元)　当前 ¥{to_float(row['已收金额']):,.2f}",
                               min_value=0.0, step=100.0, format="%.2f", value=None)
        # ⭐ 保存按钮紧跟金额框：手机上输完就能点到，不用往下翻
        ok = st.form_submit_button("✅ 保存修改", type="primary")
        st.caption("填了已收金额 → 收款时间自动记今天")
        _start_val = (date.today() if pd.isna(row["欠款日期"])
                      else pd.Timestamp(row["欠款日期"]).date())
        start = st.date_input("欠款日期", value=_start_val)
        note = st.text_input("备注（可选）", value=str(row["备注"]))
    if ok:
        new = ledger.copy()
        if new_name.strip() and new_name.strip() != cname:
            new.loc[cur, "客户名称"] = new_name.strip()          # 改名字
        if debt is not None:
            new.loc[cur, "欠款金额"] = debt
        if paid is not None:
            new.loc[cur, "已收金额"] = paid
            new.loc[cur, "最后收款时间"] = pd.Timestamp(date.today())   # 自动记今天
        if start is not None and pd.Timestamp(start) != pd.Timestamp(row["欠款日期"]):
            new.loc[cur, "欠款日期"] = pd.Timestamp(start)        # 改欠款日期
        if note.strip():
            new.loc[cur, "备注"] = note.strip()
        with st.spinner("保存中…"):
            update_ledger(new, f"✅ 已保存「{new_name.strip() or cname}」")
        st.rerun()

    st.divider()

    # ---------- 删除客户（点两次才真删） ----------
    if st.session_state["del_pending"] == cur:
        c1, c2 = st.columns(2)
        if c1.button("⚠️ 确认删除", type="primary"):
            update_ledger(ledger[ledger.index != cur], f"🗑️ 已删除「{cname}」",
                          force_reload=True)
            st.session_state["page"] = "list"
            st.session_state["del_pending"] = -1
            st.session_state["clear_search"] = True
            st.session_state["page_no"] = 1
            st.rerun()
        if c2.button("取消"):
            st.session_state["del_pending"] = -1
            st.rerun()
    else:
        if st.button("🗑️ 删除这个客户"):
            st.session_state["del_pending"] = cur
            st.rerun()

    st.stop()          # 详情页到此为止，不再显示下面的主页内容

# =====================================================================
# 逐月收款页（点主页的「📈 每月」进来）
# =====================================================================
if st.session_state["page"] == "stats":
    _y = date.today().year
    # 刚进这个页面时，自动滚到最上面（页面内点按钮不会再滚，不打扰操作）

    if st.button("← 返回客户列表"):
        st.session_state["page"] = "list"
        st.rerun()
    st.markdown(f"### 📈 {_y} 年收款统计")

    _ys = (pd.to_datetime(data["最后收款时间"], errors="coerce").dt.year
           if not data.empty else None)
    _paid = data.loc[_ys == _y] if (not data.empty and _ys is not None) else data.iloc[0:0]
    _grp2 = (data.iloc[0:0].assign(月=[], 金额=[]).groupby("月")["金额"].sum()
             if _paid.empty
             else _paid.assign(月=pd.to_datetime(_paid["最后收款时间"]).dt.month)
                      .groupby("月")["已收金额"].sum())

    _labels = [f"{m}月" for m in range(1, 13)]
    _vals = [float(_grp2.get(m, 0.0)) for m in range(1, 13)]
    _chart_df = pd.DataFrame({"月份": _labels, "收款": _vals})

    if sum(_vals) <= 0:
        # 今年一分钱都还没收到 —— 别显示一张空的怪图，直接说人话
        st.info("📭 今年还没有收款记录。\n\n"
                "等你在客户卡片上点「💰 收钱」记下第一笔，"
                "这里就会出现 12 个月的柱状图 ✓")
    elif alt is None:
        st.bar_chart(_chart_df.set_index("月份"))
    else:
        _xmax1 = max(max(_vals) * 1.35, 1.0)

        _bars = (
            alt.Chart(_chart_df)
            .mark_bar(color="#2E8B57", size=10)
            .encode(
                # 月份竖着排：手机上 12 个月的标签全都能显示出来
                y=alt.Y("月份:N", sort=_labels, title=None,
                        axis=alt.Axis(labelFontSize=14)),
                x=alt.X("收款:Q", title="收款（元）",
                        scale=alt.Scale(domain=[0, _xmax1]),     # 右边留白
                        axis=alt.Axis(labelFontSize=10, format=",.0f")),
                tooltip=[alt.Tooltip("月份:N"), alt.Tooltip("收款:Q", format=",.2f")],
            )
        )
        _txt = (
            alt.Chart(_chart_df)
            .transform_filter("datum['收款'] > 0")
            .mark_text(align="left", dx=4, fontSize=11, color="#333")
            .encode(
                y=alt.Y("月份:N", sort=_labels, title=None),
                x=alt.X("收款:Q"),
                text=alt.Text("收款:Q", format=",.0f"),
            )
        )
        st.altair_chart((_bars + _txt).properties(width="container", height=330))

    st.markdown(f"**全年合计 ¥{sum(_vals):,.2f}**　｜　"
                f"本月（{date.today().month}月）¥{_vals[date.today().month - 1]:,.2f}")
    st.caption("柱子的长短就是那个月收了多少 ✓ 柱子右边直接写着金额 ✓ 12 个月全都在 ✓")
    st.markdown('<a href="#top" style="font-size:0.85rem">⬆️ 回到顶部</a>',
                unsafe_allow_html=True)
    st.stop()

# =====================================================================
# 客户往来明细页（点卡片上的客户名进来）
# =====================================================================
if st.session_state["page"] == "customer" and st.session_state["current_id"] in ledger.index:
    _cid = st.session_state["current_id"]
    _cr = ledger.loc[_cid]
    _cn = str(_cr["客户名称"])


    if st.button("← 返回客户列表"):
        st.session_state["page"] = "list"
        st.rerun()

    st.markdown(f"### 👤 {_cn}")
    _owed_c = to_float(_cr["欠款金额"]) - to_float(_cr["已收金额"])   # 当前未付

    # ---- 流水：老数据没记录，就补一条"历史记录"，看着才完整 ----
    _logs = load_log(_cr)
    if not _logs:
        _seed = []
        if to_float(_cr["欠款金额"]) > 0:
            _d0 = ("（没记日期）" if pd.isna(_cr["欠款日期"])
                   else str(pd.Timestamp(_cr["欠款日期"]).date()))
            _seed.append({"d": _d0, "t": "欠", "v": round(to_float(_cr["欠款金额"]), 2),
                          "old": 1})
        if to_float(_cr["已收金额"]) > 0:
            _d1 = ("（没记日期）" if pd.isna(_cr["最后收款时间"])
                   else str(pd.Timestamp(_cr["最后收款时间"]).date()))
            _seed.append({"d": _d1, "t": "收", "v": round(to_float(_cr["已收金额"]), 2),
                          "old": 1})
        _logs = _seed

    # ---- 按月归堆 ----
    _ty = str(date.today().year)
    _m_owed = [0.0] * 12
    _m_paid = [0.0] * 12
    _other_year = 0
    for _it in _logs:
        _d = str(_it.get("d", ""))
        if _d[:4] != _ty:
            _other_year += 1
            continue
        try:
            _mo = int(_d[5:7])
        except Exception:
            continue
        if not (1 <= _mo <= 12):
            continue
        _v = float(_it.get("v", 0) or 0)
        if str(_it.get("t")) == "收":
            _m_paid[_mo - 1] += _v
        else:
            _m_owed[_mo - 1] += _v

    # ---- 图形：每个月两根柱子（红=欠款、绿=收款），柱子右边直接写金额 ----
    _order = [f"{m}月" for m in range(1, 13)]
    _mdf = pd.DataFrame({"月份": _order, "欠款": _m_owed, "收款": _m_paid})
    _long = _mdf.melt(id_vars="月份", value_vars=["欠款", "收款"],
                      var_name="类型", value_name="金额")
    _xmax = max(max(_m_owed + _m_paid) * 1.35, 1.0)     # 右边留白，数字不被切

    if alt is None:
        st.bar_chart(_mdf.set_index("月份"))
    else:
        _bars = (
            alt.Chart(_long)
            .mark_bar(size=8)
            .encode(
                y=alt.Y("月份:N", sort=_order, title=None,
                        axis=alt.Axis(labelFontSize=14)),
                yOffset=alt.YOffset("类型:N"),
                x=alt.X("金额:Q", title="金额（元）",
                        scale=alt.Scale(domain=[0, _xmax]),
                        axis=alt.Axis(labelFontSize=10, format=",.0f")),
                color=alt.Color("类型:N", title=None,
                                scale=alt.Scale(domain=["欠款", "收款"],
                                                range=["#E4572E", "#2E8B57"]),
                                legend=alt.Legend(orient="top", labelFontSize=12)),
                tooltip=[alt.Tooltip("月份:N"), alt.Tooltip("类型:N"),
                         alt.Tooltip("金额:Q", format=",.2f")],
            )
        )
        _txt = (
            alt.Chart(_long)
            .transform_filter("datum['金额'] > 0")      # 0 就不标，省得满屏小 0
            .mark_text(align="left", dx=4, fontSize=11, color="#333")
            .encode(
                y=alt.Y("月份:N", sort=_order, title=None),
                yOffset=alt.YOffset("类型:N"),
                x=alt.X("金额:Q"),
                text=alt.Text("金额:Q", format=",.0f"),
            )
        )
        st.altair_chart((_bars + _txt).properties(width="container", height=380))

    # ⭐ 三行清清楚楚，全年收了多少放第一行
    st.markdown(f"📅 **{_ty} 年收款：¥{sum(_m_paid):,.2f}**")
    st.markdown(f"{_ty} 年欠款：¥{sum(_m_owed):,.2f}")
    st.markdown(f"当前未付：**¥{_owed_c:,.2f}**")
    st.caption("红柱=那个月又欠了多少 ✓ 绿柱=那个月收回了多少 ✓ 柱子右边直接写着金额 ✓")
    if _other_year:
        st.caption(f"（另有 {_other_year} 笔往年的记录，没算进今年的图里）")

    st.divider()
    if st.button("📷 拍照 / 修改资料", type="primary"):
        st.session_state["page"] = "detail"
        st.rerun()

    st.markdown('<a href="#top" style="font-size:0.85rem">⬆️ 回到顶部</a>',
                unsafe_allow_html=True)
    st.stop()

# 回到主页了 —— 把"自动置顶"的标记清掉，下次再进子页面还会滚到最上面
st.session_state["scroll_mark"] = "list"

# =====================================================================
# 主页：总欠款 → 搜索 → 添加 → 客户列表（卡片 / 表格） → 图表
# =====================================================================
_head1 = (f"📅 **{date.today().year}年**　欠款 "
          f"**{money_short(data['欠款金额'].sum())}**（{len(data)} 笔）")

# ---------- 客户统计（纯本地计算，不额外联网，不占流量） ----------
_this_year = date.today().year
_years = pd.to_datetime(data["最后收款时间"], errors="coerce").dt.year if not data.empty else None
_money_in = 0.0 if data.empty else float(
    data.loc[_years == _this_year, "已收金额"].sum())        # 今年收到手的钱
_owed = float(data["未付金额"].sum()) if not data.empty else 0.0     # 还没收回来的
_total = float(data["欠款金额"].sum()) if not data.empty else 0.0     # 合计（历史累计借出）


# ---------- 收款明细：本月 / 全年 / 逐月（同样纯本地算，不额外联网） ----------
_this_month = date.today().month
_paid_year = data.loc[_years == _this_year] if (not data.empty and _years is not None) else data.iloc[0:0]
_month_in = 0.0
_by_month_txt = "暂无记录"
if not _paid_year.empty:
    _m = pd.to_datetime(_paid_year["最后收款时间"]).dt.month
    _grp = _paid_year.groupby(_m)["已收金额"].sum().sort_index()
    _month_in = float(_grp.get(_this_month, 0.0))
    _parts = [f"{int(k)}月 ¥{v:,.2f}" for k, v in _grp.items() if v > 0]
    _by_month_txt = "　｜　".join(_parts) if _parts else "暂无记录"

# 第一行：年份 + 欠款；第二行：月收 + 年收（分开两行，手机上不挤）
st.markdown(_head1)
_s1, _s2 = st.columns([2.45, 1], vertical_alignment="center")
_s1.markdown(
    f"<div style='font-size:0.8rem;line-height:1.5'>月收 <b>¥{_month_in:,.2f}</b>"
    f"　年收 <b>¥{_money_in:,.2f}</b></div>",
    unsafe_allow_html=True)
if _s2.button("📈 统计", key="go_stats"):
    st.session_state["page"] = "stats"
    st.rerun()
# 只有真的有收款记录时，才多显示一行"各月收款"（平时不占地方）
if _by_month_txt != "暂无记录":
    st.caption(f"📊 各月收款：{_by_month_txt}")

# 添加完客户 / 存完照片后，需要清空或改写搜索框（必须在控件创建之前做）
if st.session_state["pending_search"]:
    st.session_state["search_box"] = st.session_state["pending_search"]
    st.session_state["pending_search"] = ""
if st.session_state["clear_search"]:
    st.session_state["search_box"] = ""
    st.session_state["clear_search"] = False

c_search, c_go, c_add = st.columns([3, 1.4, 1.8], vertical_alignment="bottom")
keyword = c_search.text_input("搜索", placeholder="", key="search_box",
                              label_visibility="collapsed")
c_go.button("确定", type="primary")
if c_add.button("➕ 添加", type="primary"):
    st.session_state["show_add"] = not st.session_state["show_add"]
kw = keyword.strip()

if st.session_state["show_add"]:
    with st.form("add_form", clear_on_submit=True):
        submitted = st.form_submit_button("✅ 添加到台账", type="primary")
        name = st.text_input("客户名称 *", placeholder="例如：张老板")
        debt = st.number_input("欠款金额(元)", min_value=0.0, step=100.0,
                               format="%.2f", value=None)
        when = st.date_input("欠款日期", value=date.today())
        st.caption("已收金额、最后收款时间、备注 → 添加后点开这个客户，在详情页里改")

    if submitted:
        if not name.strip():
            st.warning("客户名称不能为空")
        else:
            new_row = pd.DataFrame([{
                ID_COL: 0,
                "客户名称": name.strip(),
                "欠款金额": 0.0 if debt is None else debt,
                "已收金额": 0.0,
                "欠款日期": pd.Timestamp(when),
                "最后收款时间": pd.NaT,
                "备注": "",
                "客户位置": "",
                "照片": "",
            }])
            st.session_state["show_add"] = False          # 保存后自动收起添加表单
            st.session_state["pending_search"] = name.strip()   # 顺便筛出这位新客户
            update_ledger(pd.concat([ledger, new_row], ignore_index=True),
                          f"✅ 已添加「{name.strip()}」")
            st.rerun()

# ---------- 明细：卡片式 / 表格 ----------
mode = st.radio("显示方式", ["🗂️ 卡片式", "📋 表格"], horizontal=True,
                key="view_mode", label_visibility="collapsed")

if kw:
    filtered = data[data["客户名称"].str.contains(kw, case=False, na=False)
                    | data["备注"].str.contains(kw, case=False, na=False)]
else:
    filtered = data

# ---- 固定排序：欠得最多的排最上面（已结清的自动沉到最下面）----
if not filtered.empty:
    filtered = filtered.sort_values("未付金额", ascending=False)

# ---------------- 分页：一屏最多 10 位（客户多了也不卡） ----------------
PAGE_SIZE = 10
_total = len(filtered)
_pages = max(1, (_total + PAGE_SIZE - 1) // PAGE_SIZE)
if "list_kw" not in st.session_state:
    st.session_state["list_kw"] = kw
if "page_no" not in st.session_state:
    st.session_state["page_no"] = 1
if st.session_state["list_kw"] != kw:          # 换了搜索词就回到第 1 页
    st.session_state["list_kw"] = kw
    st.session_state["page_no"] = 1
_page = min(max(1, st.session_state["page_no"]), _pages)
st.session_state["page_no"] = _page
page_rows = filtered.iloc[(_page - 1) * PAGE_SIZE: _page * PAGE_SIZE]

if _pages > 1:
    st.caption(f"共 {len(data)} 位客户　·　第 {_page}/{_pages} 页")
else:
    st.caption(f"共 {len(data)} 位客户")

if mode == "🗂️ 卡片式":
    if page_rows.empty:
        st.caption("还没有客户" if data.empty else "没找到匹配的客户")
    else:
        for _k in ("pay_id", "debt_id", "del_card"):
            if _k not in st.session_state:
                st.session_state[_k] = -1

        for rid, row in page_rows.iterrows():
            cname = str(row["客户名称"])
            unpaid = to_float(row["欠款金额"]) - to_float(row["已收金额"])
            days = days_text(with_unpaid(pd.DataFrame([row])).iloc[0])
            badge = days_badge(with_unpaid(pd.DataFrame([row])).iloc[0])
            n_photos = len(split_photos(row["照片"]))

            with st.container(border=True):
                _n1, _n2 = st.columns([5, 1], vertical_alignment="center")
                _title = f"{badge} {cname}　⏰ {days}"
                _clicked = _n1.button(_title, key=f"open_cust_{rid}_{cname}")
                if _clicked:
                    st.session_state["page"] = "customer"
                    st.session_state["current_id"] = rid
                    st.rerun()
                if _n2.button("🗑️", key=f"delc_{rid}_{cname}", help="删除这个客户"):
                    st.session_state["page"] = "del"
                    st.session_state["current_id"] = rid
                    st.rerun()
                st.markdown(f"未付 **¥{unpaid:,.2f}**"
                            + (f"　｜　📷 {n_photos} 张" if n_photos else ""))
                b1, b2, b3 = st.columns(3)
                if b1.button("💰 收钱", key=f"pay_{rid}_{cname}"):
                    st.session_state["page"] = "money"
                    st.session_state["money_kind"] = "pay"
                    st.session_state["current_id"] = rid
                    st.rerun()
                if b2.button("➕ 欠款", key=f"debt_{rid}_{cname}"):
                    st.session_state["page"] = "money"
                    st.session_state["money_kind"] = "debt"
                    st.session_state["current_id"] = rid
                    st.rerun()
                if b3.button("📷 拍照", key=f"photo_{rid}_{cname}"):
                    st.session_state["page"] = "detail"
                    st.session_state["current_id"] = rid
                    st.session_state["editing_id"] = -1
                    st.session_state["del_pending"] = -1
                    st.session_state["pay_id"] = -1
                    st.session_state["debt_id"] = -1
                    st.rerun()

else:
    base = page_rows.reset_index(drop=True)
    ids = page_rows[ID_COL].tolist()
    view = pd.DataFrame({
        "客户名称": base["客户名称"],
        "欠款金额": base["欠款金额"],
        "已收金额": base["已收金额"],
        "未付金额": base["未付金额"],
        "未回款天数": base.apply(days_text, axis=1),
        "欠款日期": base["欠款日期"],
        "最后收款时间": base["最后收款时间"],
        "备注": base["备注"],
        "删除": False,
    })

    editor_args = dict(hide_index=True, num_rows="fixed",
                       key=f"editor_{st.session_state['editor_key']}",
                       column_config=COLUMN_CONFIG)
    try:
        edited = st.data_editor(view, width="stretch", **editor_args)
    except TypeError:
        edited = st.data_editor(view, use_container_width=True, **editor_args)

    st.caption("💡 金额双击就能改，**自动保存**；删客户：勾选「删除」再点下面按钮。")

    b1, b2, _ = st.columns([1, 1, 3])
    delete_clicked = b1.button("🗑️ 删除勾选客户", type="primary")

    new_ledger, deleted = merge_edits(ledger, base, edited, ids, apply_delete=delete_clicked)

    if deleted:
        update_ledger(new_ledger, f"🗑️ 已删除 {deleted} 位客户")
        st.session_state["clear_search"] = True
        st.session_state["page_no"] = 1
        st.rerun()
    elif signature(new_ledger) != signature(ledger):
        update_ledger(new_ledger)
        st.rerun()

# ---------- 翻页按钮放在列表下面（看完这一页顺手翻） ----------
if _pages > 1:
    _p1, _p2 = st.columns(2)
    if _p1.button("◀ 上一页", disabled=_page <= 1):
        st.session_state["page_no"] = _page - 1
        st.rerun()
    if _p2.button("下一页 ▶", disabled=_page >= _pages):
        st.session_state["page_no"] = _page + 1
        st.rerun()

# ---------- 一行提醒（原来那张大图去掉了：列表能按"欠得最多"排序，一眼就看到） ----------
if not data.empty and data["未付金额"].sum() > 0:
    _pending = data[data["未付金额"] > 0].copy()
    _pending["天数"] = _pending["欠款日期"].apply(unpaid_days)
    _old = _pending[_pending["天数"] > 90]
    if not _old.empty:
        st.caption(f"⏰ 超过 90 天没回款：**{len(_old)} 位**，共 ¥{_old['未付金额'].sum():,.2f}")

st.markdown('<a href="#top" style="font-size:0.85rem">⬆️ 回到顶部</a>', unsafe_allow_html=True)
