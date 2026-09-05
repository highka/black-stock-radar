import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import requests
import re
from datetime import datetime
from streamlit_autorefresh import st_autorefresh

# ============================================================
# 🖤 黑嚕嚕－台股盤中雷達 V3.3
# V3.3：全市場股票池自動同步（上市／上櫃／興櫃），V4 再接 Fugle 即時行情
# ============================================================

st.set_page_config(page_title='🖤 黑嚕嚕－台股盤中雷達', page_icon='🖤', layout='wide', initial_sidebar_state='expanded')

st.markdown('''
<style>
.block-container{padding-top:1rem;padding-bottom:2rem}
[data-testid="stMetric"]{border:1px solid rgba(128,128,128,.25);border-radius:12px;padding:10px}
.radar-card{border:1px solid rgba(128,128,128,.30);border-radius:14px;padding:14px 16px;margin-bottom:10px;min-height:170px}
.radar-title{font-size:19px;font-weight:800}.radar-price{font-size:28px;font-weight:900;margin:4px 0}.radar-score{font-size:21px;font-weight:800;margin-top:6px}.small{opacity:.70;font-size:12px}.signal{font-weight:800;font-size:15px}
</style>''', unsafe_allow_html=True)

DEFAULT_STOCKS='''1101,1102,1216,1301,1303,1402,1476,1597,2002,2301,2303,2308,2317,2330,2345,2353,2356,2359,2368,2376,2382,2395,2408,2454,2455,2603,2609,2615,3006,3034,3037,3044,3231,3260,3443,3455,3661,3711,4763,4966,5274,5483,6125,6147,6182,6239,6271,6409,6669,8046,8299'''

@st.cache_data(ttl=3600, show_spinner=False)
def load_stock_list():
    try:
        d=pd.read_csv('stock_list.csv',dtype=str)
        d.columns=[str(c).strip() for c in d.columns]
        code='股票代號' if '股票代號' in d.columns else d.columns[0]
        d[code]=d[code].astype(str).str.extract(r'(\d+)',expand=False).fillna('').str.zfill(4)
        for c in ['股票名稱','市場']:
            if c in d.columns:d[c]=d[c].fillna('').astype(str).str.strip()
        return d
    except Exception:
        return pd.DataFrame(columns=['股票代號','股票名稱','市場'])

STOCK_LIST=load_stock_list()

@st.cache_data(ttl=86400, show_spinner=False)
def load_market_universe():
    """從 TWSE / TPEx 官方 OpenAPI 自動建立上市、上櫃、興櫃公司股票池。
    API 失敗時回退至 stock_list.csv，避免整個雷達無法啟動。
    """
    sources = [
        ('上市', 'https://openapi.twse.com.tw/v1/opendata/t187ap03_L'),
        ('上櫃', 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O'),
        ('興櫃', 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_R'),
    ]
    rows=[]
    source_status=[]
    for market,url in sources:
        try:
            r=requests.get(url,timeout=20,headers={'User-Agent':'Mozilla/5.0'})
            r.raise_for_status()
            data=r.json()
            if isinstance(data,dict):
                data=data.get('data',data.get('results',[]))
            if not isinstance(data,list):
                raise ValueError('API 回傳格式不是清單')
            for item in data:
                if not isinstance(item,dict): continue
                code=''; name=''
                if market=='上市':
                    code=item.get('公司代號',item.get('SecuritiesCompanyCode',''))
                    name=item.get('公司簡稱',item.get('CompanyAbbreviation',item.get('公司名稱','')))
                else:
                    code=item.get('SecuritiesCompanyCode',item.get('公司代號',''))
                    name=item.get('CompanyAbbreviation',item.get('公司簡稱',item.get('CompanyName','')))
                code=str(code).strip().upper()
                name=str(name).strip()
                if re.fullmatch(r'[0-9A-Z]{4,6}',code) and name:
                    rows.append({'股票代號':code,'股票名稱':name,'市場':market})
            source_status.append(f'{market}：成功')
        except Exception as e:
            source_status.append(f'{market}：失敗（{type(e).__name__}）')
    d=pd.DataFrame(rows,columns=['股票代號','股票名稱','市場']).drop_duplicates('股票代號')
    if d.empty:
        d=STOCK_LIST.copy()
        if not d.empty:
            source_status=['官方 API 目前無法取得，已回退 stock_list.csv']
        else:
            source_status=['官方 API 與 stock_list.csv 都無資料']
    return d, source_status


UNIVERSE, UNIVERSE_STATUS = load_market_universe()

def stock_name(s):
    if not UNIVERSE.empty and '股票名稱' in UNIVERSE.columns:
        x=UNIVERSE[UNIVERSE['股票代號']==str(s).zfill(4)]
        if not x.empty and str(x.iloc[0]['股票名稱']).strip():return str(x.iloc[0]['股票名稱']).strip()
    return str(s)

def stock_market(s):
    if not UNIVERSE.empty and '市場' in UNIVERSE.columns:
        x=UNIVERSE[UNIVERSE['股票代號']==str(s).zfill(4)]
        if not x.empty:return str(x.iloc[0]['市場']).strip()
    return '未分類'

@st.cache_data(ttl=900, show_spinner=False)
def get_stock_data(symbol, market=None):
    try:
        suffix='.TWO' if market in ['上櫃','興櫃'] else '.TW'
        d=yf.download(f'{str(symbol).zfill(4)}{suffix}',period='2y',interval='1d',auto_adjust=False,progress=False,threads=False)
        if d is None or d.empty:return None
        if isinstance(d.columns,pd.MultiIndex):d.columns=d.columns.get_level_values(0)
        cols=['Open','High','Low','Close','Volume']
        if any(c not in d.columns for c in cols):return None
        d=d[cols].copy().dropna(subset=['Close'])
        if len(d)<200:return None
        d.index=pd.to_datetime(d.index);d.index.name='Date'
        for c in cols:d[c]=pd.to_numeric(d[c],errors='coerce')
        return d.dropna(subset=['Close'])
    except Exception:return None

def indicators(d):
    d=d.copy()
    d['MA20']=d.Close.rolling(20).mean();d['MA60']=d.Close.rolling(60).mean();d['MA200']=d.Close.rolling(200).mean()
    delta=d.Close.diff();gain=delta.clip(lower=0);loss=-delta.clip(upper=0)
    ag=gain.rolling(14).mean();al=loss.rolling(14).mean();rs=ag/al.replace(0,np.nan)
    d['RSI']=100-(100/(1+rs));d['VOL_MA20']=d.Volume.rolling(20).mean();d['VOL_RATIO']=d.Volume/d.VOL_MA20.replace(0,np.nan)
    d['CHANGE']=d.Close.pct_change()*100;d['HIGH20']=d.High.shift(1).rolling(20).max();d['HIGH60']=d.High.shift(1).rolling(60).max()
    up=d.Volume>d.Volume.shift(1);cnt=[];n=0
    for f in up.fillna(False):n=n+1 if f else 0;cnt.append(n)
    d['CONSEC_VOL']=cnt;d['MA20_SLOPE']=d.MA20-d.MA20.shift(5);d['MA60_SLOPE']=d.MA60-d.MA60.shift(5)
    return d

def score_level(s):
    return '🟣 黑嚕嚕超強' if s>=90 else '🔥 強勢' if s>=80 else '🚀 注意' if s>=70 else '👀 觀察' if s>=60 else '⚪ 一般'

def black_score(d):
    x=d.iloc[-1];close=x.Close;ma20=x.MA20;ma60=x.MA60;ma200=x.MA200;rsi=x.RSI;vr=x.VOL_RATIO;chg=x.CHANGE;h20=x.HIGH20;h60=x.HIGH60
    trend=momentum=volume=breakout=rsi_s=extra=0;reasons=[]
    if pd.notna(ma20) and close>ma20:trend+=8;reasons.append('站上MA20')
    if pd.notna(ma60) and close>ma60:trend+=7;reasons.append('站上MA60')
    if pd.notna(ma200) and close>ma200:trend+=5;reasons.append('站上MA200')
    if pd.notna(ma20) and pd.notna(ma60) and ma20>ma60:trend+=5;reasons.append('MA20>MA60')
    if pd.notna(ma20) and pd.notna(ma60) and pd.notna(ma200) and ma20>ma60>ma200:trend+=5;reasons.append('多頭排列')
    if pd.notna(chg):
        if chg>=5:momentum+=8;reasons.append('強勢上漲')
        elif chg>=3:momentum+=6;reasons.append('明顯上漲')
        elif chg>=1:momentum+=4;reasons.append('今日偏強')
        elif chg>0:momentum+=2
    if pd.notna(x.MA20_SLOPE) and x.MA20_SLOPE>0:momentum+=5;reasons.append('MA20上彎')
    if pd.notna(x.MA60_SLOPE) and x.MA60_SLOPE>0:momentum+=4;reasons.append('MA60上彎')
    if pd.notna(ma20) and close>ma20*1.03:momentum+=3;reasons.append('脫離生命線')
    if pd.notna(vr):
        if vr>=3:volume+=20;reasons.append('3倍以上爆量')
        elif vr>=2:volume+=15;reasons.append('2倍以上放量')
        elif vr>=1.5:volume+=10;reasons.append('明顯量增')
        elif vr>=1.2:volume+=6;reasons.append('量能增加')
        elif vr>=1:volume+=3
    if pd.notna(h20) and close>=h20:breakout+=10;reasons.append('突破20日高點')
    elif pd.notna(h20) and close>=h20*.98:breakout+=6;reasons.append('接近20日高點')
    if pd.notna(h60) and close>=h60:breakout+=5;reasons.append('突破60日高點')
    if pd.notna(rsi):
        if 55<=rsi<=70:rsi_s+=10;reasons.append('RSI健康偏強')
        elif 50<=rsi<55:rsi_s+=7;reasons.append('RSI轉強')
        elif 70<rsi<=78:rsi_s+=6;reasons.append('RSI強勢')
        elif 45<=rsi<50:rsi_s+=3
        elif rsi>78:rsi_s+=1;reasons.append('RSI偏熱')
    if int(x.CONSEC_VOL)>=3:extra+=2;reasons.append('連續3日量增')
    elif int(x.CONSEC_VOL)>=2:extra+=1
    if pd.notna(ma20) and pd.notna(ma60) and close>ma20>ma60 and pd.notna(chg) and chg>0:extra+=2
    if pd.notna(vr) and pd.notna(chg) and vr>=1.5 and chg>2:extra+=1
    total=int(min(100,max(0,trend+momentum+volume+breakout+rsi_s+extra)))
    return total,{'趨勢':trend,'動能':momentum,'成交量':volume,'突破':breakout,'RSI':rsi_s,'額外強度':extra},reasons

def signals(d,score):
    x=d.iloc[-1];close=x.Close;ma20=x.MA20;ma60=x.MA60;ma200=x.MA200;rsi=x.RSI;vr=x.VOL_RATIO;chg=x.CHANGE;h20=x.HIGH20;s=[]
    if pd.notna(h20) and close>=h20 and pd.notna(vr) and vr>=1.3 and pd.notna(chg) and chg>0:s.append('🚀 強勢突破')
    if pd.notna(ma20) and pd.notna(ma60) and pd.notna(ma200) and close>ma20>ma60>ma200 and pd.notna(vr) and vr>=1.1 and pd.notna(rsi) and 50<=rsi<=78:s.append('🔥 主升段')
    if pd.notna(ma20) and pd.notna(ma200) and close>=ma20*.98 and close<=ma20*1.03 and close>=ma200 and pd.notna(rsi) and rsi>=45:s.append('🟢 守護生命線')
    if pd.notna(vr) and vr>=2 and pd.notna(chg) and chg>=3 and ((pd.notna(rsi) and rsi>=78) or vr>=3):s.append('⚠️ 爆量高危')
    weak=(pd.notna(ma20) and close<ma20) or (pd.notna(ma60) and pd.notna(ma200) and ma60<ma200) or (pd.notna(chg) and chg<=-3 and pd.notna(vr) and vr>=1.3)
    if weak and score<60:s.append('🔴 趨勢轉弱')
    if not s:s.append(score_level(score))
    return s

def build_row(symbol,df):
    if df is None or len(df)<200:return None
    d=indicators(df);x=d.iloc[-1];score,bd,reasons=black_score(d);sig=signals(d,score)
    return {'股票':str(symbol).zfill(4),'名稱':stock_name(symbol),'市場':stock_market(symbol),'價格':float(x.Close),'漲跌%':float(x.CHANGE) if pd.notna(x.CHANGE) else 0.0,'成交量':float(x.Volume),'量比':float(x.VOL_RATIO) if pd.notna(x.VOL_RATIO) else 0.0,'RSI':float(x.RSI) if pd.notna(x.RSI) else np.nan,'MA20':float(x.MA20) if pd.notna(x.MA20) else np.nan,'MA60':float(x.MA60) if pd.notna(x.MA60) else np.nan,'MA200':float(x.MA200) if pd.notna(x.MA200) else np.nan,'連量':int(x.CONSEC_VOL),'20日高':float(x.HIGH20) if pd.notna(x.HIGH20) else np.nan,'60日高':float(x.HIGH60) if pd.notna(x.HIGH60) else np.nan,'黑嚕嚕分數':score,'等級':score_level(score),'訊號':'、'.join(sig),'判斷':'、'.join(dict.fromkeys(reasons[:8])),'趨勢分':bd['趨勢'],'動能分':bd['動能'],'量能分':bd['成交量'],'突破分':bd['突破'],'RSI分':bd['RSI'],'額外分':bd['額外強度'],'_df':d}


# ============================================================
# 🧪 V3.2 歷史訊號回測引擎
# ============================================================

SIGNAL_LABELS = [
    '🚀 強勢突破','🚀 60日突破','👀 接近突破','🔥 主升段',
    '🟢 守護生命線','⚠️ 爆量高危','⚠️ 爆量突破','⚠️ 爆量過熱',
    '⚠️ 爆量下跌','🔴 趨勢轉弱'
]

def historical_signal_events(df):
    """逐日重算指標與訊號，避免使用未來資料。"""
    if df is None or len(df) < 210:
        return pd.DataFrame()
    base = df.copy()
    events = []
    for i in range(200, len(base)):
        hist = base.iloc[:i+1].copy()
        d = indicators(hist)
        x = d.iloc[-1]
        score, _, _ = black_score(d)
        ma20, ma60, ma200 = x.MA20, x.MA60, x.MA200
        close, rsi, vr, chg = x.Close, x.RSI, x.VOL_RATIO, x.CHANGE
        h20, h60 = x.HIGH20, x.HIGH60
        sigs = []
        if pd.notna(h20) and close >= h20 and pd.notna(vr) and vr >= 1.3 and pd.notna(chg) and chg > 0:
            sigs.append('🚀 強勢突破')
        if pd.notna(h60) and close >= h60 and pd.notna(vr) and vr >= 1.1 and pd.notna(chg) and chg > 0:
            sigs.append('🚀 60日突破')
        if pd.notna(h20) and close >= h20*.98 and close < h20:
            sigs.append('👀 接近突破')
        if pd.notna(ma20) and pd.notna(ma60) and pd.notna(ma200) and close > ma20 > ma60 > ma200 and pd.notna(vr) and vr >= 1.1 and pd.notna(rsi) and 50 <= rsi <= 78:
            sigs.append('🔥 主升段')
        if pd.notna(ma20) and pd.notna(ma200) and close >= ma20*.98 and close <= ma20*1.03 and close >= ma200 and pd.notna(rsi) and rsi >= 45:
            sigs.append('🟢 守護生命線')
        if pd.notna(vr) and vr >= 2 and pd.notna(chg) and chg >= 3 and ((pd.notna(rsi) and rsi >= 78) or vr >= 3):
            sigs.append('⚠️ 爆量高危')
        if pd.notna(vr) and vr >= 2 and pd.notna(chg) and chg > 0 and pd.notna(h20) and close >= h20:
            sigs.append('⚠️ 爆量突破')
        if pd.notna(vr) and vr >= 2 and pd.notna(rsi) and rsi >= 78:
            sigs.append('⚠️ 爆量過熱')
        if pd.notna(vr) and vr >= 1.5 and pd.notna(chg) and chg <= -3:
            sigs.append('⚠️ 爆量下跌')
        weak = ((pd.notna(ma20) and close < ma20) or
                (pd.notna(ma60) and pd.notna(ma200) and ma60 < ma200) or
                (pd.notna(chg) and chg <= -3 and pd.notna(vr) and vr >= 1.3))
        if weak and score < 60:
            sigs.append('🔴 趨勢轉弱')
        for sig in sigs:
            events.append({
                '日期': d.index[-1], '訊號': sig, '收盤': float(close),
                '黑嚕嚕分數': int(score), '漲跌%': float(chg) if pd.notna(chg) else np.nan,
                '量比': float(vr) if pd.notna(vr) else np.nan, 'RSI': float(rsi) if pd.notna(rsi) else np.nan
            })
    return pd.DataFrame(events)

def run_backtest(symbol, df, horizon, min_gap, selected_signals, min_score):
    """訊號日收盤進場，N 個交易日後收盤出場。"""
    ev = historical_signal_events(df)
    if ev.empty:
        return pd.DataFrame()
    ev = ev[(ev['訊號'].isin(selected_signals)) & (ev['黑嚕嚕分數'] >= min_score)].copy()
    if ev.empty:
        return ev
    close = df['Close'].copy()
    highs, lows = df['High'], df['Low']
    rows, last_by_signal = [], {}
    idx = list(close.index)
    pos_map = {pd.Timestamp(x): i for i, x in enumerate(idx)}
    for _, e in ev.sort_values('日期').iterrows():
        date = pd.Timestamp(e['日期'])
        entry_i = pos_map.get(date)
        if entry_i is None or entry_i + horizon >= len(idx):
            continue
        sig = e['訊號']
        prev_i = last_by_signal.get(sig)
        if prev_i is not None and entry_i - prev_i < min_gap:
            continue
        exit_i = entry_i + horizon
        entry = float(close.iloc[entry_i]); exit_price = float(close.iloc[exit_i])
        future_close = close.iloc[entry_i+1:exit_i+1]
        future_high = highs.iloc[entry_i+1:exit_i+1]
        future_low = lows.iloc[entry_i+1:exit_i+1]
        ret = (exit_price / entry - 1) * 100
        mfe = (float(future_high.max()) / entry - 1) * 100 if len(future_high) else 0
        mae = (float(future_low.min()) / entry - 1) * 100 if len(future_low) else 0
        rows.append({
            '股票': str(symbol).zfill(4), '名稱': stock_name(symbol),
            '訊號': sig, '訊號日期': date, '出場日期': idx[exit_i],
            '持有天數': horizon, '進場價': entry, '出場價': exit_price,
            '報酬%': ret, 'MFE%': mfe, 'MAE%': mae,
            '黑嚕嚕分數': int(e['黑嚕嚕分數']), '量比': e['量比'], 'RSI': e['RSI']
        })
        last_by_signal[sig] = entry_i
    return pd.DataFrame(rows)

def backtest_summary(events):
    if events is None or events.empty:
        return pd.DataFrame()
    g = events.groupby('訊號', dropna=False)
    out = g['報酬%'].agg(['count','mean','median','sum']).reset_index()
    wins = g['報酬%'].apply(lambda x: (x > 0).mean() * 100).reset_index(name='勝率%')
    out = out.merge(wins, on='訊號', how='left')
    out.columns = ['訊號','樣本數','平均報酬%','中位數報酬%','報酬加總%','勝率%']
    return out.sort_values(['勝率%','平均報酬%'], ascending=False)

def overall_backtest_stats(events):
    if events is None or events.empty:
        return {'樣本數':0,'勝率%':np.nan,'平均報酬%':np.nan,'中位數報酬%':np.nan,'報酬加總%':np.nan}
    r = events['報酬%']
    return {'樣本數':len(r),'勝率%':(r > 0).mean()*100,'平均報酬%':r.mean(),'中位數報酬%':r.median(),'報酬加總%':r.sum()}


# Sidebar
st.sidebar.title('🖤 黑嚕嚕－台股盤中雷達');st.sidebar.caption('V3.3｜全市場股票池＋V3.2 回測')
mode=st.sidebar.selectbox('雷達模式',['全部股票','🟣 黑嚕嚕超強','🔥 強勢股','🚀 強勢突破','🔥 主升段','🟢 守護生命線','⚠️ 大量換手高危','🔴 趨勢轉弱'])
markets=st.sidebar.multiselect('市場',['上市','上櫃','興櫃'],default=['上市','上櫃','興櫃'])
if st.sidebar.button('🔄 更新全市場股票池'):
    load_market_universe.clear()
    st.rerun()
counts=UNIVERSE['市場'].value_counts().to_dict() if not UNIVERSE.empty else {}
st.sidebar.caption(f"官方股票池：上市 {counts.get('上市',0)}｜上櫃 {counts.get('上櫃',0)}｜興櫃 {counts.get('興櫃',0)}")
max_n=st.sidebar.slider('最多掃描股票數',10,500,150,10);min_score=st.sidebar.slider('最低黑嚕嚕分數',0,100,50,5);min_vr=st.sidebar.slider('最低量比',0.5,5.0,1.0,0.1)
change_range=st.sidebar.slider('漲跌幅範圍 (%)',-10.0,10.0,(-10.0,10.0),0.5);rsi_range=st.sidebar.slider('RSI 範圍',0,100,(0,100),1)
sort_mode=st.sidebar.selectbox('排行榜排序',['黑嚕嚕分數','漲跌幅','量比','RSI','價格']);auto=st.sidebar.checkbox('自動刷新',value=False);refresh=st.sidebar.select_slider('刷新秒數',options=[30,60,120,180,300],value=120)
universe_codes=UNIVERSE['股票代號'].tolist() if not UNIVERSE.empty else DEFAULT_STOCKS.split(',')
stock_text=st.sidebar.text_area('股票池（官方全市場自動同步，可自行縮小）',','.join(universe_codes),height=180)
if auto:st_autorefresh(interval=refresh*1000,key='black_radar_refresh')

raw=stock_text.replace('\n',',').replace(' ',',').replace('，',',').replace('、',',').split(',');symbols=[]
for s in raw:
    s=s.strip()
    if s.isdigit() and s.zfill(4) not in symbols:symbols.append(s.zfill(4))
# 依市場選擇與股票池順序過濾
market_map=dict(zip(UNIVERSE['股票代號'],UNIVERSE['市場'])) if not UNIVERSE.empty else {}
if not STOCK_LIST.empty and '股票代號' in STOCK_LIST.columns and '市場' in STOCK_LIST.columns:
    market_map.update(dict(zip(STOCK_LIST['股票代號'].astype(str).str.zfill(4),STOCK_LIST['市場'])))
symbols=[x for x in symbols if not markets or market_map.get(x,'未分類') in markets]
symbols=symbols[:max_n]

st.title('🖤 黑嚕嚕－台股盤中雷達');st.caption('V3.3｜股票池自動同步 TWSE／TPEx 上市、上櫃、興櫃；行情仍為 yfinance 日資料。')
a,b,c,d,e=st.columns(5);a.metric('股票池',f'{len(symbols)} 檔');b.metric('最低分數',min_score);c.metric('最低量比',f'{min_vr:.1f}x');d.metric('市場','＋'.join(markets) if markets else '未選');e.metric('更新時間',datetime.now().strftime('%H:%M:%S'));st.divider()

rows=[];p=st.progress(0);status=st.empty()
for i,s in enumerate(symbols):
    status.text(f'正在掃描：{s} {stock_name(s)}　({i+1}/{len(symbols)})');df=get_stock_data(s, market_map.get(s))
    if df is None: p.progress((i+1)/max(len(symbols),1));continue
    r=build_row(s,df)
    if r:
        sig=r['訊號'];market_ok=(not markets or r['市場'] in markets or r['市場']=='未分類');change_ok=change_range[0]<=r['漲跌%']<=change_range[1];rsi_ok=pd.isna(r['RSI']) or rsi_range[0]<=r['RSI']<=rsi_range[1];base=r['黑嚕嚕分數']>=min_score and r['量比']>=min_vr and change_ok and rsi_ok and market_ok
        mode_ok={'全部股票':True,'🟣 黑嚕嚕超強':r['黑嚕嚕分數']>=90,'🔥 強勢股':r['黑嚕嚕分數']>=80 and r['漲跌%']>0,'🚀 強勢突破':'🚀 強勢突破' in sig,'🔥 主升段':'🔥 主升段' in sig,'🟢 守護生命線':'🟢 守護生命線' in sig,'⚠️ 大量換手高危':'⚠️ 爆量高危' in sig,'🔴 趨勢轉弱':'🔴 趨勢轉弱' in sig}.get(mode,True)
        if base and mode_ok:rows.append(r)
    p.progress((i+1)/max(len(symbols),1))
status.empty();p.empty()
if not rows:st.warning('目前沒有符合條件的股票。可以降低最低黑嚕嚕分數、量比、RSI／漲跌幅，或增加股票池。');st.stop()
result=pd.DataFrame(rows);sort_col={'黑嚕嚕分數':'黑嚕嚕分數','漲跌幅':'漲跌%','量比':'量比','RSI':'RSI','價格':'價格'}[sort_mode];result=result.sort_values(sort_col,ascending=False,na_position='last').reset_index(drop=True)

strong=int((result['黑嚕嚕分數']>=80).sum());breakout=int(result['訊號'].str.contains('🚀 強勢突破',regex=False).sum());risk=int(result['訊號'].str.contains('⚠️ 爆量高危',regex=False).sum());weak=int(result['訊號'].str.contains('🔴 趨勢轉弱',regex=False).sum())
a,b,c,d,e=st.columns(5);a.metric('符合條件',f'{len(result)} 檔');b.metric('🔥 80分以上',f'{strong} 檔');c.metric('🚀 突破',f'{breakout} 檔');d.metric('⚠️ 高危',f'{risk} 檔');e.metric('🔴 轉弱',f'{weak} 檔');st.divider()

st.subheader('🖤 黑嚕嚕焦點');top=result.head(4);cols=st.columns(len(top))
for col,(_,r) in zip(cols,top.iterrows()):
    icon='🟢' if r['漲跌%']>0 else '🔴' if r['漲跌%']<0 else '⚪'
    with col:st.markdown(f'''<div class="radar-card"><div class="radar-title">{icon} {r['股票']} {r['名稱']}</div><div class="small">{r['市場']}</div><div class="radar-price">{r['價格']:.2f}</div><div>{r['漲跌%']:+.2f}%　量比 {r['量比']:.2f}x　RSI {r['RSI']:.1f}</div><div class="radar-score">🖤 {r['黑嚕嚕分數']} / 100</div><div>{r['等級']}</div><div class="signal">{r['訊號']}</div></div>''',unsafe_allow_html=True)

t1,t2,t3,t4,t5,t6=st.tabs(['📋 黑嚕嚕排行榜','🚨 訊號中心','📊 分數拆解','📈 個股分析','⭐ 自選股','🧪 V3.2 訊號回測'])
with t1:
    show=result[['股票','名稱','市場','價格','漲跌%','量比','成交量','RSI','黑嚕嚕分數','等級','訊號']].copy();show['價格']=show['價格'].map(lambda x:f'{x:.2f}');show['漲跌%']=show['漲跌%'].map(lambda x:f'{x:+.2f}%');show['量比']=show['量比'].map(lambda x:f'{x:.2f}x');show['成交量']=show['成交量'].map(lambda x:f'{x:,.0f}');show['RSI']=show['RSI'].map(lambda x:f'{x:.1f}' if pd.notna(x) else '-')
    st.dataframe(show,use_container_width=True,hide_index=True,column_config={'黑嚕嚕分數':st.column_config.ProgressColumn('🖤 黑嚕嚕分數',min_value=0,max_value=100,format='%d')})
with t2:
    st.subheader('🚨 黑嚕嚕訊號中心')
    for title,key in [('🚀 強勢突破','🚀 強勢突破'),('🔥 主升段','🔥 主升段'),('🟢 守護生命線','🟢 守護生命線'),('⚠️ 爆量高危','⚠️ 爆量高危'),('🔴 趨勢轉弱','🔴 趨勢轉弱')]:
        g=result[result['訊號'].str.contains(key,regex=False)].copy();st.markdown(f'### {title}　{len(g)} 檔')
        if g.empty:st.info('目前沒有符合這個訊號的股票。')
        else:
            q=g[['股票','名稱','價格','漲跌%','量比','RSI','黑嚕嚕分數','判斷']].copy();q['價格']=q['價格'].map(lambda x:f'{x:.2f}');q['漲跌%']=q['漲跌%'].map(lambda x:f'{x:+.2f}%');q['量比']=q['量比'].map(lambda x:f'{x:.2f}x');q['RSI']=q['RSI'].map(lambda x:f'{x:.1f}' if pd.notna(x) else '-');st.dataframe(q,use_container_width=True,hide_index=True)
with t3:
    st.subheader('📊 黑嚕嚕分數拆解');s=st.selectbox('選擇股票',result['股票'].tolist(),key='score_stock');r=result[result['股票']==s].iloc[0]
    st.markdown(f"### 🖤 {r['股票']} {r['名稱']}　{r['黑嚕嚕分數']} / 100　{r['等級']}")
    q=pd.DataFrame({'項目':['📈 趨勢','⚡ 動能','🔊 成交量','🚀 突破','RSI','⭐ 額外強度'],'得分':[r['趨勢分'],r['動能分'],r['量能分'],r['突破分'],r['RSI分'],r['額外分']],'滿分':[30,20,20,15,10,5]});st.dataframe(q,use_container_width=True,hide_index=True)
    c1,c2=st.columns(2);c1.metric('總分',f"{r['黑嚕嚕分數']} / 100");c1.metric('量比',f"{r['量比']:.2f}x");c1.metric('RSI',f"{r['RSI']:.1f}" if pd.notna(r['RSI']) else '-');c2.metric('20日高點',f"{r['20日高']:.2f}" if pd.notna(r['20日高']) else '-');c2.metric('MA20',f"{r['MA20']:.2f}" if pd.notna(r['MA20']) else '-');c2.metric('MA200',f"{r['MA200']:.2f}" if pd.notna(r['MA200']) else '-')
    st.markdown('**目前主要判斷**');[st.write(f'• {x}') for x in r['判斷'].split('、') if x]
with t4:
    st.subheader('📈 個股分析');s=st.selectbox('選擇分析股票',result['股票'].tolist(),key='chart_stock');r=result[result['股票']==s].iloc[0];d=r['_df'].tail(120).copy();st.markdown(f"### {r['股票']} {r['名稱']}　{r['價格']:.2f}　{r['漲跌%']:+.2f}%")
    st.line_chart(d[['Close','MA20','MA60','MA200']].rename(columns={'Close':'股價'}),height=420);a,b,c,d2,e=st.columns(5);a.metric('黑嚕嚕',f"{r['黑嚕嚕分數']}分");b.metric('量比',f"{r['量比']:.2f}x");c.metric('RSI',f"{r['RSI']:.1f}" if pd.notna(r['RSI']) else '-');d2.metric('MA20',f"{r['MA20']:.2f}");e.metric('MA200',f"{r['MA200']:.2f}");st.markdown('#### 🔊 成交量');st.line_chart(d[['Volume','VOL_MA20']].rename(columns={'Volume':'成交量','VOL_MA20':'20日均量'}),height=250);st.markdown('#### 🚨 目前訊號');st.info(r['訊號']);st.markdown('#### 🧠 黑嚕嚕判讀');st.write(r['判斷'] or '目前沒有額外判讀。')
with t5:
    st.subheader('⭐ 自選股');watch=st.multiselect('加入自選股',result['股票'].tolist(),default=[],key='watchlist')
    if not watch:st.info('請從上方選擇股票加入自選股。')
    else:
        q=result[result['股票'].isin(watch)].sort_values('黑嚕嚕分數',ascending=False)[['股票','名稱','價格','漲跌%','量比','RSI','黑嚕嚕分數','等級','訊號']].copy();q['價格']=q['價格'].map(lambda x:f'{x:.2f}');q['漲跌%']=q['漲跌%'].map(lambda x:f'{x:+.2f}%');q['量比']=q['量比'].map(lambda x:f'{x:.2f}x');q['RSI']=q['RSI'].map(lambda x:f'{x:.1f}' if pd.notna(x) else '-');st.dataframe(q,use_container_width=True,hide_index=True)


with t6:
    st.subheader('🧪 V3.2 歷史訊號回測')
    st.caption('研究型回測：訊號日收盤進場，持有 N 個交易日後以收盤價出場。未納入手續費、交易稅、滑價、漲跌停與流動性。')
    c1,c2,c3=st.columns(3)
    with c1:
        bt_horizon=st.selectbox('持有交易日',[1,3,5,10,20],index=2)
    with c2:
        bt_gap=st.selectbox('同訊號冷卻天數',[0,3,5,10,20],index=1)
    with c3:
        bt_min_score=st.slider('回測最低黑嚕嚕分數',0,100,50,5)
    bt_signals=st.multiselect('回測訊號',SIGNAL_LABELS,default=['🚀 強勢突破','🔥 主升段','🟢 守護生命線'])
    bt_n=st.slider('回測股票數量',1,min(50,len(symbols)),min(20,len(symbols)),1)
    if st.button('▶ 開始 V3.2 回測',type='primary'):
        if not bt_signals:
            st.warning('請至少選擇一個訊號。')
        else:
            all_events=[]; bt_progress=st.progress(0); bt_status=st.empty()
            bt_symbols=symbols[:bt_n]
            for i,sym in enumerate(bt_symbols):
                bt_status.text(f'正在回測：{sym} {stock_name(sym)}　({i+1}/{len(bt_symbols)})')
                bdf=get_stock_data(sym, market_map.get(sym))
                if bdf is not None:
                    ev=run_backtest(sym,bdf,bt_horizon,bt_gap,bt_signals,bt_min_score)
                    if not ev.empty: all_events.append(ev)
                bt_progress.progress((i+1)/max(len(bt_symbols),1))
            bt_status.empty(); bt_progress.empty()
            bt_result=pd.concat(all_events,ignore_index=True) if all_events else pd.DataFrame()
            st.session_state['v32_bt_result']=bt_result
            st.session_state['v32_bt_horizon']=bt_horizon
    bt_result=st.session_state.get('v32_bt_result',pd.DataFrame())
    if not bt_result.empty:
        stats=overall_backtest_stats(bt_result)
        c1,c2,c3,c4,c5=st.columns(5)
        c1.metric('樣本數',stats['樣本數'])
        c2.metric('勝率',f"{stats['勝率%']:.1f}%")
        c3.metric('平均報酬',f"{stats['平均報酬%']:+.2f}%")
        c4.metric('中位數',f"{stats['中位數報酬%']:+.2f}%")
        c5.metric('報酬加總',f"{stats['報酬加總%']:+.2f}%")
        st.markdown('### 📊 各訊號績效')
        summary=backtest_summary(bt_result)
        st.dataframe(summary.style.format({
            '平均報酬%':'{:+.2f}%','中位數報酬%':'{:+.2f}%','報酬加總%':'{:+.2f}%','勝率%':'{:.1f}%'
        }),use_container_width=True,hide_index=True)
        st.markdown('### 📈 平均報酬')
        st.bar_chart(summary.set_index('訊號')['平均報酬%'])
        st.markdown('### 🎯 勝率')
        st.bar_chart(summary.set_index('訊號')['勝率%'])
        st.markdown('### 🧾 歷史回測明細')
        detail=bt_result.copy()
        for col in ['進場價','出場價','報酬%','MFE%','MAE%','量比','RSI']:
            if col in detail.columns: detail[col]=detail[col].round(2)
        st.dataframe(detail,use_container_width=True,hide_index=True)
        st.download_button('⬇️ 匯出 V3.2 回測 CSV',bt_result.to_csv(index=False).encode('utf-8-sig'),'v3.2_backtest.csv','text/csv')
    else:
        st.info('尚未完成回測。請設定條件後按「▶ 開始 V3.2 回測」。')

st.divider();st.caption('🖤 黑嚕嚕 V3.3｜股票池自動同步上市／上櫃／興櫃；技術資料使用 yfinance 日資料。V4 再接 Fugle 即時行情。');st.caption('⚠️ 本工具僅供研究與技術分析，不構成投資建議。')
