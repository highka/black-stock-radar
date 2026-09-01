import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime

# =========================================================
# 🖤 黑嚕嚕－台股盤中雷達 V2.2
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

st.markdown(
    """
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

    .section-title {
        font-size: 22px;
        font-weight: 700;
        margin-top: 10px;
        margin-bottom: 10px;
    }

    .alert-box {
        padding: 12px 15px;
        border-radius: 10px;
        margin-bottom: 8px;
        background-color: rgba(255, 165, 0, 0.08);
    }

    .score-card {
        padding: 12px;
        border-radius: 12px;
        background-color: rgba(128,128,128,0.08);
        text-align: center;
    }

    .stock-name {
        font-size: 18px;
        font-weight: 700;
    }

    .stock-code {
        font-size: 14px;
        color: #777;
    }

    </style>
    """,
    unsafe_allow_html=True
)


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

        # 清除空白
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

        if "市場" not in df.columns:
            df["市場"] = "未知"

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


# =========================================================
# 股票名稱
# =========================================================

def stock_name(symbol):

    symbol = str(symbol).zfill(4)

    result = stock_list_df.loc[
        stock_list_df["股票代號"] == symbol,
        "股票名稱"
    ]

    if not result.empty:
        return result.iloc[0]

    return symbol


# =========================================================
# 股票市場
# =========================================================

def stock_market(symbol):

    symbol = str(symbol).zfill(4)

    result = stock_list_df.loc[
        stock_list_df["股票代號"] == symbol,
        "市場"
    ]

    if not result.empty:
        return result.iloc[0]

    return "未知"


# =========================================================
# RSI
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


# =========================================================
# 技術指標
# =========================================================

def calculate_indicators(df):

    if df.empty:
        return df

    df = df.copy()

    close = df["Close"]
    volume = df["Volume"]

    # 均線
    df["MA20"] = close.rolling(20).mean()
    df["MA60"] = close.rolling(60).mean()
    df["MA200"] = close.rolling(200).mean()

    # RSI
    df["RSI"] = calculate_rsi(close)

    # 20日平均成交量
    df["VOL20"] = volume.rolling(20).mean()

    # 量比
    df["VolumeRatio"] = (
        volume /
        df["VOL20"].replace(0, np.nan)
    )

    # 前一日20日高點
    df["High20"] = (
        close.rolling(20)
        .max()
        .shift(1)
    )

    # 漲跌幅
    df["Return"] = close.pct_change() * 100

    # 連續量增
    df["VolumeUp2"] = (
        (volume > volume.shift(1))
        &
        (volume.shift(1) > volume.shift(2))
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

    # =====================================================
    # 趨勢
    # =====================================================

    if pd.notna(ma20) and close > ma20:
        score += 10

    if pd.notna(ma60) and close > ma60:
        score += 10

    if (
        pd.notna(ma20)
        and
        pd.notna(ma60)
        and
        ma20 > ma60
    ):
        score += 10

    if (
        pd.notna(ma60)
        and
        pd.notna(ma200)
        and
        ma60 > ma200
    ):
        score += 10

    # =====================================================
    # 量能
    # =====================================================

    if volume_ratio >= 1.2:
        score += 5

    if volume_ratio >= 1.5:
        score += 5

    if volume_ratio >= 2:
        score += 5

    if volume_ratio >= 3:
        score += 5

    # =====================================================
    # 動能
    # =====================================================

    if return_pct >= 1:
        score += 5

    if return_pct >= 3:
        score += 5

    if return_pct >= 5:
        score += 5

    # =====================================================
    # RSI
    # =====================================================

    if 50 <= rsi <= 70:
        score += 10

    elif 70 < rsi <= 80:
        score += 5

    # =====================================================
    # 突破
    # =====================================================

    if (
        pd.notna(high20)
        and
        close >= high20
    ):
        score += 10

    return min(score, 100)


# =========================================================
# 取得股票資料
# =========================================================

@st.cache_data(
    ttl=300,
    show_spinner=False
)
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

        # 處理 yfinance MultiIndex
        if isinstance(
            df.columns,
            pd.MultiIndex
        ):

            df.columns = (
                df.columns
                .get_level_values(0)
            )

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

        df.dropna(
            subset=["Close"],
            inplace=True
        )

        df = calculate_indicators(df)

        return df

    except Exception:

        return pd.DataFrame()


# =========================================================
# 掃描股票
# =========================================================

@st.cache_data(
    ttl=300,
    show_spinner=False
)
def scan_stocks(stock_list):

    results = []

    for symbol in stock_list:

        df = get_stock_data(symbol)

        if df.empty:
            continue

        if len(df) < 60:
            continue

        row = df.iloc[-1]

        close = float(
            row["Close"]
        )

        if len(df) >= 2:

            previous_close = float(
                df["Close"].iloc[-2]
            )

        else:

            previous_close = close

        if previous_close != 0:

            change_pct = (
                close /
                previous_close -
                1
            ) * 100

        else:

            change_pct = 0

        volume_ratio = (
            float(row["VolumeRatio"])
            if pd.notna(
                row["VolumeRatio"]
            )
            else 1
        )

        rsi = (
            float(row["RSI"])
            if pd.notna(
                row["RSI"]
            )
            else 50
        )

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

        score = calculate_score(row)

        breakout = (
            pd.notna(high20)
            and
            close >= high20
        )

        volume_up = bool(
            row["VolumeUp2"]
        )

        results.append({

            "代號": str(symbol),

            "名稱": stock_name(symbol),

            "市場": stock_market(symbol),

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
# 預設股票
# =========================================================

if stock_list_df.empty:

    st.error(
        "stock_list.csv 沒有資料，"
        "請確認檔案是否存在。"
    )

    st.stop()


DEFAULT_STOCKS = (
    stock_list_df["股票代號"]
    .dropna()
    .astype(str)
    .str.zfill(4)
    .tolist()
)


# =========================================================
# Sidebar
# =========================================================

st.sidebar.title("🖤 黑嚕嚕")

st.sidebar.caption(
    "台股盤中雷達 V2.2"
)

st.sidebar.divider()


# =========================================================
# 市場篩選
# =========================================================

st.sidebar.subheader(
    "🏢 市場"
)

market_options = [
    "上市",
    "上櫃",
    "興櫃"
]

selected_markets = st.sidebar.multiselect(
    "選擇市場",
    market_options,
    default=["上市", "上櫃"]
)


# =========================================================
# 雷達策略
# =========================================================

st.sidebar.subheader(
    "🎯 雷達策略"
)

strategy = st.sidebar.selectbox(

    "選擇策略",

    [
        "全部股票",
        "🔥 強勢股",
        "🟢 守護生命線",
        "🚀 強勢突破",
        "⚠️ 大量換手高危",
        "🔴 弱勢股"
    ]

)


# =========================================================
# 分數
# =========================================================

st.sidebar.subheader(
    "🖤 黑嚕嚕條件"
)

min_score = st.sidebar.slider(
    "最低黑嚕嚕分數",
    0,
    100,
    50,
    5
)


# =========================================================
# 量比
# =========================================================

min_volume_ratio = st.sidebar.slider(
    "最低量比",
    0.5,
    5.0,
    1.0,
    0.1
)


# =========================================================
# RSI
# =========================================================

st.sidebar.subheader(
    "📊 RSI"
)

rsi_min, rsi_max = st.sidebar.slider(
    "RSI 範圍",
    0,
    100,
    (30, 80)
)


# =========================================================
# 漲幅
# =========================================================

st.sidebar.subheader(
    "📈 漲跌幅"
)

change_min, change_max = st.sidebar.slider(
    "漲跌幅 %",
    -10.0,
    10.0,
    (-10.0, 10.0),
    0.5
)


# =========================================================
# 排序
# =========================================================

sort_options = {

    "黑嚕嚕分數": "黑嚕嚕分數",

    "漲跌幅": "漲跌幅",

    "量比": "量比",

    "RSI": "RSI",

    "價格": "價格"

}

sort_label = st.sidebar.selectbox(
    "排行榜排序",
    list(sort_options.keys())
)

sort_column = sort_options[
    sort_label
]

sort_descending = st.sidebar.checkbox(
    "由高到低",
    value=True
)


# =========================================================
# 更新
# =========================================================

st.sidebar.divider()

st.sidebar.subheader(
    "🔄 資料更新"
)

auto_refresh = st.sidebar.checkbox(
    "啟用自動更新",
    value=False
)

refresh_seconds = st.sidebar.selectbox(
    "更新頻率",
    [30, 60, 120, 300],
    index=1
)

if auto_refresh:

    try:

        from streamlit_autorefresh import (
            st_autorefresh
        )

        st_autorefresh(
            interval=refresh_seconds * 1000,
            key="black_heilu_refresh_v22"
        )

    except ImportError:

        st.warning(
            "尚未安裝 streamlit-autorefresh。"
            "請確認 requirements.txt 已包含 "
            "streamlit-autorefresh。"
        )


# =========================================================
# 主標題
# =========================================================

st.markdown(
    '<div class="main-title">'
    '🖤 黑嚕嚕－台股盤中雷達'
    '</div>',
    unsafe_allow_html=True
)

st.markdown(
    '<div class="sub-title">'
    'V2.2｜市場篩選 × 趨勢 × 量能 × RSI × 突破'
    '</div>',
    unsafe_allow_html=True
)


# =========================================================
# 目前市場條件
# =========================================================

if selected_markets:

    market_text = "、".join(
        selected_markets
    )

else:

    market_text = "未選擇市場"


st.info(
    f"🏢 市場：{market_text}　｜　"
    f"🎯 策略：{strategy}　｜　"
    f"🖤 最低分數：{min_score}"
)


# =========================================================
# 掃描
# =========================================================

with st.spinner(
    "正在掃描股票資料，第一次可能需要一些時間..."
):

    result_df = scan_stocks(
        tuple(DEFAULT_STOCKS)
    )


# =========================================================
# 無資料
# =========================================================

if result_df.empty:

    st.error(
        "目前沒有取得股票資料。"
        "請稍後再試。"
    )

    st.stop()


# =========================================================
# 市場篩選
# =========================================================

filtered = result_df.copy()

if selected_markets:

    filtered = filtered[
        filtered["市場"].isin(
            selected_markets
        )
    ]

else:

    filtered = filtered.iloc[0:0]


# =========================================================
# 基本條件
# =========================================================

filtered = filtered[
    filtered["黑嚕嚕分數"] >= min_score
]

filtered = filtered[
    filtered["量比"] >= min_volume_ratio
]

filtered = filtered[
    filtered["RSI"].between(
        rsi_min,
        rsi_max
    )
]

filtered = filtered[
    filtered["漲跌幅"].between(
        change_min,
        change_max
    )
]


# =========================================================
# 策略
# =========================================================

if strategy == "🔥 強勢股":

    filtered = filtered[
        filtered["漲跌幅"] >= 2
    ]


elif strategy == "🟢 守護生命線":

    filtered = filtered[
        (
            filtered["價格"]
            >
            filtered["MA20"]
        )
        &
        (
            filtered["MA20"]
            >
            filtered["MA60"]
        )
    ]


elif strategy == "🚀 強勢突破":

    filtered = filtered[
        filtered["突破"] == True
    ]


elif strategy == "⚠️ 大量換手高危":

    filtered = filtered[
        filtered["量比"] >= 2
    ]


elif strategy == "🔴 弱勢股":

    filtered = filtered[
        filtered["漲跌幅"] <= -2
    ]


# =========================================================
# 排序
# =========================================================

filtered = filtered.sort_values(
    sort_column,
    ascending=not sort_descending
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
        "符合條件",
        len(filtered)
    )


with col3:

    if not result_df.empty:

        strongest = result_df.loc[
            result_df["漲跌幅"].idxmax()
        ]

        st.metric(
            "最高漲幅",
            f"{strongest['漲跌幅']:.2f}%"
        )

    else:

        st.metric(
            "最高漲幅",
            "-"
        )


with col4:

    avg_volume = (
        result_df["量比"].mean()
    )

    st.metric(
        "平均量比",
        f"{avg_volume:.2f}x"
    )


with col5:

    max_score = (
        result_df["黑嚕嚕分數"].max()
    )

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

    st.markdown(
        '<div class="section-title">'
        '📋 黑嚕嚕排行榜'
        '</div>',
        unsafe_allow_html=True
    )

    if filtered.empty:

        st.warning(
            "目前沒有符合條件的股票。"
            "可以降低分數、量比或 RSI 條件。"
        )

    else:

        display_df = filtered.copy()

        display_df["價格"] = (
            display_df["價格"]
            .round(2)
        )

        display_df["漲跌幅"] = (
            display_df["漲跌幅"]
            .round(2)
        )

        display_df["量比"] = (
            display_df["量比"]
            .round(2)
        )

        display_df["RSI"] = (
            display_df["RSI"]
            .round(1)
        )

        display_df["黑嚕嚕分數"] = (
            display_df["黑嚕嚕分數"]
            .astype(int)
        )

        display_df = display_df[
            [
                "代號",
                "名稱",
                "市場",
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
            hide_index=True,
            height=600
        )


# =========================================================
# TAB 2
# =========================================================

with tab2:

    st.markdown(
        '<div class="section-title">'
        '🚨 黑嚕嚕警報中心'
        '</div>',
        unsafe_allow_html=True
    )

    alerts = []

    for _, row in result_df.iterrows():

        symbol = row["代號"]
        name = row["名稱"]

        # 爆量
        if row["量比"] >= 3:

            alerts.append(
                f"🔥 {symbol} {name}｜"
                f"爆量｜量比 "
                f"{row['量比']:.2f}x"
            )

        elif row["量比"] >= 2:

            alerts.append(
                f"🟠 {symbol} {name}｜"
                f"明顯量增｜量比 "
                f"{row['量比']:.2f}x"
            )

        # 大漲
        if row["漲跌幅"] >= 5:

            alerts.append(
                f"🚀 {symbol} {name}｜"
                f"強勢上漲 "
                f"{row['漲跌幅']:.2f}%"
            )

        # 急跌
        if row["漲跌幅"] <= -5:

            alerts.append(
                f"🔴 {symbol} {name}｜"
                f"急跌 "
                f"{row['漲跌幅']:.2f}%"
            )

        # 突破
        if row["突破"]:

            alerts.append(
                f"🚀 {symbol} {name}｜"
                "突破20日高點"
            )

        # 連量
        if row["連續量增"]:

            alerts.append(
                f"📈 {symbol} {name}｜"
                "連續量增"
            )


    if not alerts:

        st.success(
            "目前沒有特殊警報。"
        )

    else:

        for alert in alerts[:100]:

            st.markdown(
                f'<div class="alert-box">'
                f'{alert}'
                f'</div>',
                unsafe_allow_html=True
            )


# =========================================================
# TAB 3
# =========================================================

with tab3:

    st.markdown(
        '<div class="section-title">'
        '📈 個股技術分析'
        '</div>',
        unsafe_allow_html=True
    )

    selected_symbol = st.selectbox(

        "選擇股票",

        DEFAULT_STOCKS,

        format_func=lambda x:
            f"{x}｜{stock_name(x)}｜"
            f"{stock_market(x)}"
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

        # -------------------------------------------------
        # 股票名稱
        # -------------------------------------------------

        st.markdown(
            f"## {selected_symbol}｜"
            f"{stock_name(selected_symbol)}"
        )

        st.caption(
            f"市場：{stock_market(selected_symbol)}"
        )


        # -------------------------------------------------
        # KPI
        # -------------------------------------------------

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


        # -------------------------------------------------
        # 股價＋均線
        # -------------------------------------------------

        st.subheader(
            "📈 股價與均線"
        )

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


        # -------------------------------------------------
        # RSI
        # -------------------------------------------------

        st.subheader(
            "📊 RSI"
        )

        rsi_chart = (
            selected_df[["RSI"]]
            .copy()
        )

        st.line_chart(
            rsi_chart,
            height=250
        )


        # -------------------------------------------------
        # 成交量
        # -------------------------------------------------

        st.subheader(
            "📊 成交量"
        )

        volume_chart = (
            selected_df[["Volume"]]
            .copy()
        )

        st.bar_chart(
            volume_chart,
            height=250
        )


# =========================================================
# TAB 4
# =========================================================

with tab4:

    st.markdown(
        '<div class="section-title">'
        '⭐ 我的自選股'
        '</div>',
        unsafe_allow_html=True
    )


    watchlist = st.multiselect(

        "選擇自選股票",

        DEFAULT_STOCKS,

        default=[
            x
            for x in [
                "2330",
                "2308",
                "2317",
                "2454"
            ]
            if x in DEFAULT_STOCKS
        ],

        format_func=lambda x:
            f"{x}｜{stock_name(x)}"
    )


    if watchlist:

        watch_df = result_df[
            result_df["代號"]
            .isin(watchlist)
        ].copy()


        watch_df = watch_df.sort_values(
            "黑嚕嚕分數",
            ascending=False
        )


        watch_df["價格"] = (
            watch_df["價格"]
            .round(2)
        )

        watch_df["漲跌幅"] = (
            watch_df["漲跌幅"]
            .round(2)
        )

        watch_df["量比"] = (
            watch_df["量比"]
            .round(2)
        )

        watch_df["RSI"] = (
            watch_df["RSI"]
            .round(1)
        )


        st.dataframe(

            watch_df[
                [
                    "代號",
                    "名稱",
                    "市場",
                    "價格",
                    "漲跌幅",
                    "量比",
                    "RSI",
                    "黑嚕嚕分數"
                ]
            ],

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
    "🖤 黑嚕嚕－台股盤中雷達 V2.2｜"
    f"頁面更新時間："
    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
)

st.caption(
    "⚠️ 本工具目前使用公開市場資料進行研究分析，"
    "不構成投資建議。"
)
