import streamlit as st
import google.generativeai as genai
from PIL import Image
import pandas as pd
import io

# ==========================================
# 1. 页面设置与样式
# ==========================================
st.set_page_config(page_title="智能检验 2.0", page_icon="⚙️", layout="centered")
st.title("⚙️ 智能检验数据自动录入系统 V2.0")
st.markdown("---")

# ==========================================
# 2. 初始化批量累计表 (Session State)
# ==========================================
if 'batch_data' not in st.session_state:
    st.session_state.batch_data = pd.DataFrame()

# ==========================================
# 3. 加载 API Key
# ==========================================
try:
    api_key = st.secrets["GEMINI_API_KEY"]
    genai.configure(api_key=api_key)
except Exception:
    st.error("⚠️ 未在 Secrets 中发现 GEMINI_API_KEY，请检查后台设置。")
    st.stop()

# ==========================================
# 4. 拍照/上传区
# ==========================================
st.subheader("📸 第一步：拍摄包含手写时间的数据单")
img_source = st.camera_input("点击拍照") or st.file_uploader("或上传图片", type=['jpg','png','jpeg'])

if img_source:
    img = Image.open(img_source)
    
    # ==========================================
    # 5. 针对新表头优化的 AI 提示词
    # ==========================================
    prompt = """
    你是一个专业的工业数据提取专家。图片中是一张检验单，包含电脑打印的文字和人工手写的检验记录。
    
    任务：精准提取每行数据，忽略不相关的列。
    
    我只需要你返回以下 5 列数据：
    1. 描述 (印刷的长字符串描述)
    2. 检验数量 (手写数字)
    3. 检验员 (手写姓名)
    4. 开始时间 (手写的时间点，如 11:30)
    5. 结束时间 (手写的时间点，如 11:40)
    
    提取规则：
    - 请识别图片中最后几列手写的“开始时间”和“结束时间”。
    - 仅返回标准的 CSV 格式纯文本。
    - **严禁**返回任何 Markdown 标记（如 ```csv）。
    - 表头必须严格是：描述,检验数量,检验员,开始时间,结束时间
    - 如果手写部分缺失，对应的单元格请留空，但必须保留“描述”。
    """

    if st.button("🚀 识别并加入待下载列表", type="primary", use_container_width=True):
        with st.spinner("AI 正在解析手写笔迹并同步数据..."):
            try:
                model = genai.GenerativeModel('gemini-1.5-flash')
                response = model.generate_content([prompt, img])
                
                # 清洗 CSV 文本
                csv_data = response.text.strip().replace("```csv", "").replace("```", "")
                current_df = pd.read_csv(io.StringIO(csv_data))
                
                # 追加到总表
                st.session_state.batch_data = pd.concat([st.session_state.batch_data, current_df], ignore_index=True)
                st.success(f"已成功识别并累计！目前总表共有 {len(st.session_state.batch_data)} 条记录。")
            except Exception as e:
                st.error(f"识别失败，请确保图片清晰。详情: {e}")

# ==========================================
# 6. 数据管理与统一下载
# ==========================================
st.markdown("---")
st.subheader("🗂️ 第二步：累计记录预览与下载")

if not st.session_state.batch_data.empty:
    # 实时展示累计的数据
    st.dataframe(st.session_state.batch_data, use_container_width=True)

    # 导出 Excel
    output = io.BytesIO()
    st.session_state.batch_data.to_excel(output, index=False, engine='xlsxwriter')
    
    col_dl, col_cl = st.columns(2)
    with col_dl:
        st.download_button(
            label="📥 统一下载全部累计数据",
            data=output.getvalue(),
            file_name="批量检验记录汇总.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )
    with col_cl:
        if st.button("🗑️ 清空当前历史记录", use_container_width=True):
            st.session_state.batch_data = pd.DataFrame()
            st.rerun()
else:
    st.info("当前还没有识别任何数据，请在上方拍照。")
