# -*- coding: utf-8 -*-
"""
💰 客户欠款台账 · 第二阶段（Supabase 云端版 · v8）
=========================================================
和第一阶段比：**界面一模一样**，只把「数据层」从 CSV 换成了云端数据库。

    USE_CLOUD = True   → 数据存 Supabase 云端（手机/电脑看到的是同一份）
    USE_CLOUD = False  → 退回本地 data/ledger.csv（断网也能用）

配置放在 .streamlit/secrets.toml 里：
    SUPABASE_URL = "https://xxxx.supabase.co"
    SUPABASE_ANON_KEY = "sb_publishable_..."

★ 为什么每行都要一个 id？
   云端每行都有「身份证号」id。有它才能「按行更新 / 按行删除」；
   没有 id 就只能全删再全插，中途失败会丢数据。
   id = 0 表示「这行还没存到云端」，保存时走 insert。

⚠️ 照片目前仍存在本机 photos/ 文件夹。
   等要部署到公网之前，我会把照片也接到云端存储（Supabase Storage），
   否则部署后照片会丢 —— 这一步我会在部署前做，你别自己动。

运行：streamlit run app.py
"""

from __future__ import annotations

import io
import re
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
USE_CLOUD = True                 # ← 想退回本地 CSV 模式，改成 False

TABLE = "ledger"                 # Supabase 里的表名
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_FILE = DATA_DIR / "ledger.csv"
PHOTO_DIR = BASE_DIR / "photos"
MAX_PHOTOS = 6

# 数据库用英文列名（Postgres 里中文列名要加引号，容易出错），这里做映射
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
# 1. 数据层 A：Supabase 云端
# =====================================================================
def get_secret(name: str, default: str = "") -> str:
    """从 .streamlit/secrets.toml 读配置；没有这个文件也不会崩。"""
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
                         "请检查 .streamlit/secrets.toml 的文件夹名、文件名、内容")
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
        raise CloudError(f"读取云端数据失败：{exc}（表名、列名、密钥都检查一下）") from exc
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
    """
    写回云端，三步走：
      ① 先记下云端现有的所有 id（等下用它算「哪些被删了」）
      ② 新行(id=0) insert，老行(id>0) upsert
      ③ 云端有、本地没有的 id → 删掉
    顺序不能反：先插后记的话，刚插入的新行会被当成「多余的」删掉。
    """
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
# 2. 数据层 B：本地 CSV（备用，USE_CLOUD = False 时用）
# =====================================================================
def load_csv() -> pd.DataFrame:
    if not DATA_FILE.exists():
        return empty_df()
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            df = normalize(pd.read_csv(DATA_FILE, encoding=enc))
            df[ID_COL] = range(1, len(df) + 1)      # 本地模式：行号当身份证号
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
    """界面只认这一个入口：它不关心数据是云端来的还是 CSV 来的。"""
    if USE_CLOUD:
        try:
            st.session_state["cloud_error"] = ""
            return load_cloud()
        except CloudError as exc:
            st.session_state["cloud_error"] = str(exc)
            return empty_df()
    return load_csv()


def save_data(df: pd.DataFrame) -> bool:
    """True = 存成功；False = 没存上（界面会把本地这份先留着）。"""
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
            df[ID_COL] = 0              # 导入的一律当新行，交给云端发新 id
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
    """按 id 找行合并（不靠行号，安全）。先改后删。"""
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


def safe_name(text: str, limit: int = 16) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", str(text)).strip("_")
    return (cleaned or "客户")[:limit]


def save_photo(customer: str, data: bytes, suffix: str = ".jpg") -> str:
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    suffix = suffix if suffix.startswith(".") else "." + suffix
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    filename = f"{safe_name(customer)}_{stamp}{suffix.lower()}"
    (PHOTO_DIR / filename).write_bytes(data)
    return filename


def photo_file(filename: str) -> Path | None:
    if not filename:
        return None
    path = PHOTO_DIR / str(filename)
    return path if path.exists() else None


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
    st.session_state["show_add"] = bool(st.session_state["ledger"].empty)
if "mask_names" not in st.session_state:
    st.session_state["mask_names"] = False
if "cloud_error" not in st.session_state:
    st.session_state["cloud_error"] = ""
if "flash" not in st.session_state:
    st.session_state["flash"] = ""


def set_flash(msg: str) -> None:
    st.session_state["flash"] = msg


def update_ledger(df: pd.DataFrame, msg: str = "") -> None:
    """统一写入口：保存 → 重新拉一遍（保证本地和云端一致）→ 重置表格。"""
    df = normalize(df)
    if save_data(df):
        st.session_state["ledger"] = load_data()
    else:
        st.session_state["ledger"] = df          # 存不上就先留着，别让用户以为白改了
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
            st.success("☁️ 已连接云端数据库（手机电脑同一份数据）")
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
    st.caption("编码 UTF-8-BOM，Excel 不乱码。")

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
keyword = st.text_input("🔍 搜索客户", placeholder="输入「李」就能列出所有姓李的（打完按键盘的搜索/前往）",
                        key="search_box")
kw = keyword.strip()

# ============ ③ 客户卡片 ============
if ledger.empty:
    st.info("还没有客户 👉 点下面的「➕ 添加客户」加第一个，或在左侧点「载入 6 条示例数据」。")
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

        photos = split_photos(row["照片"])
        st.markdown(f"**📷 照片（{len(photos)} / {MAX_PHOTOS}）**")
        if photos:
            cols = st.columns(3)
            for i, filename in enumerate(photos):
                with cols[i % 3]:
                    path = photo_file(filename)
                    if path is not None:
                        st.image(str(path), width=130)
                    else:
                        st.caption("⚠️ 文件丢失")
                    if st.button("🗑️ 删这张", key=f"del_{picked}_{i}"):
                        rest = [x for j, x in enumerate(photos) if j != i]
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
            shot = st.camera_input("📷 拍照", key=f"cam_{picked}_{st.session_state['photo_key']}")
            ups = st.file_uploader("🖼️ 从相册 / 电脑选（可一次选多张）",
                                   type=["jpg", "jpeg", "png", "webp"],
                                   accept_multiple_files=True,
                                   key=f"up_{picked}_{st.session_state['photo_key']}")
            pending_photos = []
            if shot is not None:
                pending_photos.append((shot.getvalue(), ".jpg"))
            for item in (ups or []):
                pending_photos.append((item.getvalue(), "." + item.name.rsplit(".", 1)[-1]))
            if len(pending_photos) > room:
                st.warning(f"最多还能加 {room} 张，这次只存前 {room} 张。")
                pending_photos = pending_photos[:room]
            if st.button(f"💾 保存照片（还能加 {room} 张）", disabled=not pending_photos):
                saved = [save_photo(str(row["客户名称"]), blob, suffix)
                         for blob, suffix in pending_photos]
                new = ledger.copy()
                new.loc[picked, "照片"] = join_photos(photos + saved)
                st.session_state["photo_key"] += 1
                update_ledger(new, f"✅ 已保存 {len(saved)} 张，现在共 {len(photos) + len(saved)} 张")
                st.rerun()
        st.divider()

# ============ ④ 添加客户 ============
if st.button("➕ 添加客户" if not st.session_state["show_add"] else "➖ 收起添加表单",
             type="primary"):
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
                          f"✅ 已添加「{name.strip()}」")
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
ids = filtered[ID_COL].tolist()          # 每一行的身份证号
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
except TypeError:          # 老版本 Streamlit 不认 width，就用旧写法
    edited = st.data_editor(view, use_container_width=True, **editor_args)

st.caption("💡 金额双击就能改，**自动保存**；删客户：勾选「删除」再点下面按钮。")

b1, b2, _ = st.columns([1, 1, 3])
delete_clicked = b1.button("🗑️ 删除勾选客户", type="primary")

new_ledger, deleted = merge_edits(ledger, base, edited, ids, apply_delete=delete_clicked)

if deleted:
    update_ledger(new_ledger, f"🗑️ 已删除 {deleted} 位客户")
    st.rerun()
elif signature(new_ledger) != signature(ledger):
    update_ledger(new_ledger)          # 写云端 → 重新拉取 → 刷新看板
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
