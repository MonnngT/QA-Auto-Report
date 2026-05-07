import streamlit as st
import google.generativeai as genai
from PIL import Image
import pandas as pd
import io

# ==========================================
# 1. 页面配置与初始化
# ==========================================
st.set_page_config(page_title="风扇组件检验录入系统", page_icon="⚙️", layout="centered")
st.title("⚙️ 质量检验数据自动采集系统 V2.0")
st.markdown("---")
st.info("💡 **操作指南**：拍摄/上传单据 ➡️ AI 自动提取 ➡️ 检查累计数据 ➡️ 统一下载报表")

# 初始化 Session State，用于存放多张照片累计的识别数据
if 'batch_records' not in st.session_state:
    st.session_state.batch_records = pd.DataFrame()

# ==========================================
# 2. API Key 配置 (从 Streamlit Secrets 读取)
# ==========================================
try:
    # 确保在云端 Settings -> Secrets 中配置了 GEMINI_API_KEY
    api_key = st.secrets["GEMINI_API_KEY"]
    genai.configure(api_key=api_key)
except Exception:
    st.error("⚠️ 未检测到有效的 API Key。请确保在 Streamlit 后台 Secrets 中正确配置。")
    st.stop()

# ==========================================
# 3. 图像获取区 (支持手机拍照和本地上传)
# ==========================================
st.subheader("📸 第一步：拍照或上传检验单")
img_camera = st.camera_input("点击拍照")
img_upload = st.file_uploader("或者从相册选择", type=['jpg', 'jpeg', 'png'])

img_source = img_camera or img_upload

if img_source:
    img = Image.open(img_source)
    with st.expander("🔍 预览拍摄的照片", expanded=False):
        st.image(img, use_container_width=True)

    # ==========================================
    # 4. AI 智能提取逻辑 (针对 5 列数据优化)
    # ==========================================
    # 提取列：描述, 检验数量, 检验员, 开始时间, 结束时间
    ocr_prompt = """
    你是一位专业的质量工程师助手。图片中是一张风扇出货检验记录表，包含印刷文字和手写记录。
    
    任务：精准提取每行数据，并忽略无关信息。
    我只需要你提取并返回以下 5 列数据：
    1. 描述 (印刷的长描述，例如 "788/9-9/36/PAG/EMAX4L/28/8/62/A")
    2. 检验数量 (人工手写的数字)
    3. 检验员 (人工手写的姓名，如 '杨', '王', '田')
    4. 开始时间 (手写的时间，例如 11:30)
    5. 结束时间 (手写的时间，例如 11:35)
    
    提取规则：
    - 严格按照这5个表头输出。
    - 哪怕某一行只有“描述”而没有手写数据，也请保留该行，其他单元格留空。
    - 请直接返回标准的 CSV 格式纯文本，不要包含任何 Markdown 标记（如 ```csv）。
    - 表头必须严格是：描述,检验数量,检验员,开始时间,结束时间
    """

    if st.button("🚀 识别并加入待下载列表", type="primary", use_container_width=True):
        with st.spinner("AI 正在深度解析印刷体与手写笔迹..."):
            try:
                # 使用最新版 Flash 模型
                model = genai.GenerativeModel('gemini-1.5-flash-latest')
                response = model.generate_content([ocr_prompt, img])
                
                # 清理返回的 CSV 文本
                csv_content = response.text.strip().replace("```csv", "").replace("```", "")
                
                # 转换为 DataFrame 并追加到总表
                current_df = pd.read_csv(io.StringIO(csv_content))
                current_df.columns = current_df.columns.str.strip()
                st.session_state.batch_records = pd.concat([st.session_state.batch_records, current_df], ignore_index=True)
                
                st.success(f"✅ 成功提取并追加 {len(current_df)} 条记录！")
            except Exception as e:
                st.error(f"识别失败，请确保图片清晰且 API 配置正确。错误详情: {e}")

# ==========================================
# 5. 数据展示与统一下载
# ==========================================
st.markdown("---")
st.subheader("🗂️ 第二步：累计记录预览与下载")

if not st.session_state.batch_records.empty:
    # 展示累计的大表
    st.dataframe(st.session_state.batch_records, use_container_width=True)

    # 准备 Excel 导出
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        st.session_state.batch_records.to_excel(writer, index=False, sheet_name='检验记录')
    
    col_dl, col_cl = st.columns(2)
    with col_dl:
        st.download_button(
            label="📥 统一下载全部累计记录 (.xlsx)",
            data=output.getvalue(),
            file_name="风扇检验记录汇总表.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )
    with col_cl:
        if st.button("🗑️ 清空当前历史记录", use_container_width=True):
            st.session_state.batch_records = pd.DataFrame()
            st.rerun()
else:
    st.info("当前暂无累计记录，请在上方进行识别。")
