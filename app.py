import io
import re
from datetime import datetime

import google.generativeai as genai
import gspread
import pandas as pd
import plotly.express as px
import pypdfium2 as pdfium
import streamlit as st
from google.oauth2.service_account import Credentials
from PIL import Image


st.set_page_config(page_title="质量检验数据自动采集系统", page_icon="⚙️", layout="wide")
st.title("⚙️ 质量检验数据自动采集系统")
st.markdown("---")
st.info("💡 **操作指南**：上传单据 → AI 自动提取 → 自动存入云端 → 查看图表 → 下载报表")


try:
    api_key = st.secrets["GEMINI_API_KEY"]
    genai.configure(api_key=api_key)
except Exception:
    st.error("⚠️ 未检测到有效的 Gemini API Key。请在 Streamlit Secrets 中配置 `GEMINI_API_KEY`。")
    st.stop()


GSHEET_ID = st.secrets.get("GSHEET_ID", "")
WORKSHEET_NAME = "检验记录"

COLUMNS = [
    "客户代码",
    "生产工单号",
    "客户料号",
    "描述",
    "数量",
    "出货日期",
    "检验数量",
    "检验人员",
    "开始时间",
    "结束时间",
]

OLD_COLUMNS = ["描述", "数量", "出货日期", "检验数量", "检验人员", "开始时间", "结束时间"]
HANDWRITTEN_COLUMNS = ["检验数量", "检验人员", "开始时间", "结束时间"]


def looks_like_work_order(value):
    return bool(re.match(r"^SZ-\d+", str(value).strip(), flags=re.IGNORECASE))


def looks_like_customer_code(value):
    return bool(re.match(r"^\d{5,6}$", str(value).strip()))


def fix_shifted_print_columns(df):
    df = df.copy()
    required = ["客户代码", "生产工单号", "客户料号", "描述", "数量", "出货日期"]
    if any(col not in df.columns for col in required):
        return df

    for idx, row in df.iterrows():
        customer_code = str(row.get("客户代码", "")).strip()
        work_order = str(row.get("生产工单号", "")).strip()

        # Gemini sometimes skips the left customer-code column and starts from the
        # work-order column. Detect that clear pattern and move the printed fields back.
        if looks_like_work_order(customer_code) and not looks_like_work_order(work_order):
            old_values = {col: row.get(col, "") for col in required}
            df.at[idx, "客户代码"] = ""
            df.at[idx, "生产工单号"] = old_values["客户代码"]
            df.at[idx, "客户料号"] = old_values["生产工单号"]
            df.at[idx, "描述"] = old_values["客户料号"]
            df.at[idx, "数量"] = old_values["描述"]
            df.at[idx, "出货日期"] = old_values["数量"]

        # If the customer code was accidentally filled with SO, discard it instead
        # of keeping a misleading value. SO numbers are not part of the output schema.
        customer_code = str(df.at[idx, "客户代码"]).strip()
        if customer_code.upper().startswith("SZ-SO"):
            df.at[idx, "客户代码"] = ""

    return df


def standardize_records(df):
    if df.empty:
        return pd.DataFrame(columns=COLUMNS)

    df = df.copy()
    df.columns = df.columns.astype(str).str.strip()

    aliases = {
        "客户代号": "客户代码",
        "生产工单": "生产工单号",
        "工单号": "生产工单号",
        "工单": "生产工单号",
        "客户料号": "客户料号",
        "客户料號": "客户料号",
        "风扇描述": "描述",
        "型号描述": "描述",
        "品名描述": "描述",
        "出货日期": "出货日期",
        "出貨日期": "出货日期",
        "检验数": "检验数量",
        "检验数量": "检验数量",
        "抽检数量": "检验数量",
        "风扇数量": "检验数量",
        "检验员": "检验人员",
        "检验人员": "检验人员",
        "人员": "检验人员",
        "开始": "开始时间",
        "开始时间": "开始时间",
        "结束": "结束时间",
        "结束时间": "结束时间",
    }
    df = df.rename(columns={col: aliases.get(col, col) for col in df.columns})

    for col in COLUMNS:
        if col not in df.columns:
            df[col] = ""

    df = df[COLUMNS]
    df = fix_shifted_print_columns(df)
    for col in COLUMNS:
        df[col] = df[col].fillna("").astype(str).str.strip()

    df["检验人员"] = df["检验人员"].str.upper().replace({"NAN": "", "NONE": ""})
    return df


@st.cache_resource(show_spinner=False)
def get_worksheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=scopes
    )
    client = gspread.authorize(creds)
    sh = client.open_by_key(GSHEET_ID)

    try:
        ws = sh.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=WORKSHEET_NAME, rows=1000, cols=len(COLUMNS))
        ws.append_row(COLUMNS)
        return ws

    first_row = [x.strip() for x in ws.row_values(1)]
    if not first_row:
        ws.update("A1:J1", [COLUMNS])
    elif first_row != COLUMNS:
        records = ws.get_all_records()
        migrated = standardize_records(pd.DataFrame(records))
        ws.clear()
        ws.append_row(COLUMNS)
        if not migrated.empty:
            ws.append_rows(migrated.values.tolist(), value_input_option="USER_ENTERED")
    return ws


def load_records_from_sheet():
    try:
        ws = get_worksheet()
        records = ws.get_all_records()
        return standardize_records(pd.DataFrame(records))
    except Exception as e:
        st.error(f"❌ 读取云端数据失败：{e}")
        return pd.DataFrame(columns=COLUMNS)


def append_records_to_sheet(df_new):
    if df_new.empty:
        return
    ws = get_worksheet()
    rows = standardize_records(df_new).astype(str).fillna("").values.tolist()
    ws.append_rows(rows, value_input_option="USER_ENTERED")


def overwrite_sheet(df):
    ws = get_worksheet()
    clean_df = standardize_records(df)
    ws.clear()
    ws.append_row(COLUMNS)
    if not clean_df.empty:
        ws.append_rows(clean_df.values.tolist(), value_input_option="USER_ENTERED")


if not GSHEET_ID or "gcp_service_account" not in st.secrets:
    st.error("⚠️ 未检测到 Google Sheets 配置。请在 Streamlit Secrets 中配置 `GSHEET_ID` 和 `[gcp_service_account]`。")
    st.stop()


if "batch_records" not in st.session_state:
    with st.spinner("☁️ 正在从云端加载历史记录..."):
        st.session_state.batch_records = load_records_from_sheet()

st.success(f"☁️ 已连接云端数据库，当前共 **{len(st.session_state.batch_records)}** 条记录")


MODEL_CANDIDATES = [
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.5-flash-lite",
]

MAX_OCR_IMAGE_WIDTH = 1800


def call_gemini_with_fallback(prompt, image):
    last_error = None
    for model_name in MODEL_CANDIDATES:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content([prompt, image])
            return response, model_name
        except Exception as e:
            last_error = e
    raise RuntimeError(f"所有候选模型均无法调用。最后一次错误：{last_error}")


def parse_time(value):
    if pd.isna(value) or str(value).strip() == "":
        return None
    text = str(value).strip().replace(".", ":").replace("-", ":").replace("：", ":")
    for fmt in ("%H:%M", "%H:%M:%S", "%H%M"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.hour * 60 + dt.minute + dt.second / 60
        except ValueError:
            continue
    return None


def calc_duration_and_efficiency(df):
    if df.empty:
        return df

    df = standardize_records(df)
    starts = df["开始时间"].apply(parse_time)
    ends = df["结束时间"].apply(parse_time)

    durations = []
    for start, end in zip(starts, ends):
        if start is None or end is None:
            durations.append(None)
            continue
        duration = end - start
        if duration < 0:
            duration += 24 * 60
        durations.append(round(duration, 1))

    df["时长(分钟)"] = durations
    inspection_qty = pd.to_numeric(df["检验数量"], errors="coerce")
    total_qty = pd.to_numeric(df["数量"], errors="coerce")

    df["效率(件/分钟)"] = [
        round(qty / duration, 2) if pd.notna(qty) and duration and duration > 0 else None
        for qty, duration in zip(inspection_qty, df["时长(分钟)"])
    ]
    df["抽检率(%)"] = [
        round(inspection / total * 100, 1)
        if pd.notna(inspection) and pd.notna(total) and total > 0
        else None
        for inspection, total in zip(inspection_qty, total_qty)
    ]
    return df


def expand_uploads_to_images(files):
    items = []
    for file in files:
        name = file.name
        suffix = name.lower().rsplit(".", 1)[-1]
        if suffix == "pdf":
            try:
                pdf = pdfium.PdfDocument(file.read())
                for idx in range(len(pdf)):
                    page = pdf[idx]
                    image = page.render(scale=2.5).to_pil()
                    items.append((f"{name} - 第 {idx + 1} 页", image))
                pdf.close()
            except Exception as e:
                st.error(f"❌ PDF 文件 `{name}` 解析失败：{e}")
        else:
            try:
                items.append((name, Image.open(file)))
            except Exception as e:
                st.error(f"❌ 图片文件 `{name}` 打开失败：{e}")
    return items


def prepare_image_for_ocr(image):
    image = image.convert("RGB")
    width, height = image.size
    if width <= MAX_OCR_IMAGE_WIDTH:
        return image

    ratio = MAX_OCR_IMAGE_WIDTH / width
    new_size = (MAX_OCR_IMAGE_WIDTH, int(height * ratio))
    return image.resize(new_size, Image.Resampling.LANCZOS)


def is_blank(value):
    return pd.isna(value) or str(value).strip() in ("", "nan", "None", "NONE")


st.subheader("📤 第一步：上传检验单（支持图片或 PDF）")
img_uploads = st.file_uploader(
    "支持一次选择多张图片或多个 PDF 文件；PDF 多页会自动逐页识别",
    type=["jpg", "jpeg", "png", "pdf"],
    accept_multiple_files=True,
)

if img_uploads:
    image_items = expand_uploads_to_images(img_uploads)

    if image_items:
        st.write(f"已加载 **{len(img_uploads)}** 个文件，共 **{len(image_items)}** 张待识别图片")
        with st.expander("🔎 预览所有待识别图片", expanded=False):
            cols = st.columns(min(len(image_items), 3))
            for idx, (display_name, img) in enumerate(image_items):
                with cols[idx % 3]:
                    st.image(img, caption=display_name, use_container_width=True)

        ocr_prompt = """
你是一位专业的质量工程师助手。图片中是一张出货检验记录表，可能包含打印内容和手写记录。

任务：只提取表格中需要记录的列，忽略客户名称、SO 等无关字段。

请逐行返回以下 10 列，列名必须完全一致：
客户代码,生产工单号,客户料号,描述,数量,出货日期,检验数量,检验人员,开始时间,结束时间

字段说明：
1. 客户代码：打印列，必须来自最左侧“客户代码”数字列，例如 103301、103294。不要把 SO 或生产工单号填到这里。
2. 生产工单号：打印列，表头可能为“生产工单号”，通常格式为 SZ-040090、SZ-041317。
3. 客户料号：打印列，表头可能为“客户料号”。
4. 描述：打印的长描述，保留原始内容。
5. 数量：打印的订单数量。
6. 出货日期：打印日期，例如 6/3。保留原样，不要补全年份。
7. 检验数量：人工填写的实际检验数量。
8. 检验人员：人工填写，可能是中文姓名、姓名拼音缩写或 A/B/C/D 等字母。请统一输出大写字母；如果原本就是字母，转为大写；如果是中文姓名，输出拼音首字母大写。
9. 开始时间：人工填写时间，统一为 HH:MM 格式。
10. 结束时间：人工填写时间，统一为 HH:MM 格式。

提取规则：
- 原表左侧通常依次是“客户代码、客户名称、SO、生产工单号、客户料号、描述、数量、出货日期...”。其中“客户名称”和“SO”必须忽略，但忽略后不要窜列：客户代码仍然取最左数字列，生产工单号仍然取 SZ- 开头的生产工单号列。
- 输出前自检每一行：客户代码应该是 5-6 位数字；生产工单号应该以 SZ- 开头。如果客户代码是 SZ- 开头，说明列错位了，必须修正后再输出。
- 只输出有检验记录的行：如果某一行的“检验数量、检验人员、开始时间、结束时间”四项全部为空，跳过该行。
- 如果四个检验记录字段中任意一个有内容，就保留该行，其余空字段留空。
- 有些记录可能写在打印表格外侧或表格下方空白区，但仍然按原表格列的位置横向对齐。请把这些手写续写行也当作正常行识别：写在客户代码列下的是客户代码，写在生产工单号/客户料号/描述/数量/出货日期/检验数量/检验人员/开始时间/结束时间列下的内容分别填入对应字段。
- 对表格下方的续写行，如果某些打印字段缺失但手写检验记录存在，也要返回该行；缺失字段留空，不要因为不在打印网格内就忽略。
- 勾、√、对勾、短竖线或类似标记如果出现在“检验数量”列，通常表示检验 1 件；如果同格能看出明确数字，以数字为准。
- 严格返回标准 CSV 纯文本，不要包含 Markdown、解释、编号或代码块。
"""

        if st.button("🚀 批量识别并保存到云端", type="primary", use_container_width=True):
            progress = st.progress(0)
            success_count = 0
            all_new_rows = []
            error_messages = []

            for idx, (display_name, img) in enumerate(image_items):
                try:
                    ocr_img = prepare_image_for_ocr(img)
                    response, model_name = call_gemini_with_fallback(ocr_prompt, ocr_img)
                    csv_content = response.text.strip().replace("```csv", "").replace("```", "").strip()
                    current_df = pd.read_csv(io.StringIO(csv_content), dtype=str)
                    current_df = standardize_records(current_df)

                    mask_has_data = current_df[HANDWRITTEN_COLUMNS].apply(
                        lambda row: not all(is_blank(value) for value in row), axis=1
                    )
                    current_df = current_df[mask_has_data].reset_index(drop=True)

                    if current_df.empty:
                        continue

                    all_new_rows.append(current_df)
                    success_count += 1
                    st.caption(f"`{display_name}` 已用 `{model_name}` 识别完成，提取 {len(current_df)} 行")
                except Exception as e:
                    error_messages.append(f"❌ `{display_name}` 识别失败：{e}")
                finally:
                    progress.progress((idx + 1) / len(image_items))

            if all_new_rows:
                combined_new = standardize_records(pd.concat(all_new_rows, ignore_index=True))
                try:
                    append_records_to_sheet(combined_new)
                    st.session_state.batch_records = standardize_records(
                        pd.concat([st.session_state.batch_records, combined_new], ignore_index=True)
                    )
                    st.success(
                        f"✅ 成功识别 {success_count}/{len(image_items)} 张图片，"
                        f"新增 {len(combined_new)} 条记录并已同步到云端。"
                    )
                except Exception as e:
                    st.error(f"❌ 写入云端失败：{e}")
            else:
                st.info("本次上传未识别到带有检验记录的行。")

            for msg in error_messages:
                st.warning(msg)


st.markdown("---")
st.subheader("🗂️ 第二步：累计记录管理")

if not st.session_state.batch_records.empty:
    df_display = calc_duration_and_efficiency(st.session_state.batch_records)

    df_editable = df_display.copy()
    df_editable.insert(0, "删除", False)
    df_editable.index = range(1, len(df_editable) + 1)
    df_editable.index.name = "序号"

    st.caption("提示：可直接在表格中修正识别结果。修改后点击“保存修改”同步到云端。")

    edited_df = st.data_editor(
        df_editable,
        use_container_width=True,
        hide_index=False,
        column_config={
            "删除": st.column_config.CheckboxColumn(
                "删除",
                help="勾选要删除的行，然后点击下方“删除选中行”。",
                default=False,
            ),
            "时长(分钟)": st.column_config.NumberColumn(format="%.1f", disabled=True),
            "效率(件/分钟)": st.column_config.NumberColumn(format="%.2f", disabled=True),
            "抽检率(%)": st.column_config.NumberColumn(format="%.1f", disabled=True),
        },
        disabled=["时长(分钟)", "效率(件/分钟)", "抽检率(%)"],
        key="data_editor",
    )

    col_save, col_del, col_dl, col_clear, col_refresh = st.columns(5)

    with col_save:
        if st.button("💾 保存修改", type="primary", use_container_width=True):
            updated = standardize_records(edited_df[COLUMNS]).reset_index(drop=True)
            try:
                with st.spinner("☁️ 正在保存到云端..."):
                    overwrite_sheet(updated)
                st.session_state.batch_records = updated
                st.success("✅ 修改已保存到云端")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 保存失败：{e}")

    with col_del:
        if st.button("🗑️ 删除选中行", use_container_width=True):
            keep_mask = ~edited_df["删除"].fillna(False)
            new_records = standardize_records(edited_df.loc[keep_mask, COLUMNS]).reset_index(drop=True)
            try:
                with st.spinner("☁️ 正在同步到云端..."):
                    overwrite_sheet(new_records)
                st.session_state.batch_records = new_records
                st.success("✅ 已删除并同步到云端")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 同步云端失败：{e}")

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
                st.error(f"❌ 清空云端失败：{e}")

    with col_refresh:
        if st.button("🔄 云端刷新", use_container_width=True):
            st.session_state.batch_records = load_records_from_sheet()
            st.rerun()

    st.markdown("---")
    st.subheader("📊 第三步：效率分析图表")

    chart_df = df_display.dropna(subset=["效率(件/分钟)", "描述", "检验人员"]).copy()
    chart_df = chart_df[chart_df["效率(件/分钟)"] > 0]

    if not chart_df.empty:
        for col in ["描述", "检验人员"]:
            chart_df[col] = chart_df[col].astype(str).str.strip()
        chart_df = chart_df[
            ~chart_df["描述"].isin(["", "nan", "None"])
            & ~chart_df["检验人员"].isin(["", "nan", "None"])
        ]

    if chart_df.empty:
        st.info("暂无可用于图表分析的有效数据，需要包含描述、检验人员、开始时间、结束时间和检验数量。")
    else:
        tab1, tab2, tab3 = st.tabs(["按描述筛选", "检验人员效率", "描述效率"])

        with tab1:
            model_list = sorted(
                {
                    str(value).strip()
                    for value in chart_df["描述"].dropna().tolist()
                    if str(value).strip() not in ("", "nan", "None")
                }
            )
            selected_model = st.selectbox("选择一个描述查看不同检验人员的效率", options=model_list, key="model_filter")
            model_df = chart_df[chart_df["描述"] == selected_model].copy()

            total_qty = pd.to_numeric(model_df["检验数量"], errors="coerce").sum()
            avg_eff = model_df["效率(件/分钟)"].mean()
            avg_rate = pd.to_numeric(model_df["抽检率(%)"], errors="coerce").mean()
            inspector_count = model_df["检验人员"].nunique()
            record_count = len(model_df)

            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("总检验数量", f"{int(total_qty)}")
            m2.metric("平均效率", f"{avg_eff:.2f} 件/分钟")
            m3.metric("平均抽检率", f"{avg_rate:.1f}%" if pd.notna(avg_rate) else "N/A")
            m4.metric("涉及检验人员", f"{inspector_count} 人")
            m5.metric("记录条数", f"{record_count} 条")

            per_inspector = (
                model_df.groupby("检验人员")
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
                x="检验人员",
                y="平均效率",
                color="检验人员",
                text="平均效率",
                title=f"描述【{selected_model}】下各检验人员效率对比",
                hover_data=["总检验数量", "记录条数"],
            )
            fig1.update_traces(textposition="outside")
            fig1.update_layout(yaxis_title="平均效率(件/分钟)", height=450, showlegend=False)
            st.plotly_chart(fig1, use_container_width=True)

            with st.expander("查看该描述的全部检验明细"):
                st.dataframe(
                    model_df[
                        ["客户代码", "生产工单号", "客户料号", "检验人员", "检验数量", "开始时间", "结束时间", "时长(分钟)", "效率(件/分钟)"]
                    ].reset_index(drop=True),
                    use_container_width=True,
                )

        with tab2:
            inspector_stats = (
                chart_df.groupby("检验人员")
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
                x="检验人员",
                y="平均效率",
                color="检验人员",
                text="平均效率",
                title="各检验人员平均效率排名",
                hover_data=["总检验数量", "记录条数"],
            )
            fig2.update_traces(textposition="outside")
            fig2.update_layout(yaxis_title="平均效率(件/分钟)", height=450, showlegend=False)
            st.plotly_chart(fig2, use_container_width=True)

            with st.expander("查看检验人员统计表"):
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
                title="各描述平均效率排名",
                hover_data=["总检验数量", "记录条数"],
            )
            fig3.update_traces(textposition="outside")
            fig3.update_layout(
                xaxis_title="描述",
                yaxis_title="平均效率(件/分钟)",
                xaxis_tickangle=-45,
                height=500,
            )
            st.plotly_chart(fig3, use_container_width=True)

            with st.expander("查看描述统计表"):
                st.dataframe(model_stats, use_container_width=True)
else:
    st.info("当前云端暂无累计记录，请在上方上传图片进行识别。")
