import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime
import time
import requests
import re

# --- 1. 頁面配置 ---
st.set_page_config(page_title="專業級多週期共振監控系統 V3.1", layout="wide")

st.markdown("""
<style>
@keyframes blink { 0% { border-color: #444; } 50% { border-color: #ff4b4b; box-shadow: 0 0 15px #ff4b4b; } 100% { border-color: #444; } }
.blink-bull { border: 3px solid #00ff00 !important; animation: blink 1s infinite; background-color: rgba(0, 255, 0, 0.05); }
.blink-bear { border: 3px solid #ff4b4b !important; animation: blink 1s infinite; background-color: rgba(255, 75, 75, 0.05); }
.vix-banner { padding: 15px; border-radius: 8px; text-align: center; margin-bottom: 20px; font-weight: bold; border: 1px solid #444; font-size: 1.1em; }
</style>
""", unsafe_allow_html=True)

# --- 2. 市場診斷與支撐阻力 ---
def get_market_context():
    try:
        vix_data = yf.download("^VIX", period="5d", interval="1d", progress=False)
        spy_data = yf.download("SPY", period="5d", interval="1d", progress=False)
        if isinstance(vix_data.columns, pd.MultiIndex): vix_data.columns = vix_data.columns.get_level_values(0)
        if isinstance(spy_data.columns, pd.MultiIndex): spy_data.columns = spy_data.columns.get_level_values(0)
        vix_p = float(vix_data['Close'].iloc[-1])
        spy_c = ((spy_data['Close'].iloc[-1] - spy_data['Close'].iloc[-2]) / spy_data['Close'].iloc[-2]) * 100
        v_stat = "🔴 極端恐慌" if vix_p > 28 else "🟡 波動放大" if vix_p > 20 else "🟢 環境平穩"
        return vix_p, spy_c, v_stat
    except: return 20.0, 0.0, "數據讀取中"

def get_pivot_levels(df_daily):
    try:
        if len(df_daily) < 2: return None
        prev = df_daily.iloc[-2]
        p = (prev['High'] + prev['Low'] + prev['Close']) / 3
        return {"R1": (2 * p) - prev['Low'], "S1": (2 * p) - prev['High']}
    except: return None

# --- 3. 數據抓取 (包含更多 EMA 週期以匹配圖片特徵) ---
def fetch_pro_data(symbol, interval_p):
    try:
        fetch_range = "60d" if interval_p in ["30m", "15m"] else "7d"
        df = yf.download(symbol, period=fetch_range, interval=interval_p, progress=False)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        
        c = df['Close']
        df['EMA5'] = c.ewm(span=5, adjust=False).mean()
        df['EMA10'] = c.ewm(span=10, adjust=False).mean()
        df['EMA20'] = c.ewm(span=20, adjust=False).mean()
        df['EMA40'] = c.ewm(span=40, adjust=False).mean() # 圖片中的特徵
        df['EMA60'] = c.ewm(span=60, adjust=False).mean()
        df['EMA200'] = c.ewm(span=200, adjust=False).mean()
        df['Vol_Avg'] = df['Volume'].rolling(window=20).mean()
        
        macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
        df['Hist'] = macd - macd.ewm(span=9, adjust=False).mean()
        return df.dropna(subset=['EMA200'])
    except: return None

# --- 4. 訊號判定 (核心：整合圖片中的趨勢特徵) ---
def check_signals(df, p_limit, v_limit, use_brk, use_macd, lookback_k):
    if df is None or len(df) < lookback_k + 2: return None, "", "SIDE"
    last = df.iloc[-1]; prev = df.iloc[-2]
    price = float(last['Close'])
    pc = ((price - prev['Close']) / prev['Close']) * 100
    vr = float(last['Volume'] / last['Vol_Avg']) if last['Vol_Avg'] > 0 else 1
    
    # 圖片特徵 A: 均線束發散排列 (EMA Ribbon)
    is_ema_bull = last['EMA5'] > last['EMA10'] > last['EMA20'] > last['EMA60']
    is_ema_bear = last['EMA5'] < last['EMA10'] < last['EMA20'] < last['EMA60']
    
    # 圖片特徵 B: MACD 柱狀圖趨勢 (Dynamic Momentum)
    macd_bull_pulse = last['Hist'] > 0 and last['Hist'] > prev['Hist']
    macd_bear_pulse = last['Hist'] < 0 and last['Hist'] < prev['Hist']
    
    # 基礎形態
    is_brk_h = price > df.iloc[-6:-1]['High'].max() if use_brk else False
    is_brk_l = price < df.iloc[-6:-1]['Low'].min() if use_brk else False

    reasons = []
    sig = None

    # 多頭共振：均線發散 + MACD動能 + (突破或量價)
    if is_ema_bull and macd_bull_pulse and (pc >= p_limit or is_brk_h) and vr >= v_limit:
        sig = "BULL"
        reasons.append(f"均線發散+MACD動能(量比:{vr:.1f})")
    
    # 空頭共振：均線排列 + MACD賣壓 + (跌穿或量價)
    elif is_ema_bear and macd_bear_pulse and (pc <= -p_limit or is_brk_l) and vr >= v_limit:
        sig = "BEAR"
        reasons.append(f"空頭排列+MACD賣壓(量比:{vr:.1f})")
        
    trend = "BULL" if price > last['EMA60'] else "BEAR" if price < last['EMA60'] else "SIDE"
    return sig, "".join(reasons), trend

# --- 5. Telegram 通知 ---
def send_pro_notification(sym, action, res_details, price, pc, vr, adr_u, vix_info, levels, lookback_k):
    try:
        token = st.secrets["TELEGRAM_BOT_TOKEN"]
        chat_id = st.secrets["TELEGRAM_CHAT_ID"]
        v_val, spy_c, v_stat = vix_info
        lv_msg = f"R1:{levels['R1']:.2f} | S1:{levels['S1']:.2f}" if levels else "N/A"
        message = (
            f"🔔 {action}: {sym}\n💰 價格: {price:.2f} ({pc:+.2f}%)\n📊 量比: {vr:.1f}x | ADR: {adr_u:.1f}%\n"
            f"📍 位置: {lv_msg}\n🌐 VIX: {v_val:.2f} | SPY: {spy_c:+.2f}%\n📋 細節: {res_details}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        requests.get(f"https://api.telegram.org/bot{token}/sendMessage", params={"chat_id": chat_id, "text": message}, timeout=5)
    except: pass

# --- 新增：最新10根K線型態自動解讀（只加這一個函數，其餘完全不動） ---
def analyze_kline_patterns(df, n=10):
    """自動分析最新10根日K線形態並給出中文解讀"""
    if df is None or len(df) < n + 2:
        return "📊 數據不足"
    
    recent = df.iloc[-n:].copy()
    last = recent.iloc[-1]
    prev = recent.iloc[-2]
    
    body = abs(last['Close'] - last['Open'])
    upper = last['High'] - max(last['Open'], last['Close'])
    lower = min(last['Open'], last['Close']) - last['Low']
    total_range = last['High'] - last['Low']
    
    patterns = []
    
    # 單根經典形態
    if total_range > 0 and body / total_range < 0.15:
        patterns.append("✝️ 十字星（可能轉折）")
    elif lower > 2.2 * body and last['Close'] > last['Open']:
        patterns.append("🔨 錘頭線（強看漲）")
    elif upper > 2.2 * body and last['Close'] < last['Open']:
        patterns.append("☄️ 射擊之星（強看跌）")
    
    # 兩根吞沒形態
    if len(recent) >= 2:
        p1 = recent.iloc[-2]
        if (last['Close'] > p1['High'] and last['Open'] < p1['Close'] and last['Close'] > last['Open'] and p1['Close'] < p1['Open']):
            patterns.append("🌊 看漲吞沒（強力反轉）")
        elif (last['Close'] < p1['Low'] and last['Open'] > p1['Close'] and last['Close'] < last['Open'] and p1['Close'] > p1['Open']):
            patterns.append("🌊 看跌吞沒（強力反轉）")
    
    # 近期趨勢
    bull_count = sum(recent['Close'] > recent['Open'])
    if bull_count >= 8:
        patterns.append("📈 近10日強勢多頭")
    elif bull_count <= 3:
        patterns.append("📉 近10日強勢空頭")
    
    return " | ".join(patterns) if patterns else "⚖️ 中性整理形態"

# --- 6. UI 與 循環 ---
with st.sidebar:
    st.header("🗄️ 交易者工作站")
    sym_input = st.text_input("代碼名單", value="TSLA, NIO, TSLL, XPEV, QQQ, VOO, META, GOOGL, AAPL, NVDA, AMZN, MSFT, TSM, GLD, BTC-USD").upper()
    symbols = [s.strip() for s in sym_input.split(",") if s.strip()]
    selected_intervals = st.multiselect("共振週期", ["1m", "5m", "10m", "15m", "30m", "1h"], default=["5m", "15m"])
    refresh_rate = st.slider("刷新頻率(秒)", 30, 300, 60)
    p_thr = st.number_input("異動閾值(%)", value=0.8)
    v_thr = st.number_input("量爆倍數", value=1.2)
    use_brk = st.checkbox("啟用形態突破", True)
    use_macd = st.checkbox("啟用MACD動能", True)

st.title("🛡️ 專業級智能監控終端 V3.1")

placeholder = st.empty()

while True:
    vix_val, spy_c, v_stat = get_market_context()
    with placeholder.container():
        st.markdown(f'<div class="vix-banner">市場環境：{v_stat} | VIX: {vix_val:.2f} | SPY: {spy_c:+.2f}%</div>', unsafe_allow_html=True)
        if symbols:
            cols = st.columns(len(symbols))
            for i, sym in enumerate(symbols):
                # --- 只改這一段（原代碼其他完全不動） ---
                kline_analysis = "數據不足"
                try:
                    df_d = yf.download(sym, period="20d", interval="1d", progress=False)
                    if isinstance(df_d.columns, pd.MultiIndex): df_d.columns = df_d.columns.get_level_values(0)
                    adr = (df_d['High'] - df_d['Low']).mean()
                    adr_u = ((df_d['High'].iloc[-1] - df_d['Low'].iloc[-1]) / adr) * 100
                    levels = get_pivot_levels(df_d)
                    kline_analysis = analyze_kline_patterns(df_d, 10)   # ← 只加這一行
                except: 
                    adr_u, levels, kline_analysis = 0, None, "分析異常"

                res_sigs, res_trends, res_details = [], [], {}
                last_df = None
                for interval in selected_intervals:
                    df = fetch_pro_data(sym, interval)
                    sig, det, trend = check_signals(df, p_thr, v_thr, use_brk, use_macd, 7)
                    res_sigs.append(sig); res_trends.append(trend)
                    if sig: res_details[interval] = det
                    last_df = df
                
                if last_df is not None:
                    cp = float(last_df['Close'].iloc[-1]); c_pc = ((cp - last_df['Close'].iloc[-2]) / last_df['Close'].iloc[-2]) * 100
                    c_vr = float(last_df['Volume'].iloc[-1] / last_df['Vol_Avg'].iloc[-1]) if last_df['Vol_Avg'].iloc[-1] > 0 else 1
                    is_bull = (res_sigs[0] == "BULL") and (res_trends[-1] == "BULL")
                    is_bear = (res_sigs[0] == "BEAR") and (res_trends[-1] == "BEAR")
                    
                    color = "#00ff00" if is_bull else "#ff4b4b" if is_bear else "#888"
                    label = "🚀 多頭加速" if is_bull else "🔻 空頭加速" if is_bear else "⚖️ 觀望"
                    style = "blink-bull" if is_bull else "blink-bear" if is_bear else ""
                    
                    if is_bull or is_bear:
                        send_pro_notification(sym, label, str(res_details), cp, c_pc, c_vr, adr_u, (vix_val, spy_c, v_stat), levels, 7)

                    cols[i].markdown(f"""
                    <div class='{style}' style='border:1px solid #444; padding:10px; border-radius:10px; text-align:center;'>
                        <h4>{sym}</h4>
                        <h3 style='color:{color}'>{label}</h3>
                        <p style='font-size:1.2em;'>{cp:.2f}</p>
                        <p style='font-size:0.7em; color:#aaa;'>ADR: {adr_u:.1f}%</p>
                        <p style='font-size:0.78em; color:#66ccff; margin-top:4px;'>{kline_analysis}</p>
                    </div>
                    """, unsafe_allow_html=True)
    time.sleep(refresh_rate)
