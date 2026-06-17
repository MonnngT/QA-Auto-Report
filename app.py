import streamlit as st
import google.generativeai as genai
from PIL import Image
import pandas as pd
import plotly.express as px
import io
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
import pypdfium2 as pdfium

# ==========================================
# 1. 页面配置
# ==========================================
st.set_page_config(page_title="质量检验数据自动采集系统", page_icon="⚙️", layout="wide")
st.title("⚙️ 质量检验数据自动采集系统")
st.markdown("---")
st.info("💡 **操作指南**:上传单据 ➡️ AI 自动提取 ➡️ 自动存入云端 ➡️ 查看图表 ➡️ 下载报表")

# ==========================================
# 2. API Key & Google Sheets 配置
# ==========================================
try:
    api_key = st.secrets["GEMINI_API_KEY"]
    genai.configure(api_key=api_key)
except Exception:
    st.error("⚠️ 未检测到有效的 Gemini API Key。请在 Streamlit Secrets 中配置 GEMINI_API_KEY。")
    st.stop()

GSHEET_ID = st.secrets.get("GSHEET_ID", "")
WORKSHEET_NAME = "检验记录"  # 工作表名(子表),程序会自动创建
COLUMNS = ["描述", "数量", "出货日期", "检验数量", "检验员", "开始时间", "结束时间"]


@st.cache_resource(show_spinner=False)
def get_worksheet():
    """连接 Google Sheets 并返回 worksheet 对象。缓存连接以提速。"""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=scopes
    )
    client = gspread.authorize(creds)
    sh = client.open_by_key(GSHEET_ID)

    # 如果工作表不存在则自动创建,并写入表头
    try:
        ws = sh.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=WORKSHEET_NAME, rows=1000, cols=10)
        ws.append_row(COLUMNS)

    # 如果第一行是空的(全新表),也补上表头
    first_row = ws.row_values(1)
    if not first_row:
        ws.update("A1:G1", [COLUMNS])
    return ws


def load_records_from_sheet():
    """从 Google Sheets 读取全部记录到 DataFrame。"""
    try:
        ws = get_worksheet()
        records = ws.get_all_records()
        df = pd.DataFrame(records)
        # 确保 5 列都存在
        for col in COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df[COLUMNS] if not df.empty else pd.DataFrame(columns=COLUMNS)
    except Exception as e:
        st.error(f"❌ 读取云端数据失败:{e}")
        return pd.DataFrame(columns=COLUMNS)


def append_records_to_sheet(df_new):
    """追加新记录到云端。df_new 仅包含 COLUMNS 中的列。"""
    if df_new.empty:
        return
    ws = get_worksheet()
    rows = df_new[COLUMNS].astype(str).fillna("").values.tolist()
    ws.append_rows(rows, value_input_option="USER_ENTERED")


def overwrite_sheet(df):
    """用整个 DataFrame 覆盖云端的工作表内容(用于删除/清空)。"""
    ws = get_worksheet()
    ws.clear()
    ws.append_row(COLUMNS)
    if not df.empty:
        rows = df[COLUMNS].astype(str).fillna("").values.tolist()
        ws.append_rows(rows, value_input_option="USER_ENTERED")


# 检查 Google Sheets 配置
if not GSHEET_ID or "gcp_service_account" not in st.secrets:
    st.error(
        "⚠️ 未检测到 Google Sheets 配置。请在 Streamlit Secrets 中配置 "
        "`GSHEET_ID` 和 `[gcp_service_account]`。"
    )
    st.stop()

# ==========================================
# 3. 启动时从 Google Sheets 加载历史数据
# ==========================================
if "batch_records" not in st.session_state:
    with st.spinner("☁️ 正在从云端加载历史记录..."):
        st.session_state.batch_records = load_records_from_sheet()

# 显示云端连接状态
st.success(
    f"☁️ 已连接云端数据库 · 当前共 **{len(st.session_state.batch_records)}** 条记录"
)

# ==========================================
# 4. 模型候选列表 - 自动降级
# ==========================================
MODEL_CANDIDATES = [
    "gemini-flash-lite-latest",  # 自动追新的轻量版别名:最便宜、配额消耗最友好
    "gemini-3.1-flash-lite",     # 明确版本号兜底(当前轻量主力)
    "gemini-flash-latest",       # 自动追新的标准 Flash 别名
    "gemini-3.5-flash",          # 明确版本号兜底(当前标准 Flash,手写难识别时更准)
]


class QuotaExceededError(Exception):
    """专门标记配额超限(429)的异常。"""
    pass


def call_gemini_with_fallback(prompt, image):
    last_error = None
    for model_name in MODEL_CANDIDATES:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content([prompt, image])
            return response, model_name
        except Exception as e:
            last_error = e
            err_text = str(e).lower()
            # 配额超限(429):换模型也没用(配额按项目算),立即停止重试
            if "429" in err_text or "quota" in err_text or "exceeded" in err_text or "resource_exhausted" in err_text:
                raise QuotaExceededError(
                    "今日 Gemini 免费额度已用完(每天上限 20 次)。请等待太平洋时间午夜重置"
                    "(约北京时间下午 3-4 点),或在 Google AI Studio 升级为付费计划。"
                ) from e
            # 模型不存在/下线(404):快速跳到下一个候选
            continue
    raise RuntimeError(f"所有候选模型均无法调用。最后一次错误: {last_error}")


# ==========================================
# 5. 时长与效率计算
# ==========================================
def parse_time(t):
    if pd.isna(t) or str(t).strip() == "":
        return None
    s = str(t).strip().replace(".", ":").replace("-", ":")
    for fmt in ("%H:%M", "%H:%M:%S", "%H%M"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.hour * 60 + dt.minute + dt.second / 60
        except ValueError:
            continue
    return None


def calc_duration_and_efficiency(df):
    if df.empty:
        return df
    df = df.copy()

    # 检验员字段统一大写(避免 y 和 Y 在图表里分成两组)
    if "检验员" in df.columns:
        df["检验员"] = df["检验员"].astype(str).str.strip().str.upper().replace({"NAN": "", "NONE": ""})

    starts = df["开始时间"].apply(parse_time)
    ends = df["结束时间"].apply(parse_time)
    durations = []
    for s, e in zip(starts, ends):
        if s is None or e is None:
            durations.append(None)
        else:
            d = e - s
            if d < 0:
                d += 24 * 60
            durations.append(round(d, 1))
    df["时长(分钟)"] = durations

    qty = pd.to_numeric(df["检验数量"], errors="coerce")
    df["效率(件/分钟)"] = [
        round(q / d, 2) if (pd.notna(q) and d and d > 0) else None
        for q, d in zip(qty, df["时长(分钟)"])
    ]

    # 抽检率 = 检验数量 / 数量,百分比保留 1 位
    total_qty = pd.to_numeric(df["数量"], errors="coerce")
    df["抽检率(%)"] = [
        round(q / t * 100, 1) if (pd.notna(q) and pd.notna(t) and t > 0) else None
        for q, t in zip(qty, total_qty)
    ]
    return df


# ==========================================
# 6. 图像/PDF 上传与识别
# ==========================================
def expand_uploads_to_images(files):
    """把上传的文件(图片或 PDF)统一展开成 [(显示名, PIL.Image), ...]。
    PDF 会被逐页转成图片。使用 pypdfium2(纯 Python,无需系统依赖)。"""
    items = []
    for f in files:
        name = f.name
        suffix = name.lower().rsplit(".", 1)[-1]
        if suffix == "pdf":
            try:
                pdf_bytes = f.read()
                pdf = pdfium.PdfDocument(pdf_bytes)
                # scale=2.0 约等于 200 DPI(默认 72 DPI × 2);手写识别建议 ≥2.0
                for idx in range(len(pdf)):
                    page = pdf[idx]
                    pil_img = page.render(scale=2.5).to_pil()
                    items.append((f"{name} - 第{idx + 1}页", pil_img))
                pdf.close()
            except Exception as e:
                st.error(f"❌ PDF 文件 `{name}` 解析失败:{e}")
        else:
            try:
                items.append((name, Image.open(f)))
            except Exception as e:
                st.error(f"❌ 图片文件 `{name}` 打开失败:{e}")
    return items


st.subheader("📤 第一步:上传检验单(支持图片或 PDF)")
img_uploads = st.file_uploader(
    "支持一次选择多张图片或多个 PDF 文件;PDF 多页将自动逐页识别",
    type=["jpg", "jpeg", "png", "pdf"],
    accept_multiple_files=True,
)

if img_uploads:
    # 展开成统一的图片列表(PDF 会按页拆开)
    image_items = expand_uploads_to_images(img_uploads)

    if image_items:
        st.write(
            f"已加载 **{len(img_uploads)}** 个文件,共 **{len(image_items)}** 张待识别图片"
        )
        with st.expander("🔍 预览所有待识别图片", expanded=False):
            cols = st.columns(min(len(image_items), 3))
            for idx, (display_name, img) in enumerate(image_items):
                with cols[idx % 3]:
                    st.image(img, caption=display_name, use_container_width=True)

        ocr_prompt = """
        你是一位专业的质量工程师助手。图片中是一张出货检验记录表,包含印刷文字和手写记录。

        任务:精准提取每行数据,并忽略无关信息。
        我只需要你提取并返回以下 7 列数据:
        1. 描述 (印刷的长描述。原始表头可能写作"描述"或"风扇描述",统一按"描述"输出)
        2. 数量 (印刷的订单总数量。原始表头通常就叫"数量",代表这个订单一共有多少件,与"检验数量"不同)
        3. 出货日期 (印刷的日期,如 "5/11"。保留原样输出,不要补全年份)
        4. 检验数量 (人工手写的数字,代表实际抽检了多少件。原始表头可能写作"检验数量"或"风扇数量")
        5. 检验员 (人工手写,可能是单个汉字姓如 '杨/王/田',也可能是汉字姓的首字母如 'Y/W/T'。统一规范化为**大写字母**输出:汉字"杨"输出"Y",汉字"王"输出"W",汉字"田"输出"T",汉字"周"输出"Z",其他汉字按拼音首字母大写。如果原本就是字母,统一转大写。)
        6. 开始时间 (手写的时间,例如 11:30)
        7. 结束时间 (手写的时间,例如 11:35)

        提取规则:
        - 严格按照这7个表头输出:描述,数量,出货日期,检验数量,检验员,开始时间,结束时间
        - **只输出有手写记录的行**:如果某一行的"检验数量"、"检验员"、"开始时间"、"结束时间"这 4 个手写字段全部为空,请直接跳过该行,不要返回。
        - 只要 4 个手写字段中任意一个填了内容,就保留该行,其他空字段留空。
        - 时间统一输出为 HH:MM 格式(例如 09:05、14:30)。
        - 请直接返回标准的 CSV 格式纯文本,不要包含任何 Markdown 标记。
        """

        # 免费层配额提示:每张图片/每页 PDF 消耗 1 次请求
        st.caption(
            f"⏳ 免费层每天每个模型上限约 20 次请求。本次将识别 **{len(image_items)}** 张图片"
            f"(每张消耗 1 次)。如接近上限,建议分批上传或升级付费层级。"
        )

        if st.button("🚀 批量识别并保存到云端", type="primary", use_container_width=True):
            progress = st.progress(0)
            success_count = 0
            all_new_rows = []
            error_messages = []
            quota_hit = False  # 标记是否撞上配额上限

            for i, (display_name, img) in enumerate(image_items):
                try:
                    response, _ = call_gemini_with_fallback(ocr_prompt, img)

                    csv_content = (
                        response.text.strip().replace("```csv", "").replace("```", "").strip()
                    )
                    current_df = pd.read_csv(io.StringIO(csv_content))
                    current_df.columns = current_df.columns.str.strip()

                    # 仅保留 5 个标准列
                    for col in COLUMNS:
                        if col not in current_df.columns:
                            current_df[col] = ""
                    current_df = current_df[COLUMNS]

                    # 兜底过滤:4 个手写字段全为空的行直接丢弃(防止 AI 没听指令)
                    hand_cols = ["检验数量", "检验员", "开始时间", "结束时间"]
                    def _is_blank(v):
                        return pd.isna(v) or str(v).strip() in ("", "nan", "None")
                    mask_has_data = current_df[hand_cols].apply(
                        lambda row: not all(_is_blank(v) for v in row), axis=1
                    )
                    current_df = current_df[mask_has_data].reset_index(drop=True)

                    if current_df.empty:
                        # 整页都没有手写记录,跳过
                        continue

                    all_new_rows.append(current_df)
                    success_count += 1
                except QuotaExceededError as qe:
                    # 配额用完:继续识别后面的也一样会失败,直接中断整批
                    quota_hit = True
                    progress.progress(1.0)
                    break
                except Exception as e:
                    error_messages.append(f"❌ `{display_name}` 识别失败:{e}")
                finally:
                    if not quota_hit:
                        progress.progress((i + 1) / len(image_items))

            # 配额超限的专门提示
            if quota_hit:
                st.error(
                    "🚫 今日 Gemini 免费额度已用完(每天上限 20 次请求)。\n\n"
                    "**解决办法:**\n"
                    "- 等待额度重置:太平洋时间午夜重置,约北京时间今天下午 3-4 点\n"
                    "- 或升级付费:打开 https://aistudio.google.com/billing 绑卡升级为按量付费"
                    "(Flash 识别一张单子仅几厘钱,升级后每日上限大幅提高,无需改代码)"
                )

            # 一次性写入云端,避免多次 API 调用
            if all_new_rows:
                combined_new = pd.concat(all_new_rows, ignore_index=True)
                try:
                    append_records_to_sheet(combined_new)
                    st.session_state.batch_records = pd.concat(
                        [st.session_state.batch_records, combined_new], ignore_index=True
                    )
                    st.success(
                        f"✅ 成功识别 {success_count}/{len(image_items)} 张图片,"
                        f"新增 {len(combined_new)} 条记录并已同步到云端。"
                    )
                except Exception as e:
                    st.error(f"❌ 写入云端失败:{e}")

            for msg in error_messages:
                st.warning(msg)


# ==========================================
# 7. 数据展示、删除、下载
# ==========================================
st.markdown("---")
st.subheader("🗂️ 第二步:累计记录管理")

if not st.session_state.batch_records.empty:
    df_display = calc_duration_and_efficiency(st.session_state.batch_records)

    df_editable = df_display.copy()
    df_editable.insert(0, "🗑️删除", False)
    # 行号从 1 开始(更符合人类阅读习惯,从 0 开始容易看错)
    df_editable.index = range(1, len(df_editable) + 1)
    df_editable.index.name = "序号"

    st.caption("💡 提示:可直接在表格中修改 **描述、数量、出货日期、检验数量、检验员、开始时间、结束时间**(识别错误时手动修正)。改完点【💾 保存修改】生效。时长、效率、抽检率会自动重新计算。")

    edited_df = st.data_editor(
        df_editable,
        use_container_width=True,
        hide_index=False,
        column_config={
            "🗑️删除": st.column_config.CheckboxColumn(
                "🗑️删除",
                help="勾选要删除的行,然后点击下方【删除选中行】",
                default=False,
            ),
            "时长(分钟)": st.column_config.NumberColumn(format="%.1f", disabled=True, help="由开始/结束时间自动计算"),
            "效率(件/分钟)": st.column_config.NumberColumn(format="%.2f", disabled=True, help="由检验数量÷时长自动计算"),
            "抽检率(%)": st.column_config.NumberColumn(format="%.1f", disabled=True, help="由检验数量÷数量自动计算"),
        },
        # 只锁定计算列,7 列原始数据允许编辑
        disabled=["时长(分钟)", "效率(件/分钟)", "抽检率(%)"],
        key="data_editor",
    )

    col_save, col_del, col_dl, col_clear, col_refresh = st.columns(5)

    with col_save:
        if st.button("💾 保存修改", type="primary", use_container_width=True):
            # 取出编辑后的 5 列原始数据(去掉删除列和计算列)
            updated = edited_df[COLUMNS].copy().reset_index(drop=True)
            try:
                with st.spinner("☁️ 正在保存到云端..."):
                    overwrite_sheet(updated)
                st.session_state.batch_records = updated
                st.success("✅ 修改已保存到云端")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 保存失败:{e}")

    with col_del:
        if st.button("🗑️ 删除选中行", use_container_width=True):
            keep_mask = ~edited_df["🗑️删除"].fillna(False)
            # 同时保留用户在其他列的编辑内容
            new_records = edited_df.loc[keep_mask, COLUMNS].reset_index(drop=True)
            try:
                with st.spinner("☁️ 正在同步到云端..."):
                    overwrite_sheet(new_records)
                st.session_state.batch_records = new_records
                st.success("✅ 已删除并同步到云端")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 同步云端失败:{e}")

    with col_dl:
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            df_display.to_excel(writer, index=False, sheet_name="检验记录")
        st.download_button(
            label="📥 下载",
            data=output.getvalue(),
            file_name="检验记录汇总表.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    with col_clear:
        if st.button("🧹 清空全部", use_container_width=True):
            try:
                with st.spinner("☁️ 正在清空云端..."):
                    overwrite_sheet(pd.DataFrame(columns=COLUMNS))
                st.session_state.batch_records = pd.DataFrame(columns=COLUMNS)
                st.success("✅ 已清空")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 清空云端失败:{e}")

    with col_refresh:
        if st.button("🔄 云端刷新", use_container_width=True):
            st.session_state.batch_records = load_records_from_sheet()
            st.rerun()

    # ==========================================
    # 8. 效率图表
    # ==========================================
    st.markdown("---")
    st.subheader("📊 第三步:效率分析图表")

    chart_df = df_display.dropna(subset=["效率(件/分钟)", "描述", "检验员"]).copy()
    chart_df = chart_df[chart_df["效率(件/分钟)"] > 0]

    # 统一清洗:把描述和检验员转为干净的字符串,排除 nan/空字符串这些脏数据
    if not chart_df.empty:
        for col in ["描述", "检验员"]:
            chart_df[col] = chart_df[col].astype(str).str.strip()
        chart_df = chart_df[
            ~chart_df["描述"].isin(["", "nan", "None"])
            & ~chart_df["检验员"].isin(["", "nan", "None"])
        ]

    if chart_df.empty:
        st.info("暂无可用于图表分析的有效数据(需要包含描述、检验员、开始时间、结束时间、检验数量)。")
    else:
        tab1, tab2, tab3 = st.tabs(["🔍 按型号筛选", "👤 检验员效率", "🔧 型号效率"])

        with tab1:
            # 选型号下拉框
            # 先转字符串再去重排序,避免混合类型(字符串 + NaN/数字)导致 sorted 报错
            model_list = sorted(
                set(
                    str(x).strip()
                    for x in chart_df["描述"].dropna().tolist()
                    if str(x).strip() not in ("", "nan", "None")
                )
            )
            selected_model = st.selectbox(
                "选择一个型号查看该型号下不同检验员的效率",
                options=model_list,
                key="model_filter",
            )

            # 该型号下的数据
            model_df = chart_df[chart_df["描述"] == selected_model].copy()

            # 顶部摘要信息
            total_qty = pd.to_numeric(model_df["检验数量"], errors="coerce").sum()
            avg_eff = model_df["效率(件/分钟)"].mean()
            avg_rate = pd.to_numeric(model_df.get("抽检率(%)", pd.Series(dtype=float)), errors="coerce").mean()
            inspector_count = model_df["检验员"].nunique()
            record_count = len(model_df)

            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("📦 总检验数量", f"{int(total_qty)}")
            m2.metric("⚡ 平均效率", f"{avg_eff:.2f} 件/分")
            m3.metric("🎯 平均抽检率", f"{avg_rate:.1f}%" if pd.notna(avg_rate) else "N/A")
            m4.metric("👥 涉及检验员", f"{inspector_count} 人")
            m5.metric("📝 记录条数", f"{record_count} 条")

            # 按检验员聚合该型号的效率
            per_inspector = (
                model_df.groupby("检验员")
                .agg(
                    平均效率=("效率(件/分钟)", "mean"),
                    总检验数量=("检验数量", lambda x: pd.to_numeric(x, errors="coerce").sum()),
                    记录条数=("效率(件/分钟)", "count"),
                )
                .reset_index()
                .sort_values("平均效率", ascending=False)
            )
            per_inspector["平均效率"] = per_inspector["平均效率"].round(2)

            fig1 = px.bar(
                per_inspector,
                x="检验员",
                y="平均效率",
                color="检验员",
                text="平均效率",
                title=f"型号【{selected_model}】下各检验员效率对比",
                hover_data=["总检验数量", "记录条数"],
            )
            fig1.update_traces(textposition="outside")
            fig1.update_layout(
                yaxis_title="平均效率(件/分钟)",
                height=450,
                showlegend=False,
            )
            st.plotly_chart(fig1, use_container_width=True)

            # 明细表
            with st.expander("📋 查看该型号的全部检验明细"):
                st.dataframe(
                    model_df[
                        ["检验员", "检验数量", "开始时间", "结束时间", "时长(分钟)", "效率(件/分钟)"]
                    ].reset_index(drop=True),
                    use_container_width=True,
                )

        with tab2:
            inspector_stats = (
                chart_df.groupby("检验员")
                .agg(
                    平均效率=("效率(件/分钟)", "mean"),
                    总检验数量=("检验数量", lambda x: pd.to_numeric(x, errors="coerce").sum()),
                    记录条数=("效率(件/分钟)", "count"),
                )
                .reset_index()
                .sort_values("平均效率", ascending=False)
            )
            inspector_stats["平均效率"] = inspector_stats["平均效率"].round(2)

            fig2 = px.bar(
                inspector_stats,
                x="检验员",
                y="平均效率",
                color="检验员",
                text="平均效率",
                title="各检验员平均效率排名",
                hover_data=["总检验数量", "记录条数"],
            )
            fig2.update_traces(textposition="outside")
            fig2.update_layout(yaxis_title="平均效率(件/分钟)", height=450, showlegend=False)
            st.plotly_chart(fig2, use_container_width=True)

            with st.expander("📋 查看检验员统计表"):
                st.dataframe(inspector_stats, use_container_width=True)

        with tab3:
            model_stats = (
                chart_df.groupby("描述")
                .agg(
                    平均效率=("效率(件/分钟)", "mean"),
                    总检验数量=("检验数量", lambda x: pd.to_numeric(x, errors="coerce").sum()),
                    记录条数=("效率(件/分钟)", "count"),
                )
                .reset_index()
                .sort_values("平均效率", ascending=False)
            )
            model_stats["平均效率"] = model_stats["平均效率"].round(2)

            fig3 = px.bar(
                model_stats,
                x="描述",
                y="平均效率",
                text="平均效率",
                title="各型号平均效率排名",
                hover_data=["总检验数量", "记录条数"],
            )
            fig3.update_traces(textposition="outside")
            fig3.update_layout(
                xaxis_title="型号(描述)",
                yaxis_title="平均效率(件/分钟)",
                xaxis_tickangle=-45,
                height=500,
            )
            st.plotly_chart(fig3, use_container_width=True)

            with st.expander("📋 查看型号统计表"):
                st.dataframe(model_stats, use_container_width=True)
else:
    st.info("当前云端暂无累计记录,请在上方上传图片进行识别。")
