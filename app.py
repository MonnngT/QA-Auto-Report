import csv
import io
import re

import google.generativeai as genai
import pandas as pd
import pypdfium2 as pdfium
import streamlit as st
from PIL import Image


st.set_page_config(page_title="质量检验单识别与导出", page_icon="📋", layout="wide")
st.title("📋 质量检验单识别与导出")
st.markdown("---")
st.info("💡 **操作指南**：上传单据 → AI 自动识别 → 人工确认/修正 → 下载 Excel")


try:
    api_key = st.secrets["GEMINI_API_KEY"]
    genai.configure(api_key=api_key)
except Exception:
    st.error("⚠️ 未检测到有效的 Gemini API Key。请在 Streamlit Secrets 中配置 `GEMINI_API_KEY`。")
    st.stop()


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

HANDWRITTEN_COLUMNS = ["检验数量", "检验人员", "开始时间", "结束时间"]

MODEL_CANDIDATES = [
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.5-flash-lite",
]

MAX_OCR_IMAGE_WIDTH = 1800


def looks_like_work_order(value):
    return bool(re.match(r"^SZ-\d+", str(value).strip(), flags=re.IGNORECASE))


def fix_shifted_print_columns(df):
    df = df.copy().astype(object)
    required = ["客户代码", "生产工单号", "客户料号", "描述", "数量", "出货日期"]
    if any(col not in df.columns for col in required):
        return df

    for idx, row in df.iterrows():
        customer_code = str(row.get("客户代码", "")).strip()
        work_order = str(row.get("生产工单号", "")).strip()

        if looks_like_work_order(customer_code) and not looks_like_work_order(work_order):
            old_values = {
                col: "" if pd.isna(row.get(col, "")) else str(row.get(col, "")).strip()
                for col in required
            }
            df.at[idx, "客户代码"] = ""
            df.at[idx, "生产工单号"] = old_values["客户代码"]
            df.at[idx, "客户料号"] = old_values["生产工单号"]
            df.at[idx, "描述"] = old_values["客户料号"]
            df.at[idx, "数量"] = old_values["描述"]
            df.at[idx, "出货日期"] = old_values["数量"]

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
        "客户代码": "客户代码",
        "生产工单": "生产工单号",
        "生产工单号": "生产工单号",
        "工单号": "生产工单号",
        "工单": "生产工单号",
        "客户料号": "客户料号",
        "客户料號": "客户料号",
        "风扇描述": "描述",
        "型号描述": "描述",
        "品名描述": "描述",
        "描述": "描述",
        "数量": "数量",
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


def is_blank(value):
    return pd.isna(value) or str(value).strip() in ("", "nan", "None", "NONE")


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


def parse_ocr_csv(csv_content):
    rows = []
    reader = csv.reader(io.StringIO(csv_content))

    for raw_row in reader:
        row = [str(value).strip() for value in raw_row]
        if not row or all(value == "" for value in row):
            continue
        if row[0] == "客户代码":
            continue

        if len(row) > len(COLUMNS):
            row = row[:3] + [",".join(row[3:-6])] + row[-6:]
        elif len(row) < len(COLUMNS):
            row = row + [""] * (len(COLUMNS) - len(row))

        rows.append(row[: len(COLUMNS)])

    return pd.DataFrame(rows, columns=COLUMNS)


def build_excel_bytes(df):
    output = io.BytesIO()
    clean_df = standardize_records(df)
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        clean_df.to_excel(writer, index=False, sheet_name="识别结果")
        workbook = writer.book
        worksheet = writer.sheets["识别结果"]
        header_format = workbook.add_format({"bold": True, "bg_color": "#D9EAF7", "border": 1})
        text_format = workbook.add_format({"text_wrap": True, "valign": "top"})

        for col_idx, col_name in enumerate(clean_df.columns):
            worksheet.write(0, col_idx, col_name, header_format)
            width = min(max(len(col_name) + 6, clean_df[col_name].astype(str).str.len().max() + 2), 48)
            worksheet.set_column(col_idx, col_idx, width, text_format)

    return output.getvalue()


OCR_PROMPT = """
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
- 如果某个字段内容里包含英文逗号，请把该字段用英文双引号包起来，确保每一行恰好只有 10 个 CSV 字段。
"""


if "recognized_records" not in st.session_state:
    st.session_state.recognized_records = pd.DataFrame(columns=COLUMNS)


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

        if st.button("🚀 批量识别", type="primary", use_container_width=True):
            progress = st.progress(0)
            success_count = 0
            all_new_rows = []
            error_messages = []

            for idx, (display_name, img) in enumerate(image_items):
                try:
                    ocr_img = prepare_image_for_ocr(img)
                    response, model_name = call_gemini_with_fallback(OCR_PROMPT, ocr_img)
                    csv_content = response.text.strip().replace("```csv", "").replace("```", "").strip()
                    current_df = standardize_records(parse_ocr_csv(csv_content))

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
                st.session_state.recognized_records = standardize_records(
                    pd.concat(all_new_rows, ignore_index=True)
                )
                st.success(
                    f"✅ 成功识别 {success_count}/{len(image_items)} 张图片，"
                    f"共提取 {len(st.session_state.recognized_records)} 条记录。"
                )
            else:
                st.session_state.recognized_records = pd.DataFrame(columns=COLUMNS)
                st.info("本次上传未识别到带有检验记录的行。")

            for msg in error_messages:
                st.warning(msg)


st.markdown("---")
st.subheader("📥 第二步：确认结果并下载 Excel")

records = standardize_records(st.session_state.recognized_records)

if records.empty:
    st.info("暂无识别结果。请先在上方上传图片或 PDF 并点击“批量识别”。")
else:
    editable_df = records.copy()
    editable_df.insert(0, "删除", False)
    editable_df.index = range(1, len(editable_df) + 1)
    editable_df.index.name = "序号"

    edited_df = st.data_editor(
        editable_df,
        use_container_width=True,
        hide_index=False,
        column_config={
            "删除": st.column_config.CheckboxColumn(
                "删除",
                help="勾选要删除的行，然后点击“删除选中行”。",
                default=False,
            )
        },
        key="recognized_editor",
    )

    col_apply, col_download, col_clear = st.columns([1, 1, 1])

    with col_apply:
        if st.button("🗑️ 删除选中行", use_container_width=True):
            keep_mask = ~edited_df["删除"].fillna(False)
            st.session_state.recognized_records = standardize_records(
                edited_df.loc[keep_mask, COLUMNS].reset_index(drop=True)
            )
            st.rerun()

    with col_download:
        download_df = standardize_records(edited_df[COLUMNS])
        st.download_button(
            label="📥 下载 Excel",
            data=build_excel_bytes(download_df),
            file_name="检验单识别结果.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    with col_clear:
        if st.button("🧹 清空结果", use_container_width=True):
            st.session_state.recognized_records = pd.DataFrame(columns=COLUMNS)
            st.rerun()
