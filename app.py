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


def save_photo(customer: str, data: bytes, suffix: str = ".jpg") -> str:
    """存一张照片：云端模式传进 Storage，本地模式写进 photos/。返回文件名。"""
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
if "cloud_error" not in st.session_state:
    st.session_state["cloud_error"] = ""
if "flash" not in st.session_state:
    st.session_state["flash"] = ""


def set_flash(msg: str) -> None:
    st.session_state["flash"] = msg


def update_ledger(df: pd.DataFrame, msg: str = "") -> None:
    df = normalize(df)
    if save_data(df):
        st.session_state["ledger"] = load_data()
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
    st.header("⚙️ 设置与备份")

    if USE_CLOUD:
        if st.session_state["cloud_error"]:
            st.error("☁️ 云端有问题\n\n" + st.session_state["cloud_error"])
        else:
            st.success("☁️ 已连接云端（手机电脑同一份数据）")
    else:
        st.info("💾 本地模式：data/ledger.csv")

    st.checkbox("🙈 隐藏客户名（图表上打码）", key="mask_names",
                help="打开后「待回款金额」图里的名字变成 张**，别人瞄到也看不清是谁")

    st.divider()
    st.subheader("📥 导入 CSV")
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
    st.subheader("📤 导出 CSV")
    st.download_button(
        "下载备份文件",
        data=to_csv_bytes(st.session_state["ledger"]),
        file_name=f"欠款台账_{date.today().strftime('%Y%m%d')}.csv",
        mime="text/csv",
    )
    st.caption("编码 UTF-8-BOM，Excel 不乱码。照片在云端，不在 CSV 里。")

    st.divider()
    st.subheader("🧪 测试数据")
    if st.button("载入 6 条示例数据"):
        update_ledger(sample_data(), "✅ 已载入示例数据（替换了原有数据）")
        st.rerun()

    st.divider()
    st.subheader("🗑️ 清空数据")
    confirm = st.checkbox("我确认清空全部数据（不可恢复）", key="confirm_clear")
    if st.button("清空全部数据"):
        if confirm:
            update_ledger(empty_df(), "已清空全部数据")
            st.rerun()
        else:
            st.warning("请先勾选上面的确认框")


# ---------------------------------------------------------------- 主区域
st.title("💰 客户欠款台账")

# 把「文件上传框」里 Streamlit 自带的英文提示换成中文
st.markdown(
    """
    <style>
    [data-testid="stFileUploaderDropzoneInstructions"] span { display: none; }
    [data-testid="stFileUploaderDropzoneInstructions"] small { display: none; }
    [data-testid="stFileUploaderDropzoneInstructions"] > div::after {
        content: "点这里选照片（手机可直接拍照 / 从相册选）";
        font-size: 0.85rem;
    }
    [data-testid="stFileUploaderDropzone"] button span { display: none; }
    [data-testid="stFileUploaderDropzone"] button::after { content: "选照片"; }
    </style>
    """,
    unsafe_allow_html=True,
)

if st.session_state["flash"]:
    st.success(st.session_state["flash"])
    st.session_state["flash"] = ""

ledger = st.session_state["ledger"]
data = with_unpaid(ledger)

if st.session_state["pending_search"]:
    st.session_state["search_box"] = st.session_state["pending_search"]
    st.session_state["pending_search"] = ""

# ============ ① 总欠款 ============
st.markdown(f"**总欠款 ¥{data['欠款金额'].sum():,.2f}**　（{len(data)} 笔）")
st.caption(f"已收 ¥{data['已收金额'].sum():,.2f}　·　未付 **¥{data['未付金额'].sum():,.2f}**")

# ============ ② 搜索 ============
c_search, c_go = st.columns([4, 1], vertical_alignment="bottom")
keyword = c_search.text_input("🔍 搜索客户", placeholder="", key="search_box")
c_go.button("确定", type="primary")
kw = keyword.strip()

# ============ ③ 客户卡片 ============
if ledger.empty:
    st.info("还没有客户 👉 点下面的「➕ 添加客户」加第一个。"
            "**加完上面会出现他的卡片，拍照和传照片就在卡片里**。"
            "（也可以把左边栏往下滚，点「载入 6 条示例数据」先看看长什么样）")
elif kw:
    hit = (ledger["客户名称"].str.contains(kw, case=False, na=False)
           | ledger["客户位置"].str.contains(kw, case=False, na=False)
           | ledger["备注"].str.contains(kw, case=False, na=False))
    matches = ledger[hit]

    if matches.empty:
        st.warning(f"没找到和「{kw}」有关的客户。")
    else:
        if len(matches) == 1:
            picked = matches.index[0]
        else:
            labels = [f"{i + 1}. {r['客户名称']}"
                      + (f"（{r['客户位置']}）" if r["客户位置"] else "")
                      for i, (_, r) in enumerate(matches.iterrows())]
            pick = st.selectbox(f"匹配到 {len(matches)} 位，点这里选：", labels, key="card_pick")
            picked = matches.index[labels.index(pick)]

        row = ledger.loc[picked]
        unpaid = to_float(row["欠款金额"]) - to_float(row["已收金额"])
        days = days_text(with_unpaid(pd.DataFrame([row])).iloc[0])

        st.markdown(f"#### 👤 {row['客户名称']}　⏰ {days}")
        st.caption(f"欠款 ¥{to_float(row['欠款金额']):,.2f}　已收 ¥{to_float(row['已收金额']):,.2f}"
                   f"　未付 **¥{unpaid:,.2f}**")

        place = st.text_input("📍 位置", value=str(row["客户位置"]),
                              key=f"place_{picked}_{row['客户名称']}",
                              placeholder="例如：城南建材市场 3 号门市")
        if place.strip() != str(row["客户位置"]).strip():
            new = ledger.copy()
            new.loc[picked, "客户位置"] = place.strip()
            update_ledger(new, "✅ 位置已保存")
            st.rerun()

        # ---- 照片 ----
        photos = split_photos(row["照片"])
        st.markdown(f"**📷 照片（{len(photos)} / {MAX_PHOTOS}）**")
        if photos:
            cols = st.columns(3)
            for i, filename in enumerate(photos):
                with cols[i % 3]:
                    src = photo_src(filename)
                    if src:
                        st.image(src, width=130)
                    else:
                        st.caption("⚠️ 照片不见了")
                    if st.button("🗑️ 删这张", key=f"del_{picked}_{i}"):
                        rest = [x for j, x in enumerate(photos) if j != i]
                        delete_photo_file(filename)
                        new = ledger.copy()
                        new.loc[picked, "照片"] = join_photos(rest)
                        update_ledger(new, f"🗑️ 已删除 1 张，还剩 {len(rest)} 张")
                        st.rerun()
        else:
            st.caption("还没有照片，下面拍一张或从相册选一张。")

        if len(photos) >= MAX_PHOTOS:
            st.info(f"已经有 {MAX_PHOTOS} 张了，想换先点「🗑️ 删这张」。")
        else:
            room = MAX_PHOTOS - len(photos)
            st.markdown("**📷 选照片**：点下面的框 —— 手机可以直接拍照或从相册选，"
                        "电脑就从文件夹选（一次可以选多张）")
            ups = st.file_uploader("选择照片",
                                   type=["jpg", "jpeg", "png", "webp"],
                                   accept_multiple_files=True,
                                   label_visibility="collapsed",
                                   key=f"up_{picked}_{st.session_state['photo_key']}")
            pending_photos = []
            for item in (ups or []):
                pending_photos.append((item.getvalue(), "." + item.name.rsplit(".", 1)[-1]))
            if len(pending_photos) > room:
                st.warning(f"最多还能加 {room} 张，这次只存前 {room} 张。")
                pending_photos = pending_photos[:room]
            if st.button(f"💾 保存照片（还能加 {room} 张）", disabled=not pending_photos):
                try:
                    saved = [save_photo(str(row["客户名称"]), blob, suffix)
                             for blob, suffix in pending_photos]
                except CloudError as exc:
                    st.error(str(exc))
                else:
                    new = ledger.copy()
                    new.loc[picked, "照片"] = join_photos(photos + saved)
                    st.session_state["photo_key"] += 1
                    update_ledger(new, f"✅ 已保存 {len(saved)} 张，现在共 {len(photos) + len(saved)} 张")
                    st.rerun()
        st.divider()


# ============ ④ 添加客户 ============
if st.button("➕ 添加客户", type="primary"):
    st.session_state["show_add"] = not st.session_state["show_add"]

if st.session_state["show_add"]:
    with st.form("add_form", clear_on_submit=True):
        name = st.text_input("客户名称 *", placeholder="必填")
        debt = st.number_input("欠款金额(元)", min_value=0.0, step=100.0, format="%.2f")
        paid = st.number_input("已收金额(元)", min_value=0.0, step=100.0, format="%.2f")
        when = st.date_input("欠款日期", value=date.today())
        last_paid = st.date_input("最后收款时间（可留空）", value=None)
        note = st.text_input("备注", placeholder="选填")
        place_new = st.text_input("位置", placeholder="例如：城南建材市场 3 号门市（选填）")
        submitted = st.form_submit_button("✅ 添加到台账")

    st.caption("💡 添加后会自动跳到他的卡片，在那里拍照或从相册选照片。")

    if submitted:
        if not name.strip():
            st.warning("客户名称不能为空")
        else:
            new_row = pd.DataFrame([{
                ID_COL: 0,
                "客户名称": name.strip(),
                "欠款金额": debt,
                "已收金额": paid,
                "欠款日期": pd.Timestamp(when),
                "最后收款时间": pd.Timestamp(last_paid) if last_paid else pd.NaT,
                "备注": note,
                "客户位置": place_new,
                "照片": "",
            }])
            st.session_state["pending_search"] = name.strip()
            update_ledger(pd.concat([ledger, new_row], ignore_index=True),
                          f"✅ 已添加「{name.strip()}」 —— 👆 往上滚一点，他的卡片里有传照片的地方")
            st.rerun()

# ============ ⑤ 明细表 ============
st.subheader("📋 明细（点数字可直接改）")

if kw:
    filtered = data[data["客户名称"].str.contains(kw, case=False, na=False)
                    | data["客户位置"].str.contains(kw, case=False, na=False)
                    | data["备注"].str.contains(kw, case=False, na=False)]
else:
    filtered = data

st.caption(f"共 {len(data)} 位客户，当前显示 {len(filtered)} 位")

base = filtered.reset_index(drop=True)
ids = filtered[ID_COL].tolist()
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
    st.rerun()
elif signature(new_ledger) != signature(ledger):
    update_ledger(new_ledger)
    st.rerun()

# ============ ⑥ 待回款金额 ============
st.subheader("📈 待回款金额")
if data.empty or data["未付金额"].sum() <= 0:
    st.caption("暂无未付金额")
else:
    top = data[data["未付金额"] > 0].nlargest(6, "未付金额")[["客户名称", "未付金额"]].copy()
    top = top.sort_values("未付金额", ascending=False)
    chart_df = top.copy()
    if st.session_state["mask_names"]:
        chart_df["客户名称"] = chart_df["客户名称"].apply(mask_name)
    if alt is None:
        st.bar_chart(top.set_index("客户名称")["未付金额"])
    else:
        bar = (
            alt.Chart(chart_df)
            .mark_bar(color="#E4572E")
            .encode(
                x=alt.X("未付金额:Q", title="未付金额（元）"),
                y=alt.Y("客户名称:N", sort="-x", title=None),
                tooltip=[alt.Tooltip("客户名称:N"), alt.Tooltip("未付金额:Q", format=",.2f")],
            )
            .properties(width="container", height=max(140, 32 * len(chart_df)))
        )
        st.altair_chart(bar)

    pending = data[data["未付金额"] > 0].copy()
    pending["天数"] = pending["欠款日期"].apply(unpaid_days)
    old = pending[pending["天数"] > 90]
    if not old.empty:
        st.caption(f"⏰ 超过 90 天没回款：**{len(old)} 位**，共 ¥{old['未付金额'].sum():,.2f}")
    else:
        st.caption("⏰ 没有超过 90 天还没回款的客户")
