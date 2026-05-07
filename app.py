import streamlit as st
import google.generativeai as genai
from PIL import Image
import pandas as pd
import io

# ==========================================
# 1. 页面配置与初始化
# ==========================================
st.set_page_config(page_title="智能检验批量录入系统V2", page_icon="⚙️", layout="centered")
st.title("⚙️ 智能检验单自动录入系统 V2.0")
st.markdown("**说明：** 拍摄包含手写“开始/结束时间”的单据 ➡️ AI自动提取 ➡️ 自动累加记录 ➡️ 统一下载")

# 初始化 Session State，用于存放累计的识别数据
if 'all_data' not in st.session_state:
    st.session_state.all_data = pd.DataFrame()

# ==========================================
# 2. API Key 配置 (从 Secrets 读取)
# ==========================================
try:
    api_key = st.secrets["GEMINI_API_KEY"]
    genai.configure(api_key=api_key)
except Exception:
    st.error("⚠️ 未检测到 API Key，请确保在 Streamlit 后台 Secrets 中配置了 GEMINI_API_KEY")
    st.stop()

# ==========================================
# 3. 图像获取区 (支持拍照和上传)
# ==========================================
st.markdown("---")
st.subheader("📸 第一步：获取检验单照片")

img_camera = st.camera_input("直接拍照")
img_upload = st.file_uploader("或者从相册选择图片", type=['jpg', 'jpeg', 'png'])

img_source = img_camera or img_upload

if img_source:
    img = Image.open(img_source)
    with st.expander("预览已拍摄的照片", expanded=False):
        st.image(img, use_container_width=True)

    # ==========================================
    # 4. AI 智能提取逻辑 (针对新表头优化)
    # ==========================================
    # 提示词专门适配：描述, 检验数量, 检验员, 开始时间, 结束时间
    ocr_prompt = """
    你是一个专业的工业现场数据录入员。图片是一张出货检验单，包含电脑打印的文字和人工手写的记录。
    
    任务：精准提取每行的数据。
    我只需要你提取并整理出以下 5 列数据：
    1. 描述 (通常是电脑打印的长字符串，如 "788/9-9/36/PAG...")
    2. 检验数量 (手写的数字)
    3. 检验员 (手写的姓名)
    4. 开始时间 (手写的时间，格式如 11:30)
    5. 结束时间 (手写的时间，格式如 11:45)
    
    要求：
    - 严格按照这5个表头输出。忽略图片中其他的列（如编号、原数量等）。
    - 即使某一行只有打印的“描述”而没有手写数据，也必须保留该行，手写列留空即可。
    - 只返回标准的 CSV 格式纯文本，不要包含任何 Markdown 标记 (如 ```csv)。
    - CSV 的表头必须严格是：描述,检验数量,检验员,开始时间,结束时间
    """

    if st.button("🚀 识别并加入汇总表", type="primary", use_container_width=True):
        with st.spinner("AI 正在深度解析笔迹..."):
            try:
                # 调用 Gemini 1.5 Flash 模型
                model = genai.GenerativeModel('gemini-1.5-flash')
                response = model.generate_content([ocr_prompt, img])
                
                # 清理返回的文本
                csv_text = response.text.strip().replace("```csv", "").replace("```", "")
                
                # 转换为 DataFrame
                current_df = pd.read_csv(io.StringIO(csv_text))
                current_df.columns = current_df.columns.str.strip()
                
                # 将本次识别的数据拼接到总表中
                st.session_state.all_data = pd.concat([st.session_state.all_data, current_df], ignore_index=True)
                
                st.success(f"✅ 提取成功！已增加 {len(current_df)} 条记录。")
            except Exception as e:
                st.error(f"识别过程出错。请确保图片清晰且 API 配置正确。\n错误详情：{e}")

# ==========================================
# 5. 数据展示与统一下载
# ==========================================
st.markdown("---")
st.subheader("🗂️ 第二步：累计记录预览与下载")

if not st.session_state.all_data.empty:
    # 展示累计的大表
    st.dataframe(st.session_state.all_data, use_container_width=True)

    # 准备 Excel 下载
    towrite = io.BytesIO()
    st.session_state.all_data.to_excel(towrite, index=False, engine='xlsxwriter')
    towrite.seek(0)

    col_dl, col_cl = st.columns(2)
    with col_dl:
        st.download_button(
            label="📥 统一下载全部累计数据 (.xlsx)",
            data=towrite,
            file_name="批量检验记录报表.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )
    with col_cl:
        if st.button("🗑️ 清空所有记录", use_container_width=True):
            st.session_state.all_data = pd.DataFrame()
            st.rerun()
else:
    st.info("当前暂无累计记录，请在上方拍照识别。")
