import streamlit as st
import google.generativeai as genai
from PIL import Image
import pandas as pd
import io

# ==========================================
# 1. 页面基础设置
# ==========================================
st.set_page_config(page_title="现场检验录入", page_icon="🏭", layout="centered")
st.title("🏭 现场检验单自动录入系统")
st.markdown("**操作流程：** 拍照单据 ➡️ AI 自动提取 ➡️ 下载 Excel 文件")

# ==========================================
# 2. 静默加载 API Key (从 Streamlit Secrets 读取)
# ==========================================
try:
    # 尝试从云端配置中读取 Key
    api_key = st.secrets["GEMINI_API_KEY"]
    genai.configure(api_key=api_key)
except KeyError:
    st.error("⚠️ 系统未配置 API Key！请管理员在 Streamlit 后台 Settings -> Secrets 中进行配置。")
    st.stop() # 停止运行后续代码

# ==========================================
# 3. 图像获取区
# ==========================================
st.markdown("### 📸 第一步：获取检验单照片")

col1, col2 = st.columns(2)
with col1:
    img_camera = st.camera_input("现场直接拍照")
with col2:
    img_upload = st.file_uploader("或从相册选择", type=['jpg', 'jpeg', 'png'])

img_source = img_camera or img_upload

if img_source:
    img = Image.open(img_source)
    with st.expander("预览当前单据", expanded=False):
        st.image(img, use_container_width=True)

    # ==========================================
    # 4. 核心：AI 混合图文提取
    # ==========================================
    st.markdown("### 🤖 第二步：AI 自动识别提取")
    
    prompt = """
    你现在是一个专业的工业制造现场数据录入员。
    图片是一张出货检验单，包含电脑打印的文字和人工手写的检验数据。
    
    任务：精准提取每行的数据，无论它是打印的还是手写的。
    
    我只需要你提取并整理出以下 4 列数据：
    1. 描述 (通常是电脑打印的英文/数字混合长字符串，如 "390/8-8/25/PAG...")
    2. 检验数量 (人工手写的数字)
    3. 检验员 (人工手写的名字)
    4. 检验时长 (人工手写的时间记录)
    
    严格执行以下要求：
    - 请严格按照这4列表头输出。忽略图片中的其他列。
    - 如果某一行只打印了“描述”，手写部分是空白的，请在手写列留空，但**必须保留该行的“描述”**。
    - 请只返回标准的 CSV 格式纯文本，**绝不要**包含任何 Markdown 标记 (例如 ```csv 等)，不要有任何多余的解释文字。
    - CSV 的表头必须严格是：描述,检验数量,检验员,检验时长
    """

    if st.button("🚀 一键提取表格数据", type="primary", use_container_width=True):
        with st.spinner("AI 正在深度读取印刷体与手写笔迹，请稍候..."):
            try:
                # 调用模型
                model = genai.GenerativeModel('gemini-1.5-flash')
                response = model.generate_content([prompt, img])
                
                # 清理并转换格式
                csv_raw = response.text.strip()
                if csv_raw.startswith("```csv"):
                    csv_raw = csv_raw.replace("```csv", "").replace("```", "").strip()
                elif csv_raw.startswith("```"):
                    csv_raw = csv_raw.replace("```", "").strip()

                df = pd.read_csv(io.StringIO(csv_raw))
                df.columns = df.columns.str.strip()
                
                st.success("🎉 提取成功！请核对以下数据：")
                st.dataframe(df, use_container_width=True)

                # ==========================================
                # 5. 生成并下载 Excel
                # ==========================================
                st.markdown("### 💾 第三步：下载电子表格")
                
                towrite = io.BytesIO()
                df.to_excel(towrite, index=False, engine='xlsxwriter')
                towrite.seek(0)

                st.download_button(
                    label="📥 下载 Excel 检验记录 (.xlsx)",
                    data=towrite,
                    file_name="检验记录报表.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True
                )

            except Exception as e:
                st.error(f"处理失败，可能是图片不够清晰。\n错误详情：{e}")
                with st.expander("🔧 开发者调试信息"):
                    st.text(response.text if 'response' in locals() else "未获取到返回结果")
