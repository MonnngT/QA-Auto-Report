import streamlit as st
import google.generativeai as genai
from PIL import Image
import pandas as pd
import io

# ==========================================
# 1. 页面基础设置
# ==========================================
st.set_page_config(page_title="现场检验批量录入", page_icon="🏭", layout="centered")
st.title("🏭 现场检验单自动批量录入系统")
st.markdown("**操作流程：** 连续拍照提取 ➡️ 系统自动累加记录 ➡️ 工作结束时统一下载")

# ==========================================
# 2. 初始化“临时内存” (Session State)
# ==========================================
# 如果系统是刚打开，就建一个空的“大表”用来存放历史数据
if 'history_df' not in st.session_state:
    st.session_state.history_df = pd.DataFrame()

# ==========================================
# 3. 静默加载 API Key
# ==========================================
try:
    api_key = st.secrets["GEMINI_API_KEY"]
    genai.configure(api_key=api_key)
except KeyError:
    st.error("⚠️ 系统未配置 API Key！请在 Streamlit 后台 Secrets 中进行配置。")
    st.stop()

# ==========================================
# 4. 图像获取区
# ==========================================
st.markdown("---")
st.markdown("### 📸 第一步：拍摄本张检验单")

col1, col2 = st.columns(2)
with col1:
    img_camera = st.camera_input("调用摄像头")
with col2:
    img_upload = st.file_uploader("从相册选择", type=['jpg', 'jpeg', 'png'])

img_source = img_camera or img_upload

if img_source:
    img = Image.open(img_source)
    with st.expander("预览当前照片", expanded=False):
        st.image(img, use_container_width=True)

    # ==========================================
    # 5. 核心提取逻辑
    # ==========================================
    prompt = """
    你是一个专业的工业现场数据录入员。图片是一张出货检验单，包含打印文字和手写数据。
    任务：精准提取每行的数据，无论打印还是手写。
    我只需要你提取并整理出以下 4 列数据：
    1. 描述 (电脑打印的长字符串，如 "390/8-8/25/PAG...")
    2. 检验数量 (手写的数字)
    3. 检验员 (手写的名字)
    4. 检验时长 (手写的时间)
    
    要求：
    - 严格按照这4列表头输出。忽略其他列。
    - 如果某行只有打印的“描述”，手写部分为空，请在手写列留空，但**必须保留该行的“描述”**。
    - 只返回标准的 CSV 格式纯文本，**绝不要**包含任何 Markdown 标记 (例如 ```csv 等)。
    - 表头必须严格是：描述,检验数量,检验员,检验时长
    """

    if st.button("🚀 提取当前照片数据并加入汇总", type="primary", use_container_width=True):
        with st.spinner("AI 正在解析照片，并追加到总表中..."):
            try:
                # 调用 AI 模型
                model = genai.GenerativeModel('gemini-1.5-flash')
                response = model.generate_content([prompt, img])
                
                # 清理并转换格式
                csv_raw = response.text.strip()
                if csv_raw.startswith("```csv"):
                    csv_raw = csv_raw.replace("```csv", "").replace("```", "").strip()
                elif csv_raw.startswith("```"):
                    csv_raw = csv_raw.replace("```", "").strip()

                current_df = pd.read_csv(io.StringIO(csv_raw))
                current_df.columns = current_df.columns.str.strip()
                
                # 【关键动作】：将新识别的数据，拼接到历史大表（history_df）的下面
                st.session_state.history_df = pd.concat([st.session_state.history_df, current_df], ignore_index=True)
                
                st.success("✅ 当前照片提取成功！已加入下方总表。您可以继续拍摄下一张。")

            except Exception as e:
                st.error(f"处理失败，请重拍或检查网络。\n错误详情：{e}")

# ==========================================
# 6. 数据汇总与下载区 (脱离拍照逻辑，永久显示)
# ==========================================
st.markdown("---")
st.markdown("### 🗂️ 第二步：累计检验记录总表")

# 只有当历史大表里有数据时，才展示表格和下载按钮
if not st.session_state.history_df.empty:
    st.info(f"💡 目前系统已累计记录 **{len(st.session_state.history_df)}** 行检验数据。")
    
    # 展示累计的大表
    st.dataframe(st.session_state.history_df, use_container_width=True)

    # 下载按钮
    towrite = io.BytesIO()
    st.session_state.history_df.to_excel(towrite, index=False, engine='xlsxwriter')
    towrite.seek(0)

    st.download_button(
        label="📥 统一下载全部记录 (.xlsx)",
        data=towrite,
        file_name="批量检验记录汇总表.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True
    )
    
    # 清空数据的按钮（放在右侧角落防止误触）
    col_empty1, col_empty2 = st.columns([3, 1])
    with col_empty2:
        if st.button("🗑️ 清空所有记录", help="交接班或换批次时，可点击清空累计数据"):
            st.session_state.history_df = pd.DataFrame()
            st.rerun() # 刷新页面，重置状态
else:
    st.write("暂无累计数据，请在上方拍照提取。")
