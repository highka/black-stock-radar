import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import requests
import re
from datetime import datetime
from streamlit_autorefresh import st_autorefresh

# ============================================================
# 🖤 黑嚕嚕－台股盤中雷達 V3.3.2 A2.3.2
# V3.3.2：智能掃描 2.0；A2.3：Strategy Lab 八策略PK＋門檻／持有期矩陣＋官方 API 穩定層，V4 再接 Fugle 即時行情
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

# ============================================================
# 🌐 API 穩定層
# Streamlit Cloud 偶爾會對 TWSE/TPEx 官方 OpenAPI 發生 SSL
# 驗證錯誤。這裡採「正常 SSL 優先、SSL fallback、短重試」，
# 並把實際錯誤摘要顯示給 UI，避免只看到「失敗」無法診斷。
# ============================================================
def fetch_json_api(url, timeout=20):
    headers = {
        'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                     'AppleWebKit/537.36 (KHTML, like Gecko) '
                     'Chrome/140.0 Safari/537.36',
        'Accept':'application/json,text/plain,*/*',
        'Referer':'https://www.tpex.org.tw/' if 'tpex.org.tw' in url else 'https://www.twse.com.tw/'
    }
    last_error=None

    # 第一層：正常 SSL
    for attempt in range(2):
        try:
            r=requests.get(url,timeout=timeout,headers=headers)
            r.raise_for_status()
            return r.json(), 'SSL正常'
        except Exception as e:
            last_error=e

    # 第二層：部分雲端環境對 TPEx 憑證鏈驗證異常時，
    # 允許以 verify=False 作為 fallback。資料仍來自官方 HTTPS API。
    try:
        r=requests.get(url,timeout=timeout,headers=headers,verify=False)
        r.raise_for_status()
        return r.json(), 'SSL fallback'
    except Exception as e:
        last_error=e

    # 第三層：增加 query cache-buster，避免部分 proxy/cache 異常
    try:
        sep='&' if '?' in url else '?'
        r=requests.get(url+sep+'_ts=1',timeout=timeout,headers=headers,verify=False)
        r.raise_for_status()
        return r.json(), 'SSL fallback+retry'
    except Exception as e:
        last_error=e

    raise last_error

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
            data, api_mode = fetch_json_api(url, timeout=20)
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
            source_status.append(f'{market}：成功（{api_mode}）')
        except Exception as e:
            msg=str(e).replace(chr(10),' ')[:100]
            source_status.append(f'{market}：失敗（{type(e).__name__}） {msg}')
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
def load_market_snapshot():
    """取得官方當日行情，先做全市場第一層篩選，避免逐檔下載 2 年 yfinance。"""
    sources = [
        ('上市', 'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL'),
        ('上櫃', 'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes'),
        ('興櫃', 'https://www.tpex.org.tw/openapi/v1/tpex_esb_latest_statistics'),
    ]
    all_rows=[]; status=[]
    def pick(item, keys):
        for k in keys:
            if k in item and item[k] not in (None,''):
                return item[k]
        return ''
    def num(v):
        try:
            return float(str(v).replace(',','').replace('%','').strip())
        except Exception:
            return np.nan
    for market,url in sources:
        try:
            data, api_mode = fetch_json_api(url, timeout=20)
            if isinstance(data,dict): data=data.get('data',data.get('results',data.get('aaData',[])))
            if not isinstance(data,list): raise ValueError('API 回傳格式不是清單')
            count=0
            for item in data:
                if not isinstance(item,dict): continue
                code=str(pick(item,['Code','SecuritiesCompanyCode','公司代號','股票代號','證券代號'])).strip().upper()
                name=str(pick(item,['Name','CompanyAbbreviation','公司簡稱','股票名稱','證券名稱'])).strip()
                close=num(pick(item,['ClosingPrice','Close','收盤價','成交價','最後成交價','成交價格']))
                change=num(pick(item,['Change','change','漲跌','漲跌價差','漲跌幅']))
                volume=num(pick(item,['TradeVolume','Volume','成交股數','成交量']))
                value=num(pick(item,['TradeValue','成交金額','成交值']))
                high=num(pick(item,['HighestPrice','High','最高價']))
                low=num(pick(item,['LowestPrice','Low','最低價']))
                if not re.fullmatch(r'[0-9A-Z]{4,6}',code) or not np.isfinite(close) or close<=0:
                    continue
                all_rows.append({'股票代號':code,'股票名稱':name,'市場':market,'收盤價':close,'漲跌':change,'成交量':volume,'成交額':value,'最高價':high,'最低價':low})
                count+=1
            status.append(f'{market}：{count} 檔（{api_mode}）')
        except Exception as e:
            msg=str(e).replace(chr(10),' ')[:100]
            status.append(f'{market}：失敗（{type(e).__name__}） {msg}')
    d=pd.DataFrame(all_rows)
    if d.empty:
        return pd.DataFrame(columns=['股票代號','股票名稱','市場','收盤價','漲跌','成交量','成交額','最高價','最低價']), status
    d=d.drop_duplicates(['股票代號','市場'])
    d['成交額']=d['成交額'].fillna(0); d['成交量']=d['成交量'].fillna(0)
    d['漲跌幅%']=np.where(d['收盤價']>0,d['漲跌']/d['收盤價']*100,np.nan)
    return d,status


def smart_rank_universe(universe, snapshot, selected_markets, top_n, min_volume, min_value, min_change):
    """V3.3.2 智能初篩：先過流動性門檻，再以分市場配額避免上市市場完全吃掉候選名額。"""
    base=universe[universe['市場'].isin(selected_markets)].copy()
    if base.empty:
        return base, '沒有可掃描的市場股票池。'
    if snapshot is None or snapshot.empty:
        return base.head(top_n).copy(), '官方行情初篩無資料，退回完整股票池順序。'
    d=snapshot[snapshot['市場'].isin(selected_markets)].copy()
    if d.empty:
        return base.head(top_n).copy(), '選定市場沒有官方行情資料，退回完整股票池順序。'
    d=d[d['成交量'].fillna(0)>=min_volume]
    d=d[d['成交額'].fillna(0)>=min_value]
    d=d[d['漲跌幅%'].fillna(0)>=min_change]
    if d.empty:
        return base.head(top_n).copy(), '條件過嚴，已退回完整股票池順序。'

    # V3.3.2：把各市場分開正規化，避免上市股票因總市值／成交額天然較大而壟斷候選池。
    parts=[]
    for market,g in d.groupby('市場',sort=False):
        g=g.copy()
        turnover=np.log1p(g['成交額'].clip(lower=0))
        if turnover.max()>turnover.min():
            g['量能分']=((turnover-turnover.min())/(turnover.max()-turnover.min())*45)
        else:
            g['量能分']=22.5
        g['動能分']=(g['漲跌幅%'].clip(-10,10).add(10)/20*30)
        g['波動分']=((g['最高價']-g['最低價'])/g['收盤價'].replace(0,np.nan)).fillna(0).clip(0,0.2)/0.2*10
        g['活躍分']=(g['成交量']>0).astype(int)*15
        g['智能初篩分']=(g['量能分']+g['動能分']+g['波動分']+g['活躍分']).round(2)
        parts.append(g.sort_values(['智能初篩分','成交額','成交量'],ascending=False))
    d=pd.concat(parts,ignore_index=True) if parts else pd.DataFrame()
    if d.empty:
        return base.head(top_n).copy(), '智能初篩無結果，退回完整股票池順序。'

    # 每個市場至少分到一小部分名額；剩餘名額再依智能初篩分補足。
    markets=[m for m in selected_markets if m in set(d['市場'])]
    quotas={m:max(1,int(round(top_n*len(d[d['市場']==m])/max(len(d),1)))) for m in markets}
    while sum(quotas.values())>top_n:
        candidates=[m for m in markets if quotas[m]>1]
        if not candidates: break
        m=min(candidates,key=lambda x:quotas[x]);quotas[m]-=1
    while sum(quotas.values())<min(top_n,len(d)):
        candidates=[m for m in markets if quotas[m]<len(d[d['市場']==m])]
        if not candidates: break
        m=max(candidates,key=lambda x:float(d.loc[d['市場']==x,'智能初篩分'].max()));quotas[m]+=1
    picked=[]
    for m in markets:
        picked.append(d[d['市場']==m].head(quotas.get(m,0)))
    picked=pd.concat(picked,ignore_index=True) if picked else d.head(top_n)
    if len(picked)<top_n:
        remain=d[~d['股票代號'].isin(picked['股票代號'])].sort_values(['智能初篩分','成交額'],ascending=False)
        picked=pd.concat([picked,remain.head(top_n-len(picked))],ignore_index=True)
    picked=picked.sort_values(['智能初篩分','成交額'],ascending=False).head(top_n)
    out=base.merge(picked[['股票代號','市場','智能初篩分','成交額','成交量','漲跌幅%']],on=['股票代號','市場'],how='inner')
    counts='、'.join([f"{m} {len(out[out['市場']==m])}檔" for m in markets])
    return out.sort_values('智能初篩分',ascending=False), f'官方行情初篩後保留 {len(out)} 檔（{counts}）。'


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
    d['MA15']=d.Close.rolling(15).mean();d['MA60']=d.Close.rolling(60).mean();d['MA200']=d.Close.rolling(200).mean()
    ll=d.Low.rolling(9).min();hh=d.High.rolling(9).max()
    d['RSV']=((d.Close-ll)/(hh-ll).replace(0,np.nan)*100).clip(0,100)
    d['K']=d['RSV'].ewm(alpha=1/3,adjust=False,min_periods=1).mean()
    d['D']=d['K'].ewm(alpha=1/3,adjust=False,min_periods=1).mean()
    d['VOL_MA20']=d.Volume.rolling(20).mean();d['VOL_RATIO']=d.Volume/d.VOL_MA20.replace(0,np.nan)
    d['CHANGE']=d.Close.pct_change()*100;d['HIGH20']=d.High.shift(1).rolling(20).max();d['HIGH60']=d.High.shift(1).rolling(60).max()
    up=d.Volume>d.Volume.shift(1);cnt=[];n=0
    for f in up.fillna(False):n=n+1 if f else 0;cnt.append(n)
    d['CONSEC_VOL']=cnt;d['MA15_SLOPE']=d.MA15-d.MA15.shift(5);d['MA60_SLOPE']=d.MA60-d.MA60.shift(5)
    return d

def score_level(s):
    return '🟣 黑嚕嚕超強' if s>=90 else '🔥 強勢' if s>=80 else '🚀 注意' if s>=70 else '👀 觀察' if s>=60 else '⚪ 一般'

def black_score(d):
    x=d.iloc[-1];close=x.Close;ma15=x.MA15;ma60=x.MA60;ma200=x.MA200;k=x.K;dd=x.D;vr=x.VOL_RATIO;chg=x.CHANGE;h20=x.HIGH20;h60=x.HIGH60
    trend=momentum=volume=breakout=rsi_s=extra=0;reasons=[]
    if pd.notna(ma15) and close>ma15:trend+=8;reasons.append('站上MA15')
    if pd.notna(ma60) and close>ma60:trend+=7;reasons.append('站上MA60')
    if pd.notna(ma200) and close>ma200:trend+=5;reasons.append('站上MA200')
    if pd.notna(ma15) and pd.notna(ma60) and ma15>ma60:trend+=5;reasons.append('MA15>MA60')
    if pd.notna(ma15) and pd.notna(ma60) and pd.notna(ma200) and ma15>ma60>ma200:trend+=5;reasons.append('多頭排列')
    if pd.notna(chg):
        if chg>=5:momentum+=8;reasons.append('強勢上漲')
        elif chg>=3:momentum+=6;reasons.append('明顯上漲')
        elif chg>=1:momentum+=4;reasons.append('今日偏強')
        elif chg>0:momentum+=2
    if pd.notna(x.MA15_SLOPE) and x.MA15_SLOPE>0:momentum+=5;reasons.append('MA15上彎')
    if pd.notna(x.MA60_SLOPE) and x.MA60_SLOPE>0:momentum+=4;reasons.append('MA60上彎')
    if pd.notna(ma15) and close>ma15*1.03:momentum+=3;reasons.append('脫離生命線')
    if pd.notna(vr):
        if vr>=3:volume+=20;reasons.append('3倍以上爆量')
        elif vr>=2:volume+=15;reasons.append('2倍以上放量')
        elif vr>=1.5:volume+=10;reasons.append('明顯量增')
        elif vr>=1.2:volume+=6;reasons.append('量能增加')
        elif vr>=1:volume+=3
    if pd.notna(h20) and close>=h20:breakout+=10;reasons.append('突破20日高點')
    elif pd.notna(h20) and close>=h20*.98:breakout+=6;reasons.append('接近20日高點')
    if pd.notna(h60) and close>=h60:breakout+=5;reasons.append('突破60日高點')
    if pd.notna(k) and pd.notna(dd):
        if 50<=k<=75 and k>=dd:rsi_s+=10;reasons.append('KD多方健康')
        elif 40<=k<50 and k>=dd:rsi_s+=8;reasons.append('KD轉強')
        elif 75<k<=85 and k>=dd:rsi_s+=7;reasons.append('KD強勢')
        elif k>85 and k>=dd:rsi_s+=3;reasons.append('KD高檔偏熱')
        elif k<20 and k>=dd:rsi_s+=5;reasons.append('KD低檔轉強')
        elif k>=50 and k<dd:rsi_s+=4;reasons.append('KD高檔死叉')
        elif k<50 and k<dd:rsi_s+=2
        if k-dd>=5:rsi_s=min(10,rsi_s+1);reasons.append('KD多方差擴大')
    if int(x.CONSEC_VOL)>=3:extra+=2;reasons.append('連續3日量增')
    elif int(x.CONSEC_VOL)>=2:extra+=1
    if pd.notna(ma15) and pd.notna(ma60) and close>ma15>ma60 and pd.notna(chg) and chg>0:extra+=2
    if pd.notna(vr) and pd.notna(chg) and vr>=1.5 and chg>2:extra+=1
    total=int(min(100,max(0,trend+momentum+volume+breakout+rsi_s+extra)))
    return total,{'趨勢':trend,'動能':momentum,'成交量':volume,'突破':breakout,'KD':rsi_s,'額外強度':extra},reasons

def signals(d,score):
    x=d.iloc[-1];close=x.Close;ma15=x.MA15;ma60=x.MA60;ma200=x.MA200;k=x.K;dd=x.D;vr=x.VOL_RATIO;chg=x.CHANGE;h20=x.HIGH20;s=[]
    if pd.notna(h20) and close>=h20 and pd.notna(vr) and vr>=1.3 and pd.notna(chg) and chg>0:s.append('🚀 強勢突破')
    if pd.notna(ma15) and pd.notna(ma60) and pd.notna(ma200) and close>ma15>ma60>ma200 and pd.notna(vr) and vr>=1.1 and pd.notna(k) and pd.notna(dd) and 50<=k<=85 and k>=dd:s.append('🔥 主升段')
    if pd.notna(ma15) and pd.notna(ma200) and close>=ma15*.98 and close<=ma15*1.03 and close>=ma200 and pd.notna(k) and pd.notna(dd) and k>=45 and k>=dd:s.append('🟢 守護生命線')
    if pd.notna(vr) and vr>=2 and pd.notna(chg) and chg>=3 and ((pd.notna(k) and pd.notna(dd) and k>=85 and k>=dd) or vr>=3):s.append('⚠️ 爆量高危')
    weak=(pd.notna(ma15) and close<ma15) or (pd.notna(ma60) and pd.notna(ma200) and ma60<ma200) or (pd.notna(chg) and chg<=-3 and pd.notna(vr) and vr>=1.3)
    if weak and score<60:s.append('🔴 趨勢轉弱')
    if not s:s.append(score_level(score))
    return s

def build_row(symbol,df):
    if df is None or len(df)<200:return None
    d=indicators(df);x=d.iloc[-1];score,bd,reasons=black_score(d);sig=signals(d,score)
    return {'股票':str(symbol).zfill(4),'名稱':stock_name(symbol),'市場':stock_market(symbol),'價格':float(x.Close),'漲跌%':float(x.CHANGE) if pd.notna(x.CHANGE) else 0.0,'成交量':float(x.Volume),'量比':float(x.VOL_RATIO) if pd.notna(x.VOL_RATIO) else 0.0,'K':float(x.K) if pd.notna(x.K) else np.nan,'D':float(x.D) if pd.notna(x.D) else np.nan,'MA15':float(x.MA15) if pd.notna(x.MA15) else np.nan,'MA60':float(x.MA60) if pd.notna(x.MA60) else np.nan,'MA200':float(x.MA200) if pd.notna(x.MA200) else np.nan,'連量':int(x.CONSEC_VOL),'20日高':float(x.HIGH20) if pd.notna(x.HIGH20) else np.nan,'60日高':float(x.HIGH60) if pd.notna(x.HIGH60) else np.nan,'黑嚕嚕分數':score,'等級':score_level(score),'訊號':'、'.join(sig),'判斷':'、'.join(dict.fromkeys(reasons[:8])),'趨勢分':bd['趨勢'],'動能分':bd['動能'],'量能分':bd['成交量'],'突破分':bd['突破'],'KD分':bd['KD'],'額外分':bd['額外強度'],'_df':d}


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
        ma15, ma60, ma200 = x.MA15, x.MA60, x.MA200
        close, k, dd, vr, chg = x.Close, x.K, x.D, x.VOL_RATIO, x.CHANGE
        h20, h60 = x.HIGH20, x.HIGH60
        sigs = []
        if pd.notna(h20) and close >= h20 and pd.notna(vr) and vr >= 1.3 and pd.notna(chg) and chg > 0:
            sigs.append('🚀 強勢突破')
        if pd.notna(h60) and close >= h60 and pd.notna(vr) and vr >= 1.1 and pd.notna(chg) and chg > 0:
            sigs.append('🚀 60日突破')
        if pd.notna(h20) and close >= h20*.98 and close < h20:
            sigs.append('👀 接近突破')
        if pd.notna(ma15) and pd.notna(ma60) and pd.notna(ma200) and close > ma15 > ma60 > ma200 and pd.notna(vr) and vr >= 1.1 and pd.notna(k) and pd.notna(dd) and 50 <= k <= 85 and k >= dd:
            sigs.append('🔥 主升段')
        if pd.notna(ma15) and pd.notna(ma200) and close >= ma15*.98 and close <= ma15*1.03 and close >= ma200 and pd.notna(k) and pd.notna(dd) and k >= 45 and k >= dd:
            sigs.append('🟢 守護生命線')
        if pd.notna(vr) and vr >= 2 and pd.notna(chg) and chg >= 3 and ((pd.notna(k) and pd.notna(dd) and k >= 85 and k >= dd) or vr >= 3):
            sigs.append('⚠️ 爆量高危')
        if pd.notna(vr) and vr >= 2 and pd.notna(chg) and chg > 0 and pd.notna(h20) and close >= h20:
            sigs.append('⚠️ 爆量突破')
        if pd.notna(vr) and vr >= 2 and pd.notna(k) and pd.notna(dd) and k >= 85 and k >= dd:
            sigs.append('⚠️ 爆量過熱')
        if pd.notna(vr) and vr >= 1.5 and pd.notna(chg) and chg <= -3:
            sigs.append('⚠️ 爆量下跌')
        weak = ((pd.notna(ma15) and close < ma15) or
                (pd.notna(ma60) and pd.notna(ma200) and ma60 < ma200) or
                (pd.notna(chg) and chg <= -3 and pd.notna(vr) and vr >= 1.3))
        if weak and score < 60:
            sigs.append('🔴 趨勢轉弱')
        for sig in sigs:
            events.append({
                '日期': d.index[-1], '訊號': sig, '收盤': float(close),
                '黑嚕嚕分數': int(score), '漲跌%': float(chg) if pd.notna(chg) else np.nan,
                '量比': float(vr) if pd.notna(vr) else np.nan, 'K': float(k) if pd.notna(k) else np.nan, 'D': float(dd) if pd.notna(dd) else np.nan
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
            '黑嚕嚕分數': int(e['黑嚕嚕分數']), '量比': e['量比'], 'K': e['K'], 'D': e['D']
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



# ============================================================
# 🖤 A2：綜合選股分數回測（修正版）
# ============================================================
COMPOSITE_WEIGHTS={'智能流動性':15,'智能動能':10,'智能波動':5,'智能活躍':5,
                   'MA多頭結構':15,'生命線':10,'KD動能':10,'量價確認':10,
                   '突破':10,'策略品質':10}

A2_COMPONENT_DESC={
    '智能流動性':'成交額相對排名（歷史 OHLCV 代理）',
    '智能動能':'當日漲跌＋近5日動能（歷史 OHLCV 代理）',
    '智能波動':'當日振幅與近期波動（歷史 OHLCV 代理）',
    '智能活躍':'成交量相對20日均量（歷史 OHLCV 代理）',
    'MA多頭結構':'MA15 / MA60 / MA200 趨勢結構',
    '生命線':'股價相對 MA200 的位置',
    'KD動能':'9日KD（K/D）多空與位置',
    '量價確認':'量比＋價格方向同步性',
    '突破':'突破前20日高點的程度',
    '策略品質':'歷史訊號條件品質（權重修正為10分，使總分完整100分）'
}

def _clip_score(x, lo=0, hi=100):
    try:return float(np.clip(float(x),lo,hi))
    except Exception:return 0.0

def historical_component_parts(hist):
    """只使用截至當日資料計算 A2；不偷看未來。
    四個「智能」項目是官方盤中智能初篩的 OHLCV 歷史代理，不宣稱等同歷史官方快照。
    """
    x=hist.iloc[-1]
    close=float(x.Close); vol=float(x.Volume) if pd.notna(x.Volume) else 0.0
    ma15=float(x.MA15) if pd.notna(x.MA15) else np.nan
    ma60=float(x.MA60) if pd.notna(x.MA60) else np.nan
    ma200=float(x.MA200) if pd.notna(x.MA200) else np.nan
    k=float(x.K) if pd.notna(x.K) else 50.0; dd=float(x.D) if pd.notna(x.D) else 50.0
    vr=float(x.VOL_RATIO) if pd.notna(x.VOL_RATIO) else 1.0
    chg=float(x.CHANGE) if pd.notna(x.CHANGE) else 0.0

    # --- 1) 歷史智能代理：每個項目獨立正規化到其權重 ---
    turnover=close*vol
    turn20=hist['Close'].mul(hist['Volume']).rolling(20).median().iloc[-1]
    liq=15*_clip_score((turnover/(turn20 if pd.notna(turn20) and turn20>0 else turnover))-0.5,0,2)/2

    ret5=(close/float(hist['Close'].iloc[-6])-1)*100 if len(hist)>=6 and float(hist['Close'].iloc[-6])>0 else 0
    mom=10*(_clip_score(chg,-5,5)+5)/10*0.55 + 10*(_clip_score(ret5,-10,10)+10)/20*0.45

    amp=((float(x.High)-float(x.Low))/close*100) if close>0 and pd.notna(x.High) and pd.notna(x.Low) else 0
    amp20=hist['Close'].pct_change().rolling(20).std().iloc[-1]*100
    vol_score=5*(_clip_score(amp,0,10)/10*0.65 + _clip_score(amp20 if pd.notna(amp20) else 0,0,8)/8*0.35)

    act=5*(_clip_score(vr,0,3)/3)

    # --- 2) 技術項目 ---
    ma=0
    if pd.notna(ma15) and close>ma15: ma+=6
    if pd.notna(ma60) and pd.notna(ma15) and ma15>ma60: ma+=4
    if pd.notna(ma200) and pd.notna(ma60) and ma60>ma200: ma+=3
    if pd.notna(ma15) and pd.notna(hist['MA15_SLOPE'].iloc[-1]) and hist['MA15_SLOPE'].iloc[-1]>0: ma+=2
    ma=min(ma,15)

    life=0
    if pd.notna(ma200):
        dist=(close/ma200-1)*100
        if dist>=0 and dist<=5: life=10
        elif dist>5: life=8
        elif dist>=-3: life=6
        elif dist>=-8: life=3
        else: life=0

    # KD 動能：9日 RSV → K/D(3,3)，滿分10
    kd_score=5.0
    if pd.notna(k) and pd.notna(dd):
        if k>=dd:
            if 45<=k<=75: kd_score=10.0
            elif 30<=k<45: kd_score=8.0
            elif 75<k<=85: kd_score=7.0
            elif k>85: kd_score=3.0
            else: kd_score=6.0
        else:
            if k>=70: kd_score=4.0
            elif k>=50: kd_score=5.0
            elif k>=30: kd_score=3.0
            else: kd_score=2.0
        if k>dd and k-dd>=5: kd_score=min(10.0,kd_score+1.0)
    kd_score=_clip_score(kd_score,0,10)

    q=0
    if vr>=1.2 and chg>0:q=10
    elif vr>=1.0 and chg>0:q=8
    elif vr>=1.2:q=6
    elif chg>0:q=5
    elif vr<0.7:q=2
    else:q=3

    prev20=hist['HIGH20'].iloc[-1] if 'HIGH20' in hist.columns else np.nan
    if pd.notna(prev20) and prev20>0:
        # HIGH20 是含當日 rolling high，因此改用前一日20日高避免同日自我比較
        prior_high=hist['High'].shift(1).rolling(20).max().iloc[-1]
    else: prior_high=np.nan
    if pd.notna(prior_high) and prior_high>0:
        ratio=close/prior_high-1
        br=10 if ratio>=0 else 7 if ratio>=-0.01 else 4 if ratio>=-0.03 else 1
        if close>=prior_high: br=10
    else: br=0

    # 策略品質：依歷史訊號強度給 0~5 分，不使用未來報酬。
    tech_score=(ma/15*30 + life/10*20 + kd_score/10*20 + q/10*15 + br/10*15)
    sig=signals(hist,tech_score)
    strategy=min(10, 2.0*len(sig))
    if any('🚀 強勢突破' in s for s in sig): strategy=10
    elif any('🔥 主升段' in s for s in sig): strategy=max(strategy,8)
    elif any('🟢 守護生命線' in s for s in sig): strategy=max(strategy,6)

    parts={'智能流動性':liq,'智能動能':mom,'智能波動':vol_score,'智能活躍':act,
           'MA多頭結構':ma,'生命線':life,'KD動能':kd_score,'量價確認':q,
           '突破':br,'策略品質':strategy}
    return {k:round(_clip_score(v,0,COMPOSITE_WEIGHTS[k]),2) for k,v in parts.items()}

def composite_score_from_row(r):
    """將即時排行榜 row 轉成 A2 綜合分數；即時頁仍沿用 V3.3.2 原始資料。"""
    parts={k:0.0 for k in COMPOSITE_WEIGHTS}
    parts['MA多頭結構']=min(float(r.get('趨勢分',0))/30*15,15)
    close=r.get('價格',np.nan); ma200=r.get('MA200',np.nan)
    if pd.notna(close) and pd.notna(ma200):
        dist=float(close/ma200-1)
        parts['生命線']=10 if 0<=dist<=.05 else 8 if dist>.05 else 6 if dist>=-.03 else 3 if dist>=-.08 else 0
    k=float(r.get('K',50));dd=float(r.get('D',50))
    if pd.notna(k) and pd.notna(dd):
        kd_score=10 if k>=dd and 45<=k<=75 else 8 if k>=dd and 30<=k<45 else 7 if k>=dd and 75<k<=85 else 3 if k>85 else 5
        if k>dd and k-dd>=5: kd_score=min(10,kd_score+1)
        parts['KD動能']=kd_score
    parts['量價確認']=10 if float(r.get('量比',0))>=1.2 and float(r.get('漲跌%',0))>0 else 8 if float(r.get('量比',0))>=1 and float(r.get('漲跌%',0))>0 else 5 if float(r.get('漲跌%',0))>0 else 3
    parts['突破']=min(float(r.get('突破分',0))/15*10,10)
    smart=float(r.get('智能初篩分',np.nan)) if pd.notna(r.get('智能初篩分',np.nan)) else np.nan
    if pd.notna(smart):
        parts['智能流動性']=_clip_score(smart/100*15,0,15)
        parts['智能動能']=_clip_score((float(r.get('漲跌%',0))+10)/20*10,0,10)
        parts['智能波動']=_clip_score(float(r.get('當日振幅%',0))/20*5,0,5)
        parts['智能活躍']=5 if float(r.get('成交量',0))>0 else 0
    sig=str(r.get('訊號',''))
    parts['策略品質']=10 if '🚀 強勢突破' in sig else 8 if '🔥 主升段' in sig else 6 if '🟢 守護生命線' in sig else min(10,2*(1+sig.count('、'))) if sig else 0
    total=round(sum(parts.values()),2)
    return min(100,total),parts

def add_composite_columns(result):
    if result.empty:return result
    vals=result.apply(composite_score_from_row,axis=1); result=result.copy()
    result['綜合分數']=[x[0] for x in vals]
    for k in COMPOSITE_WEIGHTS: result[k]=[x[1][k] for x in vals]
    result['綜合等級']=result['綜合分數'].map(lambda s:'S' if s>=90 else 'A' if s>=80 else 'B' if s>=70 else 'C' if s>=60 else 'D')
    return result

def historical_composite_events(df, weights=None):
    if df is None or len(df)<210:return pd.DataFrame()
    weights=weights or COMPOSITE_WEIGHTS
    events=[]
    # 指標一次計算，避免 A2.1 對多檔股票時重複計算 1,000+ 次 rolling。
    full=indicators(df.copy())
    for i in range(200,len(full)):
        hist=full.iloc[:i+1]
        x=hist.iloc[-1]; parts=historical_component_parts(hist)
        score=round(sum(parts[k] for k in weights),2)
        tech_score,bd,_=black_score(hist)
        sig='、'.join(signals(hist,tech_score))
        events.append({'日期':hist.index[-1],'綜合分數':score,'黑嚕嚕技術分數':tech_score,
                       **parts,'收盤':float(x.Close),'漲跌%':float(x.CHANGE) if pd.notna(x.CHANGE) else 0,
                       '量比':float(x.VOL_RATIO) if pd.notna(x.VOL_RATIO) else 0,
                       'MA15':float(x.MA15) if pd.notna(x.MA15) else np.nan,
                       'MA60':float(x.MA60) if pd.notna(x.MA60) else np.nan,
                       'MA200':float(x.MA200) if pd.notna(x.MA200) else np.nan,
                       'K':float(x.K) if pd.notna(x.K) else np.nan,'D':float(x.D) if pd.notna(x.D) else np.nan,'訊號':sig})
    return pd.DataFrame(events)

def run_composite_backtest(symbol,df,horizon,min_gap,min_score,weights=None):
    ev=historical_composite_events(df,weights)
    if ev.empty:return pd.DataFrame()
    ev=ev[ev['綜合分數']>=min_score].sort_values('日期')
    close=pd.to_numeric(df['Close'],errors='coerce'); highs=pd.to_numeric(df['High'],errors='coerce'); lows=pd.to_numeric(df['Low'],errors='coerce')
    idx=list(close.index); pos={pd.Timestamp(x):i for i,x in enumerate(idx)}; rows=[]; last=-10**9
    for _,e in ev.iterrows():
        i=pos.get(pd.Timestamp(e['日期']))
        if i is None or i+horizon>=len(idx) or i-last<min_gap:continue
        entry=float(close.iloc[i]); exit_price=float(close.iloc[i+horizon])
        fh=highs.iloc[i+1:i+horizon+1]; fl=lows.iloc[i+1:i+horizon+1]
        rows.append({'股票':str(symbol).zfill(4),'名稱':stock_name(symbol),'訊號日期':pd.Timestamp(e['日期']),'出場日期':idx[i+horizon],
                     '持有天數':horizon,'進場價':entry,'出場價':exit_price,'報酬%':(exit_price/entry-1)*100,
                     'MFE%':(float(fh.max())/entry-1)*100 if len(fh) else 0,
                     'MAE%':(float(fl.min())/entry-1)*100 if len(fl) else 0,
                     '綜合分數':e['綜合分數'],'黑嚕嚕技術分數':e['黑嚕嚕技術分數']})
        last=i
    return pd.DataFrame(rows)

def summarize_score_buckets(bt):
    if bt is None or bt.empty:return pd.DataFrame()
    def bucket(s):return '90–100' if s>=90 else '80–89' if s>=80 else '70–79' if s>=70 else '60–69' if s>=60 else '0–59'
    x=bt.copy();x['分數區間']=x['綜合分數'].map(bucket)
    g=x.groupby('分數區間')['報酬%']
    out=g.agg(['count','mean','median','sum']).reset_index()
    win=x.groupby('分數區間')['報酬%'].apply(lambda z:(z>0).mean()*100).reset_index(name='勝率%')
    out=out.merge(win,on='分數區間',how='left')
    return out.set_index('分數區間').reindex(['90–100','80–89','70–79','60–69','0–59']).reset_index()

def add_forward_returns(history, horizon):
    h=history.copy().sort_values(['股票','日期']).reset_index(drop=True)
    h['未來報酬%']=np.nan
    for sym,g in h.groupby('股票',sort=False):
        ix=g.index
        h.loc[ix,'未來報酬%']=g['收盤'].shift(-horizon).div(g['收盤']).sub(1).mul(100).values
    return h

def collect_a2_history(symbols, market_map, progress=None):
    all_hist=[]
    n=max(len(symbols),1)
    for i,sym in enumerate(symbols):
        try:
            df=get_stock_data(sym,market_map.get(sym))
            if df is not None:
                ev=historical_composite_events(df)
                if not ev.empty:
                    ev['股票']=str(sym).zfill(4); all_hist.append(ev)
        except Exception:
            # 單一股票資料異常不能拖垮整批 A2 健診
            pass
        if progress is not None:
            if hasattr(progress, 'progress'):
                progress.progress((i+1)/n)
            elif callable(progress):
                progress((i+1)/n)
    return pd.concat(all_hist,ignore_index=True) if all_hist else pd.DataFrame()

def _weight_variants(base):
    """有限、可解釋的 A2.1 候選權重；每組總和固定100。"""
    keys=list(base); variants=[dict(base)]
    for src in keys:
        for dst in keys:
            if src==dst or base[src]<10:continue
            w=dict(base);w[src]-=5;w[dst]+=5
            if min(w.values())>=0 and sum(w.values())==100:variants.append(w)
    # 再加入少量雙移轉組合，避免候選爆炸
    for a in keys:
        for b in keys:
            if a>=b or base[a]<10:continue
            for c in keys:
                if c in (a,b):continue
                w=dict(base);w[a]-=5;w[b]-=5;w[c]+=10
                if min(w.values())>=0 and sum(w.values())==100:variants.append(w)
    uniq=[];seen=set()
    for w in variants:
        key=tuple(w[k] for k in keys)
        if key not in seen:seen.add(key);uniq.append(w)
    return uniq

def a21_optimize_weight_sets(history, horizon=5, min_score=70, min_gap=3, top_k=10):
    """70/30 時序切分：只用 validation 表現排名候選權重，不碰 validation 之前的未來資料。"""
    if history is None or history.empty:return pd.DataFrame(), pd.DataFrame()
    h=add_forward_returns(history,horizon).dropna(subset=['未來報酬%']).copy()
    if h.empty:return pd.DataFrame(), pd.DataFrame()
    # 依股票各自做 chronological 70/30 split
    train_parts=[];valid_parts=[]
    for _,g in h.groupby('股票',sort=False):
        g=g.sort_values('日期'); cut=max(1,int(len(g)*0.7)); train_parts.append(g.iloc[:cut]);valid_parts.append(g.iloc[cut:])
    train=pd.concat(train_parts,ignore_index=True) if train_parts else pd.DataFrame()
    valid=pd.concat(valid_parts,ignore_index=True) if valid_parts else pd.DataFrame()
    if valid.empty:return pd.DataFrame(),pd.DataFrame()
    keys=list(COMPOSITE_WEIGHTS)
    results=[]
    for no,w in enumerate(_weight_variants(COMPOSITE_WEIGHTS),1):
        def score_frame(x):
            # 歷史 component 本身已是「原始滿分」；最佳化時先標準化到0~1，再套候選權重。
            base_max=pd.Series({k:COMPOSITE_WEIGHTS[k] for k in keys})
            normalized=x[keys].div(base_max,axis='columns').clip(lower=0,upper=1)
            return normalized.mul(pd.Series(w),axis='columns').sum(axis=1)
        v=valid.copy();v['_score']=score_frame(v)
        v=v[v['_score']>=min_score].sort_values(['股票','日期'])
        chosen=[]
        for sym,g in v.groupby('股票',sort=False):
            last_date=None
            for _,row in g.iterrows():
                if last_date is not None and (pd.Timestamp(row['日期'])-pd.Timestamp(last_date)).days<min_gap:continue
                chosen.append(row);last_date=row['日期']
        sel=pd.DataFrame(chosen)
        if sel.empty:
            results.append({'候選編號':no,'樣本數':0,'勝率%':np.nan,'平均報酬%':np.nan,'中位數報酬%':np.nan,'評分':-999,**{k:w[k] for k in keys}});continue
        r=sel['未來報酬%'];sample_factor=min(len(r)/30,1.0)
        objective=(r.mean()*0.55+r.median()*0.20+((r>0).mean()*100)*0.25/10)*sample_factor
        results.append({'候選編號':no,'樣本數':len(r),'勝率%':(r>0).mean()*100,'平均報酬%':r.mean(),'中位數報酬%':r.median(),'評分':objective,**{k:w[k] for k in keys}})
    ranking=pd.DataFrame(results).sort_values(['評分','樣本數'],ascending=False).reset_index(drop=True)
    return ranking.head(top_k),ranking


# ============================================================
# 🩺 A2.2：策略健診
# ============================================================
def _trade_metrics(returns):
    r=pd.Series(returns).dropna().astype(float)
    if r.empty:return {'樣本數':0,'勝率%':np.nan,'平均報酬%':np.nan,'中位數報酬%':np.nan,'報酬加總%':np.nan,'Profit Factor':np.nan,'Expectancy%':np.nan,'最大回撤%':np.nan,'平均獲利%':np.nan,'平均虧損%':np.nan}
    wins=r[r>0];losses=r[r<0];gp=float(wins.sum());gl=float(abs(losses.sum()));pf=gp/gl if gl>0 else 999.0
    equity=(1+r/100).cumprod();peak=equity.cummax();dd=(equity/peak-1)*100
    return {'樣本數':len(r),'勝率%':float((r>0).mean()*100),'平均報酬%':float(r.mean()),'中位數報酬%':float(r.median()),'報酬加總%':float(r.sum()),'Profit Factor':pf,'Expectancy%':float(r.mean()),'最大回撤%':float(dd.min()),'平均獲利%':float(wins.mean()) if not wins.empty else np.nan,'平均虧損%':float(losses.mean()) if not losses.empty else np.nan}

def _select_diag(frame,min_score,min_gap):
    if frame is None or frame.empty:return pd.DataFrame()
    base=frame.sort_values(['股票','日期']).copy()
    # 先建立每檔股票真正的交易日序號，再套用分數門檻；避免只看「篩選後列數」造成冷卻日誤判。
    base['_trade_pos']=base.groupby('股票').cumcount()
    x=base[base['綜合分數']>=min_score].copy();out=[]
    for sym,g in x.groupby('股票',sort=False):
        last=-10**9
        for _,row in g.sort_values('日期').iterrows():
            pos=int(row['_trade_pos'])
            if pos-last<min_gap:continue
            out.append(row);last=pos
    return pd.DataFrame(out).drop(columns=['_trade_pos'],errors='ignore')

def diagnostic_threshold_table(history,horizon=5,min_gap=3):
    rows=[];h=add_forward_returns(history,horizon).dropna(subset=['未來報酬%'])
    for th in [50,55,60,65,70,75,80,85,90]:
        sel=_select_diag(h,th,min_gap);rows.append({'最低分數':th,**_trade_metrics(sel['未來報酬%'] if not sel.empty else [])})
    return pd.DataFrame(rows)

def diagnostic_horizon_table(history,min_score=70,min_gap=3):
    rows=[]
    for hzn in [1,3,5,10,20]:
        h=add_forward_returns(history,hzn).dropna(subset=['未來報酬%']);sel=_select_diag(h,min_score,min_gap)
        rows.append({'持有交易日':hzn,**_trade_metrics(sel['未來報酬%'] if not sel.empty else [])})
    return pd.DataFrame(rows)

def diagnostic_matrix(history,min_gap=3):
    rows=[]
    for th in [60,65,70,75,80,85,90]:
        for hzn in [1,3,5,10,20]:
            h=add_forward_returns(history,hzn).dropna(subset=['未來報酬%']);sel=_select_diag(h,th,min_gap)
            rows.append({'最低分數':th,'持有交易日':hzn,**_trade_metrics(sel['未來報酬%'] if not sel.empty else [])})
    return pd.DataFrame(rows)

def diagnostic_overheat(history,min_score=90,min_gap=3,horizon=5):
    h=add_forward_returns(history,horizon).dropna(subset=['未來報酬%']);sel=_select_diag(h,min_score,min_gap)
    if sel.empty:return pd.DataFrame()
    masks={
        'KD K < 50':sel['K']<50,'KD K 50–70':(sel['K']>=50)&(sel['K']<70),'KD K 70–85':(sel['K']>=70)&(sel['K']<=85),'KD K > 85':sel['K']>85,
        '距MA15 0–3%':(sel['距MA15%']>=0)&(sel['距MA15%']<=3),'距MA15 3–6%':(sel['距MA15%']>3)&(sel['距MA15%']<=6),'距MA15 > 6%':sel['距MA15%']>6,
        '5日漲幅 <= 5%':sel['5日漲幅%']<=5,'5日漲幅 5–10%':(sel['5日漲幅%']>5)&(sel['5日漲幅%']<=10),'5日漲幅 > 10%':sel['5日漲幅%']>10}
    rows=[]
    for name,mask in masks.items():
        sub=sel[mask]
        if not sub.empty:rows.append({'特徵區間':name,**_trade_metrics(sub['未來報酬%'])})
    return pd.DataFrame(rows)

def prepare_diag_history(history):
    if history is None or history.empty:return pd.DataFrame()
    h=history.copy().sort_values(['股票','日期']).reset_index(drop=True)
    h['5日漲幅%']=np.nan
    h['距MA15%']=np.nan

    # A2.3.1 防呆：
    # historical_composite_events 正常會直接提供 MA15。
    # 若 Streamlit session_state 還留著舊版資料，或歷史資料缺欄位，
    # 就以各股票收盤價 rolling(15) 重建，避免 KeyError 讓整頁中斷。
    if 'MA15' not in h.columns:
        h['MA15']=np.nan
        for sym,g in h.groupby('股票',sort=False):
            ix=g.index
            close=pd.to_numeric(g['收盤'],errors='coerce')
            h.loc[ix,'MA15']=close.rolling(15,min_periods=1).mean().values

    for sym,g in h.groupby('股票',sort=False):
        ix=g.index
        close=pd.to_numeric(g['收盤'],errors='coerce')
        ma15=pd.to_numeric(g['MA15'],errors='coerce')

        # 若單一股票 MA15 有零星缺值，再用 rolling(15) 補齊
        ma15_fallback=close.rolling(15,min_periods=1).mean()
        ma15=ma15.where(ma15.notna(),ma15_fallback)

        h.loc[ix,'5日漲幅%']=close.div(close.shift(5)).sub(1).mul(100).values
        h.loc[ix,'距MA15%']=close.div(ma15.replace(0,np.nan)).sub(1).mul(100).values
    return h


# ============================================================
# 🧪 A2.3 Strategy Lab
# 八策略公平 PK：
#   1) MA20 + RSI
#   2) MA15 + RSI
#   3) MA20 + KD
#   4) MA15 + KD
#
# 原則：
# - 同一檔股票、同一段歷史、同一進出場規則
# - 只改「快均線」與「動能指標」，其餘 A2 綜合分數邏輯盡量保持一致
# - 訊號日收盤進場，N 個交易日後收盤出場
# - 冷卻日以真正交易日序號計算
# ============================================================

A23_STRATEGIES = {
    'MA15＋RSI': {'fast_ma':15, 'osc':'RSI'},
    'MA20＋RSI': {'fast_ma':20, 'osc':'RSI'},
    'MA30＋RSI': {'fast_ma':30, 'osc':'RSI'},
    'MA60＋RSI': {'fast_ma':60, 'osc':'RSI'},
    'MA15＋KD':  {'fast_ma':15, 'osc':'KD'},
    'MA20＋KD':  {'fast_ma':20, 'osc':'KD'},
    'MA30＋KD':  {'fast_ma':30, 'osc':'KD'},
    'MA60＋KD':  {'fast_ma':60, 'osc':'KD'},
}

A23_STRATEGY_SIGNATURE = '|'.join(
    f"{name}:{cfg['fast_ma']}:{cfg['osc']}" for name,cfg in A23_STRATEGIES.items()
)

def indicators_strategy_lab(d, fast_ma=15, osc='KD'):
    d=d.copy()
    d['MA_FAST']=d.Close.rolling(int(fast_ma)).mean()
    d['MA60']=d.Close.rolling(60).mean()
    d['MA200']=d.Close.rolling(200).mean()
    d['FAST_SLOPE']=d['MA_FAST']-d['MA_FAST'].shift(5)
    d['MA60_SLOPE']=d['MA60']-d['MA60'].shift(5)
    d['VOL_MA20']=d.Volume.rolling(20).mean()
    d['VOL_RATIO']=d.Volume/d['VOL_MA20'].replace(0,np.nan)
    d['CHANGE']=d.Close.pct_change()*100
    d['HIGH20']=d.High.shift(1).rolling(20).max()

    # RSI14（Wilder 類型 EWM）
    delta=d.Close.diff()
    gain=delta.clip(lower=0)
    loss=(-delta.clip(upper=0))
    avg_gain=gain.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    avg_loss=loss.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    rs=avg_gain/avg_loss.replace(0,np.nan)
    d['RSI']=100-(100/(1+rs))
    d.loc[(avg_loss==0)&(avg_gain>0),'RSI']=100
    d.loc[(avg_loss==0)&(avg_gain==0),'RSI']=50

    # KD：9日 RSV → K/D(3,3)
    ll=d.Low.rolling(9).min()
    hh=d.High.rolling(9).max()
    d['RSV']=((d.Close-ll)/(hh-ll).replace(0,np.nan)*100).clip(0,100)
    d['K']=d['RSV'].ewm(alpha=1/3,adjust=False,min_periods=1).mean()
    d['D']=d['K'].ewm(alpha=1/3,adjust=False,min_periods=1).mean()
    return d

def momentum_score_strategy_lab(x, osc='KD'):
    """A2.3 動能項目統一滿分 10。"""
    if str(osc).upper()=='RSI':
        r=float(x.RSI) if pd.notna(x.RSI) else 50.0
        if 50<=r<=70: s=10.0
        elif 45<=r<50: s=8.0
        elif 70<r<=80: s=7.0
        elif 35<=r<45: s=5.0
        elif r>80: s=3.0
        elif r<30: s=4.0
        else: s=3.0
        return _clip_score(s,0,10)

    k=float(x.K) if pd.notna(x.K) else 50.0
    d=float(x.D) if pd.notna(x.D) else 50.0
    if k>=d:
        if 45<=k<=75: s=10.0
        elif 30<=k<45: s=8.0
        elif 75<k<=85: s=7.0
        elif k>85: s=3.0
        else: s=6.0
    else:
        if k>=70: s=4.0
        elif k>=50: s=5.0
        elif k>=30: s=3.0
        else: s=2.0
    if k>d and k-d>=5: s=min(10.0,s+1.0)
    return _clip_score(s,0,10)

def strategy_lab_parts(hist, fast_ma=15, osc='KD'):
    """同一套 A2 架構，只替換快均線與動能指標，確保四策略公平比較。"""
    x=hist.iloc[-1]
    close=float(x.Close)
    vol=float(x.Volume) if pd.notna(x.Volume) else 0.0
    fast=float(x.MA_FAST) if pd.notna(x.MA_FAST) else np.nan
    ma60=float(x.MA60) if pd.notna(x.MA60) else np.nan
    ma200=float(x.MA200) if pd.notna(x.MA200) else np.nan
    vr=float(x.VOL_RATIO) if pd.notna(x.VOL_RATIO) else 1.0
    chg=float(x.CHANGE) if pd.notna(x.CHANGE) else 0.0

    # 1) 智能 OHLCV 歷史代理（四策略完全相同）
    turnover=close*vol
    turn20=hist['Close'].mul(hist['Volume']).rolling(20).median().iloc[-1]
    denom=turn20 if pd.notna(turn20) and turn20>0 else turnover
    liq=15*_clip_score((turnover/(denom if denom>0 else 1))-0.5,0,2)/2

    ret5=(close/float(hist['Close'].iloc[-6])-1)*100 if len(hist)>=6 and float(hist['Close'].iloc[-6])>0 else 0
    mom=10*(_clip_score(chg,-5,5)+5)/10*0.55 + 10*(_clip_score(ret5,-10,10)+10)/20*0.45

    amp=((float(x.High)-float(x.Low))/close*100) if close>0 and pd.notna(x.High) and pd.notna(x.Low) else 0
    amp20=hist['Close'].pct_change().rolling(20).std().iloc[-1]*100
    vol_score=5*(_clip_score(amp,0,10)/10*0.65 + _clip_score(amp20 if pd.notna(amp20) else 0,0,8)/8*0.35)
    act=5*(_clip_score(vr,0,3)/3)

    # 2) MA 結構：快均線依策略使用 MA15 或 MA20
    ma=0
    if pd.notna(fast) and close>fast: ma+=6
    if pd.notna(ma60) and pd.notna(fast) and fast>ma60: ma+=4
    if pd.notna(ma200) and pd.notna(ma60) and ma60>ma200: ma+=3
    if pd.notna(x.FAST_SLOPE) and x.FAST_SLOPE>0: ma+=2
    ma=min(ma,15)

    # 3) MA200 生命線
    life=0
    if pd.notna(ma200):
        dist=(close/ma200-1)*100
        if 0<=dist<=5: life=10
        elif dist>5: life=8
        elif dist>=-3: life=6
        elif dist>=-8: life=3

    # 4) RSI 或 KD，統一滿分10
    osc_score=momentum_score_strategy_lab(x,osc)

    # 5) 量價
    if vr>=1.2 and chg>0:q=10
    elif vr>=1.0 and chg>0:q=8
    elif vr>=1.2:q=6
    elif chg>0:q=5
    elif vr<0.7:q=2
    else:q=3

    # 6) 突破前20日高
    prior_high=float(x.HIGH20) if pd.notna(x.HIGH20) else np.nan
    if pd.notna(prior_high) and prior_high>0:
        ratio=close/prior_high-1
        br=10 if ratio>=0 else 7 if ratio>=-0.01 else 4 if ratio>=-0.03 else 1
    else:
        br=0

    # 7) 策略品質：不用未來資料，以技術條件完整度計 0~10
    confirmations=0
    if pd.notna(fast) and close>fast: confirmations+=1
    if pd.notna(ma60) and pd.notna(fast) and fast>ma60: confirmations+=1
    if pd.notna(ma200) and close>ma200: confirmations+=1
    if osc_score>=8: confirmations+=1
    if vr>=1.2 and chg>0: confirmations+=1
    if br>=7: confirmations+=1
    strategy=min(10.0, confirmations*(10/6))

    parts={
        '智能流動性':liq,'智能動能':mom,'智能波動':vol_score,'智能活躍':act,
        'MA多頭結構':ma,'生命線':life,'KD動能':osc_score,'量價確認':q,
        '突破':br,'策略品質':strategy
    }
    return {k:round(_clip_score(v,0,COMPOSITE_WEIGHTS[k]),2) for k,v in parts.items()}

def strategy_lab_history_for_stock(symbol, df, strategy_name):
    cfg=A23_STRATEGIES[strategy_name]
    if df is None or len(df)<220:return pd.DataFrame()
    full=indicators_strategy_lab(df.copy(),cfg['fast_ma'],cfg['osc'])
    rows=[]
    for i in range(200,len(full)):
        hist=full.iloc[:i+1]
        x=hist.iloc[-1]
        parts=strategy_lab_parts(hist,cfg['fast_ma'],cfg['osc'])
        score=round(sum(parts.values()),2)
        rows.append({
            '股票':str(symbol).zfill(4),'日期':hist.index[-1],'策略':strategy_name,
            '快均線':f"MA{cfg['fast_ma']}",'動能指標':cfg['osc'],
            '綜合分數':score,'收盤':float(x.Close),
            'RSI':float(x.RSI) if pd.notna(x.RSI) else np.nan,
            'K':float(x.K) if pd.notna(x.K) else np.nan,
            'D':float(x.D) if pd.notna(x.D) else np.nan,
            '快均線值':float(x.MA_FAST) if pd.notna(x.MA_FAST) else np.nan,
            **parts
        })
    return pd.DataFrame(rows)

def collect_strategy_lab_history(symbols, market_map, progress=None, status=None):
    all_rows=[]
    total=max(len(symbols)*len(A23_STRATEGIES),1)
    done=0
    for sym in symbols:
        try:
            raw=get_stock_data(sym,market_map.get(sym))
            if raw is None:
                done+=len(A23_STRATEGIES)
                continue
            for strategy_name in A23_STRATEGIES:
                if status is not None:
                    status.text(f'A2.3 建立歷史：{sym} {stock_name(sym)}｜{strategy_name}')
                h=strategy_lab_history_for_stock(sym,raw,strategy_name)
                if not h.empty:all_rows.append(h)
                done+=1
                if progress is not None:
                    val=min(done/total,1.0)
                    if hasattr(progress,'progress'):progress.progress(val)
                    elif callable(progress):progress(val)
        except Exception:
            done+=len(A23_STRATEGIES)
            if progress is not None and hasattr(progress,'progress'):
                progress.progress(min(done/total,1.0))
            continue
    return pd.concat(all_rows,ignore_index=True) if all_rows else pd.DataFrame()

def add_forward_returns_a23(history, horizon):
    if history is None or history.empty:return pd.DataFrame()
    h=history.copy().sort_values(['策略','股票','日期']).reset_index(drop=True)
    h['未來收盤']=h.groupby(['策略','股票'])['收盤'].shift(-int(horizon))
    h['未來報酬%']=(h['未來收盤']/h['收盤']-1)*100
    return h

def select_a23(frame, min_score, min_gap):
    if frame is None or frame.empty:return pd.DataFrame()
    base=frame.sort_values(['策略','股票','日期']).copy()
    base['_trade_pos']=base.groupby(['策略','股票']).cumcount()
    x=base[base['綜合分數']>=float(min_score)].copy()
    out=[]
    for (strategy,sym),g in x.groupby(['策略','股票'],sort=False):
        last=-10**9
        for _,row in g.sort_values('日期').iterrows():
            pos=int(row['_trade_pos'])
            if pos-last<int(min_gap):continue
            out.append(row)
            last=pos
    return pd.DataFrame(out).drop(columns=['_trade_pos'],errors='ignore') if out else pd.DataFrame()

def strategy_lab_grid(history, thresholds, horizons, min_gap=3):
    rows=[]
    for hzn in horizons:
        f=add_forward_returns_a23(history,hzn).dropna(subset=['未來報酬%'])
        for strategy_name in A23_STRATEGIES:
            sf=f[f['策略']==strategy_name]
            for th in thresholds:
                sel=select_a23(sf,th,min_gap)
                metrics=_trade_metrics(sel['未來報酬%'] if not sel.empty else [])
                rows.append({'策略':strategy_name,'最低分數':int(th),'持有交易日':int(hzn),**metrics})
    return pd.DataFrame(rows)

def strategy_lab_rank(grid, min_samples=20):
    if grid is None or grid.empty:return pd.DataFrame()
    x=grid.copy()
    valid=x[(x['樣本數']>=int(min_samples)) & x['平均報酬%'].notna()].copy()
    if valid.empty:return pd.DataFrame()

    # 先以平均報酬排序，再看 PF、勝率；不製造黑箱加權分數。
    valid=valid.sort_values(
        ['平均報酬%','Profit Factor','勝率%','中位數報酬%','樣本數'],
        ascending=[False,False,False,False,False]
    ).reset_index(drop=True)
    valid.insert(0,'排名',np.arange(1,len(valid)+1))
    return valid

def strategy_lab_summary(grid, min_samples=20):
    if grid is None or grid.empty:return pd.DataFrame()
    rows=[]
    for strategy_name,g in grid.groupby('策略'):
        valid=g[(g['樣本數']>=int(min_samples)) & g['平均報酬%'].notna()].copy()
        if valid.empty:
            rows.append({'策略':strategy_name,'最佳最低分數':np.nan,'最佳持有日':np.nan,'樣本數':0,
                         '勝率%':np.nan,'平均報酬%':np.nan,'中位數報酬%':np.nan,'Profit Factor':np.nan,'最大回撤%':np.nan})
            continue
        best=valid.sort_values(['平均報酬%','Profit Factor','勝率%'],ascending=False).iloc[0]
        rows.append({
            '策略':strategy_name,'最佳最低分數':int(best['最低分數']),'最佳持有日':int(best['持有交易日']),
            '樣本數':int(best['樣本數']),'勝率%':best['勝率%'],'平均報酬%':best['平均報酬%'],
            '中位數報酬%':best['中位數報酬%'],'Profit Factor':best['Profit Factor'],'最大回撤%':best['最大回撤%']
        })
    return pd.DataFrame(rows).sort_values(['平均報酬%','Profit Factor'],ascending=False,na_position='last').reset_index(drop=True)



# ============================================================
# 🧪 A2.3.4 Strategy Lab Pro
# 新增：
# 1) 95% 平均報酬信賴區間（常態近似）
# 2) 穩健候選：樣本數達標且 95% CI 下限 > 0
# 3) 分數區間分析：避免「>=70」把 70~100 全混在一起
# 4) 每套策略甜蜜區間判定
# ============================================================

A232_SCORE_BINS = [-np.inf, 60, 65, 70, 75, 80, 85, 90, np.inf]
A232_SCORE_LABELS = ['<60','60–64','65–69','70–74','75–79','80–84','85–89','90+']

def _trade_metrics_ci(returns):
    """在既有 trade metrics 上補 95% CI 與報酬標準差。"""
    s=pd.Series(returns,dtype=float).replace([np.inf,-np.inf],np.nan).dropna()
    base=_trade_metrics(s)
    n=len(s)
    if n>=2:
        sd=float(s.std(ddof=1))
        se=sd/np.sqrt(n)
        lo=float(s.mean()-1.96*se)
        hi=float(s.mean()+1.96*se)
    elif n==1:
        sd=np.nan;lo=np.nan;hi=np.nan
    else:
        sd=np.nan;lo=np.nan;hi=np.nan
    base.update({
        '報酬標準差%':round(sd,3) if pd.notna(sd) else np.nan,
        '平均報酬95%CI下限':round(lo,3) if pd.notna(lo) else np.nan,
        '平均報酬95%CI上限':round(hi,3) if pd.notna(hi) else np.nan,
    })
    return base

def strategy_lab_grid_pro(history, thresholds, horizons, min_gap=3):
    """A2.3.2：與原 A2.3 相同的門檻回測，但加入信賴區間。"""
    rows=[]
    for hzn in horizons:
        f=add_forward_returns_a23(history,hzn).dropna(subset=['未來報酬%'])
        for strategy_name in A23_STRATEGIES:
            sf=f[f['策略']==strategy_name]
            for th in thresholds:
                sel=select_a23(sf,th,min_gap)
                metrics=_trade_metrics_ci(sel['未來報酬%'] if not sel.empty else [])
                rows.append({'策略':strategy_name,'最低分數':int(th),'持有交易日':int(hzn),**metrics})
    return pd.DataFrame(rows)

def strategy_lab_band_grid(history, horizons, min_gap=3):
    """
    分數「區間」分析，不是最低門檻。
    例如 75–79 只看 75~79，不會把 80、90 分混進來。
    """
    rows=[]
    if history is None or history.empty:return pd.DataFrame()
    for hzn in horizons:
        f=add_forward_returns_a23(history,hzn).dropna(subset=['未來報酬%']).copy()
        f['分數區間']=pd.cut(
            pd.to_numeric(f['綜合分數'],errors='coerce'),
            bins=A232_SCORE_BINS,
            labels=A232_SCORE_LABELS,
            right=False
        )
        # 先用完整歷史位置做 cooldown，再限制分數區間
        base=f.sort_values(['策略','股票','日期']).copy()
        base['_trade_pos']=base.groupby(['策略','股票']).cumcount()
        for strategy_name in A23_STRATEGIES:
            sf=base[base['策略']==strategy_name]
            for label in A232_SCORE_LABELS:
                band=sf[sf['分數區間'].astype(str)==label].copy()
                chosen=[]
                for sym,g in band.groupby('股票',sort=False):
                    last=-10**9
                    for _,row in g.sort_values('日期').iterrows():
                        pos=int(row['_trade_pos'])
                        if pos-last<int(min_gap):continue
                        chosen.append(row)
                        last=pos
                sel=pd.DataFrame(chosen)
                metrics=_trade_metrics_ci(sel['未來報酬%'] if not sel.empty else [])
                rows.append({
                    '策略':strategy_name,'分數區間':label,'持有交易日':int(hzn),**metrics
                })
    return pd.DataFrame(rows)

def strategy_lab_robust_rank(grid, min_samples=20):
    """
    穩健排名：
    - 必須達最低樣本數
    - 先看 95% CI 下限
    - 再看平均報酬、PF、勝率
    不另外創造不可解釋的黑箱分數。
    """
    if grid is None or grid.empty:return pd.DataFrame()
    x=grid.copy()
    valid=x[
        (x['樣本數']>=int(min_samples)) &
        x['平均報酬%'].notna() &
        x['平均報酬95%CI下限'].notna()
    ].copy()
    if valid.empty:return pd.DataFrame()
    valid['穩健候選']=np.where(valid['平均報酬95%CI下限']>0,'✅','—')
    valid=valid.sort_values(
        ['穩健候選','平均報酬95%CI下限','平均報酬%','Profit Factor','勝率%','樣本數'],
        ascending=[True,False,False,False,False,False]
    ).reset_index(drop=True)
    # 讓 ✅ 真正排前面
    valid['_robust']=(valid['穩健候選']=='✅').astype(int)
    valid=valid.sort_values(
        ['_robust','平均報酬95%CI下限','平均報酬%','Profit Factor','勝率%','樣本數'],
        ascending=[False,False,False,False,False,False]
    ).drop(columns=['_robust']).reset_index(drop=True)
    valid.insert(0,'穩健排名',np.arange(1,len(valid)+1))
    return valid

def strategy_lab_band_summary(band_grid, min_samples=20):
    """每套策略找出最佳「真正分數區間」。"""
    if band_grid is None or band_grid.empty:return pd.DataFrame()
    rows=[]
    for strategy_name,g in band_grid.groupby('策略'):
        v=g[(g['樣本數']>=int(min_samples)) & g['平均報酬%'].notna()].copy()
        if v.empty:continue
        v['_robust']=(v['平均報酬95%CI下限'].fillna(-999)>0).astype(int)
        best=v.sort_values(
            ['_robust','平均報酬95%CI下限','平均報酬%','Profit Factor','樣本數'],
            ascending=[False,False,False,False,False]
        ).iloc[0]
        rows.append({
            '策略':strategy_name,
            '甜蜜分數區間':best['分數區間'],
            '最佳持有日':int(best['持有交易日']),
            '樣本數':int(best['樣本數']),
            '勝率%':best['勝率%'],
            '平均報酬%':best['平均報酬%'],
            '95%CI下限':best['平均報酬95%CI下限'],
            '95%CI上限':best['平均報酬95%CI上限'],
            'Profit Factor':best['Profit Factor'],
            '最大回撤%':best['最大回撤%'],
            '穩健候選':'✅' if pd.notna(best['平均報酬95%CI下限']) and best['平均報酬95%CI下限']>0 else '—'
        })
    return pd.DataFrame(rows).sort_values(
        ['穩健候選','95%CI下限','平均報酬%','Profit Factor'],
        ascending=[True,False,False,False]
    ).reset_index(drop=True) if rows else pd.DataFrame()


# Sidebar
st.sidebar.title('🖤 黑嚕嚕－台股盤中雷達');st.sidebar.caption('V3.3.2｜全市場股票池＋A2.2＋A2.3.4 Strategy Lab Pro')
mode=st.sidebar.selectbox('雷達模式',['全部股票','🟣 黑嚕嚕超強','🔥 強勢股','🚀 強勢突破','🔥 主升段','🟢 守護生命線','⚠️ 大量換手高危','🔴 趨勢轉弱'])
markets=st.sidebar.multiselect('市場',['上市','上櫃','興櫃'],default=['上市','上櫃','興櫃'])
if st.sidebar.button('🔄 更新全市場股票池'):
    load_market_universe.clear()
    st.rerun()
counts=UNIVERSE['市場'].value_counts().to_dict() if not UNIVERSE.empty else {}
st.sidebar.caption(f"官方股票池：上市 {counts.get('上市',0)}｜上櫃 {counts.get('上櫃',0)}｜興櫃 {counts.get('興櫃',0)}")
scan_mode=st.sidebar.radio('掃描方式',['🧠 全市場智能掃描','🎯 指定股票池'],index=0)
max_n=st.sidebar.slider('技術精掃檔數',20,500,200,10);min_score=st.sidebar.slider('最低黑嚕嚕分數',0,100,50,5);min_vr=st.sidebar.slider('最低量比',0.5,5.0,1.0,0.1)
change_range=st.sidebar.slider('漲跌幅範圍 (%)',-10.0,10.0,(-10.0,10.0),0.5);k_range=st.sidebar.slider('KD K值範圍',0,100,(0,100),1)
smart_min_volume=st.sidebar.number_input('智能初篩最低成交量（股）',min_value=0,step=1000,value=100000)
smart_min_value=st.sidebar.number_input('智能初篩最低成交額（元）',min_value=0,step=1000000,value=10000000)
smart_min_change=st.sidebar.slider('智能初篩最低漲幅 (%)',-10.0,10.0,0.0,0.5)
sort_mode=st.sidebar.selectbox('排行榜排序',['黑嚕嚕分數','漲跌幅','量比','K值','價格']);auto=st.sidebar.checkbox('自動刷新',value=False);refresh=st.sidebar.select_slider('刷新秒數',options=[30,60,120,180,300],value=120)
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
smart_snapshot=pd.DataFrame();smart_status=[];smart_note=''
if scan_mode.startswith('🧠'):
    smart_snapshot,smart_status=load_market_snapshot()
    base_universe=UNIVERSE[UNIVERSE['市場'].isin(markets)].copy() if not UNIVERSE.empty else pd.DataFrame()
    smart_pool,smart_note=smart_rank_universe(base_universe,smart_snapshot,markets,max_n,smart_min_volume,smart_min_value,smart_min_change)
    symbols=smart_pool['股票代號'].astype(str).str.zfill(4).tolist()
    market_map.update(dict(zip(smart_pool['股票代號'],smart_pool['市場'])))
else:
    symbols=symbols[:max_n]

st.title('🖤 黑嚕嚕－台股盤中雷達');st.caption('V3.3.2 A2.3｜智能掃描 2.0＋A2.2 策略健診＋A2.3 Strategy Lab 八策略PK；行情仍為 yfinance 日資料。')
a,b,c,d,e=st.columns(5);a.metric('技術精掃',f'{len(symbols)} 檔');b.metric('全市場股票池',f'{len(UNIVERSE)} 檔');c.metric('最低量比',f'{min_vr:.1f}x');d.metric('市場','＋'.join(markets) if markets else '未選');e.metric('更新時間',datetime.now().strftime('%H:%M:%S'));st.divider()
if scan_mode.startswith('🧠') and not smart_pool.empty and '智能初篩分' in smart_pool.columns:
    with st.expander('🔎 查看 V3.3.2 智能候選池',expanded=False):
        preview=smart_pool[['股票代號','股票名稱','市場','智能初篩分','漲跌幅%','成交量','成交額']].copy()
        preview=preview.rename(columns={'股票代號':'股票','股票名稱':'名稱','漲跌幅%':'漲跌幅'})
        preview['漲跌幅']=preview['漲跌幅'].map(lambda x:f'{x:+.2f}%' if pd.notna(x) else '-')
        preview['成交量']=preview['成交量'].map(lambda x:f'{x:,.0f}')
        preview['成交額']=preview['成交額'].map(lambda x:f'{x:,.0f}')
        st.dataframe(preview,use_container_width=True,hide_index=True)
if scan_mode.startswith('🧠'):
    st.info(f'🧠 智能掃描：先從上市／上櫃／興櫃官方行情篩選，再對 {len(symbols)} 檔進行 2 年技術分析。{smart_note}')
    if smart_status: st.caption('｜'.join(smart_status))

rows=[];p=st.progress(0);status=st.empty()
for i,s in enumerate(symbols):
    status.text(f'正在掃描：{s} {stock_name(s)}　({i+1}/{len(symbols)})');df=get_stock_data(s, market_map.get(s))
    if df is None: p.progress((i+1)/max(len(symbols),1));continue
    r=build_row(s,df)
    if r:
        sig=r['訊號'];market_ok=(not markets or r['市場'] in markets or r['市場']=='未分類');change_ok=change_range[0]<=r['漲跌%']<=change_range[1];k_ok=pd.isna(r['K']) or k_range[0]<=r['K']<=k_range[1];base=r['黑嚕嚕分數']>=min_score and r['量比']>=min_vr and change_ok and k_ok and market_ok
        mode_ok={'全部股票':True,'🟣 黑嚕嚕超強':r['黑嚕嚕分數']>=90,'🔥 強勢股':r['黑嚕嚕分數']>=80 and r['漲跌%']>0,'🚀 強勢突破':'🚀 強勢突破' in sig,'🔥 主升段':'🔥 主升段' in sig,'🟢 守護生命線':'🟢 守護生命線' in sig,'⚠️ 大量換手高危':'⚠️ 爆量高危' in sig,'🔴 趨勢轉弱':'🔴 趨勢轉弱' in sig}.get(mode,True)
        if base and mode_ok:rows.append(r)
    p.progress((i+1)/max(len(symbols),1))
status.empty();p.empty()
if not rows:st.warning('目前沒有符合條件的股票。可以降低最低黑嚕嚕分數、量比、KD／漲跌幅，或增加股票池。');st.stop()
result=pd.DataFrame(rows);result=add_composite_columns(result);sort_col={'黑嚕嚕分數':'黑嚕嚕分數','漲跌幅':'漲跌%','量比':'量比','K值':'K','價格':'價格'}[sort_mode];result=result.sort_values(sort_col,ascending=False,na_position='last').reset_index(drop=True)

strong=int((result['黑嚕嚕分數']>=80).sum());breakout=int(result['訊號'].str.contains('🚀 強勢突破',regex=False).sum());risk=int(result['訊號'].str.contains('⚠️ 爆量高危',regex=False).sum());weak=int(result['訊號'].str.contains('🔴 趨勢轉弱',regex=False).sum())
a,b,c,d,e=st.columns(5);a.metric('符合條件',f'{len(result)} 檔');b.metric('🔥 80分以上',f'{strong} 檔');c.metric('🚀 突破',f'{breakout} 檔');d.metric('⚠️ 高危',f'{risk} 檔');e.metric('🔴 轉弱',f'{weak} 檔');st.divider()

st.subheader('🖤 黑嚕嚕焦點');top=result.head(4);cols=st.columns(len(top))
for col,(_,r) in zip(cols,top.iterrows()):
    icon='🟢' if r['漲跌%']>0 else '🔴' if r['漲跌%']<0 else '⚪'
    with col:st.markdown(f'''<div class="radar-card"><div class="radar-title">{icon} {r['股票']} {r['名稱']}</div><div class="small">{r['市場']}</div><div class="radar-price">{r['價格']:.2f}</div><div>{r['漲跌%']:+.2f}%　量比 {r['量比']:.2f}x　KD K {r['K']:.1f} / D {r['D']:.1f}</div><div class="radar-score">🖤 {r['黑嚕嚕分數']} / 100</div><div>{r['等級']}</div><div class="signal">{r['訊號']}</div></div>''',unsafe_allow_html=True)

t1,t2,t3,t4,t5,t6,t7,t8,t9=st.tabs(['📋 黑嚕嚕排行榜','🚨 訊號中心','📊 分數拆解','📈 個股分析','⭐ 自選股','🧪 V3.2 訊號回測','🖤 A2 綜合分數回測','🩺 A2.2 策略健診','🧪 A2.3 Strategy Lab'])
with t1:
    show=result[['股票','名稱','市場','價格','漲跌%','量比','成交量','K','D','黑嚕嚕分數','綜合分數','綜合等級','訊號']].copy();show['價格']=show['價格'].map(lambda x:f'{x:.2f}');show['漲跌%']=show['漲跌%'].map(lambda x:f'{x:+.2f}%');show['量比']=show['量比'].map(lambda x:f'{x:.2f}x');show['成交量']=show['成交量'].map(lambda x:f'{x:,.0f}');show['K']=show['K'].map(lambda x:f'{x:.1f}' if pd.notna(x) else '-');show['D']=show['D'].map(lambda x:f'{x:.1f}' if pd.notna(x) else '-')
    st.dataframe(show,use_container_width=True,hide_index=True,column_config={'黑嚕嚕分數':st.column_config.ProgressColumn('🖤 黑嚕嚕分數',min_value=0,max_value=100,format='%d')})
with t2:
    st.subheader('🚨 黑嚕嚕訊號中心')
    for title,key in [('🚀 強勢突破','🚀 強勢突破'),('🔥 主升段','🔥 主升段'),('🟢 守護生命線','🟢 守護生命線'),('⚠️ 爆量高危','⚠️ 爆量高危'),('🔴 趨勢轉弱','🔴 趨勢轉弱')]:
        g=result[result['訊號'].str.contains(key,regex=False)].copy();st.markdown(f'### {title}　{len(g)} 檔')
        if g.empty:st.info('目前沒有符合這個訊號的股票。')
        else:
            q=g[['股票','名稱','價格','漲跌%','量比','K','D','黑嚕嚕分數','判斷']].copy();q['價格']=q['價格'].map(lambda x:f'{x:.2f}');q['漲跌%']=q['漲跌%'].map(lambda x:f'{x:+.2f}%');q['量比']=q['量比'].map(lambda x:f'{x:.2f}x');q['K']=q['K'].map(lambda x:f'{x:.1f}' if pd.notna(x) else '-');q['D']=q['D'].map(lambda x:f'{x:.1f}' if pd.notna(x) else '-');st.dataframe(q,use_container_width=True,hide_index=True)
with t3:
    st.subheader('📊 黑嚕嚕分數拆解');s=st.selectbox('選擇股票',result['股票'].tolist(),key='score_stock');r=result[result['股票']==s].iloc[0]
    st.markdown(f"### 🖤 {r['股票']} {r['名稱']}　{r['黑嚕嚕分數']} / 100　{r['等級']}")
    q=pd.DataFrame({'項目':['📈 趨勢','⚡ 動能','🔊 成交量','🚀 突破','KD','⭐ 額外強度'],'得分':[r['趨勢分'],r['動能分'],r['量能分'],r['突破分'],r['KD分'],r['額外分']],'滿分':[30,20,20,15,10,5]});st.dataframe(q,use_container_width=True,hide_index=True)
    c1,c2=st.columns(2);c1.metric('總分',f"{r['黑嚕嚕分數']} / 100");c1.metric('量比',f"{r['量比']:.2f}x");c1.metric('KD K',f"{r['K']:.1f}" if pd.notna(r['K']) else '-');c2.metric('20日高點',f"{r['20日高']:.2f}" if pd.notna(r['20日高']) else '-');c2.metric('MA15',f"{r['MA15']:.2f}" if pd.notna(r['MA15']) else '-');c2.metric('MA200',f"{r['MA200']:.2f}" if pd.notna(r['MA200']) else '-')
    st.markdown('**目前主要判斷**');[st.write(f'• {x}') for x in r['判斷'].split('、') if x]
with t4:
    st.subheader('📈 個股分析');s=st.selectbox('選擇分析股票',result['股票'].tolist(),key='chart_stock');r=result[result['股票']==s].iloc[0];d=r['_df'].tail(120).copy();st.markdown(f"### {r['股票']} {r['名稱']}　{r['價格']:.2f}　{r['漲跌%']:+.2f}%")
    st.line_chart(d[['Close','MA15','MA60','MA200']].rename(columns={'Close':'股價'}),height=420);a,b,c,d2,e=st.columns(5);a.metric('黑嚕嚕',f"{r['黑嚕嚕分數']}分");b.metric('量比',f"{r['量比']:.2f}x");c.metric('KD K',f"{r['K']:.1f}" if pd.notna(r['K']) else '-');d2.metric('MA15',f"{r['MA15']:.2f}");e.metric('MA200',f"{r['MA200']:.2f}");st.markdown('#### 🔊 成交量');st.line_chart(d[['Volume','VOL_MA20']].rename(columns={'Volume':'成交量','VOL_MA20':'20日均量'}),height=250);st.markdown('#### 🚨 目前訊號');st.info(r['訊號']);st.markdown('#### 🧠 黑嚕嚕判讀');st.write(r['判斷'] or '目前沒有額外判讀。')
with t5:
    st.subheader('⭐ 自選股');watch=st.multiselect('加入自選股',result['股票'].tolist(),default=[],key='watchlist')
    if not watch:st.info('請從上方選擇股票加入自選股。')
    else:
        q=result[result['股票'].isin(watch)].sort_values('黑嚕嚕分數',ascending=False)[['股票','名稱','價格','漲跌%','量比','K','D','黑嚕嚕分數','綜合分數','綜合等級','訊號']].copy();q['價格']=q['價格'].map(lambda x:f'{x:.2f}');q['漲跌%']=q['漲跌%'].map(lambda x:f'{x:+.2f}%');q['量比']=q['量比'].map(lambda x:f'{x:.2f}x');q['K']=q['K'].map(lambda x:f'{x:.1f}' if pd.notna(x) else '-');q['D']=q['D'].map(lambda x:f'{x:.1f}' if pd.notna(x) else '-');st.dataframe(q,use_container_width=True,hide_index=True)


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
        for col in ['進場價','出場價','報酬%','MFE%','MAE%','量比','K','D']:
            if col in detail.columns: detail[col]=detail[col].round(2)
        st.dataframe(detail,use_container_width=True,hide_index=True)
        st.download_button('⬇️ 匯出 V3.2 回測 CSV',bt_result.to_csv(index=False).encode('utf-8-sig'),'v3.2_backtest.csv','text/csv')
    else:
        st.info('尚未完成回測。請設定條件後按「▶ 開始 V3.2 回測」。')

with t7:
    st.subheader('🖤 A2 綜合選股分數回測')
    st.caption('A2.2 基準：價格均線改 MA15；RSV 改為 9 日 KD（K/D）。歷史每日只用當天以前資料計分，訊號日收盤進場，N 個交易日後收盤出場。')
    c1,c2,c3=st.columns(3)
    with c1:a2_h=st.selectbox('A2 持有交易日',[1,3,5,10,20],index=2,key='a2_h')
    with c2:a2_gap=st.selectbox('A2 冷卻天數',[0,3,5,10,20],index=1,key='a2_gap')
    with c3:a2_min=st.slider('A2 最低綜合分數',0,100,70,5,key='a2_min')
    a2_n=st.slider('A2 回測股票數',1,min(100,len(symbols)),min(30,len(symbols)),1,key='a2_n')
    st.markdown('#### 📐 A2 固定權重')
    wt_df=pd.DataFrame({'項目':list(COMPOSITE_WEIGHTS),'權重':[COMPOSITE_WEIGHTS[k] for k in COMPOSITE_WEIGHTS],'說明':[A2_COMPONENT_DESC[k] for k in COMPOSITE_WEIGHTS]})
    st.dataframe(wt_df,use_container_width=True,hide_index=True)
    if st.button('▶ 開始 A2 綜合分數回測',type='primary',key='run_a2'):
        all_a2=[];pr=st.progress(0);ss=st.empty()
        for i,sym in enumerate(symbols[:a2_n]):
            ss.text(f'正在 A2 回測：{sym} {stock_name(sym)} ({i+1}/{a2_n})')
            bdf=get_stock_data(sym,market_map.get(sym))
            if bdf is not None:
                ev=run_composite_backtest(sym,bdf,a2_h,a2_gap,a2_min)
                if not ev.empty:all_a2.append(ev)
            pr.progress((i+1)/max(a2_n,1))
        ss.empty();pr.empty()
        st.session_state['a2_bt']=pd.concat(all_a2,ignore_index=True) if all_a2 else pd.DataFrame()
    a2=st.session_state.get('a2_bt',pd.DataFrame())
    if not a2.empty:
        rr=a2['報酬%'];c1,c2,c3,c4,c5=st.columns(5)
        c1.metric('樣本數',len(rr));c2.metric('勝率',f'{(rr>0).mean()*100:.1f}%');c3.metric('平均報酬',f'{rr.mean():+.2f}%');c4.metric('中位數',f'{rr.median():+.2f}%');c5.metric('報酬加總',f'{rr.sum():+.2f}%')
        sb=summarize_score_buckets(a2)
        st.markdown('### 🎯 不同分數區間績效');st.dataframe(sb.style.format({'mean':'{:+.2f}%','median':'{:+.2f}%','sum':'{:+.2f}%','勝率%':'{:.1f}%'}),use_container_width=True,hide_index=True)
        st.bar_chart(sb.set_index('分數區間')['mean'])
        st.markdown('### 🧾 A2 回測明細');st.dataframe(a2.round(2),use_container_width=True,hide_index=True)
        st.download_button('⬇️ 匯出 A2 回測 CSV',a2.to_csv(index=False).encode('utf-8-sig'),'A2_composite_backtest.csv','text/csv',key='dl_a2')
    else:st.info('尚未完成 A2 回測。請按「▶ 開始 A2 綜合分數回測」。若完全沒有樣本，請先把最低分數降到 60～65 測試。')

    st.divider()
    st.subheader('🧠 A2.1 綜合分數權重最佳化')
    st.caption('A2.1 採有限、可解釋的權重候選；每檔股票依日期做 70% 訓練／30% 驗證，排序只看驗證集，避免把驗證資料拿來訓練。')
    b1,b2,b3=st.columns(3)
    with b1:a21_h=st.selectbox('A2.1 持有交易日',[1,3,5,10,20],index=2,key='a21_h')
    with b2:a21_min=st.slider('A2.1 最低分數',50,90,70,5,key='a21_min')
    with b3:a21_gap=st.selectbox('A2.1 冷卻天數',[0,3,5,10,20],index=1,key='a21_gap')
    a21_n=st.slider('A2.1 股票數',1,min(100,len(symbols)),min(30,len(symbols)),1,key='a21_n')
    if st.button('▶ 開始 A2.1 權重最佳化',type='primary',key='run_a21'):
        pr=st.progress(0);ss=st.empty();hist=collect_a2_history(symbols[:a21_n],market_map,pr);ss.empty();pr.empty()
        if hist.empty:
            st.session_state['a21_rank']=pd.DataFrame();st.session_state['a21_all']=pd.DataFrame()
        else:
            top,all_rank=a21_optimize_weight_sets(hist,a21_h,a21_min,a21_gap,10)
            st.session_state['a21_rank']=top;st.session_state['a21_all']=all_rank
            st.session_state['a21_hist_n']=len(hist)
    rank=st.session_state.get('a21_rank',pd.DataFrame())
    if not rank.empty:
        st.success(f"A2.1 完成：共建立 {st.session_state.get('a21_hist_n',0):,} 筆歷史樣本；以下為驗證集表現最佳的候選權重。")
        st.markdown('### 🏆 A2.1 推薦權重')
        best=rank.iloc[0]
        best_df=pd.DataFrame({'項目':list(COMPOSITE_WEIGHTS),'原始權重':[COMPOSITE_WEIGHTS[k] for k in COMPOSITE_WEIGHTS],'推薦權重':[int(best[k]) for k in COMPOSITE_WEIGHTS],'差異':[int(best[k]-COMPOSITE_WEIGHTS[k]) for k in COMPOSITE_WEIGHTS],'說明':[A2_COMPONENT_DESC[k] for k in COMPOSITE_WEIGHTS]})
        st.dataframe(best_df,use_container_width=True,hide_index=True)
        st.metric('推薦權重總和',f"{sum(int(best[k]) for k in COMPOSITE_WEIGHTS)} / 100")
        st.markdown('### 📊 A2.1 候選排名（驗證集）')
        show=rank.copy();show['權重摘要']=show.apply(lambda r:' / '.join(f'{k}:{int(r[k])}' for k in COMPOSITE_WEIGHTS),axis=1)
        show=show[['候選編號','樣本數','勝率%','平均報酬%','中位數報酬%','評分','權重摘要']]
        st.dataframe(show.round(3),use_container_width=True,hide_index=True)
        st.download_button('⬇️ 匯出 A2.1 權重最佳化 CSV',st.session_state['a21_all'].to_csv(index=False).encode('utf-8-sig'),'A2.1_weight_optimization.csv','text/csv',key='dl_a21')
    else:st.info('尚未完成 A2.1。請按「▶ 開始 A2.1 權重最佳化」。')

with t8:
    st.subheader('🩺 A2.2 策略健診｜MA15＋KD')
    st.caption('先診斷，再調整權重。比較最低分數、持有交易日、分數×持有期，並拆解90分以上是否有追高／過熱現象。')
    c1,c2,c3,c4=st.columns(4)
    with c1:diag_h=st.selectbox('健診持有交易日',[1,3,5,10,20],index=2,key='diag_h')
    with c2:diag_min=st.slider('健診最低分數',50,90,70,5,key='diag_min')
    with c3:diag_gap=st.selectbox('健診冷卻交易日',[0,3,5,10,20],index=1,key='diag_gap')
    with c4:diag_n=st.number_input('健診股票數',min_value=5,max_value=min(500,len(symbols)),value=min(30,len(symbols)),step=5,key='diag_n')
    if st.button('▶ 執行 A2.2 策略健診',type='primary',key='run_a22'):
        p=st.progress(0);msg=st.empty();hist=collect_a2_history(symbols[:int(diag_n)],market_map,p);p.empty();msg.empty();st.session_state['a22_hist']=prepare_diag_history(hist)
    hist=st.session_state.get('a22_hist',pd.DataFrame())
    if not hist.empty:
        fmt={'勝率%':'{:.1f}%','平均報酬%':'{:+.2f}%','中位數報酬%':'{:+.2f}%','報酬加總%':'{:+.2f}%','Profit Factor':'{:.2f}','Expectancy%':'{:+.2f}%','最大回撤%':'{:+.2f}%','平均獲利%':'{:+.2f}%','平均虧損%':'{:+.2f}%'}
        st.markdown('### ① 最低分數門檻比較')
        td=diagnostic_threshold_table(hist,diag_h,diag_gap);st.dataframe(td.style.format(fmt),use_container_width=True,hide_index=True)
        st.download_button('⬇️ 匯出門檻健診 CSV',td.to_csv(index=False).encode('utf-8-sig'),'A2.2_threshold.csv','text/csv',key='a22_dl1')
        st.markdown('### ② 持有天數比較')
        hd=diagnostic_horizon_table(hist,diag_min,diag_gap);st.dataframe(hd.style.format(fmt),use_container_width=True,hide_index=True)
        st.bar_chart(hd.set_index('持有交易日')['平均報酬%'])
        st.markdown('### ③ 分數 × 持有天數矩陣')
        md=diagnostic_matrix(hist,diag_gap);pv=md.pivot(index='最低分數',columns='持有交易日',values='平均報酬%');st.dataframe(pv.style.format('{:+.2f}%'),use_container_width=True)
        st.download_button('⬇️ 匯出分數×持有天數 CSV',md.to_csv(index=False).encode('utf-8-sig'),'A2.2_score_horizon.csv','text/csv',key='a22_dl2')
        st.markdown('### ④ 90分以上過熱健診')
        od=diagnostic_overheat(hist,90,diag_gap,diag_h)
        if od.empty:st.info('目前90分以上樣本不足，先不要對90+下結論。')
        else:st.dataframe(od.style.format(fmt),use_container_width=True,hide_index=True)
        st.markdown('### ⑤ 健診摘要')
        vt=td.dropna(subset=['平均報酬%']);vh=hd.dropna(subset=['平均報酬%']);a,b,c=st.columns(3)
        if not vt.empty:z=vt.sort_values(['平均報酬%','Profit Factor'],ascending=False).iloc[0];a.metric('平均報酬最高門檻',f"≥{int(z['最低分數'])}分",f"{z['平均報酬%']:+.2f}%")
        if not vh.empty:z=vh.sort_values(['平均報酬%','Profit Factor'],ascending=False).iloc[0];b.metric('平均報酬最高持有期',f"{int(z['持有交易日'])}日",f"{z['平均報酬%']:+.2f}%")
        c.metric('歷史事件數',f"{len(hist):,}")
        st.warning('研究性回測：目前股票池存在存活者偏差；歷史智能項目為OHLCV代理；未計手續費、交易稅、滑價與漲跌停。')
    else:st.info('尚未完成 A2.2。建議先用20～30檔測試，確認流程後再擴大。')


with t9:
    st.subheader('🧪 A2.3.4 Strategy Lab Pro｜八策略公平 PK＋穩健度＋甜蜜區')
    st.caption('同一批股票、同一段歷史、同一進出場規則，只比較「MA15 / MA20」與「RSI / KD」。目標是找出真正的甜蜜點，而不是假設最高分一定最好。')

    st.markdown('### 🧬 本版比較的八套策略')
    strategy_desc=pd.DataFrame([
        {'策略':'MA15＋RSI','快均線':'MA15','動能':'RSI14','用途':'快速趨勢＋傳統動能'},
        {'策略':'MA20＋RSI','快均線':'MA20','動能':'RSI14','用途':'短中期趨勢＋傳統動能'},
        {'策略':'MA30＋RSI','快均線':'MA30','動能':'RSI14','用途':'中期趨勢＋傳統動能'},
        {'策略':'MA60＋RSI','快均線':'MA60','動能':'RSI14','用途':'波段趨勢＋傳統動能'},
        {'策略':'MA15＋KD','快均線':'MA15','動能':'9日KD','用途':'快速趨勢＋KD轉折'},
        {'策略':'MA20＋KD','快均線':'MA20','動能':'9日KD','用途':'短中期趨勢＋KD轉折'},
        {'策略':'MA30＋KD','快均線':'MA30','動能':'9日KD','用途':'中期趨勢＋KD轉折'},
        {'策略':'MA60＋KD','快均線':'MA60','動能':'9日KD','用途':'波段趨勢＋KD轉折'}
    ])
    st.dataframe(strategy_desc,use_container_width=True,hide_index=True)
    st.info('A2.3.3 新增 MA30 與 MA60，與 MA15/MA20 公平 PK；MA200 生命線仍保留。MA60 主要用來測試較慢的波段趨勢濾網。')

    a,b,c,d=st.columns(4)
    with a:
        a23_n=st.number_input('A2.3 股票數',min_value=5,max_value=min(300,len(symbols)),value=min(20,len(symbols)),step=5,key='a23_n')
    with b:
        a23_gap=st.selectbox('冷卻交易日',[0,3,5,10,20],index=1,key='a23_gap')
    with c:
        a23_min_samples=st.number_input('最低有效樣本數',min_value=5,max_value=500,value=20,step=5,key='a23_min_samples')
    with d:
        st.metric('策略組合','8 套')

    a23_thresholds=st.multiselect('要比較的最低分數',[50,55,60,65,70,75,80,85,90],default=[65,70,75,80,85,90],key='a23_thresholds')
    a23_horizons=st.multiselect('要比較的持有交易日',[1,3,5,10,20],default=[3,5,10,20],key='a23_horizons')

    total_cases=max(len(a23_thresholds),1)*max(len(a23_horizons),1)*len(A23_STRATEGIES)
    st.info(f'本次最多比較 {total_cases} 組條件。建議先用 20～30 檔確認流程，再逐步擴大股票數。')

    if st.button('▶ 執行 A2.3 八策略 PK',type='primary',key='run_a23'):
        if not a23_thresholds or not a23_horizons:
            st.warning('最低分數與持有交易日都至少要選一個。')
        else:
            p=st.progress(0);status23=st.empty()
            hist23=collect_strategy_lab_history(symbols[:int(a23_n)],market_map,p,status23)
            p.empty();status23.empty()
            if hist23.empty:
                st.session_state['a23_hist']=pd.DataFrame()
                st.session_state['a23_grid']=pd.DataFrame()
                st.session_state['a23_band_grid']=pd.DataFrame()
                st.session_state['a23_strategy_signature']=A23_STRATEGY_SIGNATURE
            else:
                grid23=strategy_lab_grid_pro(hist23,a23_thresholds,a23_horizons,int(a23_gap))
                band23=strategy_lab_band_grid(hist23,a23_horizons,int(a23_gap))
                st.session_state['a23_hist']=hist23
                st.session_state['a23_grid']=grid23
                st.session_state['a23_band_grid']=band23
                st.session_state['a23_settings']={
                    '股票數':int(a23_n),'冷卻':int(a23_gap),
                    '門檻':list(a23_thresholds),'持有日':list(a23_horizons)
                }
                st.session_state['a23_strategy_signature']=A23_STRATEGY_SIGNATURE

    # A2.3.4：避免 Streamlit 沿用舊版四策略 session_state。
    # 若策略集合有變更，就自動清除舊 A2.3 回測結果，要求重新執行。
    saved_sig=st.session_state.get('a23_strategy_signature')
    if saved_sig is not None and saved_sig != A23_STRATEGY_SIGNATURE:
        for _k in ['a23_hist','a23_grid','a23_band_grid','a23_settings']:
            st.session_state.pop(_k,None)

    grid23=st.session_state.get('a23_grid',pd.DataFrame())
    hist23=st.session_state.get('a23_hist',pd.DataFrame())
    band23=st.session_state.get('a23_band_grid',pd.DataFrame())

    if not grid23.empty:
        fmt23={'勝率%':'{:.1f}%','平均報酬%':'{:+.2f}%','中位數報酬%':'{:+.2f}%',
               '報酬加總%':'{:+.2f}%','Profit Factor':'{:.2f}','Expectancy%':'{:+.2f}%',
               '最大回撤%':'{:+.2f}%','平均獲利%':'{:+.2f}%','平均虧損%':'{:+.2f}%'}

        st.markdown('### 🏆 ① 八策略最佳組合')
        sm23=strategy_lab_summary(grid23,int(a23_min_samples))
        if sm23.empty or sm23['平均報酬%'].notna().sum()==0:
            st.warning('目前沒有任何組合達到最低有效樣本數。可降低「最低有效樣本數」或增加回測股票數。')
        else:
            st.dataframe(sm23.style.format({
                '勝率%':'{:.1f}%','平均報酬%':'{:+.2f}%','中位數報酬%':'{:+.2f}%',
                'Profit Factor':'{:.2f}','最大回撤%':'{:+.2f}%'
            }),use_container_width=True,hide_index=True)
            winner=sm23.dropna(subset=['平均報酬%']).iloc[0]
            c1,c2,c3,c4=st.columns(4)
            c1.metric('目前冠軍',winner['策略'])
            c2.metric('最佳最低分數',f"≥{int(winner['最佳最低分數'])} 分")
            c3.metric('最佳持有期',f"{int(winner['最佳持有日'])} 日")
            c4.metric('平均報酬',f"{winner['平均報酬%']:+.2f}%",f"PF {winner['Profit Factor']:.2f}")

        st.markdown('### 🥇 ② 全條件排行榜')
        rank23=strategy_lab_rank(grid23,int(a23_min_samples))
        if rank23.empty:
            st.info('沒有達到最低樣本數的有效組合。')
        else:
            cols23=['排名','策略','最低分數','持有交易日','樣本數','勝率%','平均報酬%','中位數報酬%','Profit Factor','最大回撤%','平均獲利%','平均虧損%']
            st.dataframe(rank23[cols23].head(30).style.format(fmt23),use_container_width=True,hide_index=True)

        st.markdown('### 🗺️ ③ 策略 × 分數 × 持有期矩陣')
        metric23=st.selectbox('矩陣顯示指標',['平均報酬%','勝率%','Profit Factor','最大回撤%','樣本數'],index=0,key='a23_metric')
        strategy23=st.selectbox('查看策略',list(A23_STRATEGIES.keys()),index=list(A23_STRATEGIES.keys()).index('MA15＋KD'),key='a23_strategy_view')
        sub23=grid23[grid23['策略']==strategy23]
        if not sub23.empty:
            pv23=sub23.pivot(index='最低分數',columns='持有交易日',values=metric23)
            if metric23 in ['平均報酬%','最大回撤%']:
                st.dataframe(pv23.style.format('{:+.2f}%'),use_container_width=True)
            elif metric23=='勝率%':
                st.dataframe(pv23.style.format('{:.1f}%'),use_container_width=True)
            elif metric23=='Profit Factor':
                st.dataframe(pv23.style.format('{:.2f}'),use_container_width=True)
            else:
                st.dataframe(pv23.style.format('{:.0f}'),use_container_width=True)

        st.markdown('### 📊 ④ 八策略在相同條件下直接 PK')
        q1,q2=st.columns(2)
        with q1:
            common_th=st.selectbox('固定最低分數',sorted(grid23['最低分數'].unique().tolist()),index=0,key='a23_common_th')
        with q2:
            common_h=st.selectbox('固定持有日',sorted(grid23['持有交易日'].unique().tolist()),index=0,key='a23_common_h')
        direct=grid23[(grid23['最低分數']==common_th)&(grid23['持有交易日']==common_h)].copy()
        direct=direct.sort_values(['平均報酬%','Profit Factor'],ascending=False)
        st.dataframe(direct[['策略','樣本數','勝率%','平均報酬%','中位數報酬%','Profit Factor','最大回撤%']].style.format(fmt23),use_container_width=True,hide_index=True)
        if not direct.empty:
            st.bar_chart(direct.set_index('策略')['平均報酬%'])


        st.markdown('### 🛡️ ⑤ A2.3.2 穩健度排名｜加入 95% 信賴區間')
        robust23=strategy_lab_robust_rank(grid23,int(a23_min_samples))
        if robust23.empty:
            st.info('目前沒有足夠樣本可做穩健排名。')
        else:
            robust_cols=[
                '穩健排名','穩健候選','策略','最低分數','持有交易日','樣本數',
                '勝率%','平均報酬%','平均報酬95%CI下限','平均報酬95%CI上限',
                'Profit Factor','最大回撤%'
            ]
            st.dataframe(
                robust23[robust_cols].head(30).style.format({
                    '勝率%':'{:.1f}%','平均報酬%':'{:+.2f}%',
                    '平均報酬95%CI下限':'{:+.2f}%','平均報酬95%CI上限':'{:+.2f}%',
                    'Profit Factor':'{:.2f}','最大回撤%':'{:+.2f}%'
                }),
                use_container_width=True,hide_index=True
            )
            robust_ok=robust23[robust23['穩健候選']=='✅']
            if not robust_ok.empty:
                rb=robust_ok.iloc[0]
                st.success(
                    f"目前較穩健候選：{rb['策略']}｜最低 {int(rb['最低分數'])} 分｜"
                    f"持有 {int(rb['持有交易日'])} 日｜平均 {rb['平均報酬%']:+.2f}%｜"
                    f"95% CI 下限 {rb['平均報酬95%CI下限']:+.2f}%｜PF {rb['Profit Factor']:.2f}"
                )
            else:
                st.warning('目前沒有組合的「平均報酬 95% CI 下限 > 0」。這不代表策略無效，但代表證據還不夠穩健，建議增加股票數與樣本。')

        st.markdown('### 🎯 ⑥ 真正分數甜蜜區｜70–74、75–79、80–84 分開看')
        if band23 is None or band23.empty:
            st.info('尚無分數區間資料，請重新執行 A2.3。')
        else:
            sweet23=strategy_lab_band_summary(band23,int(a23_min_samples))
            if sweet23.empty:
                st.info('目前各分數區間樣本不足。')
            else:
                st.dataframe(
                    sweet23.style.format({
                        '勝率%':'{:.1f}%','平均報酬%':'{:+.2f}%',
                        '95%CI下限':'{:+.2f}%','95%CI上限':'{:+.2f}%',
                        'Profit Factor':'{:.2f}','最大回撤%':'{:+.2f}%'
                    }),
                    use_container_width=True,hide_index=True
                )

            b1,b2=st.columns(2)
            with b1:
                band_strategy=st.selectbox('甜蜜區策略',list(A23_STRATEGIES.keys()),index=list(A23_STRATEGIES.keys()).index('MA15＋KD'),key='a232_band_strategy')
            with b2:
                band_horizon=st.selectbox('甜蜜區持有日',sorted(band23['持有交易日'].dropna().unique().tolist()),key='a232_band_horizon')
            view_band=band23[(band23['策略']==band_strategy)&(band23['持有交易日']==band_horizon)].copy()
            order_map={v:i for i,v in enumerate(A232_SCORE_LABELS)}
            view_band['_order']=view_band['分數區間'].map(order_map)
            view_band=view_band.sort_values('_order').drop(columns=['_order'])
            if not view_band.empty:
                st.dataframe(
                    view_band[['分數區間','樣本數','勝率%','平均報酬%','平均報酬95%CI下限',
                               '平均報酬95%CI上限','Profit Factor','最大回撤%']].style.format({
                        '勝率%':'{:.1f}%','平均報酬%':'{:+.2f}%',
                        '平均報酬95%CI下限':'{:+.2f}%','平均報酬95%CI上限':'{:+.2f}%',
                        'Profit Factor':'{:.2f}','最大回撤%':'{:+.2f}%'
                    }),
                    use_container_width=True,hide_index=True
                )
                st.bar_chart(view_band.set_index('分數區間')['平均報酬%'])

        st.markdown('### 🔬 ⑦ 研究結論提醒')
        st.write('A2.3.2 不只挑「平均報酬最高」；現在同時看樣本數、Profit Factor、最大回撤、勝率與 95% 信賴區間。若冠軍只靠很少樣本，先視為候選，不直接定版。')
        st.warning('A2.3 仍屬研究性回測：目前股票池存在存活者偏差；智能四項是 OHLCV 歷史代理；未計手續費、交易稅、滑價、漲跌停與實際資金部位。下一階段應做 A2.4 走勢外樣本／投資組合回測。')

        st.download_button('⬇️ 匯出 A2.3 全策略比較 CSV',
                           grid23.to_csv(index=False).encode('utf-8-sig'),
                           'A2.3_strategy_lab_grid.csv','text/csv',key='a23_dl_grid')
        if not hist23.empty:
            st.download_button('⬇️ 匯出 A2.3 歷史分數明細 CSV',
                               hist23.to_csv(index=False).encode('utf-8-sig'),
                               'A2.3_strategy_lab_history.csv','text/csv',key='a23_dl_hist')
        if band23 is not None and not band23.empty:
            st.download_button('⬇️ 匯出 A2.3.3 分數甜蜜區 CSV',
                               band23.to_csv(index=False).encode('utf-8-sig'),
                               'A2.3.3_score_band_analysis.csv','text/csv',key='a232_dl_band')
    else:
        st.info('尚未完成 A2.3。按「▶ 執行 A2.3 八策略 PK」開始比較。')


st.divider();st.caption('🖤 黑嚕嚕 V3.3.2 A2.3.4｜Strategy Lab Pro 八策略PK＋A2.2策略健診＋MA15/KD 基準＋智能掃描2.0；V4 再接 Fugle 即時行情。');st.caption('⚠️ 本工具僅供研究與技術分析，不構成投資建議。')
