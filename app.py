import streamlit as st
import google.generativeai as genai
from PIL import Image
import pandas as pd
import io

# ==========================================
# 1. 页面配置与初始化
# ==========================================
st.set_page_config(page_title="风扇组件检验录入系统", page_icon="⚙️", layout="centered")
st.title("⚙️ 质量检验数据自动采集系统 V2.1")
st.markdown("---")
st.info("💡 **操作指南**:拍摄/上传单据 ➡️ AI 自动提取 ➡️ 检查累计数据 ➡️ 统一下载报表")

# 初始化 Session State,用于存放多张照片累计的识别数据
if 'batch_records' not in st.session_state:
    st.session_state.batch_records = pd.DataFrame()

# ==========================================
# 2. API Key 配置 (从 Streamlit Secrets 读取)
# ==========================================
try:
    api_key = st.secrets["GEMINI_API_KEY"]
    genai.configure(api_key=api_key)
except Exception:
    st.error("⚠️ 未检测到有效的 API Key。请确保在 Streamlit 后台 Secrets 中正确配置 GEMINI_API_KEY。")
    st.stop()

# ==========================================
# 【关键修改】模型候选列表 - 自动降级
# ==========================================
# 旧的 gemini-1.5-flash 已被 Google 下线,改用以下新模型。
# 程序会按顺序尝试,直到某一个能用为止。
MODEL_CANDIDATES = [
    "gemini-flash-latest",      # 推荐:始终指向最新稳定版 Flash
    "gemini-2.5-flash",         # 备选 1
    "gemini-2.0-flash",         # 备选 2
    "gemini-2.5-flash-lite",    # 备选 3:更便宜的 lite 版
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
            # 如果是 404(模型不存在)就尝试下一个,其它错误也继续尝试
            continue
    # 全部失败
    raise RuntimeError(f"所有候选模型均无法调用。最后一次错误: {last_error}")

# ==========================================
# 3. 图像获取区 (支持手机拍照和本地上传)
# ==========================================
st.subheader("📸 第一步:拍照或上传检验单")
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
    - 请直接返回标准的 CSV 格式纯文本,不要包含任何 Markdown 标记(如 ```csv)。
    - 表头必须严格是:描述,检验数量,检验员,开始时间,结束时间
    """

    if st.button("🚀 识别并加入待下载列表", type="primary", use_container_width=True):
        with st.spinner("AI 正在深度解析印刷体与手写笔迹..."):
            try:
                # 【关键修改】调用带降级的封装函数
                response, used_model = call_gemini_with_fallback(ocr_prompt, img)
                
                # 清理返回的 CSV 文本
                csv_content = response.text.strip().replace("```csv", "").replace("```", "").strip()
                
                # 转换为 DataFrame 并追加到总表
                current_df = pd.read_csv(io.StringIO(csv_content))
                current_df.columns = current_df.columns.str.strip()
                st.session_state.batch_records = pd.concat(
                    [st.session_state.batch_records, current_df], ignore_index=True
                )
                
                st.success(f"✅ 成功提取并追加 {len(current_df)} 条记录!(使用模型: {used_model})")
            except Exception as e:
                st.error(f"识别失败,请确保图片清晰且 API 配置正确。错误详情: {e}")

# ==========================================
# 5. 数据展示与统一下载
# ==========================================
st.markdown("---")
st.subheader("🗂️ 第二步:累计记录预览与下载")

if not st.session_state.batch_records.empty:
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
    st.info("当前暂无累计记录,请在上方进行识别。")
