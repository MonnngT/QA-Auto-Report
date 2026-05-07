import streamlit as st
import google.generativeai as genai
from PIL import Image
import pandas as pd
import plotly.express as px
import io
from datetime import datetime

# ==========================================
# 1. 页面配置与初始化
# ==========================================
st.set_page_config(page_title="风扇组件检验录入系统", page_icon="⚙️", layout="wide")
st.title("⚙️ 质量检验数据自动采集系统 V3.0")
st.markdown("---")
st.info("💡 **操作指南**:上传单据 ➡️ AI 自动提取 ➡️ 自动计算时长与效率 ➡️ 查看图表 ➡️ 下载报表")

# 初始化 Session State
if 'batch_records' not in st.session_state:
    st.session_state.batch_records = pd.DataFrame()

# ==========================================
# 2. API Key 配置
# ==========================================
try:
    api_key = st.secrets["GEMINI_API_KEY"]
    genai.configure(api_key=api_key)
except Exception:
    st.error("⚠️ 未检测到有效的 API Key。请确保在 Streamlit 后台 Secrets 中正确配置 GEMINI_API_KEY。")
    st.stop()

# ==========================================
# 3. 模型候选列表 - 自动降级
# ==========================================
MODEL_CANDIDATES = [
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.5-flash-lite",
]

def call_gemini_with_fallback(prompt, image):
    """依次尝试候选模型,直到成功为止。"""
    last_error = None
    for model_name in MODEL_CANDIDATES:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content([prompt, image])
            return response, model_name
        except Exception as e:
            last_error = e
            continue
    raise RuntimeError(f"所有候选模型均无法调用。最后一次错误: {last_error}")

# ==========================================
# 4. 时长与效率计算工具函数
# ==========================================
def parse_time(t):
    """把各种格式的时间字符串解析为分钟数(从 00:00 起算)。无法解析返回 None。"""
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
    """根据开始/结束时间和检验数量计算时长(分钟)和效率(件/分钟)。"""
    if df.empty:
        return df
    df = df.copy()

    starts = df["开始时间"].apply(parse_time)
    ends = df["结束时间"].apply(parse_time)
    durations = []
    for s, e in zip(starts, ends):
        if s is None or e is None:
            durations.append(None)
        else:
            d = e - s
            if d < 0:  # 跨日
                d += 24 * 60
            durations.append(round(d, 1))
    df["时长(分钟)"] = durations

    qty = pd.to_numeric(df["检验数量"], errors="coerce")
    df["效率(件/分钟)"] = [
        round(q / d, 2) if (pd.notna(q) and d and d > 0) else None
        for q, d in zip(qty, df["时长(分钟)"])
    ]
    return df

# ==========================================
# 5. 图像上传区(已去除拍照)
# ==========================================
st.subheader("📤 第一步:上传检验单图片")
img_uploads = st.file_uploader(
    "支持一次选择多张图片",
    type=['jpg', 'jpeg', 'png'],
    accept_multiple_files=True
)

if img_uploads:
    st.write(f"已选择 **{len(img_uploads)}** 张图片")
    with st.expander("🔍 预览所有图片", expanded=False):
        cols = st.columns(min(len(img_uploads), 3))
        for idx, file in enumerate(img_uploads):
            with cols[idx % 3]:
                st.image(Image.open(file), caption=file.name, use_container_width=True)

    # ==========================================
    # 6. AI 智能提取
    # ==========================================
    ocr_prompt = """
    你是一位专业的质量工程师助手。图片中是一张风扇出货检验记录表,包含印刷文字和手写记录。

    任务:精准提取每行数据,并忽略无关信息。
    我只需要你提取并返回以下 5 列数据:
    1. 描述 (印刷的长描述,例如 "788/9-9/36/PAG/EMAX4L/28/8/62/A")
    2. 检验数量 (人工手写的数字)
    3. 检验员 (人工手写的姓名,如 '杨', '王', '田')
    4. 开始时间 (手写的时间,例如 11:30)
    5. 结束时间 (手写的时间,例如 11:35)

    提取规则:
    - 严格按照这5个表头输出。
    - 哪怕某一行只有"描述"而没有手写数据,也请保留该行,其他单元格留空。
    - 时间统一输出为 HH:MM 格式(例如 09:05、14:30)。
    - 请直接返回标准的 CSV 格式纯文本,不要包含任何 Markdown 标记(如 ```csv)。
    - 表头必须严格是:描述,检验数量,检验员,开始时间,结束时间
    """

    if st.button("🚀 批量识别并加入累计列表", type="primary", use_container_width=True):
        progress = st.progress(0)
        success_count = 0
        total_rows_added = 0
        used_model_set = set()
        error_messages = []

        for i, file in enumerate(img_uploads):
            try:
                img = Image.open(file)
                response, used_model = call_gemini_with_fallback(ocr_prompt, img)
                used_model_set.add(used_model)

                csv_content = response.text.strip().replace("```csv", "").replace("```", "").strip()
                current_df = pd.read_csv(io.StringIO(csv_content))
                current_df.columns = current_df.columns.str.strip()

                st.session_state.batch_records = pd.concat(
                    [st.session_state.batch_records, current_df], ignore_index=True
                )
                success_count += 1
                total_rows_added += len(current_df)
            except Exception as e:
                error_messages.append(f"❌ 图片 `{file.name}` 识别失败:{e}")
            finally:
                progress.progress((i + 1) / len(img_uploads))

        if success_count > 0:
            st.success(
                f"✅ 共成功识别 {success_count}/{len(img_uploads)} 张图片,"
                f"新增 {total_rows_added} 条记录。"
                f"使用模型:{', '.join(used_model_set)}"
            )
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
            "时长(分钟)": st.column_config.NumberColumn(format="%.1f"),
            "效率(件/分钟)": st.column_config.NumberColumn(format="%.2f"),
        },
        disabled=[c for c in df_editable.columns if c != "🗑️删除"],
        key="data_editor",
    )

    col_del, col_dl, col_clear = st.columns(3)

    with col_del:
        if st.button("🗑️ 删除选中行", use_container_width=True):
            keep_mask = ~edited_df["🗑️删除"].fillna(False)
            kept_indices = edited_df.index[keep_mask].tolist()
            st.session_state.batch_records = (
                st.session_state.batch_records.loc[kept_indices].reset_index(drop=True)
            )
            st.rerun()

    with col_dl:
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df_display.to_excel(writer, index=False, sheet_name='检验记录')
        st.download_button(
            label="📥 下载全部记录 (.xlsx)",
            data=output.getvalue(),
            file_name="风扇检验记录汇总表.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )

    with col_clear:
        if st.button("🧹 清空全部记录", use_container_width=True):
            st.session_state.batch_records = pd.DataFrame()
            st.rerun()

    # ==========================================
    # 8. 效率图表
    # ==========================================
    st.markdown("---")
    st.subheader("📊 第三步:效率分析图表")

    chart_df = df_display.dropna(subset=["效率(件/分钟)", "描述", "检验员"]).copy()
    chart_df = chart_df[chart_df["效率(件/分钟)"] > 0]

    if chart_df.empty:
        st.info("暂无可用于图表分析的有效数据(需要包含描述、检验员、开始时间、结束时间、检验数量)。")
    else:
        tab1, tab2, tab3 = st.tabs(["📈 型号 × 检验员", "👤 检验员效率", "🔧 型号效率"])

        with tab1:
            fig1 = px.bar(
                chart_df,
                x="描述",
                y="效率(件/分钟)",
                color="检验员",
                barmode="group",
                title="不同型号 × 不同检验员的效率对比",
                hover_data=["检验数量", "时长(分钟)", "开始时间", "结束时间"],
            )
            fig1.update_layout(
                xaxis_title="型号(描述)",
                yaxis_title="效率(件/分钟)",
                xaxis_tickangle=-45,
                height=500,
            )
            st.plotly_chart(fig1, use_container_width=True)

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
    st.info("当前暂无累计记录,请在上方上传图片进行识别。")
