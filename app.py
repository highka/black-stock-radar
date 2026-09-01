import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime

# =========================================================
# 🖤 黑嚕嚕－台股盤中雷達 V2.1
# =========================================================

st.set_page_config(
    page_title="🖤 黑嚕嚕－台股盤中雷達",
    page_icon="🖤",
    layout="wide",
    initial_sidebar_state="expanded"
)

# =========================================================
# CSS
# =========================================================

st.markdown("""
<style>
    .main-title {
        font-size: 32px;
        font-weight: 800;
        margin-bottom: 0px;
    }

    .sub-title {
        color: #777;
        font-size: 15px;
        margin-bottom: 20px;
    }

    .score-box {
        padding: 10px 15px;
        border-radius: 10px;
        background-color: rgba(128,128,128,0.08);
        text-align: center;
    }

    .alert-box {
        padding: 12px;
        border-radius: 10px;
        margin-bottom: 8px;
        background-color: rgba(255, 165, 0, 0.08);
    }

    div[data-testid="stMetric"] {
        border-radius: 10px;
        padding: 8px;
    }
</style>
""", unsafe_allow_html=True)

# =========================================================
# 股票清單
# =========================================================

@st.cache_data
def load_stock_list():

    try:

        df = pd.read_csv(
            "stock_list.csv",
            dtype={"股票代號": str}
        )

        df["股票代號"] = (
            df["股票代號"]
            .astype(str)
            .str.strip()
            .str.zfill(4)
        )

        df["股票名稱"] = (
            df["股票名稱"]
            .astype(str)
            .str.strip()
        )

        return df

    except Exception as e:

        st.error(
            f"無法讀取 stock_list.csv：{e}"
        )

        return pd.DataFrame(
            columns=[
                "股票代號",
                "股票名稱",
                "市場"
            ]
        )


stock_list_df = load_stock_list()


def stock_name(symbol):

    result = stock_list_df.loc[
        stock_list_df["股票代號"] == str(symbol),
        "股票名稱"
    ]

    if not result.empty:

        return result.iloc[0]

    return str(symbol)

# =========================================================
# 台股清單
# =========================================================

DEFAULT_STOCKS = list(STOCK_NAMES.keys())DEFAULT_STOCKS = (
    stock_list_df["股票代號"]
    .dropna()
    .astype(str)
    .str.zfill(4)
    .tolist()
)

def stock_name(symbol):
    return STOCK_NAMES.get(symbol, symbol)


# =========================================================
# 技術指標
# =========================================================

def calculate_rsi(series, period=14):
    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    rsi = 100 - (100 / (1 + rs))

    return rsi.fillna(50)


def calculate_indicators(df):

    if df.empty:
        return df

    df = df.copy()

    close = df["Close"]
    volume = df["Volume"]

    df["MA20"] = close.rolling(20).mean()
    df["MA60"] = close.rolling(60).mean()
    df["MA200"] = close.rolling(200).mean()

    df["RSI"] = calculate_rsi(close)

    df["VOL20"] = volume.rolling(20).mean()

    df["VolumeRatio"] = (
        volume / df["VOL20"].replace(0, np.nan)
    )

    df["High20"] = close.rolling(20).max().shift(1)

    df["Return"] = close.pct_change() * 100

    df["VolumeUp"] = volume > volume.shift(1)

    df["VolumeUp2"] = (
        (volume > volume.shift(1))
        & (volume.shift(1) > volume.shift(2))
    )

    return df


# =========================================================
# 黑嚕嚕分數
# =========================================================

def calculate_score(row):

    score = 0

    close = row.get("Close", np.nan)
    ma20 = row.get("MA20", np.nan)
    ma60 = row.get("MA60", np.nan)
    ma200 = row.get("MA200", np.nan)
    rsi = row.get("RSI", 50)
    volume_ratio = row.get("VolumeRatio", 1)
    return_pct = row.get("Return", 0)
    high20 = row.get("High20", np.nan)

    # -------------------------
    # 趨勢
    # -------------------------

    if pd.notna(ma20) and close > ma20:
        score += 10

    if pd.notna(ma60) and close > ma60:
        score += 10

    if (
        pd.notna(ma20)
        and pd.notna(ma60)
        and ma20 > ma60
    ):
        score += 10

    if (
        pd.notna(ma60)
        and pd.notna(ma200)
        and ma60 > ma200
    ):
        score += 10

    # -------------------------
    # 量能
    # -------------------------

    if volume_ratio >= 1.2:
        score += 5

    if volume_ratio >= 1.5:
        score += 5

    if volume_ratio >= 2:
        score += 5

    if volume_ratio >= 3:
        score += 5

    # -------------------------
    # 動能
    # -------------------------

    if return_pct >= 1:
        score += 5

    if return_pct >= 3:
        score += 5

    if return_pct >= 5:
        score += 5

    # -------------------------
    # RSI
    # -------------------------

    if 50 <= rsi <= 70:
        score += 10

    elif 70 < rsi <= 80:
        score += 5

    # -------------------------
    # 突破
    # -------------------------

    if pd.notna(high20) and close >= high20:
        score += 10

    return min(score, 100)


# =========================================================
# 取得股票資料
# =========================================================

@st.cache_data(ttl=300, show_spinner=False)
def get_stock_data(symbol):

    ticker = f"{symbol}.TW"

    try:

        df = yf.download(
            ticker,
            period="1y",
            interval="1d",
            auto_adjust=False,
            progress=False
        )

        if df.empty:
            return pd.DataFrame()

        # yfinance 新版本可能產生 MultiIndex
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        required = [
            "Open",
            "High",
            "Low",
            "Close",
            "Volume"
        ]

        for col in required:
            if col not in df.columns:
                return pd.DataFrame()

        df = df[required].copy()

        df.dropna(subset=["Close"], inplace=True)

        df = calculate_indicators(df)

        return df

    except Exception:
        return pd.DataFrame()


# =========================================================
# 掃描股票
# =========================================================

@st.cache_data(ttl=300, show_spinner=False)
def scan_stocks(stock_list):

    results = []

    for symbol in stock_list:

        df = get_stock_data(symbol)

        if df.empty or len(df) < 60:
            continue

        row = df.iloc[-1]

        close = float(row["Close"])

        previous_close = (
            float(df["Close"].iloc[-2])
            if len(df) >= 2
            else close
        )

        change_pct = (
            (close / previous_close - 1) * 100
            if previous_close != 0
            else 0
        )

        volume_ratio = float(
            row["VolumeRatio"]
        ) if pd.notna(row["VolumeRatio"]) else 1

        rsi = float(
            row["RSI"]
        ) if pd.notna(row["RSI"]) else 50

        score = calculate_score(row)

        ma20 = (
            float(row["MA20"])
            if pd.notna(row["MA20"])
            else np.nan
        )

        ma60 = (
            float(row["MA60"])
            if pd.notna(row["MA60"])
            else np.nan
        )

        ma200 = (
            float(row["MA200"])
            if pd.notna(row["MA200"])
            else np.nan
        )

        high20 = (
            float(row["High20"])
            if pd.notna(row["High20"])
            else np.nan
        )

        breakout = (
            pd.notna(high20)
            and close >= high20
        )

        volume_up = bool(
            row["VolumeUp2"]
        )

        results.append({

            "代號": symbol,

            "名稱": stock_name(symbol),

            "價格": close,

            "漲跌幅": change_pct,

            "量比": volume_ratio,

            "RSI": rsi,

            "MA20": ma20,

            "MA60": ma60,

            "MA200": ma200,

            "20日高": high20,

            "突破": breakout,

            "連續量增": volume_up,

            "黑嚕嚕分數": score
        })

    if not results:
        return pd.DataFrame()

    return pd.DataFrame(results)


# =========================================================
# 側邊欄
# =========================================================

st.sidebar.title("🖤 黑嚕嚕")

st.sidebar.caption(
    "台股盤中雷達 V2.1"
)

st.sidebar.divider()

mode = st.sidebar.selectbox(

    "📡 雷達模式",

    [
        "全部股票",
        "🔥 強勢股",
        "🟢 守護生命線",
        "🚀 強勢突破",
        "⚠️ 大量換手高危",
        "🔴 弱勢股"
    ]
)

min_score = st.sidebar.slider(
    "最低黑嚕嚕分數",
    min_value=0,
    max_value=100,
    value=50,
    step=5
)

min_volume_ratio = st.sidebar.slider(
    "最低量比",
    min_value=0.5,
    max_value=5.0,
    value=1.0,
    step=0.1
)

st.sidebar.divider()

st.sidebar.subheader("🔄 資料更新")

auto_refresh = st.sidebar.checkbox(
    "啟用自動更新",
    value=False
)

refresh_seconds = st.sidebar.selectbox(
    "更新頻率",
    [30, 60, 120, 300],
    index=1
)

st.sidebar.caption(
    "V2.1 暫時使用 yfinance。\n"
    "正式盤中即時行情將在後續版本接入 Fugle。"
)


# =========================================================
# 主畫面
# =========================================================

st.markdown(
    '<div class="main-title">🖤 黑嚕嚕－台股盤中雷達</div>',
    unsafe_allow_html=True
)

st.markdown(
    '<div class="sub-title">'
    'V2.1｜技術分析 × 量能 × 趨勢 × 突破'
    '</div>',
    unsafe_allow_html=True
)


# =========================================================
# 自動更新說明
# =========================================================

if auto_refresh:

    st.info(
        f"🔄 自動更新已開啟：每 {refresh_seconds} 秒更新一次資料。"
    )

    try:

        from streamlit_autorefresh import st_autorefresh

        st_autorefresh(
            interval=refresh_seconds * 1000,
            key="black_heilu_refresh"
        )

    except ImportError:

        st.warning(
            "目前尚未安裝 streamlit-autorefresh，"
            "請先完成 requirements.txt 設定。"
        )


# =========================================================
# 掃描
# =========================================================

with st.spinner("正在掃描台股資料，第一次可能需要一些時間..."):

    result_df = scan_stocks(
        tuple(DEFAULT_STOCKS)
    )


# =========================================================
# 資料不存在
# =========================================================

if result_df.empty:

    st.error(
        "目前沒有取得股票資料。"
        "請稍後再試，或確認網路連線。"
    )

    st.stop()


# =========================================================
# 篩選
# =========================================================

filtered = result_df.copy()

filtered = filtered[
    filtered["黑嚕嚕分數"] >= min_score
]

filtered = filtered[
    filtered["量比"] >= min_volume_ratio
]


if mode == "🔥 強勢股":

    filtered = filtered[
        filtered["漲跌幅"] >= 2
    ]

elif mode == "🟢 守護生命線":

    filtered = filtered[
        (filtered["價格"] > filtered["MA20"])
        &
        (filtered["MA20"] > filtered["MA60"])
    ]

elif mode == "🚀 強勢突破":

    filtered = filtered[
        filtered["突破"] == True
    ]

elif mode == "⚠️ 大量換手高危":

    filtered = filtered[
        filtered["量比"] >= 2
    ]

elif mode == "🔴 弱勢股":

    filtered = filtered[
        filtered["漲跌幅"] <= -2
    ]


filtered = filtered.sort_values(
    "黑嚕嚕分數",
    ascending=False
)


# =========================================================
# KPI
# =========================================================

col1, col2, col3, col4, col5 = st.columns(5)

with col1:
    st.metric(
        "掃描股票",
        len(result_df)
    )

with col2:
    st.metric(
        "目前符合",
        len(filtered)
    )

with col3:

    if len(result_df) > 0:

        strongest = result_df.iloc[
            result_df["漲跌幅"].idxmax()
        ]

        st.metric(
            "最強漲幅",
            f"{strongest['漲跌幅']:.2f}%"
        )

with col4:

    avg_volume = result_df["量比"].mean()

    st.metric(
        "平均量比",
        f"{avg_volume:.2f}x"
    )

with col5:

    if len(result_df) > 0:

        max_score = result_df[
            "黑嚕嚕分數"
        ].max()

    else:

        max_score = 0

    st.metric(
        "最高分數",
        f"{max_score:.0f}"
    )


st.divider()


# =========================================================
# Tabs
# =========================================================

tab1, tab2, tab3, tab4 = st.tabs(
    [
        "📋 盤中排行榜",
        "🚨 警報中心",
        "📈 個股分析",
        "⭐ 自選股"
    ]
)


# =========================================================
# TAB 1
# =========================================================

with tab1:

    st.subheader(
        f"📋 {mode}"
    )

    if filtered.empty:

        st.warning(
            "目前沒有符合條件的股票。"
            "可以降低最低分數或量比條件。"
        )

    else:

        display_df = filtered.copy()

        display_df["價格"] = display_df[
            "價格"
        ].round(2)

        display_df["漲跌幅"] = display_df[
            "漲跌幅"
        ].round(2)

        display_df["量比"] = display_df[
            "量比"
        ].round(2)

        display_df["RSI"] = display_df[
            "RSI"
        ].round(1)

        display_df["黑嚕嚕分數"] = display_df[
            "黑嚕嚕分數"
        ].astype(int)

        display_df = display_df[
            [
                "代號",
                "名稱",
                "價格",
                "漲跌幅",
                "量比",
                "RSI",
                "突破",
                "連續量增",
                "黑嚕嚕分數"
            ]
        ]

        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True
        )


# =========================================================
# TAB 2 警報
# =========================================================

with tab2:

    st.subheader(
        "🚨 黑嚕嚕警報中心"
    )

    alerts = []

    for _, row in result_df.iterrows():

        symbol = row["代號"]
        name = row["名稱"]

        if row["量比"] >= 3:

            alerts.append(
                f"🔥 {symbol} {name}："
                f"爆量，量比 {row['量比']:.2f}x"
            )

        elif row["量比"] >= 2:

            alerts.append(
                f"🟠 {symbol} {name}："
                f"明顯量增，量比 {row['量比']:.2f}x"
            )

        if row["漲跌幅"] >= 5:

            alerts.append(
                f"🚀 {symbol} {name}："
                f"強勢上漲 {row['漲跌幅']:.2f}%"
            )

        if row["漲跌幅"] <= -5:

            alerts.append(
                f"🔴 {symbol} {name}："
                f"急跌 {row['漲跌幅']:.2f}%"
            )

        if row["突破"]:

            alerts.append(
                f"🚀 {symbol} {name}："
                "突破20日高點"
            )

        if row["連續量增"]:

            alerts.append(
                f"📈 {symbol} {name}："
                "連續量增"
            )


    if not alerts:

        st.success(
            "目前沒有特殊警報。"
        )

    else:

        for alert in alerts[:50]:

            st.markdown(
                f'<div class="alert-box">{alert}</div>',
                unsafe_allow_html=True
            )


# =========================================================
# TAB 3 個股分析
# =========================================================

with tab3:

    st.subheader(
        "📈 個股技術分析"
    )

    selected_symbol = st.selectbox(
        "選擇股票",
        DEFAULT_STOCKS,
        format_func=lambda x:
            f"{x}｜{stock_name(x)}"
    )

    selected_df = get_stock_data(
        selected_symbol
    )

    if selected_df.empty:

        st.warning(
            "無法取得這檔股票的資料。"
        )

    else:

        latest = selected_df.iloc[-1]

        current_price = float(
            latest["Close"]
        )

        current_rsi = float(
            latest["RSI"]
        )

        current_volume_ratio = float(
            latest["VolumeRatio"]
        )

        current_score = calculate_score(
            latest
        )

        st.markdown(
            f"## {selected_symbol}｜"
            f"{stock_name(selected_symbol)}"
        )

        c1, c2, c3, c4 = st.columns(4)

        with c1:

            st.metric(
                "目前價格",
                f"{current_price:.2f}"
            )

        with c2:

            st.metric(
                "RSI",
                f"{current_rsi:.1f}"
            )

        with c3:

            st.metric(
                "量比",
                f"{current_volume_ratio:.2f}x"
            )

        with c4:

            st.metric(
                "🖤 黑嚕嚕",
                f"{current_score}/100"
            )

        st.divider()

        chart_df = selected_df[
            [
                "Close",
                "MA20",
                "MA60",
                "MA200"
            ]
        ].copy()

        chart_df.columns = [
            "股價",
            "MA20",
            "MA60",
            "MA200"
        ]

        st.line_chart(
            chart_df,
            height=450
        )

        st.subheader(
            "📊 成交量"
        )

        st.bar_chart(
            selected_df[
                ["Volume"]
            ],
            height=250
        )


# =========================================================
# TAB 4 自選股
# =========================================================

with tab4:

    st.subheader(
        "⭐ 我的自選股"
    )

    watchlist = st.multiselect(

        "選擇自選股票",

        DEFAULT_STOCKS,

        default=[
            "2330",
            "2308",
            "2317",
            "2454"
        ],

        format_func=lambda x:
            f"{x}｜{stock_name(x)}"
    )

    if watchlist:

        watch_df = result_df[
            result_df["代號"].isin(watchlist)
        ].copy()

        watch_df = watch_df.sort_values(
            "黑嚕嚕分數",
            ascending=False
        )

        st.dataframe(
            watch_df[
                [
                    "代號",
                    "名稱",
                    "價格",
                    "漲跌幅",
                    "量比",
                    "RSI",
                    "黑嚕嚕分數"
                ]
            ].round(2),

            use_container_width=True,

            hide_index=True
        )

    else:

        st.info(
            "請選擇你的自選股票。"
        )


# =========================================================
# Footer
# =========================================================

st.divider()

st.caption(
    f"🖤 黑嚕嚕－台股盤中雷達 V2.1｜"
    f"資料更新時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
)

st.caption(
    "⚠️ 本工具僅供資訊與研究用途，不構成投資建議。"
)
