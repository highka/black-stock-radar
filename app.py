import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import requests
import re
try:
    import plotly.graph_objects as go
    PLOTLY_OK = True
except ModuleNotFoundError:
    go = None
    PLOTLY_OK = False
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh
import json

# ============================================================
# 🖤 黑嚕嚕－台股盤中雷達 V3.6.12
# V3.6.12：Fugle 5秒快照＋即時未完成日K注入＋Yahoo歷史日K＋官方行情備援＋A2.3.5可靠度驗證
# ============================================================

st.set_page_config(page_title='🖤 黑嚕嚕－台股盤中雷達', page_icon='🖤', layout='wide', initial_sidebar_state='expanded')

V3_6_13_LABEL = 'V3.6.16｜Gate D 資金配置鎖定 / MTM 穩健度壓力測試'

st.markdown('''
<style>
.block-container{padding-top:1rem;padding-bottom:2rem}
[data-testid="stMetric"]{border:1px solid rgba(128,128,128,.25);border-radius:12px;padding:10px}
.radar-card{border:1px solid rgba(128,128,128,.30);border-radius:14px;padding:14px 16px;margin-bottom:10px;min-height:170px}
.radar-title{font-size:19px;font-weight:800}.radar-price{font-size:28px;font-weight:900;margin:4px 0}.radar-score{font-size:21px;font-weight:800;margin-top:6px}.small{opacity:.70;font-size:12px}.signal{font-weight:800;font-size:15px}
</style>''', unsafe_allow_html=True)


TAIWAN_TZ = ZoneInfo('Asia/Taipei')

def taiwan_now():
    return datetime.now(TAIWAN_TZ)

def taiwan_time_text(dt=None):
    dt=dt or taiwan_now()
    return dt.strftime('%Y-%m-%d %H:%M:%S')

def taiwan_market_session(dt=None):
    dt=dt or taiwan_now()
    if dt.weekday()>=5:return '休市日'
    hm=dt.hour*60+dt.minute
    if hm<540:return '開盤前'
    if hm<=810:return '盤中'
    return '收盤後'

def universe_effective_key(dt=None):
    dt=dt or taiwan_now()
    effective=dt.date() if dt.hour>=18 else (dt-timedelta(days=1)).date()
    return effective.isoformat()


# ============================================================
# ⚡ V3.6.12 Fugle 即時行情層
# Fugle 官方文件：
#   /snapshot/quotes/TSE / OTC / ESB 約每 5 秒更新
# API Key 建議放在 Streamlit Secrets：
#   FUGLE_API_KEY = "..."
# 若未設定或方案不支援 snapshot，系統自動退回官方日行情 / Yahoo 日K。
# ============================================================

def get_secret_value(name, default=''):
    try:
        return str(st.secrets[name]).strip()
    except Exception:
        return default

def epoch_to_taiwan_text(v):
    try:
        x=float(v)
        if x<=0:return ''
        # Fugle 範例為 microseconds；同時相容 milliseconds / seconds
        if x>1e14:
            dt=pd.to_datetime(int(x),unit='us',utc=True)
        elif x>1e11:
            dt=pd.to_datetime(int(x),unit='ms',utc=True)
        else:
            dt=pd.to_datetime(x,unit='s',utc=True)
        return dt.tz_convert(TAIWAN_TZ).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return ''

def quote_age_seconds(time_text, now=None):
    if not time_text:return np.nan
    try:
        now=now or taiwan_now()
        dt=datetime.strptime(time_text,'%Y-%m-%d %H:%M:%S').replace(tzinfo=TAIWAN_TZ)
        return max(0.0,(now-dt).total_seconds())
    except Exception:
        return np.nan

def quote_freshness_label(time_text, source=''):
    if source.startswith('Fugle'):
        if not time_text:return '🟠 Fugle 無時間'
        age=quote_age_seconds(time_text)
        if pd.notna(age) and age<=90:return '🟢 即時'
        if pd.notna(age) and age<=600:return '🟡 稍延遲'
        return '🟠 Fugle 舊快照'
    if source in ('官方日行情','TWSE每日收盤行情'):
        return '🔴 非即時｜官方最新盤後價'
    if source=='Yahoo Finance 日K':
        return '🔴 非即時｜Yahoo 最新日K'
    return '🔴 非即時｜最新可用價'

@st.cache_data(ttl=10, show_spinner=False)
def load_fugle_snapshot(markets_tuple, _api_key=''):
    """以市場別一次抓整批 Fugle 快照，避免逐檔打 API。"""
    cols=['股票代號','股票名稱','市場','收盤價','漲跌','漲跌幅%','成交量',
          '成交額','開盤價','最高價','最低價','報價日期','報價時間','行情來源']
    if not _api_key:
        return pd.DataFrame(columns=cols), ['Fugle：未設定 API Key'], ''

    market_code={'上市':'TSE','上櫃':'OTC','興櫃':'ESB'}
    rows=[];status=[];latest_time=''
    headers={'X-API-KEY':_api_key,'Accept':'application/json'}
    for market in markets_tuple:
        code=market_code.get(market)
        if not code:continue
        url=f'https://api.fugle.tw/marketdata/v1.0/stock/snapshot/quotes/{code}'
        try:
            r=requests.get(url,headers=headers,params={'type':'COMMONSTOCK'},timeout=15)
            if r.status_code in (401,403):
                raise RuntimeError(f'HTTP {r.status_code}：API Key 或方案無 snapshot 權限')
            r.raise_for_status()
            payload=r.json()
            data=payload.get('data',[]) if isinstance(payload,dict) else []
            pdate=str(payload.get('date','')) if isinstance(payload,dict) else ''
            ptime=str(payload.get('time','')) if isinstance(payload,dict) else ''
            n=0
            for item in data:
                if not isinstance(item,dict):continue
                sym=str(item.get('symbol','')).strip()
                if not re.fullmatch(r'\d{4,6}',sym):continue
                close=pd.to_numeric(item.get('closePrice'),errors='coerce')
                if pd.isna(close) or float(close)<=0:continue
                # Fugle tradeVolume 以台股成交量常用「張」表示；轉成股數與 yfinance 對齊
                vol_raw=pd.to_numeric(item.get('tradeVolume'),errors='coerce')
                vol_shares=float(vol_raw)*1000 if pd.notna(vol_raw) else np.nan
                last_text=epoch_to_taiwan_text(item.get('lastUpdated'))
                if last_text and (not latest_time or last_text>latest_time):
                    latest_time=last_text
                rows.append({
                    '股票代號':sym.zfill(4),'股票名稱':str(item.get('name','')).strip(),'市場':market,
                    '收盤價':float(close),
                    '漲跌':pd.to_numeric(item.get('change'),errors='coerce'),
                    '漲跌幅%':pd.to_numeric(item.get('changePercent'),errors='coerce'),
                    '成交量':vol_shares,
                    '成交額':pd.to_numeric(item.get('tradeValue'),errors='coerce'),
                    '開盤價':pd.to_numeric(item.get('openPrice'),errors='coerce'),
                    '最高價':pd.to_numeric(item.get('highPrice'),errors='coerce'),
                    '最低價':pd.to_numeric(item.get('lowPrice'),errors='coerce'),
                    '報價日期':pdate,
                    '報價時間':last_text or (f'{pdate} {ptime[:2]}:{ptime[2:4]}:{ptime[4:6]}' if len(ptime)>=6 else ''),
                    '行情來源':'Fugle 5秒快照'
                })
                n+=1
            status.append(f'Fugle {market}：{n} 檔')
        except Exception as e:
            status.append(f'Fugle {market}：失敗（{str(e)[:90]}）')
    d=pd.DataFrame(rows,columns=cols)
    if not d.empty:
        d=d.drop_duplicates(['股票代號','市場'],keep='last')
    return d,status,latest_time or taiwan_time_text()


@st.cache_data(ttl=5, show_spinner=False)
def fugle_intraday_quote(symbol, _api_key=''):
    if not _api_key:return None,'未設定 Fugle API Key'
    try:
        url=f'https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/{str(symbol).zfill(4)}'
        r=requests.get(url,headers={'X-API-KEY':_api_key,'Accept':'application/json'},timeout=15)
        if r.status_code in (401,403):return None,f'HTTP {r.status_code}：API Key 或方案權限不足'
        if r.status_code==429:return None,'HTTP 429：API 已達速率限制'
        r.raise_for_status();x=r.json()
        total=x.get('total',{}) if isinstance(x.get('total',{}),dict) else {}
        def num(k):
            return pd.to_numeric(x.get(k),errors='coerce')
        return {
            '股票':str(x.get('symbol',symbol)),'名稱':str(x.get('name','')),'日期':str(x.get('date','')),
            'lastPrice':num('lastPrice'),'closePrice':num('closePrice'),'previousClose':num('previousClose'),
            'openPrice':num('openPrice'),'highPrice':num('highPrice'),'lowPrice':num('lowPrice'),
            'change':num('change'),'changePercent':num('changePercent'),
            'tradeVolume':pd.to_numeric(total.get('tradeVolume'),errors='coerce'),
            'lastUpdated':epoch_to_taiwan_text(x.get('lastUpdated')),'isClose':x.get('isClose',None)
        },'OK'
    except Exception as e:return None,f'{type(e).__name__}: {str(e)[:140]}'

def expected_quote_date_tw(now=None):
    now=now or taiwan_now()
    if now.weekday()>=5:return None
    return now.strftime('%Y-%m-%d') if now.hour*60+now.minute>=540 else None

def quote_date_health(qdate,source,now=None):
    expected=expected_quote_date_tw(now);qdate=str(qdate or '')
    if source=='Fugle 5秒快照' and expected:
        return '🟢 當日行情' if qdate==expected else f'🔴 日期落後（{qdate or "未知"}）'
    if source=='Fugle 5秒快照':return '🟢 Fugle'
    if source in ('官方日行情','TWSE每日收盤行情'):return f'🔴 盤後備援（{qdate or "日期未提供"}）'
    if source=='Yahoo Finance 日K':return f'🔴 歷史日K備援（{qdate or "看技術資料日"}）'
    return '🔴 非即時備援'

def combine_quote_snapshots(fugle_df, official_df):
    """Fugle 優先；缺漏股票再由官方日行情補足。"""
    frames=[]
    if fugle_df is not None and not fugle_df.empty:
        f=fugle_df.copy()
        frames.append(f)
    if official_df is not None and not official_df.empty:
        o=official_df.copy()
        if '行情來源' not in o.columns:o['行情來源']='官方日行情'
        if '報價日期' not in o.columns:o['報價日期']=''
        if '報價時間' not in o.columns:o['報價時間']=''
        if '開盤價' not in o.columns:o['開盤價']=np.nan
        frames.append(o)
    if not frames:return pd.DataFrame()
    d=pd.concat(frames,ignore_index=True,sort=False)
    # Fugle 先 append，所以 keep first
    return d.drop_duplicates(['股票代號','市場'],keep='first').reset_index(drop=True)


def expected_completed_market_date(now=None):
    """推估最近已完成交易日。18:00 後可使用當日官方盤後資料；凌晨則回推前一工作日。"""
    now=now or taiwan_now()
    d=now.date()
    # 18:00 前，當天完整官方盤後資料尚不一定齊全，先回推一天
    if now.hour < 18:
        d=d-timedelta(days=1)
    while d.weekday()>=5:
        d=d-timedelta(days=1)
    return pd.Timestamp(d)


@st.cache_data(ttl=900, show_spinner=False)
def load_twse_daily_report():
    """
    直接讀 TWSE 每日收盤行情 MI_INDEX（ALLBUT0999）。
    這個來源包含：成交股數、開盤、最高、最低、收盤、漲跌。
    用來修正 STOCK_DAY_ALL / yfinance 更新延遲造成的最後一根K棒落後。
    """
    cols=['股票代號','股票名稱','市場','收盤價','漲跌','漲跌幅%','成交量','成交額',
          '開盤價','最高價','最低價','報價日期','報價時間','行情來源']
    try:
        url='https://www.twse.com.tw/exchangeReport/MI_INDEX'
        r=requests.get(url,params={'response':'json','type':'ALLBUT0999'},timeout=20,
                       headers={'User-Agent':'Mozilla/5.0'})
        r.raise_for_status()
        x=r.json()
        if str(x.get('stat','')).upper()!='OK':
            return pd.DataFrame(columns=cols),f"TWSE MI_INDEX：{x.get('stat','非OK')}",''
        tables=x.get('tables',[])
        target=None
        for tb in tables:
            fields=tb.get('fields',[])
            if '證券代號' in fields and '收盤價' in fields and '開盤價' in fields:
                target=tb;break
        if not target:
            return pd.DataFrame(columns=cols),'TWSE MI_INDEX：找不到個股行情表',''
        fields=target['fields']
        rows=[]
        def clean_num(v):
            s=str(v).replace(',','').replace('--','').strip()
            return pd.to_numeric(s,errors='coerce')
        for vals in target.get('data',[]):
            item=dict(zip(fields,vals))
            code=str(item.get('證券代號','')).strip()
            if not re.fullmatch(r'\d{4,6}',code):continue
            close=clean_num(item.get('收盤價'))
            if pd.isna(close):continue
            sign=str(item.get('漲跌(+/-)',''))
            chg=clean_num(item.get('漲跌價差'))
            if pd.notna(chg) and ('-' in sign or '－' in sign):
                chg=-abs(float(chg))
            elif pd.notna(chg):
                chg=abs(float(chg))
            prev=float(close)-float(chg) if pd.notna(chg) else np.nan
            rows.append({
                '股票代號':code.zfill(4),'股票名稱':str(item.get('證券名稱','')).strip(),'市場':'上市',
                '收盤價':float(close),'漲跌':chg,
                '漲跌幅%':float(chg)/prev*100 if pd.notna(chg) and prev>0 else np.nan,
                '成交量':clean_num(item.get('成交股數')),'成交額':clean_num(item.get('成交金額')),
                '開盤價':clean_num(item.get('開盤價')),'最高價':clean_num(item.get('最高價')),
                '最低價':clean_num(item.get('最低價')),
                '報價日期':expected_completed_market_date().strftime('%Y-%m-%d'),
                '報價時間':taiwan_time_text(),'行情來源':'TWSE每日收盤行情'
            })
        d=pd.DataFrame(rows,columns=cols)
        return d,f'TWSE MI_INDEX：{len(d)} 檔',taiwan_time_text()
    except Exception as e:
        return pd.DataFrame(columns=cols),f'TWSE MI_INDEX：失敗（{type(e).__name__}: {str(e)[:90]}）',''

def inject_official_eod_bar(df, quote_row):
    """
    B模式盤後修正：
    Fugle 不可用時，若有 TWSE/TPEx 官方盤後行情，
    將最近已完成交易日 OHLCV 補入 Yahoo 歷史日K，讓個股圖與技術指標不再卡在前一日。
    """
    if df is None or quote_row is None:return df,False,''
    if str(quote_row.get('行情來源','')) not in ('官方日行情','TWSE每日收盤行情'):return df,False,''
    try:
        close=pd.to_numeric(quote_row.get('收盤價'),errors='coerce')
        if pd.isna(close) or float(close)<=0:return df,False,''
        dt=expected_completed_market_date()
        op=pd.to_numeric(quote_row.get('開盤價'),errors='coerce')
        hi=pd.to_numeric(quote_row.get('最高價'),errors='coerce')
        lo=pd.to_numeric(quote_row.get('最低價'),errors='coerce')
        vol=pd.to_numeric(quote_row.get('成交量'),errors='coerce')
        # 官方快照若缺開盤價，以 Yahoo 同日值優先，否則用 close 補齊
        out=df.copy()
        same=[x for x in out.index if pd.Timestamp(x).normalize()==dt.normalize()]
        if same:
            old=out.loc[same[-1]]
            if pd.isna(op) or op<=0:op=pd.to_numeric(old.get('Open'),errors='coerce')
            if pd.isna(hi) or hi<=0:hi=pd.to_numeric(old.get('High'),errors='coerce')
            if pd.isna(lo) or lo<=0:lo=pd.to_numeric(old.get('Low'),errors='coerce')
        op=float(op) if pd.notna(op) and op>0 else float(close)
        hi=float(hi) if pd.notna(hi) and hi>0 else max(op,float(close))
        lo=float(lo) if pd.notna(lo) and lo>0 else min(op,float(close))
        vol=float(vol) if pd.notna(vol) and vol>=0 else 0.0
        row=pd.DataFrame({'Open':[op],'High':[hi],'Low':[lo],'Close':[float(close)],'Volume':[vol]},index=[dt])
        if same:
            out.loc[same[-1],['Open','High','Low','Close','Volume']]=[op,hi,lo,float(close),vol]
        else:
            out=pd.concat([out,row])
        out=out[~out.index.duplicated(keep='last')].sort_index()
        out.attrs.update(df.attrs)
        out.attrs['official_eod_bar']=True
        out.attrs['official_eod_date']=dt.strftime('%Y-%m-%d')
        return out,True,dt.strftime('%Y-%m-%d')
    except Exception:
        return df,False,''

def inject_live_daily_bar(df, quote_row):
    """
    把 Fugle 今日 OHLCV 當成「未完成日K」暫時注入 2 年歷史，
    讓 MA / KD / RSI / 突破 / 量比在盤中跟著目前行情重算。
    不修改原始 Yahoo 歷史資料。
    """
    if df is None or quote_row is None:return df,False
    if str(quote_row.get('行情來源',''))!='Fugle 5秒快照':
        return df,False
    try:
        qdate=str(quote_row.get('報價日期','')).strip()
        if not qdate:return df,False
        dt=pd.Timestamp(qdate)
        close=pd.to_numeric(quote_row.get('收盤價'),errors='coerce')
        op=pd.to_numeric(quote_row.get('開盤價'),errors='coerce')
        hi=pd.to_numeric(quote_row.get('最高價'),errors='coerce')
        lo=pd.to_numeric(quote_row.get('最低價'),errors='coerce')
        vol=pd.to_numeric(quote_row.get('成交量'),errors='coerce')
        if pd.isna(close) or close<=0:return df,False

        out=df.copy()
        # 未提供 OHLC 時以目前價補齊，避免產生 NaN 技術指標
        op=float(op) if pd.notna(op) and op>0 else float(close)
        hi=float(hi) if pd.notna(hi) and hi>0 else max(op,float(close))
        lo=float(lo) if pd.notna(lo) and lo>0 else min(op,float(close))
        vol=float(vol) if pd.notna(vol) and vol>=0 else 0.0
        row=pd.DataFrame({'Open':[op],'High':[hi],'Low':[lo],'Close':[float(close)],'Volume':[vol]},index=[dt])

        # 同一天已存在 yfinance 未完成bar就覆蓋，否則 append
        same=[x for x in out.index if pd.Timestamp(x).normalize()==dt.normalize()]
        if same:
            out.loc[same[-1],['Open','High','Low','Close','Volume']]=[op,hi,lo,float(close),vol]
        else:
            out=pd.concat([out,row])
        out=out[~out.index.duplicated(keep='last')].sort_index()
        out.attrs.update(df.attrs)
        out.attrs['live_bar']='Fugle'
        out.attrs['live_quote_time']=str(quote_row.get('報價時間',''))
        return out,True
    except Exception:
        return df,False

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
def load_market_universe(refresh_key=None):
    """
    V3.6.12 多來源股票池：
    每個市場各自嘗試「公司基本資料 API」；若數量異常或失敗，
    再用「每日行情 API」建立交易中股票池。
    不再因為只有某一市場成功幾十檔，就誤認為是完整全市場。
    """
    primary_sources = {
        '上市':'https://openapi.twse.com.tw/v1/opendata/t187ap03_L',
        '上櫃':'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O',
        '興櫃':'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_R',
    }
    fallback_sources = {
        '上市':'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL',
        '上櫃':'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes',
        '興櫃':'https://www.tpex.org.tw/openapi/v1/tpex_esb_latest_statistics',
    }

    # 僅用來判定「明顯不完整」，不是策略篩選。
    suspicious_min = {'上市':300,'上櫃':200,'興櫃':20}

    def pick(item, keys):
        for k in keys:
            if k in item and item[k] not in (None,''):
                return item[k]
        return ''

    def normalize_records(data):
        if isinstance(data,dict):
            for key in ['data','results','aaData']:
                if isinstance(data.get(key),list):
                    return data[key]
        return data if isinstance(data,list) else []

    def parse_primary(market, data):
        rows=[]
        for item in normalize_records(data):
            if not isinstance(item,dict):
                continue
            code=str(pick(item,[
                '公司代號','SecuritiesCompanyCode','Code','股票代號','證券代號'
            ])).strip().upper()
            name=str(pick(item,[
                '公司簡稱','CompanyAbbreviation','Name','公司名稱','CompanyName','股票名稱','證券名稱'
            ])).strip()
            if re.fullmatch(r'[0-9A-Z]{4,6}',code) and name:
                rows.append({'股票代號':code,'股票名稱':name,'市場':market})
        return pd.DataFrame(rows,columns=['股票代號','股票名稱','市場']).drop_duplicates('股票代號')

    def parse_fallback(market, data):
        rows=[]
        for item in normalize_records(data):
            if not isinstance(item,dict):
                continue
            code=str(pick(item,[
                'Code','SecuritiesCompanyCode','公司代號','股票代號','證券代號'
            ])).strip().upper()
            name=str(pick(item,[
                'Name','CompanyAbbreviation','公司簡稱','股票名稱','證券名稱','CompanyName'
            ])).strip()
            if re.fullmatch(r'[0-9A-Z]{4,6}',code):
                # 行情API有時名稱欄缺值，代號仍可用於OOS下載
                rows.append({'股票代號':code,'股票名稱':name or code,'市場':market})
        return pd.DataFrame(rows,columns=['股票代號','股票名稱','市場']).drop_duplicates('股票代號')

    market_frames=[]
    source_status=[]

    for market in ['上市','上櫃','興櫃']:
        primary_df=pd.DataFrame(columns=['股票代號','股票名稱','市場'])
        fallback_df=pd.DataFrame(columns=['股票代號','股票名稱','市場'])
        primary_msg=''
        fallback_msg=''

        try:
            raw, mode = fetch_json_api(primary_sources[market], timeout=20)
            primary_df=parse_primary(market,raw)
            primary_msg=f'基本資料 {len(primary_df)}檔（{mode}）'
        except Exception as e:
            primary_msg=f'基本資料失敗 {type(e).__name__}: {str(e)[:70]}'

        need_fallback = len(primary_df) < suspicious_min[market]

        if need_fallback:
            try:
                raw, mode = fetch_json_api(fallback_sources[market], timeout=20)
                fallback_df=parse_fallback(market,raw)
                fallback_msg=f'日行情備援 {len(fallback_df)}檔（{mode}）'
            except Exception as e:
                fallback_msg=f'日行情備援失敗 {type(e).__name__}: {str(e)[:70]}'

        # 以筆數較完整者為主；若兩者都有資料就合併，避免漏掉停牌/當日無成交個股
        candidates=[x for x in [primary_df,fallback_df] if x is not None and not x.empty]
        if candidates:
            md=pd.concat(candidates,ignore_index=True)
            md=md.drop_duplicates('股票代號',keep='first')
            market_frames.append(md)
            chosen=f'{len(md)}檔'
        else:
            chosen='0檔'

        source_status.append(
            f'{market}：{chosen}｜{primary_msg}'
            + (f'｜{fallback_msg}' if fallback_msg else '')
        )

    if market_frames:
        d=pd.concat(market_frames,ignore_index=True)
        d=d.drop_duplicates('股票代號',keep='first')
    else:
        d=pd.DataFrame(columns=['股票代號','股票名稱','市場'])

    # 最後一層才使用 stock_list.csv，而且明確標示
    if d.empty:
        d=STOCK_LIST.copy()
        if not d.empty:
            # 對齊欄位
            rename={}
            if '股票代號' not in d.columns and len(d.columns)>0:
                rename[d.columns[0]]='股票代號'
            d=d.rename(columns=rename)
            if '股票名稱' not in d.columns:
                d['股票名稱']=d['股票代號'].astype(str)
            if '市場' not in d.columns:
                d['市場']='未分類'
            d=d[['股票代號','股票名稱','市場']].copy()
            source_status.append(f'⚠️ 官方三市場皆無法取得，回退 stock_list.csv：{len(d)}檔')
        else:
            source_status.append('❌ 官方 API 與 stock_list.csv 都無資料')

    return d.reset_index(drop=True), source_status, taiwan_time_text()


UNIVERSE, UNIVERSE_STATUS, UNIVERSE_FETCH_TIME = load_market_universe(universe_effective_key())

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
                op=num(pick(item,['OpeningPrice','Open','開盤價']))
                high=num(pick(item,['HighestPrice','High','最高價']))
                low=num(pick(item,['LowestPrice','Low','最低價']))
                if not re.fullmatch(r'[0-9A-Z]{4,6}',code) or not np.isfinite(close) or close<=0:
                    continue
                all_rows.append({'股票代號':code,'股票名稱':name,'市場':market,'收盤價':close,'漲跌':change,'成交量':volume,'成交額':value,'開盤價':op,'最高價':high,'最低價':low,'報價日期':'','報價時間':'','行情來源':'官方日行情'})
                count+=1
            status.append(f'{market}：{count} 檔（{api_mode}）')
        except Exception as e:
            msg=str(e).replace(chr(10),' ')[:100]
            status.append(f'{market}：失敗（{type(e).__name__}） {msg}')
    d=pd.DataFrame(all_rows)
    if d.empty:
        return pd.DataFrame(columns=['股票代號','股票名稱','市場','收盤價','漲跌','成交量','成交額','開盤價','最高價','最低價','報價日期','報價時間','行情來源']), status, taiwan_time_text()
    d=d.drop_duplicates(['股票代號','市場'])
    d['成交額']=d['成交額'].fillna(0); d['成交量']=d['成交量'].fillna(0)
    _prev=d['收盤價']-d['漲跌']
    d['漲跌幅%']=np.where(_prev>0,d['漲跌']/_prev*100,np.nan)
    return d,status,taiwan_time_text()


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
        d.index=pd.to_datetime(d.index)
        if getattr(d.index,'tz',None) is not None:
            d.index=d.index.tz_convert(TAIWAN_TZ).tz_localize(None)
        d.index.name='Date'
        for c in cols:d[c]=pd.to_numeric(d[c],errors='coerce')
        d=d.dropna(subset=['Close'])
        d.attrs['source']='Yahoo Finance 日K'
        d.attrs['fetch_time_tw']=taiwan_time_text()
        d.attrs['latest_trade_date']=pd.Timestamp(d.index[-1]).strftime('%Y-%m-%d') if len(d) else ''
        return d
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

def build_row(symbol,df,quote_row=None,quote_fetch_time=None):
    if df is None or len(df)<200:return None

    calc_df,live_used=inject_live_daily_bar(df,quote_row)
    official_used=False;official_date=''
    if not live_used:
        calc_df,official_used,official_date=inject_official_eod_bar(calc_df,quote_row)
    d=indicators(calc_df);x=d.iloc[-1];score,bd,reasons=black_score(d);sig=signals(d,score)

    price=float(x.Close)
    change=float(x.CHANGE) if pd.notna(x.CHANGE) else 0.0
    display_volume=float(x.Volume)
    price_source='Yahoo Finance 日K'
    quote_time=df.attrs.get('fetch_time_tw','')
    quote_date=''
    quote_used=False

    if quote_row is not None:
        try:
            qprice=pd.to_numeric(quote_row.get('收盤價',np.nan),errors='coerce')
            qchg=pd.to_numeric(quote_row.get('漲跌幅%',np.nan),errors='coerce')
            qvol=pd.to_numeric(quote_row.get('成交量',np.nan),errors='coerce')
            qsource=str(quote_row.get('行情來源','官方日行情'))
            qtime=str(quote_row.get('報價時間','') or quote_fetch_time or '')
            qdate=str(quote_row.get('報價日期','') or '')
            if pd.notna(qprice) and float(qprice)>0:
                price=float(qprice);price_source=qsource;quote_used=True
            if pd.notna(qchg):change=float(qchg)
            if pd.notna(qvol) and float(qvol)>=0:display_volume=float(qvol)
            if qtime:quote_time=qtime
            quote_date=qdate or (official_date if official_used else '')
        except Exception:
            pass

    freshness=quote_freshness_label(quote_time,price_source)
    tech_state='⚡ Fugle 盤中未完成日K' if live_used else ('🏛️ 官方盤後K' if official_used else '📅 Yahoo 完整日K')

    return {
        '股票':str(symbol).zfill(4),'名稱':stock_name(symbol),'市場':stock_market(symbol),
        '價格':price,'漲跌%':change,'成交量':display_volume,
        '量比':float(x.VOL_RATIO) if pd.notna(x.VOL_RATIO) else 0.0,
        'K':float(x.K) if pd.notna(x.K) else np.nan,'D':float(x.D) if pd.notna(x.D) else np.nan,
        'MA15':float(x.MA15) if pd.notna(x.MA15) else np.nan,'MA60':float(x.MA60) if pd.notna(x.MA60) else np.nan,
        'MA200':float(x.MA200) if pd.notna(x.MA200) else np.nan,'連量':int(x.CONSEC_VOL),
        '20日高':float(x.HIGH20) if pd.notna(x.HIGH20) else np.nan,'60日高':float(x.HIGH60) if pd.notna(x.HIGH60) else np.nan,
        '黑嚕嚕分數':score,'等級':score_level(score),'訊號':'、'.join(sig),
        '判斷':'、'.join(dict.fromkeys(reasons[:8])),'趨勢分':bd['趨勢'],'動能分':bd['動能'],
        '量能分':bd['成交量'],'突破分':bd['突破'],'KD分':bd['KD'],'額外分':bd['額外強度'],
        '技術資料日':pd.Timestamp(d.index[-1]).strftime('%Y-%m-%d'),
        '價格來源':price_source,'行情狀態':freshness,'行情時間':quote_time,
        '日期檢查':quote_date_health(quote_date,price_source),'技術狀態':tech_state,'報價日期':quote_date,
        '_df':d
    }


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

A23_STRATEGY_SIGNATURE = 'A2.3.5|' + '|'.join(
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

    # 2) MA 結構：A2.3.5 公平化 MA60
    # MA15/20/30：收盤>快均線、快均線>MA60、MA60>MA200、快均線上彎
    # MA60：不再出現「MA60 > MA60」的不可能條件，改成
    #       收盤>MA60、MA60>MA200、MA60上彎、收盤與MA60距離不過度乖離
    ma=0
    if int(fast_ma) < 60:
        if pd.notna(fast) and close>fast: ma+=6
        if pd.notna(ma60) and pd.notna(fast) and fast>ma60: ma+=4
        if pd.notna(ma200) and pd.notna(ma60) and ma60>ma200: ma+=3
        if pd.notna(x.FAST_SLOPE) and x.FAST_SLOPE>0: ma+=2
    else:
        if pd.notna(ma60) and close>ma60: ma+=6
        if pd.notna(ma200) and pd.notna(ma60) and ma60>ma200: ma+=4
        if pd.notna(x.FAST_SLOPE) and x.FAST_SLOPE>0: ma+=3
        if pd.notna(ma60) and ma60>0:
            dist60=(close/ma60-1)*100
            if 0<=dist60<=12: ma+=2
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

    # 7) 策略品質：A2.3.5 同步公平化 MA60
    confirmations=0
    if pd.notna(fast) and close>fast: confirmations+=1
    if int(fast_ma) < 60:
        if pd.notna(ma60) and pd.notna(fast) and fast>ma60: confirmations+=1
    else:
        if pd.notna(ma200) and pd.notna(ma60) and ma60>ma200: confirmations+=1
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
# 🧪 A2.3.6 Strategy Lab Pro
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



# ============================================================
# 🧪 A2.3.5 Reliability Lab
# - 非重疊交易：同一股票持有期間內不重複進場
# - 年度拆解：依進場年度檢查策略是否只在單一年份有效
# - 樣本規模穩定度：50 / 100 / 200 / 300 檔逐步放大
# - 真實資金曲線：本金、最大持股、單筆配置、費用、稅、滑價
# ============================================================

def select_a235_nonoverlap(frame, min_score, horizon, min_gap=0):
    """同一股票持有中不重複進場；冷卻以真正交易日序號計算。"""
    if frame is None or frame.empty:return pd.DataFrame()
    base=frame.sort_values(['策略','股票','日期']).copy()
    base['_trade_pos']=base.groupby(['策略','股票']).cumcount()
    x=base[base['綜合分數']>=float(min_score)].copy()
    effective_gap=max(int(min_gap),int(horizon)+1)
    out=[]
    for (strategy,sym),g in x.groupby(['策略','股票'],sort=False):
        last=-10**9
        for _,row in g.sort_values('日期').iterrows():
            pos=int(row['_trade_pos'])
            if pos-last<effective_gap:continue
            out.append(row)
            last=pos
    return pd.DataFrame(out).drop(columns=['_trade_pos'],errors='ignore') if out else pd.DataFrame()

def strategy_lab_grid_nonoverlap(history, thresholds, horizons, min_gap=0):
    rows=[]
    for hzn in horizons:
        f=add_forward_returns_a23(history,hzn).dropna(subset=['未來報酬%'])
        for strategy_name in A23_STRATEGIES:
            sf=f[f['策略']==strategy_name]
            for th in thresholds:
                sel=select_a235_nonoverlap(sf,th,hzn,min_gap)
                metrics=_trade_metrics_ci(sel['未來報酬%'] if not sel.empty else [])
                rows.append({
                    '策略':strategy_name,'最低分數':int(th),'持有交易日':int(hzn),
                    '交易模式':'非重疊',**metrics
                })
    return pd.DataFrame(rows)

def strategy_lab_yearly_nonoverlap(history, thresholds, horizons, min_gap=0):
    """依進場年度拆解，避免只看整段期間平均。"""
    rows=[]
    if history is None or history.empty:return pd.DataFrame()
    years=sorted(pd.to_datetime(history['日期']).dt.year.dropna().unique().tolist())
    for hzn in horizons:
        f=add_forward_returns_a23(history,hzn).dropna(subset=['未來報酬%']).copy()
        f['進場年度']=pd.to_datetime(f['日期']).dt.year
        for strategy_name in A23_STRATEGIES:
            sf=f[f['策略']==strategy_name]
            for th in thresholds:
                sel=select_a235_nonoverlap(sf,th,hzn,min_gap)
                if sel.empty:
                    continue
                sel['進場年度']=pd.to_datetime(sel['日期']).dt.year
                for yr in years:
                    y=sel[sel['進場年度']==yr]
                    if y.empty:continue
                    rows.append({
                        '策略':strategy_name,'最低分數':int(th),'持有交易日':int(hzn),
                        '年度':int(yr),**_trade_metrics_ci(y['未來報酬%'])
                    })
    return pd.DataFrame(rows)

def strategy_lab_size_stability(history, thresholds, horizons, min_gap=0):
    """用同一份已建立的歷史資料依股票數前綴比較 50/100/200/300。"""
    if history is None or history.empty:return pd.DataFrame()
    symbols_order=list(dict.fromkeys(history['股票'].astype(str).tolist()))
    max_n=len(symbols_order)
    sizes=[n for n in [50,100,200,300] if n<=max_n]
    if max_n not in sizes:sizes.append(max_n)
    rows=[]
    for n in sorted(set(sizes)):
        sub=history[history['股票'].astype(str).isin(set(symbols_order[:n]))].copy()
        g=strategy_lab_grid_nonoverlap(sub,thresholds,horizons,min_gap)
        if g.empty:continue
        g['股票數']=int(n)
        rows.append(g)
    return pd.concat(rows,ignore_index=True) if rows else pd.DataFrame()

def _a235_signal_frame(history, strategy_name, min_score, horizon, min_gap=0):
    """建立投資組合模擬訊號，並附上每筆實際退出日期。"""
    h=history[history['策略']==strategy_name].copy()
    if h.empty:return pd.DataFrame()
    h=h.sort_values(['股票','日期']).reset_index(drop=True)
    h['退出日期']=h.groupby('股票')['日期'].shift(-int(horizon))
    h=h.dropna(subset=['退出日期'])
    h['策略']=strategy_name
    sel=select_a235_nonoverlap(h,horizon=horizon,min_score=min_score,min_gap=min_gap)
    return sel.sort_values(['日期','綜合分數'],ascending=[True,False]).reset_index(drop=True)

def run_a235_portfolio(history, strategy_name, min_score, horizon,
                       initial_capital=1_000_000, max_positions=10, position_pct=10.0,
                       fee_pct=0.1425, sell_tax_pct=0.30, slippage_pct=0.10,
                       min_gap=0):
    """
    研究型每日收盤資金曲線：
    - 訊號日收盤進場
    - horizon 個交易日後收盤出場
    - 同一股票不得重疊持有
    - 同日訊號依綜合分數高到低填滿可用部位
    - 可設定手續費 / 交易稅 / 滑價
    """
    if history is None or history.empty:
        return pd.DataFrame(),pd.DataFrame(),{}

    hist=history[history['策略']==strategy_name].copy()
    if hist.empty:return pd.DataFrame(),pd.DataFrame(),{}

    hist['日期']=pd.to_datetime(hist['日期'])
    close_mat=hist.pivot_table(index='日期',columns='股票',values='收盤',aggfunc='last').sort_index()
    signals=_a235_signal_frame(history,strategy_name,min_score,horizon,min_gap)
    if signals.empty:return pd.DataFrame(),pd.DataFrame(),{}
    signals['日期']=pd.to_datetime(signals['日期'])
    signals['退出日期']=pd.to_datetime(signals['退出日期'])

    by_date={d:g.sort_values('綜合分數',ascending=False) for d,g in signals.groupby('日期')}
    dates=close_mat.index
    cash=float(initial_capital)
    positions={}
    trades=[]
    equity_rows=[]

    fee=float(fee_pct)/100
    tax=float(sell_tax_pct)/100
    slip=float(slippage_pct)/100
    alloc=float(position_pct)/100

    for dt in dates:
        row=close_mat.loc[dt]

        # 先出場：持有到期者於當日收盤賣出
        exit_syms=[]
        for sym,pos in list(positions.items()):
            if dt>=pos['exit_date']:
                px=row.get(sym,np.nan)
                if pd.isna(px):continue
                sell_px=float(px)*(1-slip)
                gross=pos['qty']*sell_px
                sell_cost=gross*(fee+tax)
                proceeds=gross-sell_cost
                cash+=proceeds
                pnl=proceeds-pos['total_buy_cost']
                ret=pnl/pos['total_buy_cost']*100 if pos['total_buy_cost']>0 else np.nan
                trades.append({
                    '股票':sym,'進場日':pos['entry_date'],'出場日':dt,
                    '進場分數':pos['score'],'持有交易日':int(horizon),
                    '買進成本':pos['total_buy_cost'],'賣出淨收入':proceeds,
                    '損益':pnl,'報酬%':ret
                })
                exit_syms.append(sym)
        for sym in exit_syms:
            positions.pop(sym,None)

        # 進場前先估算目前權益
        mtm=sum(pos['qty']*float(row.get(sym,np.nan))
                for sym,pos in positions.items() if pd.notna(row.get(sym,np.nan)))
        equity_before=cash+mtm

        # 再進場：同日依分數由高到低
        if dt in by_date:
            for _,sig in by_date[dt].iterrows():
                sym=str(sig['股票'])
                if sym in positions or len(positions)>=int(max_positions):continue
                px=row.get(sym,np.nan)
                if pd.isna(px) or float(px)<=0:continue
                target=min(equity_before*alloc, cash/(1+fee))
                if target<=0:continue
                buy_px=float(px)*(1+slip)
                qty=target/(buy_px*(1+fee))
                gross=qty*buy_px
                buy_fee=gross*fee
                total_cost=gross+buy_fee
                if total_cost>cash:continue
                cash-=total_cost
                positions[sym]={
                    'qty':qty,'entry_date':dt,'exit_date':pd.to_datetime(sig['退出日期']),
                    'score':float(sig['綜合分數']),'total_buy_cost':total_cost
                }

        # 每日收盤淨值
        pos_value=0.0
        for sym,pos in positions.items():
            px=row.get(sym,np.nan)
            if pd.notna(px):pos_value+=pos['qty']*float(px)
        equity=cash+pos_value
        equity_rows.append({
            '日期':dt,'現金':cash,'持股市值':pos_value,'總資產':equity,'持股數':len(positions)
        })

    eq=pd.DataFrame(equity_rows)
    tr=pd.DataFrame(trades)
    if eq.empty:return eq,tr,{}

    eq['累積報酬%']=(eq['總資產']/float(initial_capital)-1)*100
    eq['高水位']=eq['總資產'].cummax()
    eq['回撤%']=(eq['總資產']/eq['高水位']-1)*100

    total_return=(eq['總資產'].iloc[-1]/float(initial_capital)-1)*100
    mdd=float(eq['回撤%'].min())
    days=max((eq['日期'].iloc[-1]-eq['日期'].iloc[0]).days,1)
    cagr=((eq['總資產'].iloc[-1]/float(initial_capital))**(365.25/days)-1)*100 if eq['總資產'].iloc[-1]>0 else np.nan
    if not tr.empty:
        win=float((tr['報酬%']>0).mean()*100)
        avg=float(tr['報酬%'].mean())
        pf=(tr.loc[tr['損益']>0,'損益'].sum()/abs(tr.loc[tr['損益']<0,'損益'].sum())
            if (tr['損益']<0).any() else np.inf)
    else:
        win=avg=np.nan;pf=np.nan

    stats={
        '期末資產':float(eq['總資產'].iloc[-1]),
        '總報酬%':float(total_return),'年化報酬%':float(cagr),'最大回撤%':mdd,
        '完成交易':int(len(tr)),'勝率%':win,'平均每筆%':avg,'Profit Factor':float(pf) if pd.notna(pf) else np.nan
    }
    return eq,tr,stats


# Sidebar

# ===== A2.4 籌碼實驗室 C版：先研究，不改原100分 =====
def _chip_num(v): return pd.to_numeric(str(v).replace(',','').strip(),errors='coerce')
@st.cache_data(ttl=1800, show_spinner=False)
def load_twse_t86_day(date_yyyymmdd, return_status=False):
    cols=['日期','股票','名稱','外資買賣超股數','投信買賣超股數']
    endpoints=[
        'https://www.twse.com.tw/rwd/zh/fund/T86',
        'https://www.twse.com.tw/fund/T86',
    ]
    headers={
        'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36',
        'Accept':'application/json,text/plain,*/*',
        'Referer':'https://www.twse.com.tw/'
    }
    logs=[]

    def find_idx(fields,*parts):
        for i,f in enumerate(fields):
            fs=str(f).replace(' ','')
            if all(p.replace(' ','') in fs for p in parts):
                return i
        return None

    def extract_table(x):
        cands=[]
        if isinstance(x,dict):
            if isinstance(x.get('fields'),list) and isinstance(x.get('data'),list):
                cands.append((x.get('fields',[]),x.get('data',[]),'top-level'))
            tables=x.get('tables',[])
            if isinstance(tables,list):
                for tb in tables:
                    if isinstance(tb,dict):
                        cands.append((tb.get('fields',[]),tb.get('data',[]),'tables'))
        for fields,data,mode in cands:
            txt='|'.join(map(str,fields))
            if fields and data and '證券代號' in txt and '投信買賣超股數' in txt and '外陸資買賣超股數' in txt:
                return fields,data,mode
        return [],[],'none'

    for url in endpoints:
        try:
            r=requests.get(
                url,
                params={'date':date_yyyymmdd,'response':'json','selectType':'ALLBUT0999'},
                headers=headers,timeout=20
            )
            logs.append(f"{url.split('twse.com.tw')[-1]} HTTP {r.status_code}")
            if r.status_code!=200:
                continue

            try:
                x=r.json()
            except Exception as e:
                logs.append(f"JSON解析失敗:{type(e).__name__}")
                continue

            fields,data,mode=extract_table(x)
            logs.append(f"stat={x.get('stat','—')} mode={mode} fields={len(fields)} rows={len(data)}")
            if not fields or not data:
                continue

            ic=find_idx(fields,'證券代號')
            inn=find_idx(fields,'證券名稱')
            iff=find_idx(fields,'外陸資買賣超股數')
            itt=find_idx(fields,'投信買賣超股數')
            if None in (ic,inn,iff,itt):
                logs.append('必要欄位索引失敗')
                continue

            rows=[]
            for a in data:
                if not isinstance(a,(list,tuple)) or len(a)<=max(ic,inn,iff,itt):
                    continue
                code=str(a[ic]).strip()
                if re.fullmatch(r'\d{4,6}',code):
                    rows.append({
                        '日期':pd.to_datetime(date_yyyymmdd),
                        '股票':code.zfill(4),
                        '名稱':str(a[inn]).strip(),
                        '外資買賣超股數':_chip_num(a[iff]),
                        '投信買賣超股數':_chip_num(a[itt]),
                    })

            df=pd.DataFrame(rows,columns=cols)
            if not df.empty:
                status='｜'.join(logs[-3:])+f'｜成功 {len(df)} 筆'
                return (df,status) if return_status else df

        except Exception as e:
            logs.append(f"{url.split('twse.com.tw')[-1]} {type(e).__name__}:{str(e)[:70]}")

    empty=pd.DataFrame(columns=cols)
    status='｜'.join(logs[-8:]) if logs else 'T86 無回應'
    return (empty,status) if return_status else empty

def recent_weekdays(n):
    d=taiwan_now().date();out=[]
    while len(out)<n:
        if d.weekday()<5:out.append(pd.Timestamp(d))
        d-=timedelta(days=1)
    return list(reversed(out))
@st.cache_data(ttl=1800, show_spinner=False)
def load_twse_chip_history(days=15, return_status=False):
    parts=[];logs=[]
    now=taiwan_now()
    candidates=recent_weekdays(days+15)
    if now.weekday()<5 and now.hour<18:
        today=pd.Timestamp(now.date())
        candidates=[d for d in candidates if d.normalize()!=today.normalize()]

    for d in candidates:
        x,status=load_twse_t86_day(d.strftime('%Y%m%d'),return_status=True)
        logs.append(f"{d.strftime('%Y-%m-%d')} {status}")
        if x is not None and not x.empty:
            parts.append(x)

    if not parts:
        empty=pd.DataFrame(columns=['日期','股票','名稱','外資買賣超股數','投信買賣超股數'])
        status='最近查詢皆無資料；'+('｜'.join(logs[-5:]) if logs else '無查詢紀錄')
        return (empty,status) if return_status else empty

    z=pd.concat(parts,ignore_index=True).sort_values(['股票','日期'])
    valid=sorted(pd.to_datetime(z['日期']).dt.normalize().unique())[-days:]
    out=z[pd.to_datetime(z['日期']).dt.normalize().isin(valid)].copy()
    latest=pd.to_datetime(out['日期']).max().strftime('%Y-%m-%d')
    status=f'成功：{len(out):,}筆 / {out["股票"].nunique():,}檔 / 最新{latest}'
    return (out,status) if return_status else out
def signed_streak(vals):
    s=pd.Series(vals).dropna().astype(float)
    if s.empty or s.iloc[-1]==0:return 0
    sign=1 if s.iloc[-1]>0 else -1;n=0
    for v in s.iloc[::-1]:
        if (v>0)==(sign>0):n+=1
        else:break
    return sign*n
def chip_features(hist,price_frames):
    if hist is None or hist.empty:return pd.DataFrame()
    rows=[]
    for code,g in hist.groupby('股票'):
        g=g.sort_values('日期');fs=signed_streak(g['外資買賣超股數']);ts=signed_streak(g['投信買賣超股數'])
        f5=float(g['外資買賣超股數'].tail(5).sum());t5=float(g['投信買賣超股數'].tail(5).sum())
        pdf=price_frames.get(code);v5=float(pd.to_numeric(pdf['Volume'],errors='coerce').tail(5).sum()) if pdf is not None and not pdf.empty else np.nan
        fi=f5/v5*100 if pd.notna(v5) and v5>0 else np.nan;ti=t5/v5*100 if pd.notna(v5) and v5>0 else np.nan
        score=lambda s,i:float(np.clip(s,-5,5))+(0 if pd.isna(i) else float(np.clip(i,-5,5)))
        rows.append({'股票':code,'外資連買賣天數':fs,'投信連買賣天數':ts,'外資5日買賣超股數':f5,'投信5日買賣超股數':t5,'外資5日強度%':fi,'投信5日強度%':ti,'外資研究分':round(score(fs,fi),2),'投信研究分':round(score(ts,ti),2),'籌碼研究分':round(score(fs,fi)+score(ts,ti),2),'籌碼資料日':g['日期'].max().strftime('%Y-%m-%d')})
    return pd.DataFrame(rows)
def chip_signal_label(r):
    f=int(r.get('外資連買賣天數',0) or 0);q=int(r.get('投信連買賣天數',0) or 0)
    if f>=3 and q>=3:return '🔥 外資＋投信同步連買'
    if q>=3:return '🟣 投信連買'
    if f>=3:return '🔵 外資連買'
    if f<=-3 and q<=-3:return '⚠️ 法人同步連賣'
    if q<=-3:return '🟠 投信連賣'
    if f<=-3:return '🟡 外資連賣'
    return '—'

# ===== A2.4.1 籌碼歷史回測 =====
def build_chip_daily_features(hist):
    if hist is None or hist.empty:
        return pd.DataFrame()
    parts=[]
    for code,g in hist.groupby('股票'):
        g=g.sort_values('日期').copy()
        g['外資連買賣天數_hist']=0
        g['投信連買賣天數_hist']=0
        fs=[];ts=[]
        for i in range(len(g)):
            fs.append(signed_streak(g['外資買賣超股數'].iloc[:i+1]))
            ts.append(signed_streak(g['投信買賣超股數'].iloc[:i+1]))
        g['外資連買賣天數_hist']=fs;g['投信連買賣天數_hist']=ts
        g['外資5日買賣超股數_hist']=g['外資買賣超股數'].rolling(5,min_periods=1).sum()
        g['投信5日買賣超股數_hist']=g['投信買賣超股數'].rolling(5,min_periods=1).sum()
        parts.append(g)
    return pd.concat(parts,ignore_index=True) if parts else pd.DataFrame()

def _a24_metrics(trades):
    if trades is None or trades.empty:
        return {'樣本數':0,'勝率%':np.nan,'平均報酬%':np.nan,'中位數%':np.nan,'PF':np.nan}
    r=pd.to_numeric(trades['報酬%'],errors='coerce').dropna()
    if r.empty:return {'樣本數':0,'勝率%':np.nan,'平均報酬%':np.nan,'中位數%':np.nan,'PF':np.nan}
    wins=r[r>0].sum();loss=-r[r<0].sum()
    return {'樣本數':len(r),'勝率%':(r>0).mean()*100,'平均報酬%':r.mean(),'中位數%':r.median(),'PF':wins/loss if loss>0 else np.nan}

def run_a24_chip_backtest(result_df, chip_hist, score_threshold=85, horizon=20, cooldown=20):
    """
    用目前 result 中每檔股票的歷史 _df 做技術分數近似回測，
    再把 T86 法人資料按日期對齊，測四組權重。
    注意：此版先做「研究級」驗證；技術歷史分數使用現有 indicators/black_score 邏輯逐日計算。
    """
    if chip_hist is None or chip_hist.empty:return pd.DataFrame(),pd.DataFrame()
    chip=build_chip_daily_features(chip_hist)
    all_trades=[]
    models=[
        ('原100分',1.00,0.00,0.00),
        ('技術90＋外資5＋投信5',0.90,0.05,0.05),
        ('技術85＋外資5＋投信10',0.85,0.05,0.10),
        ('技術80＋外資10＋投信10',0.80,0.10,0.10),
    ]
    for _,rr in result_df.iterrows():
        code=str(rr['股票']).zfill(4);df=rr.get('_df')
        if df is None or len(df)<220:continue
        d=indicators(df.copy())
        cg=chip[chip['股票']==code].copy()
        if cg.empty:continue
        cg['日期']=pd.to_datetime(cg['日期']).dt.normalize()
        cmap=cg.set_index('日期')
        last_entry=-999999
        for i in range(220,len(d)-horizon):
            if i-last_entry<cooldown:continue
            dt=pd.Timestamp(d.index[i]).normalize()
            if dt not in cmap.index:continue
            sub=d.iloc[:i+1]
            try:
                tech_score=float(black_score(sub)[0])
            except Exception:
                continue
            if tech_score<score_threshold:continue
            cr=cmap.loc[dt]
            # 若同日有重複，取最後一筆
            if isinstance(cr,pd.DataFrame):cr=cr.iloc[-1]
            fs=float(cr.get('外資連買賣天數_hist',0));ts=float(cr.get('投信連買賣天數_hist',0))
            # 連買賣天數映射 0~100：-5天=0, 0天=50, +5天=100
            fscore=float(np.clip((fs+5)/10*100,0,100))
            tscore=float(np.clip((ts+5)/10*100,0,100))
            entry=float(d['Close'].iloc[i]);exitp=float(d['Close'].iloc[i+horizon])
            ret=(exitp/entry-1)*100
            for name,wt,wf,wi in models:
                final=tech_score*wt+fscore*wf+tscore*wi
                # 各模型採相同 85 分門檻，直接看納入籌碼後是否改善
                if final>=score_threshold:
                    all_trades.append({'模型':name,'股票':code,'進場日':dt,'技術分數':tech_score,
                                       '外資連買賣天數':fs,'投信連買賣天數':ts,'綜合模型分':final,
                                       '持有日':horizon,'報酬%':ret})
            last_entry=i
    trades=pd.DataFrame(all_trades)
    if trades.empty:return trades,pd.DataFrame()
    rows=[]
    for name,g in trades.groupby('模型'):
        m=_a24_metrics(g);m['模型']=name
        # 年度穩定度
        yrs=[]
        gg=g.copy();gg['年']=pd.to_datetime(gg['進場日']).dt.year
        for y,yg in gg.groupby('年'):
            ym=_a24_metrics(yg);yrs.append(ym['平均報酬%'])
        m['年度正報酬比例%']=np.mean([x>0 for x in yrs])*100 if yrs else np.nan
        rows.append(m)
    summary=pd.DataFrame(rows)
    order=['原100分','技術90＋外資5＋投信5','技術85＋外資5＋投信10','技術80＋外資10＋投信10']
    summary['模型']=pd.Categorical(summary['模型'],categories=order,ordered=True)
    summary=summary.sort_values('模型')
    return trades,summary


# ===== A2.4.2 籌碼策略健診 =====
def _a242_factor_score(streak, intensity):
    """
    法人單一因子 0~100：
    60% 連買賣天數 + 40% 5日買賣超強度。
    streak -5~+5 映射 0~100；
    intensity -5%~+5% 映射 0~100。
    """
    s=float(np.clip((float(streak)+5)/10*100,0,100))
    i=50.0 if pd.isna(intensity) else float(np.clip(50+float(intensity)*10,0,100))
    return 0.60*s+0.40*i

def collect_a242_event_base(result_df, chip_hist, max_events_per_stock=140):
    """
    建立一次性的歷史事件底表：
    日期對齊技術分數、法人 streak、法人5日強度，以及 5/10/20/30 日 forward return。
    """
    if chip_hist is None or chip_hist.empty:return pd.DataFrame()
    chip=build_chip_daily_features(chip_hist).copy()
    if chip.empty:return pd.DataFrame()

    out=[]
    for _,rr in result_df.iterrows():
        code=str(rr['股票']).zfill(4); df=rr.get('_df')
        if df is None or len(df)<230:continue

        d=indicators(df.copy()).copy()
        if d.empty:continue
        d.index=pd.to_datetime(d.index).tz_localize(None).normalize()

        cg=chip[chip['股票']==code].copy()
        if cg.empty:continue
        cg['日期']=pd.to_datetime(cg['日期']).dt.tz_localize(None).dt.normalize()
        cg=cg.sort_values('日期').tail(max_events_per_stock)

        # 用歷史日成交量計算5日強度，不能使用今天的總量去回推過去。
        vol5=pd.to_numeric(d['Volume'],errors='coerce').rolling(5,min_periods=1).sum()
        vol5_map=vol5.to_dict()

        for _,cr in cg.iterrows():
            dt=pd.Timestamp(cr['日期']).normalize()
            if dt not in d.index:continue
            pos=d.index.get_loc(dt)
            if isinstance(pos,slice) or isinstance(pos,(list,np.ndarray)):continue
            if pos<220:continue

            try:
                tech=float(black_score(d.iloc[:pos+1])[0])
            except Exception:
                continue

            v5=float(vol5_map.get(dt,np.nan))
            f5=float(cr.get('外資5日買賣超股數_hist',0))
            t5=float(cr.get('投信5日買賣超股數_hist',0))
            fi=f5/v5*100 if pd.notna(v5) and v5>0 else np.nan
            ti=t5/v5*100 if pd.notna(v5) and v5>0 else np.nan
            fs=float(cr.get('外資連買賣天數_hist',0))
            ts=float(cr.get('投信連買賣天數_hist',0))
            ff=_a242_factor_score(fs,fi); tf=_a242_factor_score(ts,ti)

            row={'股票':code,'名稱':rr.get('名稱',''),'日期':dt,'技術分數':tech,
                 '外資連買賣天數':fs,'投信連買賣天數':ts,
                 '外資5日強度%':fi,'投信5日強度%':ti,
                 '外資因子分':ff,'投信因子分':tf}
            close=float(d['Close'].iloc[pos])
            for h in [5,10,20,30]:
                row[f'報酬{h}日%']=(float(d['Close'].iloc[pos+h])/close-1)*100 if pos+h<len(d) else np.nan
            out.append(row)
    return pd.DataFrame(out)

def _a242_nonoverlap(frame, score_col, threshold, horizon, min_sample=30,
                     streak_mode='不限', min_streak=0):
    if frame is None or frame.empty:return pd.DataFrame()
    z=frame.copy()
    z=z[pd.to_numeric(z[score_col],errors='coerce')>=threshold]
    if streak_mode=='外資':
        z=z[z['外資連買賣天數']>=min_streak]
    elif streak_mode=='投信':
        z=z[z['投信連買賣天數']>=min_streak]
    elif streak_mode=='雙法人':
        z=z[(z['外資連買賣天數']>=min_streak)&(z['投信連買賣天數']>=min_streak)]
    if z.empty:return z

    retcol=f'報酬{horizon}日%'
    z=z[pd.to_numeric(z[retcol],errors='coerce').notna()].sort_values(['股票','日期'])
    keep=[]
    for code,g in z.groupby('股票'):
        last=None
        for idx,r in g.iterrows():
            if last is None or (pd.Timestamp(r['日期'])-last).days>=max(horizon+1,2):
                keep.append(idx);last=pd.Timestamp(r['日期'])
    return z.loc[keep].copy() if keep else z.iloc[0:0].copy()

def run_a242_diagnostic(event_base, thresholds=(75,80,85,90), horizons=(5,10,20,30),
                        min_sample=50, streak_levels=(0,3,5)):
    if event_base is None or event_base.empty:return pd.DataFrame(),pd.DataFrame()

    models=[
        ('原100分',1.00,0.00,0.00),
        ('技術95＋外資5',0.95,0.05,0.00),
        ('技術95＋投信5',0.95,0.00,0.05),
        ('技術90＋外資5＋投信5',0.90,0.05,0.05),
        ('技術90＋外資10',0.90,0.10,0.00),
        ('技術90＋投信10',0.90,0.00,0.10),
        ('技術85＋外資5＋投信10',0.85,0.05,0.10),
        ('技術85＋外資10＋投信5',0.85,0.10,0.05),
        ('技術80＋外資10＋投信10',0.80,0.10,0.10),
    ]
    streak_tests=[('不限',0)]
    for n in streak_levels:
        if n>0:
            streak_tests += [('外資',n),('投信',n),('雙法人',n)]

    rows=[]
    for model,wt,wf,wi in models:
        z=event_base.copy()
        z['_模型分']=z['技術分數']*wt+z['外資因子分']*wf+z['投信因子分']*wi
        for th in thresholds:
            for h in horizons:
                for sm,ms in streak_tests:
                    tr=_a242_nonoverlap(z,'_模型分',th,h,min_sample,sm,ms)
                    if len(tr)<min_sample:continue
                    r=pd.to_numeric(tr[f'報酬{h}日%'],errors='coerce').dropna()
                    if len(r)<min_sample:continue
                    wins=r[r>0].sum();loss=-r[r<0].sum()
                    pf=wins/loss if loss>0 else np.nan
                    years=pd.to_datetime(tr['日期']).dt.year
                    yrmeans=tr.assign(_y=years,_r=pd.to_numeric(tr[f'報酬{h}日%'],errors='coerce')).groupby('_y')['_r'].mean().dropna()
                    yearly_pos=(yrmeans>0).mean()*100 if len(yrmeans)>0 else np.nan
                    # 簡易95% CI，僅當敏感度參考
                    se=r.std(ddof=1)/np.sqrt(len(r)) if len(r)>1 else np.nan
                    ci_low=r.mean()-1.96*se if pd.notna(se) else np.nan
                    rows.append({
                        '模型':model,'門檻':th,'持有日':h,'連買條件':sm,'連買天數':ms,
                        '樣本數':len(r),'勝率%':(r>0).mean()*100,'平均報酬%':r.mean(),
                        '中位數%':r.median(),'PF':pf,'95%CI下限%':ci_low,
                        '年度正報酬比例%':yearly_pos
                    })
    grid=pd.DataFrame(rows)
    if grid.empty:return grid,pd.DataFrame()

    # 同條件原100分作 baseline，比較真正改善幅度
    base=grid[grid['模型']=='原100分'][['門檻','持有日','連買條件','連買天數','勝率%','平均報酬%','PF']].copy()
    base=base.rename(columns={'勝率%':'基準勝率%','平均報酬%':'基準平均報酬%','PF':'基準PF'})
    comp=grid.merge(base,on=['門檻','持有日','連買條件','連買天數'],how='left')
    comp['勝率改善ppt']=comp['勝率%']-comp['基準勝率%']
    comp['平均報酬改善ppt']=comp['平均報酬%']-comp['基準平均報酬%']
    comp['PF改善']=comp['PF']-comp['基準PF']

    # 穩健度分數：改善三項 + CI + 樣本，不讓小樣本輕易奪冠
    comp['穩健度分數']=(
        comp['勝率改善ppt'].fillna(-99)*1.0+
        comp['平均報酬改善ppt'].fillna(-99)*2.0+
        comp['PF改善'].fillna(-99)*5.0+
        np.clip(comp['95%CI下限%'].fillna(-99),-10,10)*0.5+
        np.log1p(comp['樣本數'])*0.3
    )
    rank=comp[comp['模型']!='原100分'].sort_values(
        ['穩健度分數','平均報酬改善ppt','勝率改善ppt','樣本數'],
        ascending=[False,False,False,False]
    )
    return comp,rank


# ===== V3.6.12 外資因子證明版 =====
# 核心：驗證「外資5%」是否在不同門檻、持有期、連買天數、買超強度下仍穩定改善。
# 不以單一最佳參數定版，優先看跨條件穩健度。

def _v353_metrics(frame, retcol):
    if frame is None or frame.empty:
        return {'樣本數':0,'勝率%':np.nan,'平均報酬%':np.nan,'中位數%':np.nan,'PF':np.nan,
                '95%CI下限%':np.nan,'95%CI上限%':np.nan}
    r = pd.to_numeric(frame[retcol], errors='coerce').dropna()
    if r.empty:
        return {'樣本數':0,'勝率%':np.nan,'平均報酬%':np.nan,'中位數%':np.nan,'PF':np.nan,
                '95%CI下限%':np.nan,'95%CI上限%':np.nan}
    wins = r[r > 0].sum()
    loss = -r[r < 0].sum()
    pf = wins / loss if loss > 0 else np.nan
    se = r.std(ddof=1) / np.sqrt(len(r)) if len(r) > 1 else np.nan
    return {
        '樣本數': len(r),
        '勝率%': (r > 0).mean() * 100,
        '平均報酬%': r.mean(),
        '中位數%': r.median(),
        'PF': pf,
        '95%CI下限%': r.mean() - 1.96 * se if pd.notna(se) else np.nan,
        '95%CI上限%': r.mean() + 1.96 * se if pd.notna(se) else np.nan,
    }

def _v353_nonoverlap(frame, score_col, threshold, horizon):
    if frame is None or frame.empty:
        return pd.DataFrame()
    z = frame[pd.to_numeric(frame[score_col], errors='coerce') >= threshold].copy()
    retcol = f'報酬{horizon}日%'
    if retcol not in z.columns:
        return z.iloc[0:0]
    z = z[pd.to_numeric(z[retcol], errors='coerce').notna()].sort_values(['股票','日期'])

    keep = []
    for code, g in z.groupby('股票'):
        last_pos = None
        # 用每檔股票事件順序近似交易日非重疊
        for idx, r in g.iterrows():
            dt = pd.Timestamp(r['日期'])
            if last_pos is None or (dt - last_pos).days >= max(horizon + 1, 2):
                keep.append(idx)
                last_pos = dt
    return z.loc[keep].copy() if keep else z.iloc[0:0]

def _v353_year_stats(frame, retcol):
    if frame is None or frame.empty:
        return 0, np.nan
    x = frame.copy()
    x['年'] = pd.to_datetime(x['日期']).dt.year
    yr = x.groupby('年')[retcol].mean().dropna()
    if len(yr) == 0:
        return 0, np.nan
    return int(len(yr)), float((yr > 0).mean() * 100)

def _v353_streak_band(x):
    if pd.isna(x): return '無資料'
    x = float(x)
    if x <= 0: return '≤0天'
    if x <= 2: return '1-2天'
    if x <= 4: return '3-4天'
    return '5天以上'

def _v353_intensity_band(x):
    if pd.isna(x): return '無資料'
    x = float(x)
    if x <= 0: return '≤0%'
    if x < 0.5: return '0-0.5%'
    if x < 1.0: return '0.5-1%'
    if x < 2.0: return '1-2%'
    return '≥2%'

def run_v353_foreign_proof(event_base, thresholds=(75,80,85,90), horizons=(20,30,40), min_sample=40):
    """
    三候選：
    A 技術95+外資5
    B 技術90+外資10
    C 技術85+外資5+投信10
    同時保留原100分。
    """
    if event_base is None or event_base.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    z = event_base.copy()
    models = [
        ('原100分', 1.00, 0.00, 0.00),
        ('A 技術95＋外資5', 0.95, 0.05, 0.00),
        ('B 技術90＋外資10', 0.90, 0.10, 0.00),
        ('C 技術85＋外資5＋投信10', 0.85, 0.05, 0.10),
    ]

    for name, wt, wf, wi in models:
        z[name] = z['技術分數'] * wt + z['外資因子分'] * wf + z['投信因子分'] * wi

    rows = []
    for th in thresholds:
        for h in horizons:
            retcol = f'報酬{h}日%'
            if retcol not in z.columns:
                continue

            base_tr = _v353_nonoverlap(z, '原100分', th, h)
            base_m = _v353_metrics(base_tr, retcol)

            for name, _, _, _ in models:
                tr = _v353_nonoverlap(z, name, th, h)
                if len(tr) < min_sample:
                    continue
                m = _v353_metrics(tr, retcol)
                yrs, ypos = _v353_year_stats(tr, retcol)
                rows.append({
                    '模型': name,
                    '門檻': th,
                    '持有日': h,
                    **m,
                    '涵蓋年度數': yrs,
                    '年度正報酬比例%': ypos,
                    '勝率改善ppt': m['勝率%'] - base_m['勝率%'] if pd.notna(base_m['勝率%']) else np.nan,
                    '平均報酬改善ppt': m['平均報酬%'] - base_m['平均報酬%'] if pd.notna(base_m['平均報酬%']) else np.nan,
                    'PF改善': m['PF'] - base_m['PF'] if pd.notna(base_m['PF']) and pd.notna(m['PF']) else np.nan,
                })

    grid = pd.DataFrame(rows)
    if grid.empty:
        return grid, pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    candidates = grid[grid['模型'] != '原100分'].copy()
    candidates['三項全改善'] = (
        (candidates['勝率改善ppt'] > 0) &
        (candidates['平均報酬改善ppt'] > 0) &
        (candidates['PF改善'] > 0)
    )
    candidates['穩健分'] = (
        candidates['勝率改善ppt'].fillna(-99) * 1.0 +
        candidates['平均報酬改善ppt'].fillna(-99) * 2.0 +
        candidates['PF改善'].fillna(-99) * 5.0 +
        np.clip(candidates['95%CI下限%'].fillna(-99), -10, 10) * 0.4 +
        np.log1p(candidates['樣本數']) * 0.25
    )

    model_summary = (
        candidates.groupby('模型', as_index=False)
        .agg(
            測試組合數=('模型','size'),
            三項全改善比例=('三項全改善', lambda s: float(s.mean()*100)),
            平均勝率改善ppt=('勝率改善ppt','mean'),
            平均報酬改善ppt=('平均報酬改善ppt','mean'),
            平均PF改善=('PF改善','mean'),
            平均樣本數=('樣本數','mean'),
            平均穩健分=('穩健分','mean'),
        )
        .sort_values(['三項全改善比例','平均報酬改善ppt','平均PF改善'],
                     ascending=[False,False,False])
    )

    # 外資連買區間驗證：只針對 A 技術95+外資5
    streak_rows = []
    baseA = z.copy()
    baseA['_A分'] = baseA['A 技術95＋外資5']
    baseA['外資連買區間'] = baseA['外資連買賣天數'].apply(_v353_streak_band)

    for th in thresholds:
        for h in horizons:
            retcol = f'報酬{h}日%'
            for band, g in baseA.groupby('外資連買區間'):
                tr = _v353_nonoverlap(g, '_A分', th, h)
                if len(tr) < min_sample:
                    continue
                m = _v353_metrics(tr, retcol)
                streak_rows.append({'門檻':th,'持有日':h,'外資連買區間':band,**m})
    streak_df = pd.DataFrame(streak_rows)

    # 外資5日強度區間驗證
    intensity_rows = []
    baseA['外資強度區間'] = baseA['外資5日強度%'].apply(_v353_intensity_band)
    for th in thresholds:
        for h in horizons:
            retcol = f'報酬{h}日%'
            for band, g in baseA.groupby('外資強度區間'):
                tr = _v353_nonoverlap(g, '_A分', th, h)
                if len(tr) < min_sample:
                    continue
                m = _v353_metrics(tr, retcol)
                intensity_rows.append({'門檻':th,'持有日':h,'外資強度區間':band,**m})
    intensity_df = pd.DataFrame(intensity_rows)

    return grid, model_summary, streak_df, intensity_df


# ===== V3.6.12 外資最佳權重驗證 =====
def run_v354_weight_curve(event_base, thresholds=(75,80,85,90), horizons=(20,30,40),
                          min_sample=40, weights=(0,2.5,5,7.5,10,12.5,15)):
    if event_base is None or event_base.empty:return pd.DataFrame(),pd.DataFrame(),pd.DataFrame(),pd.DataFrame(),pd.DataFrame()
    z=event_base.copy();rows=[]
    for fw in weights:
        col=f'_W{fw:g}';z[col]=z['技術分數']*((100-fw)/100)+z['外資因子分']*(fw/100)
        for th in thresholds:
            for h in horizons:
                rc=f'報酬{h}日%'
                if rc not in z.columns:continue
                tr=_v353_nonoverlap(z,col,th,h)
                if len(tr)<min_sample:continue
                m=_v353_metrics(tr,rc);yrs,ypos=_v353_year_stats(tr,rc)
                rows.append({'外資權重%':fw,'技術權重%':100-fw,'門檻':th,'持有日':h,**m,'涵蓋年度數':yrs,'年度正報酬比例%':ypos})
    grid=pd.DataFrame(rows)
    if grid.empty:return grid,pd.DataFrame(),pd.DataFrame(),pd.DataFrame(),pd.DataFrame()
    base=grid[grid['外資權重%']==0][['門檻','持有日','勝率%','平均報酬%','PF']].rename(columns={'勝率%':'基準勝率%','平均報酬%':'基準平均報酬%','PF':'基準PF'})
    comp=grid.merge(base,on=['門檻','持有日'],how='left')
    comp['勝率改善ppt']=comp['勝率%']-comp['基準勝率%'];comp['平均報酬改善ppt']=comp['平均報酬%']-comp['基準平均報酬%'];comp['PF改善']=comp['PF']-comp['基準PF']
    comp['三項全改善']=(comp['勝率改善ppt']>0)&(comp['平均報酬改善ppt']>0)&(comp['PF改善']>0)
    s=(comp[comp['外資權重%']>0].groupby('外資權重%',as_index=False)
       .agg(測試組合數=('外資權重%','size'),三項全改善比例=('三項全改善',lambda x:x.mean()*100),
            平均勝率改善ppt=('勝率改善ppt','mean'),平均報酬改善ppt=('平均報酬改善ppt','mean'),
            平均PF改善=('PF改善','mean'),平均樣本數=('樣本數','mean'),平均CI下限=('95%CI下限%','mean')))
    s['通過基本證據']=(s['平均勝率改善ppt']>0)&(s['平均報酬改善ppt']>0)&(s['平均PF改善']>0)
    s['證據分']=s['三項全改善比例']*.08+s['平均勝率改善ppt']+s['平均報酬改善ppt']*2+s['平均PF改善']*5+np.clip(s['平均CI下限'],-10,10)*.25
    s=s.sort_values(['通過基本證據','證據分'],ascending=[False,False])
    hold=(comp[comp['外資權重%']>0].groupby(['外資權重%','持有日'],as_index=False)
          .agg(平均勝率改善ppt=('勝率改善ppt','mean'),平均報酬改善ppt=('平均報酬改善ppt','mean'),
               平均PF改善=('PF改善','mean'),三項全改善比例=('三項全改善',lambda x:x.mean()*100),平均樣本數=('樣本數','mean')))
    valid=s[s['通過基本證據']];bestw=float(valid.iloc[0]['外資權重%']) if not valid.empty else float(s.iloc[0]['外資權重%'])
    z['_BEST']=z['技術分數']*((100-bestw)/100)+z['外資因子分']*(bestw/100)
    z['外資連買區間']=z['外資連買賣天數'].apply(_v353_streak_band);z['外資強度區間']=z['外資5日強度%'].apply(_v353_intensity_band)
    def bands(col):
        rr=[]
        for th in thresholds:
            for h in horizons:
                rc=f'報酬{h}日%'
                for band,g in z.groupby(col):
                    tr=_v353_nonoverlap(g,'_BEST',th,h)
                    if len(tr)>=min_sample:rr.append({'最佳外資權重%':bestw,'門檻':th,'持有日':h,col:band,**_v353_metrics(tr,rc)})
        return pd.DataFrame(rr)
    return comp,s,hold,bands('外資連買區間'),bands('外資強度區間')


def add_v356_flip_features(event_base, chip_hist):
    """
    將外資買賣超的前一日/前兩日/前三日狀態對齊到事件底表，
    產生「賣→買第1天」「連賣後轉買」等翻多條件。
    """
    if event_base is None or event_base.empty or chip_hist is None or chip_hist.empty:
        return event_base

    ch = chip_hist.copy()
    ch['日期'] = pd.to_datetime(ch['日期']).dt.tz_localize(None).dt.normalize()
    ch = ch.sort_values(['股票','日期'])
    ch['外資_今日'] = pd.to_numeric(ch['外資買賣超股數'], errors='coerce')
    ch['外資_前1日'] = ch.groupby('股票')['外資_今日'].shift(1)
    ch['外資_前2日'] = ch.groupby('股票')['外資_今日'].shift(2)
    ch['外資_前3日'] = ch.groupby('股票')['外資_今日'].shift(3)

    ch['外資賣轉買第1天'] = (
        (ch['外資_前1日'] < 0) &
        (ch['外資_今日'] > 0)
    )

    ch['外資連賣2日後轉買'] = (
        (ch['外資_前1日'] < 0) &
        (ch['外資_前2日'] < 0) &
        (ch['外資_今日'] > 0)
    )

    ch['外資連賣3日後轉買'] = (
        (ch['外資_前1日'] < 0) &
        (ch['外資_前2日'] < 0) &
        (ch['外資_前3日'] < 0) &
        (ch['外資_今日'] > 0)
    )

    keep = ch[['股票','日期','外資賣轉買第1天','外資連賣2日後轉買','外資連賣3日後轉買']].copy()
    base = event_base.copy()
    base['日期'] = pd.to_datetime(base['日期']).dt.tz_localize(None).dt.normalize()
    return base.merge(keep, on=['股票','日期'], how='left')


# ===== V3.6.12 外資 Gate 驗證 =====
# 結論延伸：外資不直接加權，改測「是否應當作進場確認條件」。
def _v355_gate_mask(z, gate_name):
    fs = pd.to_numeric(z['外資連買賣天數'], errors='coerce').fillna(0)
    fi = pd.to_numeric(z['外資5日強度%'], errors='coerce')
    flip1 = z.get('外資賣轉買第1天', pd.Series(False,index=z.index)).fillna(False).astype(bool)
    flip2 = z.get('外資連賣2日後轉買', pd.Series(False,index=z.index)).fillna(False).astype(bool)
    flip3 = z.get('外資連賣3日後轉買', pd.Series(False,index=z.index)).fillna(False).astype(bool)

    if gate_name == '原技術訊號':
        return pd.Series(True, index=z.index)
    if gate_name == '連買1-2天＋強度≥2%':
        return (fs >= 1) & (fs <= 2) & (fi >= 2.0)
    if gate_name == '賣→買第1天':
        return flip1
    if gate_name == '連賣2日→轉買':
        return flip2
    if gate_name == '連賣3日→轉買':
        return flip3
    if gate_name == '賣→買＋強度≥1%':
        return flip1 & (fi >= 1.0)
    if gate_name == '賣→買＋強度≥2%':
        return flip1 & (fi >= 2.0)
    if gate_name == '連賣2日→轉買＋強度≥1%':
        return flip2 & (fi >= 1.0)
    if gate_name == '連賣2日→轉買＋強度≥2%':
        return flip2 & (fi >= 2.0)
    return pd.Series(False, index=z.index)

def run_v355_gate_validation(event_base, thresholds=(75,80,85,90),
                             horizons=(20,30,40), min_sample=30):
    if event_base is None or event_base.empty:
        return pd.DataFrame(), pd.DataFrame()

    gates = [
        '原技術訊號',
        '連買1-2天＋強度≥2%',
        '賣→買第1天',
        '連賣2日→轉買',
        '連賣3日→轉買',
        '賣→買＋強度≥1%',
        '賣→買＋強度≥2%',
        '連賣2日→轉買＋強度≥1%',
        '連賣2日→轉買＋強度≥2%',
    ]

    rows = []
    z = event_base.copy()

    for th in thresholds:
        for h in horizons:
            retcol = f'報酬{h}日%'
            if retcol not in z.columns:
                continue

            base_candidates = z[pd.to_numeric(z['技術分數'], errors='coerce') >= th].copy()
            base_trades = _v353_nonoverlap(base_candidates.assign(_score=z.loc[base_candidates.index,'技術分數']),
                                           '_score', th, h)
            base_m = _v353_metrics(base_trades, retcol)
            base_n = max(int(base_m.get('樣本數',0)), 1)

            for gate in gates:
                g = base_candidates[_v355_gate_mask(base_candidates, gate)].copy()
                if g.empty:
                    continue
                g['_score'] = g['技術分數']
                tr = _v353_nonoverlap(g, '_score', th, h)

                # 原技術訊號允許作為基準；Gate 組需達最低樣本
                if gate != '原技術訊號' and len(tr) < min_sample:
                    continue

                m = _v353_metrics(tr, retcol)
                yrs, ypos = _v353_year_stats(tr, retcol)

                rows.append({
                    'Gate': gate,
                    '門檻': th,
                    '持有日': h,
                    **m,
                    '訊號保留率%': (m['樣本數'] / base_n * 100) if base_n > 0 else np.nan,
                    '勝率改善ppt': m['勝率%'] - base_m['勝率%'] if pd.notna(base_m['勝率%']) else np.nan,
                    '平均報酬改善ppt': m['平均報酬%'] - base_m['平均報酬%'] if pd.notna(base_m['平均報酬%']) else np.nan,
                    'PF改善': m['PF'] - base_m['PF'] if pd.notna(m['PF']) and pd.notna(base_m['PF']) else np.nan,
                    '涵蓋年度數': yrs,
                    '年度正報酬比例%': ypos,
                })

    grid = pd.DataFrame(rows)
    if grid.empty:
        return grid, pd.DataFrame()

    test = grid[grid['Gate'] != '原技術訊號'].copy()
    test['三項全改善'] = (
        (test['勝率改善ppt'] > 0) &
        (test['平均報酬改善ppt'] > 0) &
        (test['PF改善'] > 0)
    )
    test['品質提升但不過濾過度'] = (
        test['三項全改善'] &
        (test['訊號保留率%'] >= 20)
    )

    summary = (
        test.groupby('Gate', as_index=False)
        .agg(
            測試組合數=('Gate','size'),
            三項全改善比例=('三項全改善', lambda s: s.mean()*100),
            有效改善比例=('品質提升但不過濾過度', lambda s: s.mean()*100),
            平均訊號保留率=('訊號保留率%','mean'),
            平均勝率改善ppt=('勝率改善ppt','mean'),
            平均報酬改善ppt=('平均報酬改善ppt','mean'),
            平均PF改善=('PF改善','mean'),
            平均樣本數=('樣本數','mean'),
            平均CI下限=('95%CI下限%','mean'),
        )
    )

    summary['通過Gate證據'] = (
        (summary['三項全改善比例'] >= 60) &
        (summary['平均勝率改善ppt'] > 0) &
        (summary['平均報酬改善ppt'] > 0) &
        (summary['平均PF改善'] > 0) &
        (summary['平均訊號保留率'] >= 20)
    )

    summary['Gate證據分'] = (
        summary['三項全改善比例'] * 0.08 +
        summary['有效改善比例'] * 0.05 +
        summary['平均勝率改善ppt'] * 1.0 +
        summary['平均報酬改善ppt'] * 2.0 +
        summary['平均PF改善'] * 5.0 +
        np.clip(summary['平均CI下限'], -10, 10) * 0.25 -
        np.where(summary['平均訊號保留率'] < 20, 5, 0)
    )

    summary = summary.sort_values(
        ['通過Gate證據','Gate證據分','三項全改善比例'],
        ascending=[False,False,False]
    )
    return grid, summary



# ===== V3.6.12 正式版：法人只做資訊標籤，不參與技術100分 =====

def v361_chip_diagnostics(result_df, chip_days=15):
    """
    把法人資料管線逐段診斷出來：
    1. T86歷史筆數 / 最新日
    2. chip_features 筆數
    3. 排行榜股票數
    4. merge成功數
    5. 外資/投信非空數
    """
    out = {
        'T86歷史筆數':0,
        'T86股票數':0,
        'T86最新日期':'—',
        'chip_features筆數':0,
        '排行榜股票數':0,
        'merge成功數':0,
        '外資有效檔數':0,
        '投信有效檔數':0,
        'T86抓取狀態':'',
        '狀態':''
    }

    if result_df is None or result_df.empty:
        out['狀態']='排行榜無資料'
        return out, pd.DataFrame(), pd.DataFrame()

    out['排行榜股票數']=int(result_df['股票'].astype(str).str.zfill(4).nunique())

    try:
        hist,_t86_status=load_twse_chip_history(chip_days,return_status=True)
    except Exception as e:
        out['狀態']=f'T86讀取失敗：{type(e).__name__}: {str(e)[:100]}'
        return out, pd.DataFrame(), pd.DataFrame()

    out['T86抓取狀態']=_t86_status

    if hist is None or hist.empty:
        out['狀態']=f'T86歷史資料為空｜{_t86_status}'
        return out, pd.DataFrame(), pd.DataFrame()

    out['T86歷史筆數']=len(hist)
    out['T86股票數']=int(hist['股票'].astype(str).str.zfill(4).nunique())
    try:
        out['T86最新日期']=pd.to_datetime(hist['日期']).max().strftime('%Y-%m-%d')
    except Exception:
        out['T86最新日期']='日期解析失敗'

    try:
        pmap={
            str(r['股票']).zfill(4):r['_df']
            for _,r in result_df.iterrows()
            if '_df' in result_df.columns and r.get('_df') is not None
        }
        cf=chip_features(hist,pmap)
    except Exception as e:
        out['狀態']=f'chip_features失敗：{type(e).__name__}: {str(e)[:100]}'
        return out, hist, pd.DataFrame()

    if cf is None or cf.empty:
        out['狀態']='chip_features結果為空'
        return out, hist, pd.DataFrame()

    cf=cf.copy()
    cf['股票']=cf['股票'].astype(str).str.zfill(4)
    out['chip_features筆數']=len(cf)

    base=result_df[['股票']].copy()
    base['股票']=base['股票'].astype(str).str.zfill(4)

    merged=base.merge(cf,on='股票',how='left')
    if '籌碼資料日' in merged.columns:
        out['merge成功數']=int(merged['籌碼資料日'].notna().sum())
    if '外資連買賣天數' in merged.columns:
        out['外資有效檔數']=int(pd.to_numeric(merged['外資連買賣天數'],errors='coerce').notna().sum())
    if '投信連買賣天數' in merged.columns:
        out['投信有效檔數']=int(pd.to_numeric(merged['投信連買賣天數'],errors='coerce').notna().sum())

    if out['merge成功數']==0:
        sample_result=base['股票'].head(10).tolist()
        sample_chip=cf['股票'].head(10).tolist()
        out['狀態']=f'merge=0；排行榜樣本{sample_result} / 法人樣本{sample_chip}'
    else:
        out['狀態']='OK'

    return out, hist, cf


def v360_merge_chip_data(result_df, chip_days=15):
    """
    正式法人資料管線：
    TWSE T86 -> 最近交易日法人歷史 -> 計算連買賣/5日強度 -> merge 到 result。
    只新增資訊欄位，不改技術100分。
    """
    if result_df is None or result_df.empty:
        return result_df, pd.DataFrame(), '無排行榜資料'

    try:
        hist, _hist_status = load_twse_chip_history(chip_days, return_status=True)
    except Exception as e:
        return result_df.copy(), pd.DataFrame(), f'法人資料讀取失敗：{type(e).__name__}: {str(e)[:100]}'

    if hist is None or hist.empty:
        return result_df.copy(), pd.DataFrame(), f'TWSE T86 無有效資料｜{_hist_status}'

    try:
        price_map = {
            str(r['股票']).zfill(4): r['_df']
            for _, r in result_df.iterrows()
            if '_df' in r and r['_df'] is not None
        }
        cf = chip_features(hist, price_map)
    except Exception as e:
        return result_df.copy(), pd.DataFrame(), f'法人因子計算失敗：{type(e).__name__}: {str(e)[:100]}'

    if cf is None or cf.empty:
        return result_df.copy(), pd.DataFrame(), '法人因子計算後無資料'

    out = result_df.copy()
    out['股票'] = out['股票'].astype(str).str.zfill(4)
    cf['股票'] = cf['股票'].astype(str).str.zfill(4)

    merge_cols = [
        '股票',
        '外資連買賣天數',
        '投信連買賣天數',
        '外資5日買賣超股數',
        '投信5日買賣超股數',
        '外資5日強度%',
        '投信5日強度%',
        '籌碼資料日'
    ]
    merge_cols = [c for c in merge_cols if c in cf.columns]
    out = out.merge(cf[merge_cols], on='股票', how='left')

    latest = ''
    if '籌碼資料日' in cf.columns and cf['籌碼資料日'].notna().any():
        latest = str(cf['籌碼資料日'].dropna().max())

    return out, cf, latest or '日期未知'


def v358_chip_label(foreign_streak, foreign_strength, trust_streak=None):
    fs = pd.to_numeric(pd.Series([foreign_streak]), errors='coerce').iloc[0]
    fi = pd.to_numeric(pd.Series([foreign_strength]), errors='coerce').iloc[0]
    ts = pd.to_numeric(pd.Series([trust_streak]), errors='coerce').iloc[0] if trust_streak is not None else np.nan

    if pd.isna(fs):
        foreign = '⚪ 外資無資料'
    elif fs > 0:
        foreign = f'🟢 外資連買{int(fs)}日'
    elif fs < 0:
        foreign = f'🔴 外資連賣{abs(int(fs))}日'
    else:
        foreign = '⚪ 外資中性'

    if pd.notna(fi):
        foreign += f'｜5日強度{fi:+.2f}%'

    if pd.isna(ts):
        trust = '⚪ 投信無資料'
    elif ts > 0:
        trust = f'🟢 投信連買{int(ts)}日'
    elif ts < 0:
        trust = f'🔴 投信連賣{abs(int(ts))}日'
    else:
        trust = '⚪ 投信中性'

    return foreign, trust

def v358_attach_chip_labels(df):
    """只新增顯示欄，不修改黑嚕嚕技術分數。"""
    if df is None or df.empty:
        return df
    x = df.copy()

    # 相容既有欄名；找不到就顯示無資料。
    f_streak_col = next((c for c in ['外資連買賣天數','外資連買天數'] if c in x.columns), None)
    f_strength_col = next((c for c in ['外資5日強度%','外資強度%'] if c in x.columns), None)
    t_streak_col = next((c for c in ['投信連買賣天數','投信連買天數'] if c in x.columns), None)

    labels=[]; trusts=[]
    for _,r in x.iterrows():
        fs = r.get(f_streak_col, np.nan) if f_streak_col else np.nan
        fi = r.get(f_strength_col, np.nan) if f_strength_col else np.nan
        ts = r.get(t_streak_col, np.nan) if t_streak_col else np.nan
        a,b = v358_chip_label(fs,fi,ts)
        labels.append(a); trusts.append(b)

    x['外資狀態'] = labels
    x['投信狀態'] = trusts
    return x


# ===== V3.6.12 B：進出場 / 停損停利研究 =====
def _v360_trade_metrics(rets):
    r = pd.Series(rets, dtype=float).dropna()
    if r.empty:
        return {
            '樣本數':0,'勝率%':np.nan,'平均報酬%':np.nan,'中位數%':np.nan,
            'PF':np.nan,'平均獲利%':np.nan,'平均虧損%':np.nan,'最大單筆虧損%':np.nan
        }
    gp = r[r>0].sum()
    gl = -r[r<0].sum()
    return {
        '樣本數':len(r),
        '勝率%':(r>0).mean()*100,
        '平均報酬%':r.mean(),
        '中位數%':r.median(),
        'PF':gp/gl if gl>0 else np.nan,
        '平均獲利%':r[r>0].mean() if (r>0).any() else np.nan,
        '平均虧損%':r[r<0].mean() if (r<0).any() else np.nan,
        '最大單筆虧損%':r.min()
    }

def _v360_entry_exit_one(df, entry_i, max_hold=30, stop_loss_pct=None,
                         take_profit_pct=None, trailing_pct=None,
                         ma_exit=None):
    """
    日K研究版：
    - 進場：訊號日收盤
    - 固定停損/停利：以日內 High/Low 是否觸及判定
    - 同日同時碰停損與停利：採保守原則，先算停損
    - 移動停利：以進場後最高價回撤%
    - MA出場：收盤跌破指定MA
    """
    if df is None or entry_i >= len(df)-1:
        return None

    entry = float(df['Close'].iloc[entry_i])
    if entry <= 0:
        return None

    highest = entry
    exit_i = min(entry_i + max_hold, len(df)-1)
    exit_price = float(df['Close'].iloc[exit_i])
    reason = f'持有{max_hold}日'

    for j in range(entry_i+1, min(entry_i+max_hold, len(df)-1)+1):
        high = float(df['High'].iloc[j])
        low = float(df['Low'].iloc[j])
        close = float(df['Close'].iloc[j])
        highest = max(highest, high)

        sl_price = entry * (1 - stop_loss_pct/100) if stop_loss_pct else None
        tp_price = entry * (1 + take_profit_pct/100) if take_profit_pct else None

        # 保守假設：同一天同時碰停損與停利，先視為停損
        if sl_price is not None and low <= sl_price:
            exit_i, exit_price, reason = j, sl_price, f'停損{stop_loss_pct:g}%'
            break

        if tp_price is not None and high >= tp_price:
            exit_i, exit_price, reason = j, tp_price, f'停利{take_profit_pct:g}%'
            break

        if trailing_pct:
            trail_price = highest * (1 - trailing_pct/100)
            if low <= trail_price and highest > entry:
                exit_i, exit_price, reason = j, trail_price, f'移動停利{trailing_pct:g}%'
                break

        if ma_exit:
            col = f'MA{int(ma_exit)}'
            if col in df.columns:
                ma = pd.to_numeric(df[col].iloc[j], errors='coerce')
                if pd.notna(ma) and close < float(ma):
                    exit_i, exit_price, reason = j, close, f'跌破MA{int(ma_exit)}'
                    break

    ret = (exit_price/entry - 1)*100
    return {
        '進場日':pd.Timestamp(df.index[entry_i]).strftime('%Y-%m-%d'),
        '出場日':pd.Timestamp(df.index[exit_i]).strftime('%Y-%m-%d'),
        '進場價':entry,'出場價':exit_price,'報酬%':ret,
        '持有天數':exit_i-entry_i,'出場原因':reason
    }

def run_v360_exit_lab(result_df, score_threshold=85, cooldown=20,
                      max_hold_list=(20,30,40),
                      stop_losses=(None,5,8,10),
                      take_profits=(None,10,15,20),
                      trailing_list=(None,8,10),
                      ma_exits=(None,15,60,200),
                      min_sample=30):
    """
    以歷史技術分數訊號為進場，交叉測試出場規則。
    為避免組合爆炸，只測「單一類型風控」：
    1) 純時間出場
    2) 固定停損
    3) 固定停利
    4) 移動停利
    5) MA跌破出場
    """
    rows=[]
    detail=[]

    configs=[]
    for hold in max_hold_list:
        configs.append((f'時間出場{hold}日',hold,None,None,None,None))
        for sl in stop_losses:
            if sl is not None:
                configs.append((f'{hold}日＋停損{sl}%',hold,sl,None,None,None))
        for tp in take_profits:
            if tp is not None:
                configs.append((f'{hold}日＋停利{tp}%',hold,None,tp,None,None))
        for tr in trailing_list:
            if tr is not None:
                configs.append((f'{hold}日＋移動停利{tr}%',hold,None,None,tr,None))
        for ma in ma_exits:
            if ma is not None:
                configs.append((f'{hold}日＋跌破MA{ma}',hold,None,None,None,ma))

    for cfg_name,hold,sl,tp,tr,ma in configs:
        rets=[]
        reasons=[]
        trades=[]
        for _,rr in result_df.iterrows():
            code=str(rr['股票']).zfill(4)
            name=rr.get('名稱','')
            df=rr.get('_df')
            if df is None or len(df)<230:
                continue

            d=indicators(df.copy())
            if d is None or len(d)<230:
                continue

            last_entry=-999999
            for i in range(220, len(d)-max(max_hold_list)-1):
                if i-last_entry < cooldown:
                    continue
                try:
                    score=float(black_score(d.iloc[:i+1])[0])
                except Exception:
                    continue
                if score < score_threshold:
                    continue

                trade=_v360_entry_exit_one(
                    d,i,max_hold=hold,
                    stop_loss_pct=sl,
                    take_profit_pct=tp,
                    trailing_pct=tr,
                    ma_exit=ma
                )
                if not trade:
                    continue

                rets.append(trade['報酬%'])
                reasons.append(trade['出場原因'])
                trades.append({
                    '策略':cfg_name,'股票':code,'名稱':name,
                    '技術分數':score,**trade
                })
                last_entry=i

        if len(rets) < min_sample:
            continue

        m=_v360_trade_metrics(rets)
        reason_s=pd.Series(reasons)
        stop_rate=(reason_s.str.contains('停損',na=False).mean()*100) if len(reason_s) else np.nan
        tp_rate=(reason_s.str.contains('停利',na=False).mean()*100) if len(reason_s) else np.nan
        ma_rate=(reason_s.str.contains('跌破MA',na=False).mean()*100) if len(reason_s) else np.nan

        rows.append({
            '策略':cfg_name,
            '最大持有日':hold,
            '停損%':sl if sl is not None else np.nan,
            '停利%':tp if tp is not None else np.nan,
            '移動停利%':tr if tr is not None else np.nan,
            'MA出場':ma if ma is not None else np.nan,
            **m,
            '停損觸發率%':stop_rate,
            '停利觸發率%':tp_rate,
            'MA出場率%':ma_rate
        })
        detail.extend(trades)

    summary=pd.DataFrame(rows)
    detail_df=pd.DataFrame(detail)

    if not summary.empty:
        # 綜合排序：PF/報酬/勝率兼顧，同時懲罰最大單筆虧損過大
        summary['風控分'] = (
            summary['平均報酬%'].fillna(-99)*2 +
            summary['勝率%'].fillna(0)*0.08 +
            summary['PF'].fillna(0)*5 +
            summary['最大單筆虧損%'].fillna(-99)*0.15
        )
        summary=summary.sort_values(
            ['風控分','PF','平均報酬%','勝率%'],
            ascending=[False,False,False,False]
        )

    return summary,detail_df



# ===== V3.6.12 40日風控第二階段 =====
def _v361_entry_exit_one(df, entry_i, max_hold=40,
                         initial_stop=None,
                         ma_confirm=None,
                         ma_confirm_days=2,
                         profit_trigger=None,
                         trailing_after_profit=None,
                         breakeven_trigger=None):
    """
    第二階段風控：
    - 初始停損
    - MA跌破需連續確認N日
    - 獲利達門檻後才啟動移動停利
    - 獲利達門檻後停損拉到成本附近
    日K內同時觸發多條件時採保守順序：
    初始停損 -> 保本 -> 移動停利 -> MA確認 -> 時間出場
    """
    if df is None or entry_i >= len(df)-1:
        return None

    entry=float(df['Close'].iloc[entry_i])
    if entry<=0:
        return None

    highest=entry
    ma_below_count=0
    exit_i=min(entry_i+max_hold,len(df)-1)
    exit_price=float(df['Close'].iloc[exit_i])
    reason=f'時間出場{max_hold}日'

    for j in range(entry_i+1,min(entry_i+max_hold,len(df)-1)+1):
        high=float(df['High'].iloc[j]); low=float(df['Low'].iloc[j]); close=float(df['Close'].iloc[j])
        highest=max(highest,high)
        unreal=(highest/entry-1)*100

        # 1. 初始停損
        if initial_stop is not None:
            sl=entry*(1-initial_stop/100)
            if low<=sl:
                return {'進場日':pd.Timestamp(df.index[entry_i]).strftime('%Y-%m-%d'),
                        '出場日':pd.Timestamp(df.index[j]).strftime('%Y-%m-%d'),
                        '進場價':entry,'出場價':sl,'報酬%':(sl/entry-1)*100,
                        '持有天數':j-entry_i,'出場原因':f'初始停損{initial_stop:g}%'}

        # 2. 保本
        if breakeven_trigger is not None and unreal>=breakeven_trigger:
            if low<=entry:
                return {'進場日':pd.Timestamp(df.index[entry_i]).strftime('%Y-%m-%d'),
                        '出場日':pd.Timestamp(df.index[j]).strftime('%Y-%m-%d'),
                        '進場價':entry,'出場價':entry,'報酬%':0.0,
                        '持有天數':j-entry_i,'出場原因':f'獲利{breakeven_trigger:g}%後保本'}

        # 3. 獲利後啟動移動停利
        if profit_trigger is not None and trailing_after_profit is not None and unreal>=profit_trigger:
            trail=highest*(1-trailing_after_profit/100)
            if low<=trail:
                return {'進場日':pd.Timestamp(df.index[entry_i]).strftime('%Y-%m-%d'),
                        '出場日':pd.Timestamp(df.index[j]).strftime('%Y-%m-%d'),
                        '進場價':entry,'出場價':trail,'報酬%':(trail/entry-1)*100,
                        '持有天數':j-entry_i,'出場原因':f'獲利{profit_trigger:g}%後移動停利{trailing_after_profit:g}%'}

        # 4. MA連續確認
        if ma_confirm is not None:
            col=f'MA{int(ma_confirm)}'
            if col in df.columns:
                ma=pd.to_numeric(df[col].iloc[j],errors='coerce')
                if pd.notna(ma) and close<float(ma):
                    ma_below_count += 1
                else:
                    ma_below_count = 0
                if ma_below_count >= ma_confirm_days:
                    return {'進場日':pd.Timestamp(df.index[entry_i]).strftime('%Y-%m-%d'),
                            '出場日':pd.Timestamp(df.index[j]).strftime('%Y-%m-%d'),
                            '進場價':entry,'出場價':close,'報酬%':(close/entry-1)*100,
                            '持有天數':j-entry_i,'出場原因':f'跌破MA{int(ma_confirm)}確認{ma_confirm_days}日'}

    return {'進場日':pd.Timestamp(df.index[entry_i]).strftime('%Y-%m-%d'),
            '出場日':pd.Timestamp(df.index[exit_i]).strftime('%Y-%m-%d'),
            '進場價':entry,'出場價':exit_price,'報酬%':(exit_price/entry-1)*100,
            '持有天數':exit_i-entry_i,'出場原因':reason}

def run_v361_risk_lab(result_df, score_threshold=85, cooldown=20, min_sample=30):
    configs=[
        ('40日純時間',dict(max_hold=40)),
        ('40日＋初始停損10%',dict(max_hold=40,initial_stop=10)),
        ('40日＋MA15確認2日',dict(max_hold=40,ma_confirm=15,ma_confirm_days=2)),
        ('40日＋MA60確認2日',dict(max_hold=40,ma_confirm=60,ma_confirm_days=2)),
        ('40日＋獲利10%後移動停利8%',dict(max_hold=40,profit_trigger=10,trailing_after_profit=8)),
        ('40日＋獲利15%後移動停利8%',dict(max_hold=40,profit_trigger=15,trailing_after_profit=8)),
        ('40日＋獲利10%後保本',dict(max_hold=40,breakeven_trigger=10)),
        ('40日＋獲利15%後保本',dict(max_hold=40,breakeven_trigger=15)),
        ('40日＋停損10%＋獲利15%後移動停利8%',
         dict(max_hold=40,initial_stop=10,profit_trigger=15,trailing_after_profit=8)),
        ('40日＋停損10%＋MA60確認2日',
         dict(max_hold=40,initial_stop=10,ma_confirm=60,ma_confirm_days=2)),
    ]

    rows=[];detail=[]
    for name,cfg in configs:
        rets=[];reasons=[];trades=[]
        for _,rr in result_df.iterrows():
            code=str(rr['股票']).zfill(4); nm=rr.get('名稱',''); df=rr.get('_df')
            if df is None or len(df)<270: continue
            d=indicators(df.copy())
            if d is None or len(d)<270: continue

            last=-999999
            for i in range(220,len(d)-61):
                if i-last<cooldown: continue
                try: score=float(black_score(d.iloc[:i+1])[0])
                except Exception: continue
                if score<score_threshold: continue

                tr=_v361_entry_exit_one(d,i,**cfg)
                if tr is None: continue
                rets.append(tr['報酬%']); reasons.append(tr['出場原因'])
                trades.append({'策略':name,'股票':code,'名稱':nm,'技術分數':score,**tr})
                last=i

        if len(rets)<min_sample: continue
        m=_v360_trade_metrics(rets)
        rs=pd.Series(reasons,dtype=str)
        rows.append({
            '策略':name,**m,
            '停損觸發率%':rs.str.contains('停損',na=False).mean()*100 if len(rs) else np.nan,
            '保本觸發率%':rs.str.contains('保本',na=False).mean()*100 if len(rs) else np.nan,
            '移動停利觸發率%':rs.str.contains('移動停利',na=False).mean()*100 if len(rs) else np.nan,
            'MA確認出場率%':rs.str.contains('確認',na=False).mean()*100 if len(rs) else np.nan,
        })
        detail.extend(trades)

    s=pd.DataFrame(rows); ddf=pd.DataFrame(detail)
    if not s.empty:
        # 比純時間基準的改善
        b=s[s['策略']=='40日純時間']
        if not b.empty:
            br=b.iloc[0]
            s['勝率改善ppt']=s['勝率%']-br['勝率%']
            s['平均報酬改善ppt']=s['平均報酬%']-br['平均報酬%']
            s['PF改善']=s['PF']-br['PF']
            s['最大虧損改善ppt']=s['最大單筆虧損%']-br['最大單筆虧損%']
        s['風控平衡分']=(
            s['平均報酬%'].fillna(-99)*1.5 +
            s['PF'].fillna(0)*5 +
            s['勝率%'].fillna(0)*0.05 +
            s['最大單筆虧損%'].fillna(-99)*0.30
        )
        s=s.sort_values(['風控平衡分','PF','平均報酬%'],ascending=[False,False,False])
    return s,ddf


# ===== V3.6.12 MAE/MFE + 停損甜蜜點 =====
def _v362_excursion(df, entry_i, hold=40):
    if df is None or entry_i>=len(df)-1:
        return np.nan,np.nan,np.nan
    entry=float(df['Close'].iloc[entry_i])
    if entry<=0:return np.nan,np.nan,np.nan
    end=min(entry_i+hold,len(df)-1)
    highs=pd.to_numeric(df['High'].iloc[entry_i+1:end+1],errors='coerce')
    lows=pd.to_numeric(df['Low'].iloc[entry_i+1:end+1],errors='coerce')
    if highs.empty or lows.empty:return np.nan,np.nan,np.nan
    return ((float(lows.min())/entry-1)*100,
            (float(highs.max())/entry-1)*100,
            (float(df['Close'].iloc[end])/entry-1)*100)

def collect_v362_base_trades(result_df, score_threshold=85, cooldown=20, hold=40):
    rows=[]
    for _,rr in result_df.iterrows():
        code=str(rr['股票']).zfill(4);nm=rr.get('名稱','');df=rr.get('_df')
        if df is None or len(df)<270:continue
        d=indicators(df.copy());last=-999999
        for i in range(220,len(d)-hold-1):
            if i-last<cooldown:continue
            try:score=float(black_score(d.iloc[:i+1])[0])
            except Exception:continue
            if score<score_threshold:continue
            mae,mfe,ret=_v362_excursion(d,i,hold)
            if pd.isna(ret):continue
            rows.append({'股票':code,'名稱':nm,'進場日':pd.Timestamp(d.index[i]).strftime('%Y-%m-%d'),
                         '技術分數':score,'40日報酬%':ret,'MAE%':mae,'MFE%':mfe,
                         '最後結果':'獲利' if ret>0 else '虧損'})
            last=i
    return pd.DataFrame(rows)

def v362_mae_profile(base):
    if base is None or base.empty:return pd.DataFrame()
    rows=[]
    for label,g in [('全部',base),('最後獲利',base[base['40日報酬%']>0]),('最後虧損',base[base['40日報酬%']<=0])]:
        if g.empty:continue
        mae=pd.to_numeric(g['MAE%'],errors='coerce').dropna()
        mfe=pd.to_numeric(g['MFE%'],errors='coerce').dropna()
        rows.append({'族群':label,'樣本數':len(g),'MAE中位數%':mae.median(),
                     'MAE25分位%':mae.quantile(.25),'MAE10分位%':mae.quantile(.10),
                     'MFE中位數%':mfe.median(),'MFE75分位%':mfe.quantile(.75),
                     'MFE90分位%':mfe.quantile(.90),
                     '40日平均報酬%':pd.to_numeric(g['40日報酬%'],errors='coerce').mean()})
    return pd.DataFrame(rows)

def run_v362_stop_sweep(result_df, score_threshold=85, cooldown=20,
                        stops=(7,8,9,10,11,12,15), hold=40, min_sample=30):
    rows=[];details=[]
    configs=[('40日純時間',None)]+[(f'40日＋停損{s:g}%',float(s)) for s in stops]

    for name,sl in configs:
        rets=[];trades=[]
        for _,rr in result_df.iterrows():
            code=str(rr['股票']).zfill(4);nm=rr.get('名稱','');df=rr.get('_df')
            if df is None or len(df)<270:continue
            d=indicators(df.copy());last=-999999
            for i in range(220,len(d)-hold-1):
                if i-last<cooldown:continue
                try:score=float(black_score(d.iloc[:i+1])[0])
                except Exception:continue
                if score<score_threshold:continue
                entry=float(d['Close'].iloc[i]);end=i+hold
                mae,mfe,base_ret=_v362_excursion(d,i,hold)
                exit_price=float(d['Close'].iloc[end]);exit_i=end;reason='40日時間出場'
                if sl is not None:
                    stop_price=entry*(1-sl/100)
                    for j in range(i+1,end+1):
                        if float(d['Low'].iloc[j])<=stop_price:
                            exit_price=stop_price;exit_i=j;reason=f'停損{sl:g}%';break
                ret=(exit_price/entry-1)*100
                rets.append(ret)
                trades.append({'策略':name,'股票':code,'名稱':nm,'進場日':pd.Timestamp(d.index[i]).strftime('%Y-%m-%d'),
                               '出場日':pd.Timestamp(d.index[exit_i]).strftime('%Y-%m-%d'),
                               '技術分數':score,'MAE%':mae,'MFE%':mfe,'原40日報酬%':base_ret,
                               '報酬%':ret,'出場原因':reason})
                last=i

        if len(rets)<min_sample:continue
        m=_v360_trade_metrics(rets);td=pd.DataFrame(trades)
        stop_rate=(td['出場原因'].astype(str).str.contains('停損').mean()*100) if not td.empty else np.nan
        false_stop=np.nan
        if sl is not None and not td.empty:
            stopped=td[td['出場原因'].astype(str).str.contains('停損')].copy()
            if not stopped.empty:
                false_stop=(pd.to_numeric(stopped['原40日報酬%'],errors='coerce')>0).mean()*100
        rows.append({'策略':name,'停損%':sl if sl is not None else np.nan,**m,
                     '停損觸發率%':stop_rate,'被停損但40日後原可獲利比例%':false_stop})
        details.extend(trades)

    s=pd.DataFrame(rows);d=pd.DataFrame(details)
    if not s.empty:
        base=s[s['策略']=='40日純時間']
        if not base.empty:
            b=base.iloc[0]
            s['勝率改善ppt']=s['勝率%']-b['勝率%']
            s['平均報酬改善ppt']=s['平均報酬%']-b['平均報酬%']
            s['PF改善']=s['PF']-b['PF']
            s['最大虧損改善ppt']=s['最大單筆虧損%']-b['最大單筆虧損%']
            s['報酬保留率%']=np.where(b['平均報酬%']!=0,s['平均報酬%']/b['平均報酬%']*100,np.nan)
        s['停損平衡分']=s['平均報酬%'].fillna(-99)*1.5+s['PF'].fillna(0)*5+s['最大單筆虧損%'].fillna(-99)*0.35+s['勝率%'].fillna(0)*0.03
        s=s.sort_values(['停損平衡分','平均報酬%','PF'],ascending=[False,False,False])
    return s,d


# ===== V3.6.12 停損確認機制驗證 =====
# 目的：比較「盤中觸價停損」與「收盤確認 / 連續2日確認」，
# 看能不能降低誤殺趨勢股，同時保留尾端風險控制。

def _v364_exit_one(df, entry_i, hold=40, mode='time', stop_pct=10):
    if df is None or entry_i >= len(df)-1:
        return None

    entry=float(df['Close'].iloc[entry_i])
    if entry<=0:
        return None

    end=min(entry_i+hold,len(df)-1)
    exit_i=end
    exit_price=float(df['Close'].iloc[end])
    reason=f'時間出場{hold}日'
    stop_price=entry*(1-stop_pct/100)

    below_close_count=0

    for j in range(entry_i+1,end+1):
        low=float(df['Low'].iloc[j])
        close=float(df['Close'].iloc[j])

        if mode=='intraday':
            if low<=stop_price:
                exit_i=j
                exit_price=stop_price
                reason=f'盤中停損{stop_pct:g}%'
                break

        elif mode=='close':
            if close<=stop_price:
                exit_i=j
                exit_price=close
                reason=f'收盤停損{stop_pct:g}%'
                break

        elif mode=='close2':
            if close<=stop_price:
                below_close_count+=1
            else:
                below_close_count=0
            if below_close_count>=2:
                exit_i=j
                exit_price=close
                reason=f'收盤連2日停損{stop_pct:g}%'
                break

    ret=(exit_price/entry-1)*100

    # 原40日結果，用來判斷是否誤殺
    base_ret=(float(df['Close'].iloc[end])/entry-1)*100

    return {
        '進場日':pd.Timestamp(df.index[entry_i]).strftime('%Y-%m-%d'),
        '出場日':pd.Timestamp(df.index[exit_i]).strftime('%Y-%m-%d'),
        '進場價':entry,
        '出場價':exit_price,
        '報酬%':ret,
        '原40日報酬%':base_ret,
        '出場原因':reason,
        '持有天數':exit_i-entry_i
    }

def run_v364_stop_confirmation_lab(result_df, score_threshold=85, cooldown=20,
                                   hold=40, min_sample=30):
    configs=[
        ('40日純時間','time',10),
        ('盤中停損10%','intraday',10),
        ('盤中停損12%','intraday',12),
        ('收盤停損10%','close',10),
        ('收盤停損12%','close',12),
        ('收盤連2日停損10%','close2',10),
        ('收盤連2日停損12%','close2',12),
    ]

    rows=[]
    details=[]

    for name,mode,sl in configs:
        rets=[]
        trades=[]

        for _,rr in result_df.iterrows():
            code=str(rr['股票']).zfill(4)
            nm=rr.get('名稱','')
            df=rr.get('_df')

            if df is None or len(df)<270:
                continue

            d=indicators(df.copy())
            last=-999999

            for i in range(220,len(d)-hold-1):
                if i-last<cooldown:
                    continue

                try:
                    score=float(black_score(d.iloc[:i+1])[0])
                except Exception:
                    continue

                if score<score_threshold:
                    continue

                tr=_v364_exit_one(d,i,hold=hold,mode=mode,stop_pct=sl)
                if not tr:
                    continue

                rets.append(tr['報酬%'])
                trades.append({
                    '策略':name,
                    '股票':code,
                    '名稱':nm,
                    '技術分數':score,
                    **tr
                })
                last=i

        if len(rets)<min_sample:
            continue

        m=_v360_trade_metrics(rets)
        td=pd.DataFrame(trades)

        triggered=td[~td['出場原因'].astype(str).str.startswith('時間出場')].copy()
        trigger_rate=(len(triggered)/len(td)*100) if len(td) else np.nan

        false_stop=np.nan
        if not triggered.empty:
            false_stop=(pd.to_numeric(triggered['原40日報酬%'],errors='coerce')>0).mean()*100

        avg_hold=pd.to_numeric(td['持有天數'],errors='coerce').mean() if not td.empty else np.nan

        rows.append({
            '策略':name,
            '樣本數':m['樣本數'],
            '勝率%':m['勝率%'],
            '平均報酬%':m['平均報酬%'],
            '中位數%':m['中位數%'],
            'PF':m['PF'],
            '平均獲利%':m['平均獲利%'],
            '平均虧損%':m['平均虧損%'],
            '最大單筆虧損%':m['最大單筆虧損%'],
            '停損觸發率%':trigger_rate,
            '被停損但40日後原可獲利比例%':false_stop,
            '平均持有天數':avg_hold
        })

        details.extend(trades)

    s=pd.DataFrame(rows)
    d=pd.DataFrame(details)

    if not s.empty:
        base=s[s['策略']=='40日純時間']
        if not base.empty:
            b=base.iloc[0]
            s['勝率改善ppt']=s['勝率%']-b['勝率%']
            s['平均報酬改善ppt']=s['平均報酬%']-b['平均報酬%']
            s['PF改善']=s['PF']-b['PF']
            s['最大虧損改善ppt']=s['最大單筆虧損%']-b['最大單筆虧損%']
            s['報酬保留率%']=np.where(
                b['平均報酬%']!=0,
                s['平均報酬%']/b['平均報酬%']*100,
                np.nan
            )

        # 強調「保留報酬 + 降低尾端風險 + 少誤殺」
        s['確認停損分']=(
            s['報酬保留率%'].fillna(0)*0.20 +
            s['PF'].fillna(0)*4 +
            s['最大虧損改善ppt'].fillna(0)*0.35 -
            s['被停損但40日後原可獲利比例%'].fillna(0)*0.10
        )

        s=s.sort_values(
            ['確認停損分','報酬保留率%','PF'],
            ascending=[False,False,False]
        )

    return s,d


# ===== V3.6.12 贏家路徑 + 時間停損 =====
def collect_v365_paths(result_df, score_threshold=85, cooldown=20, hold=40):
    checkpoints=[5,10,15,20,30,40]
    rows=[]
    for _,rr in result_df.iterrows():
        code=str(rr['股票']).zfill(4)
        nm=rr.get('名稱','')
        df=rr.get('_df')
        if df is None or len(df)<270:
            continue
        d=indicators(df.copy())
        last=-999999

        for i in range(220,len(d)-hold-1):
            if i-last<cooldown:
                continue
            try:
                score=float(black_score(d.iloc[:i+1])[0])
            except Exception:
                continue
            if score<score_threshold:
                continue

            entry=float(d['Close'].iloc[i])
            if entry<=0:
                continue

            row={
                '股票':code,'名稱':nm,
                '進場日':pd.Timestamp(d.index[i]).strftime('%Y-%m-%d'),
                '技術分數':score
            }

            for h in checkpoints:
                j=i+h
                row[f'D{h}報酬%']=(float(d['Close'].iloc[j])/entry-1)*100
                highs=pd.to_numeric(d['High'].iloc[i+1:j+1],errors='coerce')
                lows=pd.to_numeric(d['Low'].iloc[i+1:j+1],errors='coerce')
                row[f'D{h}MFE%']=(float(highs.max())/entry-1)*100 if not highs.empty else np.nan
                row[f'D{h}MAE%']=(float(lows.min())/entry-1)*100 if not lows.empty else np.nan

            final=row['D40報酬%']
            if final>=30:
                grp='大贏家≥30%'
            elif final>0:
                grp='一般贏家0~30%'
            else:
                grp='虧損≤0%'
            row['40日結果族群']=grp
            rows.append(row)
            last=i

    return pd.DataFrame(rows)

def v365_path_summary(paths):
    if paths is None or paths.empty:
        return pd.DataFrame()
    checkpoints=[5,10,15,20,30,40]
    rows=[]
    order=['大贏家≥30%','一般贏家0~30%','虧損≤0%','全部']
    for grp in order:
        g=paths if grp=='全部' else paths[paths['40日結果族群']==grp]
        if g.empty:
            continue
        for h in checkpoints:
            r=pd.to_numeric(g[f'D{h}報酬%'],errors='coerce')
            rows.append({
                '族群':grp,'交易日':h,'樣本數':len(g),
                '平均報酬%':r.mean(),'中位數報酬%':r.median(),
                '正報酬比例%':(r>0).mean()*100,
                '平均MFE%':pd.to_numeric(g[f'D{h}MFE%'],errors='coerce').mean(),
                '平均MAE%':pd.to_numeric(g[f'D{h}MAE%'],errors='coerce').mean()
            })
    return pd.DataFrame(rows)

def v365_time_stop_lab(paths, hard_stop=12):
    """
    先以 D5/D10/D15/D20 的收盤報酬判斷時間停損；
    若未達門檻則該日收盤出場，否則持有到40日。
    同時用40日MAE近似檢查硬停損是否曾觸發。
    這是路徑篩選研究，下一版再做逐日精確成交模擬。
    """
    if paths is None or paths.empty:
        return pd.DataFrame()

    configs=[
        ('40日純時間',None,None),
        ('D10仍≤0%出場',10,0),
        ('D10仍≤+3%出場',10,3),
        ('D15仍≤0%出場',15,0),
        ('D15仍≤+3%出場',15,3),
        ('D20仍≤0%出場',20,0),
        ('D20仍≤+3%出場',20,3),
    ]
    rows=[]
    base_rets=pd.to_numeric(paths['D40報酬%'],errors='coerce')

    for name,day,threshold in configs:
        rets=[]
        early=0
        false_exit=0
        for _,x in paths.iterrows():
            # 先做 -12% 硬停損近似：若40日內MAE <= -12，報酬以 -12% 計。
            # 注意：日K跳空實際成交可能更差，故仍屬研究假設。
            if pd.notna(x.get('D40MAE%')) and float(x['D40MAE%'])<=-hard_stop:
                ret=-float(hard_stop)
            elif day is None:
                ret=float(x['D40報酬%'])
            else:
                chk=float(x[f'D{day}報酬%'])
                if chk<=threshold:
                    ret=chk
                    early+=1
                    if float(x['D40報酬%'])>0:
                        false_exit+=1
                else:
                    ret=float(x['D40報酬%'])
            rets.append(ret)

        arr=pd.Series(rets,dtype=float)
        wins=arr[arr>0]
        losses=arr[arr<=0]
        pf=(wins.sum()/abs(losses.sum())) if len(losses) and abs(losses.sum())>0 else np.nan
        rows.append({
            '策略':name,'樣本數':len(arr),
            '勝率%':(arr>0).mean()*100,
            '平均報酬%':arr.mean(),'中位數%':arr.median(),
            'PF':pf,
            '最大單筆虧損%':arr.min(),
            '提早出場率%':early/len(arr)*100 if len(arr) else np.nan,
            '提早出場但40日後原可獲利比例%':false_exit/early*100 if early else np.nan,
            '相對40日原始平均報酬差ppt':arr.mean()-base_rets.mean()
        })

    out=pd.DataFrame(rows)
    if not out.empty:
        out['時間停損分']=(
            out['平均報酬%'].fillna(-99)*1.5+
            out['PF'].fillna(0)*4+
            out['勝率%'].fillna(0)*0.05-
            out['提早出場但40日後原可獲利比例%'].fillna(0)*0.08
        )
        out=out.sort_values(['時間停損分','平均報酬%'],ascending=[False,False])
    return out


# ===== V3.6.12 讓贏家奔跑：40日後延伸持有 =====
def _v366_exit_trade(df, entry_i, hard_stop=12, base_hold=40,
                     extend_to=40, trend_rule='none', exit_rule='time'):
    """
    嚴格逐日模擬：
    1) 訊號日收盤進場
    2) 全程保留盤中 -12% 硬停損
    3) 到第40交易日才判斷是否符合延伸條件（不偷看未來）
    4) 未符合 -> D40收盤出場
    5) 符合 -> 最多延伸到 D60/D80，期間依指定趨勢規則出場
    """
    if df is None or entry_i >= len(df)-1:
        return None
    entry = float(df['Close'].iloc[entry_i])
    if not np.isfinite(entry) or entry <= 0:
        return None

    last_i = min(entry_i + int(extend_to), len(df)-1)
    d40_i = min(entry_i + int(base_hold), len(df)-1)
    stop_price = entry * (1-hard_stop/100)

    exit_i = d40_i
    exit_price = float(df['Close'].iloc[d40_i])
    reason = f'D{base_hold}固定出場'
    extended = False
    d40_ret = (exit_price/entry-1)*100

    # D1~D40 先執行硬停損
    for j in range(entry_i+1, d40_i+1):
        low = float(df['Low'].iloc[j])
        if low <= stop_price:
            return {
                '進場日':pd.Timestamp(df.index[entry_i]).strftime('%Y-%m-%d'),
                '出場日':pd.Timestamp(df.index[j]).strftime('%Y-%m-%d'),
                '進場價':entry,'出場價':stop_price,
                '報酬%':-float(hard_stop),'D40報酬%':d40_ret,
                '持有天數':j-entry_i,'出場原因':f'硬停損{hard_stop:g}%',
                'D40符合延伸':False,'延伸持有':False
            }

    if int(extend_to) <= int(base_hold) or trend_rule == 'none':
        return {
            '進場日':pd.Timestamp(df.index[entry_i]).strftime('%Y-%m-%d'),
            '出場日':pd.Timestamp(df.index[d40_i]).strftime('%Y-%m-%d'),
            '進場價':entry,'出場價':exit_price,'報酬%':d40_ret,'D40報酬%':d40_ret,
            '持有天數':d40_i-entry_i,'出場原因':reason,
            'D40符合延伸':False,'延伸持有':False
        }

    r40 = df.iloc[d40_i]
    close40 = float(r40['Close'])
    ma15 = pd.to_numeric(r40.get('MA15', np.nan), errors='coerce')
    ma60 = pd.to_numeric(r40.get('MA60', np.nan), errors='coerce')
    ma60_prev = pd.to_numeric(df['MA60'].iloc[max(entry_i, d40_i-5)], errors='coerce') if 'MA60' in df.columns else np.nan

    if trend_rule == 'ma15':
        trend_ok = pd.notna(ma15) and close40 > float(ma15)
    elif trend_rule == 'ma15_ma60':
        trend_ok = pd.notna(ma15) and pd.notna(ma60) and close40 > float(ma15) > float(ma60)
    elif trend_rule == 'strong':
        trend_ok = (
            pd.notna(ma15) and pd.notna(ma60) and pd.notna(ma60_prev)
            and close40 > float(ma15) > float(ma60)
            and float(ma60) > float(ma60_prev)
        )
    else:
        trend_ok = False

    if not trend_ok:
        return {
            '進場日':pd.Timestamp(df.index[entry_i]).strftime('%Y-%m-%d'),
            '出場日':pd.Timestamp(df.index[d40_i]).strftime('%Y-%m-%d'),
            '進場價':entry,'出場價':exit_price,'報酬%':d40_ret,'D40報酬%':d40_ret,
            '持有天數':d40_i-entry_i,'出場原因':'D40趨勢不足',
            'D40符合延伸':False,'延伸持有':False
        }

    extended = True
    exit_i = last_i
    exit_price = float(df['Close'].iloc[last_i])
    reason = f'D{extend_to}時間出場'

    below_count = 0
    for j in range(d40_i+1, last_i+1):
        low = float(df['Low'].iloc[j])
        close = float(df['Close'].iloc[j])

        if low <= stop_price:
            exit_i, exit_price, reason = j, stop_price, f'硬停損{hard_stop:g}%'
            break

        if exit_rule == 'ma15':
            ma = pd.to_numeric(df['MA15'].iloc[j], errors='coerce')
            if pd.notna(ma) and close < float(ma):
                exit_i, exit_price, reason = j, close, '延伸後跌破MA15'
                break
        elif exit_rule == 'ma60':
            ma = pd.to_numeric(df['MA60'].iloc[j], errors='coerce')
            if pd.notna(ma) and close < float(ma):
                exit_i, exit_price, reason = j, close, '延伸後跌破MA60'
                break
        elif exit_rule == 'ma15_2d':
            ma = pd.to_numeric(df['MA15'].iloc[j], errors='coerce')
            if pd.notna(ma) and close < float(ma):
                below_count += 1
            else:
                below_count = 0
            if below_count >= 2:
                exit_i, exit_price, reason = j, close, '延伸後連2日跌破MA15'
                break

    ret = (exit_price/entry-1)*100
    return {
        '進場日':pd.Timestamp(df.index[entry_i]).strftime('%Y-%m-%d'),
        '出場日':pd.Timestamp(df.index[exit_i]).strftime('%Y-%m-%d'),
        '進場價':entry,'出場價':exit_price,'報酬%':ret,'D40報酬%':d40_ret,
        '持有天數':exit_i-entry_i,'出場原因':reason,
        'D40符合延伸':True,'延伸持有':extended
    }

def run_v366_winner_extension_lab(result_df, score_threshold=85, cooldown=20,
                                  hard_stop=12, min_sample=30):
    configs = [
        ('基準｜40日＋硬停損12%',40,'none','time'),
        ('D60｜D40站MA15上',60,'ma15','time'),
        ('D60｜D40 MA15>MA60',60,'ma15_ma60','time'),
        ('D60｜強趨勢＋跌破MA15出',60,'strong','ma15'),
        ('D60｜強趨勢＋MA15連2日出',60,'strong','ma15_2d'),
        ('D80｜D40站MA15上',80,'ma15','time'),
        ('D80｜D40 MA15>MA60',80,'ma15_ma60','time'),
        ('D80｜強趨勢＋跌破MA15出',80,'strong','ma15'),
        ('D80｜強趨勢＋MA15連2日出',80,'strong','ma15_2d'),
        ('D80｜強趨勢＋跌破MA60出',80,'strong','ma60'),
    ]

    rows, details = [], []
    max_need = 80

    for cfg_name, extend_to, trend_rule, exit_rule in configs:
        trades = []
        for _, rr in result_df.iterrows():
            code = str(rr['股票']).zfill(4)
            name = rr.get('名稱','')
            df = rr.get('_df')
            if df is None or len(df) < 310:
                continue
            d = indicators(df.copy())
            last_entry = -999999

            for i in range(220, len(d)-max_need-1):
                if i-last_entry < cooldown:
                    continue
                try:
                    score = float(black_score(d.iloc[:i+1])[0])
                except Exception:
                    continue
                if score < score_threshold:
                    continue

                tr = _v366_exit_trade(
                    d, i, hard_stop=hard_stop, base_hold=40,
                    extend_to=extend_to, trend_rule=trend_rule, exit_rule=exit_rule
                )
                if not tr:
                    continue
                trades.append({'策略':cfg_name,'股票':code,'名稱':name,'技術分數':score,**tr})
                last_entry = i

        td = pd.DataFrame(trades)
        if len(td) < min_sample:
            continue

        m = _v360_trade_metrics(pd.to_numeric(td['報酬%'], errors='coerce'))
        ext = td[td['延伸持有']==True]
        ext_rate = len(ext)/len(td)*100 if len(td) else np.nan
        ext_avg = pd.to_numeric(ext['報酬%'],errors='coerce').mean() if len(ext) else np.nan
        ext_d40 = pd.to_numeric(ext['D40報酬%'],errors='coerce').mean() if len(ext) else np.nan
        ext_gain = ext_avg-ext_d40 if len(ext) else np.nan
        giveback = np.nan
        if len(ext):
            giveback = (pd.to_numeric(ext['報酬%'],errors='coerce') <
                        pd.to_numeric(ext['D40報酬%'],errors='coerce')).mean()*100

        rows.append({
            '策略':cfg_name, **m,
            '延伸比例%':ext_rate,
            '延伸股D40平均報酬%':ext_d40,
            '延伸股最終平均報酬%':ext_avg,
            '延伸增加報酬ppt':ext_gain,
            '延伸後低於D40報酬比例%':giveback,
            '平均持有天數':pd.to_numeric(td['持有天數'],errors='coerce').mean()
        })
        details.extend(trades)

    s = pd.DataFrame(rows)
    detail = pd.DataFrame(details)
    if not s.empty:
        base = s[s['策略'].str.startswith('基準｜')]
        if not base.empty:
            b = base.iloc[0]
            s['勝率改善ppt'] = s['勝率%'] - b['勝率%']
            s['平均報酬改善ppt'] = s['平均報酬%'] - b['平均報酬%']
            s['PF改善'] = s['PF'] - b['PF']
            s['最大虧損改善ppt'] = s['最大單筆虧損%'] - b['最大單筆虧損%']
        s['奔跑分'] = (
            s['平均報酬改善ppt'].fillna(0)*2.0 +
            s['PF改善'].fillna(0)*4.0 +
            s['延伸增加報酬ppt'].fillna(0)*0.8 -
            s['延伸後低於D40報酬比例%'].fillna(0)*0.05
        )
        s = s.sort_values(['奔跑分','平均報酬%','PF'],ascending=[False,False,False])
    return s, detail


# ===== V3.6.12 超級贏家壓力測試 =====
# 固定驗證目前候選：
# 基準 = 40日 + 硬停損12%
# 候選 = D40仍站MA15 -> 延伸到D80
# 不再調參，專門檢查是否被少數超級飆股撐起來。

def _v367_strategy_detail(result_df, score_threshold=85, cooldown=20, min_sample=30):
    configs = [
        ('基準｜40日＋硬停損12%', 40, 'none', 'time'),
        ('候選｜D40站MA15→D80', 80, 'ma15', 'time'),
    ]

    details = []
    max_need = 80

    for strategy, extend_to, trend_rule, exit_rule in configs:
        for _, rr in result_df.iterrows():
            code = str(rr['股票']).zfill(4)
            name = rr.get('名稱','')
            df = rr.get('_df')
            if df is None or len(df) < 310:
                continue

            d = indicators(df.copy())
            last_entry = -999999

            for i in range(220, len(d)-max_need-1):
                if i-last_entry < cooldown:
                    continue
                try:
                    score = float(black_score(d.iloc[:i+1])[0])
                except Exception:
                    continue
                if score < score_threshold:
                    continue

                tr = _v366_exit_trade(
                    d, i,
                    hard_stop=12,
                    base_hold=40,
                    extend_to=extend_to,
                    trend_rule=trend_rule,
                    exit_rule=exit_rule
                )
                if not tr:
                    continue

                details.append({
                    '策略':strategy,
                    '股票':code,
                    '名稱':name,
                    '技術分數':score,
                    **tr
                })
                last_entry = i

    out = pd.DataFrame(details)
    if out.empty:
        return out

    out['進場年'] = pd.to_datetime(out['進場日']).dt.year
    out['報酬%'] = pd.to_numeric(out['報酬%'], errors='coerce')
    return out

def _v367_perf(df):
    if df is None or df.empty:
        return {
            '樣本數':0,'勝率%':np.nan,'平均報酬%':np.nan,'中位數%':np.nan,
            'PF':np.nan,'總報酬點數':np.nan,'最大單筆虧損%':np.nan
        }

    r = pd.to_numeric(df['報酬%'], errors='coerce').dropna()
    if r.empty:
        return {
            '樣本數':0,'勝率%':np.nan,'平均報酬%':np.nan,'中位數%':np.nan,
            'PF':np.nan,'總報酬點數':np.nan,'最大單筆虧損%':np.nan
        }

    gp = r[r>0].sum()
    gl = -r[r<0].sum()

    return {
        '樣本數':len(r),
        '勝率%':(r>0).mean()*100,
        '平均報酬%':r.mean(),
        '中位數%':r.median(),
        'PF':gp/gl if gl>0 else np.nan,
        '總報酬點數':r.sum(),
        '最大單筆虧損%':r.min()
    }

def v367_remove_top_stress(detail):
    """
    各策略各自移除最高報酬前 0 / 1 / 3 / 5 / 10 筆，再重算績效。
    候選策略若移除Top5後仍明顯優於基準，可信度大幅提升。
    """
    if detail is None or detail.empty:
        return pd.DataFrame()

    rows = []
    remove_levels = [0,1,3,5,10]

    for strategy, g in detail.groupby('策略'):
        g = g.sort_values('報酬%', ascending=False).reset_index(drop=True)
        original_n = len(g)

        for n in remove_levels:
            if n >= original_n:
                continue
            x = g.iloc[n:].copy() if n > 0 else g.copy()
            m = _v367_perf(x)
            rows.append({
                '策略':strategy,
                '移除最高報酬筆數':n,
                '原始樣本數':original_n,
                **m
            })

    stress = pd.DataFrame(rows)

    if not stress.empty:
        base = stress[stress['策略'].str.startswith('基準｜')][
            ['移除最高報酬筆數','平均報酬%','PF','勝率%']
        ].rename(columns={
            '平均報酬%':'基準平均報酬%',
            'PF':'基準PF',
            '勝率%':'基準勝率%'
        })

        stress = stress.merge(base, on='移除最高報酬筆數', how='left')
        stress['相對基準平均報酬改善ppt'] = stress['平均報酬%'] - stress['基準平均報酬%']
        stress['相對基準PF改善'] = stress['PF'] - stress['基準PF']
        stress['相對基準勝率改善ppt'] = stress['勝率%'] - stress['基準勝率%']

    return stress

def v367_profit_concentration(detail):
    """
    檢查獲利是否過度集中在少數交易 / 少數股票。
    """
    if detail is None or detail.empty:
        return pd.DataFrame(), pd.DataFrame()

    trade_rows = []
    stock_rows = []

    for strategy, g in detail.groupby('策略'):
        pos = g[g['報酬%']>0].copy()
        pos = pos.sort_values('報酬%', ascending=False)
        total_profit = pos['報酬%'].sum()

        def share(n):
            if total_profit <= 0 or pos.empty:
                return np.nan
            return pos.head(n)['報酬%'].sum()/total_profit*100

        trade_rows.append({
            '策略':strategy,
            '正報酬交易數':len(pos),
            'Top1獲利貢獻率%':share(1),
            'Top3獲利貢獻率%':share(3),
            'Top5獲利貢獻率%':share(5),
            'Top10獲利貢獻率%':share(10)
        })

        by_stock = (
            g.groupby(['股票','名稱'], as_index=False)['報酬%']
            .sum()
            .sort_values('報酬%', ascending=False)
        )
        positive_stock = by_stock[by_stock['報酬%']>0].copy()
        total_stock_profit = positive_stock['報酬%'].sum()

        if total_stock_profit > 0 and not positive_stock.empty:
            top1 = positive_stock.head(1)['報酬%'].sum()/total_stock_profit*100
            top3 = positive_stock.head(3)['報酬%'].sum()/total_stock_profit*100
            top5 = positive_stock.head(5)['報酬%'].sum()/total_stock_profit*100
        else:
            top1=top3=top5=np.nan

        stock_rows.append({
            '策略':strategy,
            '獲利股票數':len(positive_stock),
            'Top1股票獲利貢獻率%':top1,
            'Top3股票獲利貢獻率%':top3,
            'Top5股票獲利貢獻率%':top5
        })

    return pd.DataFrame(trade_rows), pd.DataFrame(stock_rows)

def v367_yearly_stability(detail):
    if detail is None or detail.empty:
        return pd.DataFrame()

    rows = []
    for (strategy, year), g in detail.groupby(['策略','進場年']):
        m = _v367_perf(g)
        rows.append({'策略':strategy,'年度':int(year),**m})

    out = pd.DataFrame(rows)
    if out.empty:
        return out

    base = out[out['策略'].str.startswith('基準｜')][
        ['年度','平均報酬%','PF','勝率%']
    ].rename(columns={
        '平均報酬%':'基準平均報酬%',
        'PF':'基準PF',
        '勝率%':'基準勝率%'
    })

    out = out.merge(base,on='年度',how='left')
    out['平均報酬改善ppt'] = out['平均報酬%'] - out['基準平均報酬%']
    out['PF改善'] = out['PF'] - out['基準PF']
    out['勝率改善ppt'] = out['勝率%'] - out['基準勝率%']
    return out

def v367_stockpool_stability(result_df, score_threshold=85, cooldown=20):
    """
    依目前排行榜順序測 50 / 100 / 200 / 全部。
    若目前 result 本身不足某層級，自動略過。
    """
    if result_df is None or result_df.empty:
        return pd.DataFrame()

    levels = [50,100,200,len(result_df)]
    seen = set()
    rows = []

    for n in levels:
        n = min(n, len(result_df))
        if n in seen or n < 10:
            continue
        seen.add(n)

        sub = result_df.head(n).copy()
        detail = _v367_strategy_detail(sub, score_threshold, cooldown, min_sample=10)
        if detail.empty:
            continue

        for strategy, g in detail.groupby('策略'):
            m = _v367_perf(g)
            rows.append({
                '股票池': '全部' if n==len(result_df) else str(n),
                '股票數': n,
                '策略':strategy,
                **m
            })

    out = pd.DataFrame(rows)
    if out.empty:
        return out

    base = out[out['策略'].str.startswith('基準｜')][
        ['股票池','平均報酬%','PF','勝率%']
    ].rename(columns={
        '平均報酬%':'基準平均報酬%',
        'PF':'基準PF',
        '勝率%':'基準勝率%'
    })

    out = out.merge(base,on='股票池',how='left')
    out['平均報酬改善ppt'] = out['平均報酬%'] - out['基準平均報酬%']
    out['PF改善'] = out['PF'] - out['基準PF']
    out['勝率改善ppt'] = out['勝率%'] - out['基準勝率%']
    return out

def v367_final_verdict(top_stress, trade_conc, stock_conc, yearly, stockpool):
    """
    自動給研究結論，不直接宣告正式策略，只分：
    通過 / 邊界 / 未通過。
    """
    score = 0
    notes = []

    # 1) Top5剔除後仍優於基準
    cand5 = top_stress[
        (top_stress['策略'].str.startswith('候選｜')) &
        (top_stress['移除最高報酬筆數']==5)
    ]
    if not cand5.empty:
        r = cand5.iloc[0]
        if r['相對基準平均報酬改善ppt'] > 0 and r['相對基準PF改善'] >= 0:
            score += 2
            notes.append('✅ 移除Top5後，平均報酬與PF仍優於基準')
        elif r['相對基準平均報酬改善ppt'] > 0:
            score += 1
            notes.append('🟡 移除Top5後報酬仍優於基準，但PF未同步改善')
        else:
            notes.append('❌ 移除Top5後已失去平均報酬優勢')

    # 2) Top5交易獲利貢獻率
    tc = trade_conc[trade_conc['策略'].str.startswith('候選｜')]
    if not tc.empty:
        s = float(tc.iloc[0]['Top5獲利貢獻率%'])
        if s < 50:
            score += 1
            notes.append(f'✅ Top5交易獲利貢獻率 {s:.1f}% < 50%')
        else:
            notes.append(f'🟡 Top5交易獲利貢獻率 {s:.1f}%，獲利偏集中')

    # 3) 年度
    yc = yearly[yearly['策略'].str.startswith('候選｜')].copy()
    if not yc.empty:
        valid = yc[pd.to_numeric(yc['平均報酬改善ppt'],errors='coerce').notna()]
        pos = (valid['平均報酬改善ppt']>0).mean()*100 if len(valid) else np.nan
        if pd.notna(pos) and pos >= 60:
            score += 1
            notes.append(f'✅ {pos:.1f}% 年度平均報酬優於基準')
        else:
            notes.append(f'🟡 跨年度優勢不足（正改善年度 {pos:.1f}%）' if pd.notna(pos) else '🟡 年度資料不足')

    # 4) 股票池
    pc = stockpool[stockpool['策略'].str.startswith('候選｜')].copy()
    if not pc.empty:
        valid = pc[pd.to_numeric(pc['平均報酬改善ppt'],errors='coerce').notna()]
        pos = (valid['平均報酬改善ppt']>0).mean()*100 if len(valid) else np.nan
        if pd.notna(pos) and pos >= 75:
            score += 1
            notes.append(f'✅ {pos:.1f}% 股票池層級平均報酬優於基準')
        else:
            notes.append(f'🟡 股票池穩定度不足（正改善層級 {pos:.1f}%）' if pd.notna(pos) else '🟡 股票池資料不足')

    if score >= 5:
        verdict = '🟢 通過壓力測試'
    elif score >= 3:
        verdict = '🟡 邊界通過，仍需保守'
    else:
        verdict = '🔴 未通過壓力測試'

    return verdict, notes


# ===== V3.6.12 全市場 Out-of-Sample 驗證 =====
# 參數鎖死，不再最佳化：
# 技術分數 >=85 / 盤中硬停損12% / D40站MA15 -> D80，否則D40出場。
LOCKED_SCORE = 85
LOCKED_STOP = 12
LOCKED_D40 = 40
LOCKED_D80 = 80

def _v368_locked_trade(df, entry_i):
    return _v366_exit_trade(
        df,
        entry_i,
        hard_stop=LOCKED_STOP,
        base_hold=LOCKED_D40,
        extend_to=LOCKED_D80,
        trend_rule='ma15',
        exit_rule='time'
    )

def _v368_locked_baseline_trade(df, entry_i):
    return _v366_exit_trade(
        df,
        entry_i,
        hard_stop=LOCKED_STOP,
        base_hold=LOCKED_D40,
        extend_to=LOCKED_D40,
        trend_rule='none',
        exit_rule='time'
    )

def _v368_perf_from_returns(rets):
    r = pd.Series(rets, dtype=float).dropna()
    if r.empty:
        return {
            '樣本數':0,'勝率%':np.nan,'平均報酬%':np.nan,'中位數%':np.nan,
            'PF':np.nan,'最大單筆虧損%':np.nan
        }
    gp = r[r>0].sum()
    gl = -r[r<0].sum()
    return {
        '樣本數':len(r),
        '勝率%':(r>0).mean()*100,
        '平均報酬%':r.mean(),
        '中位數%':r.median(),
        'PF':gp/gl if gl>0 else np.nan,
        '最大單筆虧損%':r.min()
    }

def _v368_split_symbols(symbols):
    """
    固定 deterministic split，避免每次重跑分組不同：
    偶數位置 = 驗證A，奇數位置 = 驗證B。
    """
    syms=[str(s).zfill(4) for s in symbols]
    a=syms[::2]
    b=syms[1::2]
    return a,b

def _v368_get_full_universe(result_df):
    """
    優先使用正式股票池；若無法從既有 universe 取得，至少回傳目前 result 股票。
    """
    syms=[]
    try:
        # 既有 app 多數版本有 load_market_universe(refresh_key)
        u = load_market_universe(universe_effective_key())
        if isinstance(u, tuple):
            u = u[0]
        if isinstance(u, pd.DataFrame) and not u.empty:
            col = '股票' if '股票' in u.columns else ('代號' if '代號' in u.columns else None)
            if col:
                syms = u[col].astype(str).str.zfill(4).tolist()
    except Exception:
        pass

    if not syms and result_df is not None and not result_df.empty and '股票' in result_df.columns:
        syms = result_df['股票'].astype(str).str.zfill(4).tolist()

    # 去重且保持順序
    seen=set()
    out=[]
    for s in syms:
        if s not in seen:
            seen.add(s); out.append(s)
    return out

@st.cache_data(ttl=3600, show_spinner=False)
def _v368_download_symbol(symbol):
    """
    全市場驗證用歷史資料。上市優先.TW，失敗再.TWO。
    """
    import yfinance as yf
    for suf in ['.TW','.TWO']:
        try:
            d=yf.download(
                str(symbol)+suf,
                period='3y',
                interval='1d',
                auto_adjust=False,
                progress=False,
                threads=False
            )
            if d is not None and not d.empty and len(d)>=320:
                if isinstance(d.columns, pd.MultiIndex):
                    d.columns=[c[0] if isinstance(c,tuple) else c for c in d.columns]
                need=['Open','High','Low','Close','Volume']
                if all(c in d.columns for c in need):
                    d=d[need].copy()
                    d.index=pd.to_datetime(d.index).tz_localize(None)
                    return d
        except Exception:
            continue
    return pd.DataFrame()

def _v368_collect_for_symbols(symbols, max_symbols=None, cooldown=20):
    """
    對完全鎖死的策略做OOS驗證。
    進場門檻固定85，不允許調整。
    """
    rows=[]
    use = symbols[:max_symbols] if max_symbols else symbols
    total=len(use)
    prog=st.progress(0) if total else None

    for idx,sym in enumerate(use):
        try:
            df=_v368_download_symbol(sym)
            if df is None or df.empty or len(df)<320:
                continue
            d=indicators(df.copy())
            if d is None or len(d)<320:
                continue
            last=-999999
            for i in range(220, len(d)-LOCKED_D80-1):
                if i-last < cooldown:
                    continue
                try:
                    score=float(black_score(d.iloc[:i+1])[0])
                except Exception:
                    continue
                if score < LOCKED_SCORE:
                    continue

                b=_v368_locked_baseline_trade(d,i)
                c=_v368_locked_trade(d,i)
                if b and c:
                    rows.append({
                        '股票':str(sym).zfill(4),
                        '進場日':pd.Timestamp(d.index[i]).strftime('%Y-%m-%d'),
                        '技術分數':score,
                        '基準報酬%':float(b['報酬%']),
                        '候選報酬%':float(c['報酬%']),
                        '候選延伸持有':bool(c.get('延伸持有',False)),
                        '候選出場原因':c.get('出場原因',''),
                        '基準持有天數':b.get('持有天數',np.nan),
                        '候選持有天數':c.get('持有天數',np.nan)
                    })
                    last=i
        finally:
            if prog is not None:
                prog.progress((idx+1)/max(total,1))
    if prog is not None:
        prog.empty()
    return pd.DataFrame(rows)

def _v368_compare(df):
    if df is None or df.empty:
        return pd.DataFrame()

    bp=_v368_perf_from_returns(df['基準報酬%'])
    cp=_v368_perf_from_returns(df['候選報酬%'])

    return pd.DataFrame([
        {
            '策略':'基準｜40日＋硬停損12%',
            **bp,
            '平均報酬改善ppt':0.0,
            'PF改善':0.0,
            '勝率改善ppt':0.0
        },
        {
            '策略':'候選｜D40站MA15→D80',
            **cp,
            '平均報酬改善ppt':cp['平均報酬%']-bp['平均報酬%'],
            'PF改善':cp['PF']-bp['PF'] if pd.notna(cp['PF']) and pd.notna(bp['PF']) else np.nan,
            '勝率改善ppt':cp['勝率%']-bp['勝率%']
        }
    ])

def _v368_group_tests(all_trades, group_map):
    rows=[]
    if all_trades is None or all_trades.empty:
        return pd.DataFrame()

    for label, symbols in group_map.items():
        g=all_trades[all_trades['股票'].isin(set(symbols))].copy()
        if g.empty:
            continue
        cmp=_v368_compare(g)
        if cmp.empty:
            continue
        cand=cmp[cmp['策略'].str.startswith('候選｜')]
        base=cmp[cmp['策略'].str.startswith('基準｜')]
        if cand.empty or base.empty:
            continue
        c=cand.iloc[0]; b=base.iloc[0]
        rows.append({
            '驗證組':label,
            '股票數':len(set(symbols)),
            '樣本數':int(c['樣本數']),
            '候選勝率%':c['勝率%'],
            '基準勝率%':b['勝率%'],
            '勝率改善ppt':c['勝率改善ppt'],
            '候選平均報酬%':c['平均報酬%'],
            '基準平均報酬%':b['平均報酬%'],
            '平均報酬改善ppt':c['平均報酬改善ppt'],
            '候選PF':c['PF'],
            '基準PF':b['PF'],
            'PF改善':c['PF改善'],
            '候選最大單筆虧損%':c['最大單筆虧損%']
        })
    return pd.DataFrame(rows)

def _v368_top_remove_oos(all_trades, remove_levels=(0,1,3,5,10)):
    if all_trades is None or all_trades.empty:
        return pd.DataFrame()
    rows=[]
    for n in remove_levels:
        if n >= len(all_trades):
            continue
        # 同時移除「候選報酬」最高的n筆，避免超級贏家拉高。
        order=all_trades.sort_values('候選報酬%',ascending=False)
        x=order.iloc[n:].copy() if n else order.copy()

        bp=_v368_perf_from_returns(x['基準報酬%'])
        cp=_v368_perf_from_returns(x['候選報酬%'])
        rows.append({
            '移除候選最高報酬筆數':n,
            '樣本數':len(x),
            '候選平均報酬%':cp['平均報酬%'],
            '基準平均報酬%':bp['平均報酬%'],
            '平均報酬改善ppt':cp['平均報酬%']-bp['平均報酬%'],
            '候選PF':cp['PF'],
            '基準PF':bp['PF'],
            'PF改善':cp['PF']-bp['PF'] if pd.notna(cp['PF']) and pd.notna(bp['PF']) else np.nan,
            '候選勝率%':cp['勝率%'],
            '基準勝率%':bp['勝率%'],
            '勝率改善ppt':cp['勝率%']-bp['勝率%']
        })
    return pd.DataFrame(rows)

def _v368_auto_verdict(group_tests, topstress):
    score=0
    notes=[]

    if group_tests is not None and not group_tests.empty:
        gt=group_tests.copy()
        pos_ret=(gt['平均報酬改善ppt']>0).mean()*100
        pos_pf=(gt['PF改善']>=0).mean()*100
        if pos_ret>=75:
            score+=2; notes.append(f'✅ {pos_ret:.1f}% 驗證組平均報酬優於基準')
        else:
            notes.append(f'🟡 僅 {pos_ret:.1f}% 驗證組平均報酬優於基準')
        if pos_pf>=75:
            score+=1; notes.append(f'✅ {pos_pf:.1f}% 驗證組PF不低於基準')
        else:
            notes.append(f'🟡 僅 {pos_pf:.1f}% 驗證組PF不低於基準')

    if topstress is not None and not topstress.empty:
        x=topstress[topstress['移除候選最高報酬筆數']==5]
        if not x.empty:
            r=x.iloc[0]
            if r['平均報酬改善ppt']>0:
                score+=2; notes.append('✅ OOS移除Top5後平均報酬仍優於基準')
            else:
                notes.append('❌ OOS移除Top5後失去平均報酬優勢')
            if pd.notna(r['PF改善']) and r['PF改善']>=0:
                score+=1; notes.append('✅ OOS移除Top5後PF仍不低於基準')
            else:
                notes.append('🟡 OOS移除Top5後PF未保持優勢')

    if score>=5:
        verdict='🟢 Out-of-Sample 通過'
    elif score>=3:
        verdict='🟡 Out-of-Sample 邊界通過'
    else:
        verdict='🔴 Out-of-Sample 未通過'
    return verdict, notes


# ===== V3.6.12 OOS 資料管線診斷＋樣本修正 =====
# 不改策略，只修驗證引擎與樣本透明度。
# 鎖死：85分 / -12% / D40站MA15 -> D80

def _v3681_universe_symbols(result_df):
    """
    優先抓完整上市/上櫃/興櫃股票池。
    若 load_market_universe 回傳市場欄位，全部保留；最後才 fallback 到 result。
    """
    syms=[]
    try:
        u = load_market_universe(universe_effective_key())
        if isinstance(u, tuple):
            u=u[0]
        if isinstance(u,pd.DataFrame) and not u.empty:
            # V3.6.12 FIX:
            # load_market_universe() 的正式欄位其實是「股票代號」，
            # 舊版漏掉這個欄位，因此 syms 一直是空的，最後錯誤 fallback
            # 到畫面 result（當時只有 5 檔），造成 OOS 股票池只有 5 檔。
            for col in ['股票代號','股票','代號','證券代號']:
                if col in u.columns:
                    syms=(
                        u[col].astype(str)
                        .str.strip()
                        .str.upper()
                        .tolist()
                    )
                    break
    except Exception:
        pass

    if not syms and result_df is not None and not result_df.empty and '股票' in result_df.columns:
        # 只有官方完整股票池真的失敗時才 fallback 到目前掃描結果
        syms=result_df['股票'].astype(str).str.strip().tolist()

    seen=set(); out=[]
    for s in syms:
        s=str(s).strip().upper()
        if not s or s in ('NAN','NONE'):
            continue
        # 台股一般股票代號多為4碼；仍保留合法4~6碼代號以相容興櫃/特殊代號
        if not re.fullmatch(r'[0-9A-Z]{4,6}',s):
            continue
        if s not in seen:
            seen.add(s); out.append(s)
    return out

@st.cache_data(ttl=3600, show_spinner=False)
def _v3681_download_symbol(symbol):
    """
    3年日K。上市.TW / 上櫃興櫃.TWO。
    回傳 (df, status)
    """
    import yfinance as yf
    attempts=[]
    for suf in ['.TW','.TWO']:
        ticker=str(symbol)+suf
        try:
            d=yf.download(
                ticker,
                period='3y',
                interval='1d',
                auto_adjust=False,
                progress=False,
                threads=False
            )
            if d is None or d.empty:
                attempts.append(f'{ticker}:empty')
                continue

            if isinstance(d.columns,pd.MultiIndex):
                d.columns=[c[0] if isinstance(c,tuple) else c for c in d.columns]

            need=['Open','High','Low','Close','Volume']
            if not all(c in d.columns for c in need):
                attempts.append(f'{ticker}:missing_columns')
                continue

            d=d[need].dropna(subset=['Close']).copy()
            d.index=pd.to_datetime(d.index).tz_localize(None)

            if len(d)<320:
                attempts.append(f'{ticker}:bars={len(d)}')
                continue

            return d, f'OK {ticker} bars={len(d)}'
        except Exception as e:
            attempts.append(f'{ticker}:{type(e).__name__}')

    return pd.DataFrame(), ' / '.join(attempts[-2:])

def _v3681_collect(symbols, cooldown=20):
    """
    逐檔建立漏斗統計：
    股票池 -> K線成功 -> 指標成功 -> 曾有85分訊號 -> 有效交易
    """
    rows=[]
    diag=[]
    total=len(symbols)
    prog=st.progress(0) if total else None

    for idx,sym in enumerate(symbols):
        status={
            '股票':str(sym).zfill(4),
            'K線成功':False,
            '指標成功':False,
            '曾達85分':False,
            '有效交易':False,
            '交易筆數':0,
            '資料狀態':''
        }

        try:
            df, dl_status=_v3681_download_symbol(sym)
            status['資料狀態']=dl_status
            if df is None or df.empty:
                diag.append(status)
                continue
            status['K線成功']=True

            try:
                d=indicators(df.copy())
            except Exception as e:
                status['資料狀態'] += f' / indicators:{type(e).__name__}'
                diag.append(status)
                continue

            if d is None or len(d)<320:
                status['資料狀態'] += ' / indicators_empty'
                diag.append(status)
                continue
            status['指標成功']=True

            last=-999999
            trade_count=0

            for i in range(220, len(d)-LOCKED_D80-1):
                if i-last < cooldown:
                    continue

                try:
                    score=float(black_score(d.iloc[:i+1])[0])
                except Exception:
                    continue

                if score < LOCKED_SCORE:
                    continue

                status['曾達85分']=True

                b=_v368_locked_baseline_trade(d,i)
                c=_v368_locked_trade(d,i)
                if b and c:
                    rows.append({
                        '股票':str(sym).zfill(4),
                        '進場日':pd.Timestamp(d.index[i]).strftime('%Y-%m-%d'),
                        '技術分數':score,
                        '基準報酬%':float(b['報酬%']),
                        '候選報酬%':float(c['報酬%']),
                        '候選延伸持有':bool(c.get('延伸持有',False)),
                        '候選出場原因':c.get('出場原因',''),
                        '基準持有天數':b.get('持有天數',np.nan),
                        '候選持有天數':c.get('持有天數',np.nan)
                    })
                    trade_count += 1
                    last=i

            status['交易筆數']=trade_count
            status['有效交易']=trade_count>0
            diag.append(status)

        finally:
            if prog is not None:
                prog.progress((idx+1)/max(total,1))

    if prog is not None:
        prog.empty()

    return pd.DataFrame(rows), pd.DataFrame(diag)

def _v3681_funnel(diag_df):
    if diag_df is None or diag_df.empty:
        return pd.DataFrame()

    total=len(diag_df)
    stages=[
        ('股票池', total),
        ('K線成功', int(diag_df['K線成功'].sum())),
        ('指標成功', int(diag_df['指標成功'].sum())),
        ('曾達85分', int(diag_df['曾達85分'].sum())),
        ('有效交易股票', int(diag_df['有效交易'].sum())),
        ('最終交易筆數', int(diag_df['交易筆數'].sum())),
    ]

    rows=[]
    for stage,n in stages:
        rows.append({
            '階段':stage,
            '數量':n,
            '相對股票池比例%':(n/total*100) if total and stage!='最終交易筆數' else np.nan
        })
    return pd.DataFrame(rows)

def _v3681_group_compare(all_trades, symbols, label, min_trades=30):
    g=all_trades[all_trades['股票'].isin(set(symbols))].copy()
    if g.empty:
        return {
            '驗證組':label,'股票數':len(symbols),'樣本數':0,'樣本判定':'不足',
            '候選勝率%':np.nan,'基準勝率%':np.nan,'勝率改善ppt':np.nan,
            '候選平均報酬%':np.nan,'基準平均報酬%':np.nan,'平均報酬改善ppt':np.nan,
            '候選PF':np.nan,'基準PF':np.nan,'PF改善':np.nan
        }

    bp=_v368_perf_from_returns(g['基準報酬%'])
    cp=_v368_perf_from_returns(g['候選報酬%'])
    enough=len(g)>=min_trades

    return {
        '驗證組':label,
        '股票數':len(symbols),
        '樣本數':len(g),
        '樣本判定':'足夠' if enough else '不足',
        '候選勝率%':cp['勝率%'],
        '基準勝率%':bp['勝率%'],
        '勝率改善ppt':cp['勝率%']-bp['勝率%'],
        '候選平均報酬%':cp['平均報酬%'],
        '基準平均報酬%':bp['平均報酬%'],
        '平均報酬改善ppt':cp['平均報酬%']-bp['平均報酬%'],
        '候選PF':cp['PF'],
        '基準PF':bp['PF'],
        'PF改善':cp['PF']-bp['PF'] if pd.notna(cp['PF']) and pd.notna(bp['PF']) else np.nan
    }

def _v3681_build_groups(symbols, all_trades, min_trades=30):
    rows=[]
    for n in [50,100,200]:
        if len(symbols)>=n:
            rows.append(_v3681_group_compare(all_trades, symbols[:n], f'前{n}檔', min_trades))

    if symbols:
        rows.append(_v3681_group_compare(all_trades, symbols, '全部本次驗證', min_trades))

    a,b=_v368_split_symbols(symbols)
    if a:
        rows.append(_v3681_group_compare(all_trades, a, 'OOS-A｜偶數序列', min_trades))
    if b:
        rows.append(_v3681_group_compare(all_trades, b, 'OOS-B｜奇數序列', min_trades))

    return pd.DataFrame(rows)

def _v3681_safe_verdict(groups, topstress, min_trades=30):
    if groups is None or groups.empty:
        return '⚪ 無法判定：沒有有效驗證組', []

    valid=groups[groups['樣本數']>=min_trades].copy()
    if valid.empty:
        return f'⚪ 無法判定：所有驗證組樣本皆少於 {min_trades} 筆', [
            '目前不把小樣本結果解讀成通過或失敗。',
            '先檢查漏斗與K線下載成功率，再擴大驗證股票數。'
        ]

    score=0; notes=[]
    pos_ret=(valid['平均報酬改善ppt']>0).mean()*100
    pos_pf=(valid['PF改善']>=0).mean()*100

    if pos_ret>=75:
        score+=2; notes.append(f'✅ {pos_ret:.1f}% 足夠樣本驗證組平均報酬優於基準')
    else:
        notes.append(f'🟡 {pos_ret:.1f}% 足夠樣本驗證組平均報酬優於基準')

    if pos_pf>=75:
        score+=1; notes.append(f'✅ {pos_pf:.1f}% 足夠樣本驗證組PF不低於基準')
    else:
        notes.append(f'🟡 {pos_pf:.1f}% 足夠樣本驗證組PF不低於基準')

    if topstress is not None and not topstress.empty:
        x=topstress[topstress['移除候選最高報酬筆數']==5]
        if not x.empty and int(x.iloc[0]['樣本數'])>=min_trades:
            r=x.iloc[0]
            if r['平均報酬改善ppt']>0:
                score+=2; notes.append('✅ 移除Top5後平均報酬仍優於基準')
            else:
                notes.append('❌ 移除Top5後失去平均報酬優勢')
            if pd.notna(r['PF改善']) and r['PF改善']>=0:
                score+=1; notes.append('✅ 移除Top5後PF仍不低於基準')

    if score>=5:
        verdict='🟢 OOS通過'
    elif score>=3:
        verdict='🟡 OOS邊界通過'
    else:
        verdict='🔴 OOS未通過'

    return verdict, notes


# ===== V3.6.12 市場 / 流動性分層診斷 =====
# 不再最佳化出場規則；固定用「40日＋12%硬停損」當基準，
# 專門回答：85分訊號在哪些股票族群有效、哪些族群失效。

@st.cache_data(ttl=3600, show_spinner=False)
def _v369_download_with_meta(symbol):
    """
    下載3年日K，並回傳實際ticker市場後綴與資料。
    """
    import yfinance as yf
    for suffix, market_hint in [('.TW','上市'),('.TWO','上櫃/興櫃')]:
        ticker=str(symbol)+suffix
        try:
            d=yf.download(
                ticker,
                period='3y',
                interval='1d',
                auto_adjust=False,
                progress=False,
                threads=False
            )
            if d is None or d.empty:
                continue
            if isinstance(d.columns,pd.MultiIndex):
                d.columns=[c[0] if isinstance(c,tuple) else c for c in d.columns]
            need=['Open','High','Low','Close','Volume']
            if not all(c in d.columns for c in need):
                continue
            d=d[need].copy().dropna(subset=['Close'])
            d.index=pd.to_datetime(d.index).tz_localize(None)
            if len(d)>=320:
                return d, market_hint, ticker
        except Exception:
            continue
    return pd.DataFrame(), '', ''

def _v369_market_map():
    """
    由完整官方股票池建立 股票代號 -> 市場 對照。
    """
    mp={}
    try:
        u=UNIVERSE.copy()
        if isinstance(u,pd.DataFrame) and not u.empty:
            code_col='股票代號' if '股票代號' in u.columns else None
            if code_col and '市場' in u.columns:
                for _,r in u[[code_col,'市場']].dropna().iterrows():
                    mp[str(r[code_col]).strip().upper()] = str(r['市場']).strip()
    except Exception:
        pass
    return mp

def _v369_liquidity_bucket(turnover):
    if pd.isna(turnover):
        return '未知'
    if turnover < 1e7:
        return '<1千萬'
    if turnover < 5e7:
        return '1千萬~5千萬'
    if turnover < 1e8:
        return '5千萬~1億'
    if turnover < 5e8:
        return '1億~5億'
    return '≥5億'

def _v369_price_bucket(price):
    if pd.isna(price):
        return '未知'
    if price < 30:
        return '<30'
    if price < 60:
        return '30~60'
    if price < 100:
        return '60~100'
    if price < 300:
        return '100~300'
    return '≥300'

def _v369_volume_bucket(volume):
    if pd.isna(volume):
        return '未知'
    if volume < 500_000:
        return '<500張'
    if volume < 2_000_000:
        return '500~2,000張'
    if volume < 5_000_000:
        return '2,000~5,000張'
    if volume < 10_000_000:
        return '5,000~10,000張'
    return '≥10,000張'

def _v369_score_bucket(score):
    if score < 85:
        return '<85'
    if score < 90:
        return '85~89'
    if score < 95:
        return '90~94'
    return '95+'

def _v369_ma200_bucket(close, ma200):
    if pd.isna(close) or pd.isna(ma200):
        return '未知'
    return '站上MA200' if close >= ma200 else '跌破MA200'

def _v369_volratio_bucket(vr):
    if pd.isna(vr):
        return '未知'
    if vr < 0.8:
        return '<0.8'
    if vr < 1.2:
        return '0.8~1.2'
    if vr < 2:
        return '1.2~2'
    return '≥2'

def _v369_collect_events(symbols, cooldown=20):
    """
    只收集技術分數 >=85 的歷史事件，
    用「40日＋12%硬停損」計算最終報酬。
    同時記錄事件當下的市場、價位、成交量、成交額、量比、MA200狀態。
    """
    market_map=_v369_market_map()
    rows=[]
    diag=[]
    total=len(symbols)
    prog=st.progress(0) if total else None

    for idx,sym in enumerate(symbols):
        stat={
            '股票':str(sym).zfill(4),
            'K線成功':False,
            '曾達85分':False,
            '交易筆數':0
        }
        try:
            df, market_hint, ticker=_v369_download_with_meta(sym)
            if df is None or df.empty:
                diag.append(stat)
                continue
            stat['K線成功']=True

            d=indicators(df.copy())
            last=-999999
            count=0
            code=str(sym).zfill(4)
            market=market_map.get(code, market_hint or '未知')

            for i in range(220, len(d)-81):
                if i-last < cooldown:
                    continue
                try:
                    score=float(black_score(d.iloc[:i+1])[0])
                except Exception:
                    continue
                if score < 85:
                    continue

                stat['曾達85分']=True

                tr=_v368_locked_baseline_trade(d,i)
                if not tr:
                    continue

                row=d.iloc[i]
                close=float(row['Close'])
                volume=float(row['Volume']) if pd.notna(row['Volume']) else np.nan
                turnover=close*volume if pd.notna(volume) else np.nan

                vol_ma20=pd.to_numeric(row.get('VOL_MA20',np.nan),errors='coerce')
                vol_ratio=(volume/float(vol_ma20)) if pd.notna(vol_ma20) and float(vol_ma20)>0 else np.nan
                ma200=pd.to_numeric(row.get('MA200',np.nan),errors='coerce')

                rows.append({
                    '股票':code,
                    '市場':market,
                    '進場日':pd.Timestamp(d.index[i]).strftime('%Y-%m-%d'),
                    '技術分數':score,
                    '進場價':close,
                    '成交量股':volume,
                    '估算成交額':turnover,
                    '量比':vol_ratio,
                    'MA200':ma200,
                    '40日基準報酬%':float(tr['報酬%']),
                    '40日出場原因':tr.get('出場原因','')
                })
                count+=1
                last=i

            stat['交易筆數']=count
            diag.append(stat)
        finally:
            if prog is not None:
                prog.progress((idx+1)/max(total,1))

    if prog is not None:
        prog.empty()

    events=pd.DataFrame(rows)
    if not events.empty:
        events['分數區間']=events['技術分數'].apply(_v369_score_bucket)
        events['股價區間']=events['進場價'].apply(_v369_price_bucket)
        events['成交量區間']=events['成交量股'].apply(_v369_volume_bucket)
        events['成交額區間']=events['估算成交額'].apply(_v369_liquidity_bucket)
        events['量比區間']=events['量比'].apply(_v369_volratio_bucket)
        events['MA200狀態']=events.apply(lambda r:_v369_ma200_bucket(r['進場價'],r['MA200']),axis=1)

    return events, pd.DataFrame(diag)

def _v369_metrics(g):
    r=pd.to_numeric(g['40日基準報酬%'],errors='coerce').dropna()
    if r.empty:
        return {
            '樣本數':0,'勝率%':np.nan,'平均報酬%':np.nan,
            '中位數%':np.nan,'PF':np.nan,'最大單筆虧損%':np.nan
        }
    gp=r[r>0].sum()
    gl=-r[r<0].sum()
    return {
        '樣本數':len(r),
        '勝率%':(r>0).mean()*100,
        '平均報酬%':r.mean(),
        '中位數%':r.median(),
        'PF':gp/gl if gl>0 else np.nan,
        '最大單筆虧損%':r.min()
    }

def _v369_group_summary(events, col, min_sample=30):
    if events is None or events.empty or col not in events.columns:
        return pd.DataFrame()
    rows=[]
    for key,g in events.groupby(col,dropna=False):
        m=_v369_metrics(g)
        rows.append({col:key,**m,'可判讀':m['樣本數']>=min_sample})
    out=pd.DataFrame(rows)
    if not out.empty:
        out['正期望']=(out['平均報酬%']>0)&(out['PF']>1)
        out=out.sort_values(['可判讀','正期望','平均報酬%'],ascending=[False,False,False])
    return out

def _v369_cross_summary(events, col1, col2, min_sample=20):
    if events is None or events.empty:
        return pd.DataFrame()
    rows=[]
    for (a,b),g in events.groupby([col1,col2],dropna=False):
        m=_v369_metrics(g)
        rows.append({col1:a,col2:b,**m,'可判讀':m['樣本數']>=min_sample})
    out=pd.DataFrame(rows)
    if not out.empty:
        out['正期望']=(out['平均報酬%']>0)&(out['PF']>1)
        out=out.sort_values(['可判讀','正期望','平均報酬%'],ascending=[False,False,False])
    return out

def _v369_key_findings(tables):
    """
    從各分層表自動找出「樣本足夠且正期望」與「樣本足夠且負期望」群組。
    """
    good=[]
    bad=[]
    for label,df,keycol in tables:
        if df is None or df.empty:
            continue
        for _,r in df.iterrows():
            if not bool(r.get('可判讀',False)):
                continue
            text=f"{label}={r.get(keycol)}｜n={int(r['樣本數'])}｜均報{r['平均報酬%']:.2f}%｜PF {r['PF']:.2f}"
            if r['平均報酬%']>0 and pd.notna(r['PF']) and r['PF']>1:
                good.append(text)
            elif r['平均報酬%']<0 or (pd.notna(r['PF']) and r['PF']<1):
                bad.append(text)
    return good[:8],bad[:8]


# ===== V3.6.12 Gate PK + OOS + 獲利集中度 =====
V3610_GATES = {
    '基準｜85+': {'score_min':85,'score_max':None,'ma200':False,'turnover_min':None},
    'Gate A｜85+＋站上MA200': {'score_min':85,'score_max':None,'ma200':True,'turnover_min':None},
    'Gate B｜85+＋MA200＋成交額≥5千萬': {'score_min':85,'score_max':None,'ma200':True,'turnover_min':5e7},
    'Gate C｜85+＋MA200＋成交額≥5億': {'score_min':85,'score_max':None,'ma200':True,'turnover_min':5e8},
    'Gate D｜90~94＋站上MA200': {'score_min':90,'score_max':94.9999,'ma200':True,'turnover_min':None},
    'Gate E｜90~94＋MA200＋成交額≥5億': {'score_min':90,'score_max':94.9999,'ma200':True,'turnover_min':5e8},
}

def _v3610_gate_mask(df,cfg):
    score=pd.to_numeric(df['技術分數'],errors='coerce')
    m=score>=cfg['score_min']
    if cfg['score_max'] is not None: m &= score<=cfg['score_max']
    if cfg['ma200']:
        m &= pd.to_numeric(df['進場價'],errors='coerce') >= pd.to_numeric(df['MA200'],errors='coerce')
    if cfg['turnover_min'] is not None:
        m &= pd.to_numeric(df['估算成交額'],errors='coerce') >= cfg['turnover_min']
    return m.fillna(False)

def _v3610_metrics(r):
    r=pd.Series(r,dtype=float).dropna()
    if r.empty:
        return {'樣本數':0,'勝率%':np.nan,'平均報酬%':np.nan,'中位數%':np.nan,'PF':np.nan,'最大單筆虧損%':np.nan}
    gp=r[r>0].sum(); gl=-r[r<0].sum()
    return {
        '樣本數':len(r),'勝率%':(r>0).mean()*100,'平均報酬%':r.mean(),
        '中位數%':r.median(),'PF':gp/gl if gl>0 else np.nan,'最大單筆虧損%':r.min()
    }

def _v3610_prepare_events(result_df,sample_cap=1000,cooldown=20):
    universe=_v3681_universe_symbols(result_df)
    symbols=universe[:min(int(sample_cap),len(universe))]
    events,diag=_v369_collect_events(symbols,cooldown=cooldown)
    if events is None or events.empty: return pd.DataFrame(),diag
    events=events.copy()
    events['進場日']=pd.to_datetime(events['進場日'])
    events['進場年']=events['進場日'].dt.year
    return events,diag

def _v3610_gate_pk(events,min_sample=30):
    rows=[]
    for name,cfg in V3610_GATES.items():
        g=events[_v3610_gate_mask(events,cfg)]
        m=_v3610_metrics(g['40日基準報酬%'])
        rows.append({'模型':name,**m,'可判讀':m['樣本數']>=min_sample})
    out=pd.DataFrame(rows)
    if not out.empty:
        b=out[out['模型']=='基準｜85+'].iloc[0]
        out['勝率改善ppt']=out['勝率%']-b['勝率%']
        out['平均報酬改善ppt']=out['平均報酬%']-b['平均報酬%']
        out['中位數改善ppt']=out['中位數%']-b['中位數%']
        out['PF改善']=out['PF']-b['PF']
    return out.sort_values(['可判讀','平均報酬%','PF'],ascending=[False,False,False])

def _v3610_time_oos(events,min_sample=30):
    parts=[]
    for _,g in events.groupby('股票'):
        g=g.sort_values('進場日').copy()
        if len(g)<4: continue
        cut=max(1,int(len(g)*0.7))
        g['樣本區段']='開發70%'
        g.iloc[cut:,g.columns.get_loc('樣本區段')]='OOS30%'
        parts.append(g)
    z=pd.concat(parts,ignore_index=True) if parts else pd.DataFrame()
    rows=[]
    if z.empty: return pd.DataFrame()
    for seg,sg in z.groupby('樣本區段'):
        for name,cfg in V3610_GATES.items():
            g=sg[_v3610_gate_mask(sg,cfg)]
            m=_v3610_metrics(g['40日基準報酬%'])
            rows.append({'樣本區段':seg,'模型':name,**m,'可判讀':m['樣本數']>=min_sample})
    out=pd.DataFrame(rows)
    base=out[out['模型']=='基準｜85+'][['樣本區段','勝率%','平均報酬%','中位數%','PF']].rename(
        columns={'勝率%':'基準勝率%','平均報酬%':'基準平均報酬%','中位數%':'基準中位數%','PF':'基準PF'})
    out=out.merge(base,on='樣本區段',how='left')
    out['勝率改善ppt']=out['勝率%']-out['基準勝率%']
    out['平均報酬改善ppt']=out['平均報酬%']-out['基準平均報酬%']
    out['中位數改善ppt']=out['中位數%']-out['基準中位數%']
    out['PF改善']=out['PF']-out['基準PF']
    return out

def _v3610_walk_forward(events,min_sample=20):
    rows=[]
    for year,ydf in events.groupby('進場年'):
        for name,cfg in V3610_GATES.items():
            g=ydf[_v3610_gate_mask(ydf,cfg)]
            m=_v3610_metrics(g['40日基準報酬%'])
            rows.append({'年度':int(year),'模型':name,**m,'可判讀':m['樣本數']>=min_sample})
    out=pd.DataFrame(rows)
    base=out[out['模型']=='基準｜85+'][['年度','平均報酬%','PF','勝率%']].rename(
        columns={'平均報酬%':'基準平均報酬%','PF':'基準PF','勝率%':'基準勝率%'})
    out=out.merge(base,on='年度',how='left')
    out['平均報酬改善ppt']=out['平均報酬%']-out['基準平均報酬%']
    out['PF改善']=out['PF']-out['基準PF']
    out['勝率改善ppt']=out['勝率%']-out['基準勝率%']
    return out

def _v3610_profit_concentration(events,min_sample=30):
    conc=[]; stress=[]
    for name,cfg in V3610_GATES.items():
        g=events[_v3610_gate_mask(events,cfg)].copy()
        if g.empty: continue
        r=pd.to_numeric(g['40日基準報酬%'],errors='coerce').dropna()
        pos=r[r>0].sort_values(ascending=False); total=pos.sum()
        def share(pct):
            if len(pos)==0 or total<=0:return np.nan
            n=max(1,int(np.ceil(len(pos)*pct/100)))
            return pos.head(n).sum()/total*100
        conc.append({'模型':name,'樣本數':len(r),'正報酬交易數':len(pos),
                     'Top1%獲利貢獻率%':share(1),'Top3%獲利貢獻率%':share(3),'Top5%獲利貢獻率%':share(5)})
        for pct in [0,1,3,5]:
            x=g.sort_values('40日基準報酬%',ascending=False).copy()
            n=0 if pct==0 else max(1,int(np.ceil(len(x)*pct/100)))
            if n:x=x.iloc[n:].copy()
            m=_v3610_metrics(x['40日基準報酬%'])
            stress.append({'模型':name,'移除最高報酬比例%':pct,'移除筆數':n,**m,'可判讀':m['樣本數']>=min_sample})
    conc=pd.DataFrame(conc); stress=pd.DataFrame(stress)
    if not stress.empty:
        base=stress[stress['模型']=='基準｜85+'][['移除最高報酬比例%','平均報酬%','PF','勝率%']].rename(
            columns={'平均報酬%':'基準平均報酬%','PF':'基準PF','勝率%':'基準勝率%'})
        stress=stress.merge(base,on='移除最高報酬比例%',how='left')
        stress['平均報酬改善ppt']=stress['平均報酬%']-stress['基準平均報酬%']
        stress['PF改善']=stress['PF']-stress['基準PF']
        stress['勝率改善ppt']=stress['勝率%']-stress['基準勝率%']
    return conc,stress

def _v3610_gate_summary(pk,oos,wf,stress,min_sample=30):
    rows=[]
    for name in V3610_GATES:
        if name=='基準｜85+': continue
        p=pk[pk['模型']==name]
        o=oos[(oos['模型']==name)&(oos['樣本區段']=='OOS30%')]
        w=wf[(wf['模型']==name)&(wf['可判讀']==True)]
        s5=stress[(stress['模型']==name)&(stress['移除最高報酬比例%']==5)]
        row={'模型':name}
        row['整體樣本數']=int(p.iloc[0]['樣本數']) if not p.empty else 0
        row['整體平均報酬改善ppt']=float(p.iloc[0]['平均報酬改善ppt']) if not p.empty else np.nan
        row['整體PF改善']=float(p.iloc[0]['PF改善']) if not p.empty else np.nan
        row['OOS樣本數']=int(o.iloc[0]['樣本數']) if not o.empty else 0
        row['OOS平均報酬改善ppt']=float(o.iloc[0]['平均報酬改善ppt']) if not o.empty else np.nan
        row['OOS_PF改善']=float(o.iloc[0]['PF改善']) if not o.empty else np.nan
        row['可判讀年度數']=len(w)
        row['正改善年度比例%']=((w['平均報酬改善ppt']>0).mean()*100) if len(w) else np.nan
        row['移除Top5%後平均報酬改善ppt']=float(s5.iloc[0]['平均報酬改善ppt']) if not s5.empty else np.nan
        row['移除Top5%後PF改善']=float(s5.iloc[0]['PF改善']) if not s5.empty else np.nan
        score=0
        if row['整體樣本數']>=min_sample and row['整體平均報酬改善ppt']>0: score+=1
        if row['整體PF改善']>=0: score+=1
        if row['OOS樣本數']>=min_sample and row['OOS平均報酬改善ppt']>0: score+=2
        if row['OOS_PF改善']>=0: score+=1
        if pd.notna(row['正改善年度比例%']) and row['正改善年度比例%']>=60: score+=1
        if pd.notna(row['移除Top5%後平均報酬改善ppt']) and row['移除Top5%後平均報酬改善ppt']>0: score+=1
        row['Gate證據分']=score
        row['Gate候選']=score>=5
        rows.append(row)
    out=pd.DataFrame(rows)
    return out.sort_values(['Gate候選','Gate證據分','OOS平均報酬改善ppt'],ascending=[False,False,False]) if not out.empty else out


# ===== V3.6.12 正式進場引擎：鎖定 Gate D =====

# ===== V3.6.12 Gate D 真實資金 / 持倉壓力測試 =====
# 原則：不再調 Gate D 進場條件，只測試「同時持股數、資金配置、成本、重疊交易」。
def _v3612_daily_close_map(events):
    out={}
    if events is None or events.empty: return out
    for sym,g in events.groupby('股票'):
        gg=g.sort_values('進場日')
        # 事件表本身不一定保存完整逐日價格，因此本版以每筆交易的進/出場報酬
        # 建立「實現損益資金曲線」，不假裝成每日 mark-to-market。
        out[str(sym)]=gg
    return out

def _v3612_prepare_trades(events):
    if events is None or events.empty:
        return pd.DataFrame()
    z=events[_v3611_event_mask(events)].copy()
    if z.empty: return z
    z['進場日']=pd.to_datetime(z['進場日'])
    # 40日策略的實際報酬欄已含既有停損邏輯；若有實際出場日/持有天數則優先使用。
    ret_col='40日基準報酬%'
    z['策略報酬%']=pd.to_numeric(z[ret_col],errors='coerce')
    z=z.dropna(subset=['策略報酬%','進場日']).copy()
    if '實際持有天數' in z.columns:
        hold=pd.to_numeric(z['實際持有天數'],errors='coerce').fillna(40).clip(lower=1)
    elif '平均持有天數' in z.columns:
        hold=pd.to_numeric(z['平均持有天數'],errors='coerce').fillna(40).clip(lower=1)
    else:
        hold=pd.Series(40,index=z.index,dtype=float)
    z['持有天數']=hold.astype(int)
    if '出場日' in z.columns:
        z['資金出場日']=pd.to_datetime(z['出場日'],errors='coerce')
    else:
        z['資金出場日']=pd.NaT
    miss=z['資金出場日'].isna()
    z.loc[miss,'資金出場日']=z.loc[miss,'進場日']+pd.to_timedelta(z.loc[miss,'持有天數'],unit='D')
    return z.sort_values(['進場日','技術分數'],ascending=[True,False]).reset_index(drop=True)

def _v3612_simulate(trades, initial_capital=1_000_000, max_positions=10,
                    position_pct=10.0, fee_pct=0.1425, tax_pct=0.30,
                    slippage_pct=0.10):
    """
    事件驅動資金模擬：
    - 同時最多 max_positions
    - 每筆投入目前權益的 position_pct%
    - 同股持有中不重複進場
    - 同日訊號以技術分數高者優先
    - 買賣手續費、賣出交易稅、雙邊滑價納入
    - 權益曲線為「實現損益曲線」；最大回撤不冒充逐日MTM回撤
    """
    if trades is None or trades.empty:
        return pd.DataFrame(),pd.DataFrame(),{}

    x=trades.copy().sort_values(['進場日','技術分數'],ascending=[True,False])
    cash=float(initial_capital)
    realized_equity=float(initial_capital)
    open_pos=[]
    logs=[]
    curve=[{'日期':x['進場日'].min(),'實現權益':realized_equity,'現金':cash,'持倉數':0}]
    buy_cost=fee_pct/100 + slippage_pct/100
    sell_cost=fee_pct/100 + tax_pct/100 + slippage_pct/100

    dates=sorted(set(x['進場日']).union(set(x['資金出場日'])))
    for dt in dates:
        # 先出場，釋放資金
        closing=[p for p in open_pos if p['exit_date']<=dt]
        for p in closing:
            gross=p['alloc']*(1+p['gross_ret']/100)
            proceeds=max(0.0,gross*(1-sell_cost))
            cash+=proceeds
            net_pnl=proceeds-p['alloc']*(1+buy_cost)
            realized_equity+=net_pnl
            logs.append({
                '股票':p['股票'],'名稱':p.get('名稱',''),'進場日':p['entry_date'],
                '出場日':p['exit_date'],'技術分數':p['score'],
                '投入資金':p['alloc'],'毛報酬%':p['gross_ret'],
                '淨損益':net_pnl,'淨報酬%':net_pnl/(p['alloc']*(1+buy_cost))*100 if p['alloc'] else 0,
                '持有天數':p['hold']
            })
            open_pos.remove(p)

        todays=x[x['進場日']==dt]
        held={p['股票'] for p in open_pos}
        for _,r in todays.iterrows():
            if len(open_pos)>=max_positions: break
            sym=str(r['股票'])
            if sym in held: continue
            # 以「目前實現權益」決定目標部位，並受現金限制
            target=max(0.0,realized_equity*(position_pct/100))
            alloc=min(target, cash/(1+buy_cost))
            if alloc<=0: break
            cash-=alloc*(1+buy_cost)
            open_pos.append({
                '股票':sym,'名稱':r.get('名稱',''),'entry_date':dt,
                'exit_date':r['資金出場日'],'score':float(r['技術分數']),
                'alloc':alloc,'gross_ret':float(r['策略報酬%']),
                'hold':int(r['持有天數'])
            })
            held.add(sym)

        curve.append({'日期':dt,'實現權益':realized_equity,'現金':cash,'持倉數':len(open_pos)})

    # 最後把剩餘持倉按其策略報酬實現，避免資金曲線遺漏
    for p in list(open_pos):
        gross=p['alloc']*(1+p['gross_ret']/100)
        proceeds=max(0.0,gross*(1-sell_cost))
        cash+=proceeds
        net_pnl=proceeds-p['alloc']*(1+buy_cost)
        realized_equity+=net_pnl
        logs.append({
            '股票':p['股票'],'名稱':p.get('名稱',''),'進場日':p['entry_date'],
            '出場日':p['exit_date'],'技術分數':p['score'],
            '投入資金':p['alloc'],'毛報酬%':p['gross_ret'],
            '淨損益':net_pnl,'淨報酬%':net_pnl/(p['alloc']*(1+buy_cost))*100 if p['alloc'] else 0,
            '持有天數':p['hold']
        })
    if dates:
        curve.append({'日期':max(dates),'實現權益':realized_equity,'現金':cash,'持倉數':0})

    eq=pd.DataFrame(curve).sort_values('日期').drop_duplicates('日期',keep='last')
    lg=pd.DataFrame(logs)
    if not eq.empty:
        peak=eq['實現權益'].cummax()
        dd=(eq['實現權益']/peak-1)*100
        max_dd=float(dd.min())
    else: max_dd=np.nan
    total_ret=(realized_equity/initial_capital-1)*100
    wins=(lg['淨損益']>0).mean()*100 if len(lg) else np.nan
    gp=lg.loc[lg['淨損益']>0,'淨損益'].sum() if len(lg) else 0
    gl=-lg.loc[lg['淨損益']<0,'淨損益'].sum() if len(lg) else 0
    pf=gp/gl if gl>0 else (np.inf if gp>0 else 0)
    stats={
        '初始資金':initial_capital,'期末實現權益':realized_equity,'總報酬%':total_ret,
        '實現權益最大回撤%':max_dd,'完成交易':len(lg),'淨勝率%':wins,'淨PF':pf,
        '最大同時持股':max_positions,'單筆目標資金%':position_pct
    }
    return eq,lg,stats

def _v3613_simulate(trades, initial_capital=1_000_000, max_positions=20,
                    position_pct=5.0, fee_pct=0.1425, tax_pct=0.30,
                    slippage_pct=0.10):
    # 延用 V3.6.12 的成交/成本邏輯，再額外量測「容量是否卡住」
    if trades is None or trades.empty:
        return pd.DataFrame(),pd.DataFrame(),{}

    x=trades.copy().sort_values(['進場日','技術分數'],ascending=[True,False])
    cash=float(initial_capital)
    realized_equity=float(initial_capital)
    open_pos=[]
    logs=[]
    curve=[{'日期':x['進場日'].min(),'實現權益':realized_equity,'現金':cash,'持倉數':0}]
    buy_cost=fee_pct/100 + slippage_pct/100
    sell_cost=fee_pct/100 + tax_pct/100 + slippage_pct/100

    signal_count=0
    accepted_count=0
    rejected_slot=0
    rejected_cash=0
    rejected_same_stock=0
    peak_positions=0
    exposure_samples=[]

    dates=sorted(set(x['進場日']).union(set(x['資金出場日'])))
    for dt in dates:
        closing=[p for p in open_pos if p['exit_date']<=dt]
        for p in closing:
            gross=p['alloc']*(1+p['gross_ret']/100)
            proceeds=max(0.0,gross*(1-sell_cost))
            cash+=proceeds
            net_pnl=proceeds-p['alloc']*(1+buy_cost)
            realized_equity+=net_pnl
            logs.append({
                '股票':p['股票'],'名稱':p.get('名稱',''),'進場日':p['entry_date'],
                '出場日':p['exit_date'],'技術分數':p['score'],
                '投入資金':p['alloc'],'毛報酬%':p['gross_ret'],
                '淨損益':net_pnl,'淨報酬%':net_pnl/(p['alloc']*(1+buy_cost))*100 if p['alloc'] else 0,
                '持有天數':p['hold']
            })
            open_pos.remove(p)

        todays=x[x['進場日']==dt]
        held={p['股票'] for p in open_pos}
        for _,r in todays.iterrows():
            signal_count += 1
            sym=str(r['股票'])
            if sym in held:
                rejected_same_stock += 1
                continue
            if len(open_pos)>=max_positions:
                rejected_slot += 1
                continue

            target=max(0.0,realized_equity*(position_pct/100))
            alloc=min(target, cash/(1+buy_cost))
            # 若連目標部位的 20% 都放不下，視為資金不足而不硬塞極小部位
            if alloc<=0 or (target>0 and alloc < target*0.20):
                rejected_cash += 1
                continue

            cash-=alloc*(1+buy_cost)
            open_pos.append({
                '股票':sym,'名稱':r.get('名稱',''),'entry_date':dt,
                'exit_date':r['資金出場日'],'score':float(r['技術分數']),
                'alloc':alloc,'gross_ret':float(r['策略報酬%']),
                'hold':int(r['持有天數'])
            })
            held.add(sym)
            accepted_count += 1

        peak_positions=max(peak_positions,len(open_pos))
        gross_open=sum(p['alloc'] for p in open_pos)
        exposure_samples.append(gross_open/max(realized_equity,1e-9)*100)
        curve.append({'日期':dt,'實現權益':realized_equity,'現金':cash,'持倉數':len(open_pos)})

    for p in list(open_pos):
        gross=p['alloc']*(1+p['gross_ret']/100)
        proceeds=max(0.0,gross*(1-sell_cost))
        cash+=proceeds
        net_pnl=proceeds-p['alloc']*(1+buy_cost)
        realized_equity+=net_pnl
        logs.append({
            '股票':p['股票'],'名稱':p.get('名稱',''),'進場日':p['entry_date'],
            '出場日':p['exit_date'],'技術分數':p['score'],
            '投入資金':p['alloc'],'毛報酬%':p['gross_ret'],
            '淨損益':net_pnl,'淨報酬%':net_pnl/(p['alloc']*(1+buy_cost))*100 if p['alloc'] else 0,
            '持有天數':p['hold']
        })
    if dates:
        curve.append({'日期':max(dates),'實現權益':realized_equity,'現金':cash,'持倉數':0})

    eq=pd.DataFrame(curve).sort_values('日期').drop_duplicates('日期',keep='last')
    lg=pd.DataFrame(logs)
    if not eq.empty:
        peak=eq['實現權益'].cummax()
        dd=(eq['實現權益']/peak-1)*100
        max_dd=float(dd.min())
        avg_positions=float(eq['持倉數'].mean())
    else:
        max_dd=np.nan
        avg_positions=np.nan

    total_ret=(realized_equity/initial_capital-1)*100
    wins=(lg['淨損益']>0).mean()*100 if len(lg) else np.nan
    gp=lg.loc[lg['淨損益']>0,'淨損益'].sum() if len(lg) else 0
    gl=-lg.loc[lg['淨損益']<0,'淨損益'].sum() if len(lg) else 0
    pf=gp/gl if gl>0 else (np.inf if gp>0 else 0)

    capacity_reject = rejected_slot + rejected_cash
    stats={
        '初始資金':initial_capital,'期末實現權益':realized_equity,'總報酬%':total_ret,
        '實現權益最大回撤%':max_dd,'完成交易':len(lg),'淨勝率%':wins,'淨PF':pf,
        '最大同時持股':max_positions,'單筆目標資金%':position_pct,
        '實際最高持股':peak_positions,'平均持股數':avg_positions,
        '總進場訊號':signal_count,'接受訊號':accepted_count,
        '槽位不足淘汰':rejected_slot,'資金不足淘汰':rejected_cash,
        '同股重複略過':rejected_same_stock,
        '容量淘汰率%':capacity_reject/signal_count*100 if signal_count else 0,
        '訊號承接率%':accepted_count/signal_count*100 if signal_count else 0,
        '平均資金使用率%':float(np.mean(exposure_samples)) if exposure_samples else 0,
        '最高資金使用率%':float(np.max(exposure_samples)) if exposure_samples else 0,
    }
    return eq,lg,stats

def _v3613_scenario_grid(trades, initial_capital, fee_pct, tax_pct, slippage_pct):
    rows=[]
    # V3.6.14：只把名目配置 <=100% 視為可行候選；避免 25檔×5%=125% 這類假容量勝出。
    pos_list=[5,10,15,20,25,30,35,40]
    pct_list=[2.0,2.5,3.0,3.33,4.0,5.0,7.5,10.0]
    for pos in pos_list:
        for pct in pct_list:
            if pos*pct>125:
                continue
            eq,lg,s=_v3613_simulate(trades,initial_capital,pos,pct,fee_pct,tax_pct,slippage_pct)
            s['名目資金需求%']=float(pos*pct)
            s['配置可行']=bool(pos*pct<=100.0001)
            s['現金緩衝%']=float(max(0,100-pos*pct))
            rows.append(s)
    df=pd.DataFrame(rows)
    if not df.empty:
        df['報酬回撤比']=df['總報酬%']/df['實現權益最大回撤%'].abs().replace(0,np.nan)
        feasible=df['配置可行']
        work=df.loc[feasible].copy()
        for c in ['總報酬%','淨PF','訊號承接率%','報酬回撤比']:
            lo,hi=float(work[c].min()),float(work[c].max())
            work[c+'_N']=0.5 if hi<=lo else (work[c]-lo)/(hi-lo)
        mdd=work['實現權益最大回撤%'].abs(); lo,hi=float(mdd.min()),float(mdd.max())
        work['MDD_N']=0.5 if hi<=lo else 1-(mdd-lo)/(hi-lo)
        work['容量效率分數']=(work['總報酬%_N']*.30+work['MDD_N']*.25+work['淨PF_N']*.20+work['訊號承接率%_N']*.20+work['報酬回撤比_N']*.05)*100
        df=df.merge(work[['最大同時持股','單筆目標資金%','容量效率分數']],on=['最大同時持股','單筆目標資金%'],how='left')
        df=df.sort_values(['配置可行','容量效率分數','總報酬%'],ascending=[False,False,False]).reset_index(drop=True)
    return df

# V3.6.10 結論：
# Gate D（90~94 + 站上MA200）在樣本量、OOS改善、跨年度穩定與移除Top5%後
# 的綜合證據排名第一，因此本版鎖死規則，不再調參。
V3611_GATE_NAME='Gate D｜90~94＋站上MA200'
V3611_SCORE_MIN=90.0
V3611_SCORE_MAX=95.0

def _v3611_event_mask(df):
    score=pd.to_numeric(df['技術分數'],errors='coerce')
    close=pd.to_numeric(df['進場價'],errors='coerce')
    ma200=pd.to_numeric(df['MA200'],errors='coerce')
    return ((score>=V3611_SCORE_MIN)&(score<V3611_SCORE_MAX)&(close>=ma200)).fillna(False)

def _v3611_current_candidates(result_df):
    """
    正式盤中/盤後候選：
    只用鎖定 Gate D，不再加入成交額、KD、量比等新門檻。
    """
    if result_df is None or result_df.empty:
        return pd.DataFrame()
    z=result_df.copy()
    score_col='黑嚕嚕分數' if '黑嚕嚕分數' in z.columns else ('綜合分數' if '綜合分數' in z.columns else None)
    if score_col is None or '價格' not in z.columns or 'MA200' not in z.columns:
        return pd.DataFrame()

    score=pd.to_numeric(z[score_col],errors='coerce')
    price=pd.to_numeric(z['價格'],errors='coerce')
    ma200=pd.to_numeric(z['MA200'],errors='coerce')
    z=z[(score>=90)&(score<95)&(price>=ma200)].copy()
    if z.empty:
        return z

    z['Gate']='D40｜90~94＋站上MA200'
    z['距MA200%']=(pd.to_numeric(z['價格'],errors='coerce')/pd.to_numeric(z['MA200'],errors='coerce')-1)*100
    if '成交量' in z.columns:
        z['估算成交額']=pd.to_numeric(z['價格'],errors='coerce')*pd.to_numeric(z['成交量'],errors='coerce')
    else:
        z['估算成交額']=np.nan

    # 排名只排序，不作額外淘汰：先分數，再成交額，再距MA200較近者
    z['_score']=pd.to_numeric(z[score_col],errors='coerce')
    z['_liq']=pd.to_numeric(z['估算成交額'],errors='coerce').fillna(0)
    z['_dist']=pd.to_numeric(z['距MA200%'],errors='coerce').abs()
    z=z.sort_values(['_score','_liq','_dist'],ascending=[False,False,True])

    keep=['股票','名稱','市場','價格','漲跌%','K','D','MA15','MA60','MA200',
          '距MA200%','量比','估算成交額',score_col,'訊號','價格來源','行情狀態','行情時間','技術資料日','Gate']
    keep=[c for c in keep if c in z.columns]
    return z[keep].reset_index(drop=True)

def _v3611_metrics(g):
    return _v3610_metrics(g['40日基準報酬%'])

def _v3611_locked_validation(events,min_sample=50):
    """
    鎖死 Gate D 後的正式驗證：
    1. 整體
    2. 時間 OOS 70/30
    3. 年度
    4. 市場
    5. 成交額
    6. 股價
    7. 移除Top5%大贏家
    """
    if events is None or events.empty:
        return {}

    base=events.copy()
    gate=events[_v3611_event_mask(events)].copy()

    overall=[]
    for name,g in [('基準｜85+',base),(V3611_GATE_NAME,gate)]:
        m=_v3611_metrics(g)
        overall.append({'模型':name,**m,'可判讀':m['樣本數']>=min_sample})
    overall=pd.DataFrame(overall)
    b=overall.iloc[0]
    overall['勝率改善ppt']=overall['勝率%']-b['勝率%']
    overall['平均報酬改善ppt']=overall['平均報酬%']-b['平均報酬%']
    overall['中位數改善ppt']=overall['中位數%']-b['中位數%']
    overall['PF改善']=overall['PF']-b['PF']

    # OOS：每檔依時間前70/後30
    parts=[]
    for _,g in events.groupby('股票'):
        g=g.sort_values('進場日').copy()
        if len(g)<4: continue
        cut=max(1,int(np.floor(len(g)*0.7)))
        g['樣本區段']='開發70%'
        g.iloc[cut:,g.columns.get_loc('樣本區段')]='OOS30%'
        parts.append(g)
    zz=pd.concat(parts,ignore_index=True) if parts else pd.DataFrame()

    oos_rows=[]
    if not zz.empty:
        for seg,sg in zz.groupby('樣本區段'):
            for name,g in [('基準｜85+',sg),(V3611_GATE_NAME,sg[_v3611_event_mask(sg)])]:
                m=_v3611_metrics(g)
                oos_rows.append({'樣本區段':seg,'模型':name,**m,'可判讀':m['樣本數']>=min_sample})
    oos=pd.DataFrame(oos_rows)
    if not oos.empty:
        bb=oos[oos['模型']=='基準｜85+'][['樣本區段','勝率%','平均報酬%','中位數%','PF']].rename(
            columns={'勝率%':'基準勝率%','平均報酬%':'基準平均報酬%','中位數%':'基準中位數%','PF':'基準PF'})
        oos=oos.merge(bb,on='樣本區段',how='left')
        oos['勝率改善ppt']=oos['勝率%']-oos['基準勝率%']
        oos['平均報酬改善ppt']=oos['平均報酬%']-oos['基準平均報酬%']
        oos['中位數改善ppt']=oos['中位數%']-oos['基準中位數%']
        oos['PF改善']=oos['PF']-oos['基準PF']

    def stratify(col):
        rows=[]
        vals=[x for x in events[col].dropna().unique()]
        for v in vals:
            bg=events[events[col]==v]
            gg=bg[_v3611_event_mask(bg)]
            bm=_v3611_metrics(bg); gm=_v3611_metrics(gg)
            rows.append({
                col:v,'基準樣本':bm['樣本數'],'Gate樣本':gm['樣本數'],
                'Gate勝率%':gm['勝率%'],'基準勝率%':bm['勝率%'],
                '勝率改善ppt':gm['勝率%']-bm['勝率%'],
                'Gate平均報酬%':gm['平均報酬%'],'基準平均報酬%':bm['平均報酬%'],
                '平均報酬改善ppt':gm['平均報酬%']-bm['平均報酬%'],
                'Gate中位數%':gm['中位數%'],'基準中位數%':bm['中位數%'],
                '中位數改善ppt':gm['中位數%']-bm['中位數%'],
                'GatePF':gm['PF'],'基準PF':bm['PF'],'PF改善':gm['PF']-bm['PF'],
                '可判讀':gm['樣本數']>=min_sample
            })
        return pd.DataFrame(rows).sort_values(['可判讀','平均報酬改善ppt'],ascending=[False,False]) if rows else pd.DataFrame()

    market=stratify('市場')
    turnover=stratify('成交額區間')
    price=stratify('股價區間')

    # 年度
    ey=events.copy()
    ey['年度']=pd.to_datetime(ey['進場日']).dt.year
    year=stratify.__name__  # placeholder to keep linter quiet
    year_rows=[]
    for y,bg in ey.groupby('年度'):
        gg=bg[_v3611_event_mask(bg)]
        bm=_v3611_metrics(bg); gm=_v3611_metrics(gg)
        year_rows.append({
            '年度':int(y),'基準樣本':bm['樣本數'],'Gate樣本':gm['樣本數'],
            'Gate勝率%':gm['勝率%'],'基準勝率%':bm['勝率%'],'勝率改善ppt':gm['勝率%']-bm['勝率%'],
            'Gate平均報酬%':gm['平均報酬%'],'基準平均報酬%':bm['平均報酬%'],
            '平均報酬改善ppt':gm['平均報酬%']-bm['平均報酬%'],
            'GatePF':gm['PF'],'基準PF':bm['PF'],'PF改善':gm['PF']-bm['PF'],
            '可判讀':gm['樣本數']>=max(20,min_sample//2)
        })
    year=pd.DataFrame(year_rows)

    # Top5%壓力
    stress_rows=[]
    for name,g in [('基準｜85+',base),(V3611_GATE_NAME,gate)]:
        x=g.sort_values('40日基準報酬%',ascending=False).copy()
        n=max(1,int(np.ceil(len(x)*0.05))) if len(x) else 0
        x=x.iloc[n:].copy() if n else x
        m=_v3611_metrics(x)
        stress_rows.append({'模型':name,'移除Top5%筆數':n,**m})
    stress=pd.DataFrame(stress_rows)
    if len(stress)==2:
        sb=stress.iloc[0]
        stress['勝率改善ppt']=stress['勝率%']-sb['勝率%']
        stress['平均報酬改善ppt']=stress['平均報酬%']-sb['平均報酬%']
        stress['中位數改善ppt']=stress['中位數%']-sb['中位數%']
        stress['PF改善']=stress['PF']-sb['PF']

    # 正式判定
    gate_overall=overall[overall['模型']==V3611_GATE_NAME].iloc[0]
    gate_oos=oos[(oos['模型']==V3611_GATE_NAME)&(oos['樣本區段']=='OOS30%')].iloc[0] if not oos.empty else None
    gate_stress=stress[stress['模型']==V3611_GATE_NAME].iloc[0] if not stress.empty else None
    valid_year=year[year['可判讀']==True] if not year.empty else pd.DataFrame()
    positive_year_ratio=(valid_year['平均報酬改善ppt']>0).mean()*100 if len(valid_year) else np.nan

    rules=[]
    def add_rule(name,ok,value):
        rules.append({'驗證條件':name,'結果':value,'通過':bool(ok)})

    add_rule('整體平均報酬優於85+基準',gate_overall['平均報酬改善ppt']>0,f"{gate_overall['平均報酬改善ppt']:.2f} ppt")
    add_rule('整體PF不低於基準',gate_overall['PF改善']>=0,f"{gate_overall['PF改善']:.2f}")
    if gate_oos is not None:
        add_rule('OOS樣本數足夠',gate_oos['樣本數']>=min_sample,f"{int(gate_oos['樣本數'])} 筆")
        add_rule('OOS平均報酬優於基準',gate_oos['平均報酬改善ppt']>0,f"{gate_oos['平均報酬改善ppt']:.2f} ppt")
        add_rule('OOS PF不低於基準',gate_oos['PF改善']>=0,f"{gate_oos['PF改善']:.2f}")
    add_rule('跨年度正改善比例≥60%',pd.notna(positive_year_ratio) and positive_year_ratio>=60,f"{positive_year_ratio:.1f}%")
    if gate_stress is not None:
        add_rule('移除Top5%後平均報酬仍優於基準',gate_stress['平均報酬改善ppt']>0,f"{gate_stress['平均報酬改善ppt']:.2f} ppt")
        add_rule('移除Top5%後PF仍不低於基準',gate_stress['PF改善']>=0,f"{gate_stress['PF改善']:.2f}")

    rules=pd.DataFrame(rules)
    passed=int(rules['通過'].sum()) if not rules.empty else 0
    total=len(rules)
    verdict='🟢 正式通過' if total and passed==total else ('🟡 條件通過' if total and passed>=total-2 else '🔴 未通過')

    return {
        'overall':overall,'oos':oos,'year':year,'market':market,
        'turnover':turnover,'price':price,'stress':stress,
        'rules':rules,'verdict':verdict,'passed':passed,'total':total,
        'events':events,'gate_events':gate
    }

st.sidebar.title('🖤 黑嚕嚕－台股盤中雷達');st.sidebar.caption('V3.6.12｜B模式：即時優先＋最新盤後價備援')
FUGLE_SECRET_KEY=get_secret_value('FUGLE_API_KEY','')
fugle_session_key=st.sidebar.text_input('Fugle API Key（可留空）',type='password',value='',help='建議正式版放 Streamlit Secrets：FUGLE_API_KEY')
FUGLE_API_KEY=(FUGLE_SECRET_KEY or fugle_session_key).strip()
if FUGLE_API_KEY:
    st.sidebar.success('⚡ Fugle Key 已載入')
else:
    st.sidebar.info('⚪ Fugle 尚未設定，使用官方日行情 / Yahoo 日K備援')
mode=st.sidebar.selectbox('雷達模式',['全部股票','🟣 黑嚕嚕超強','🔥 強勢股','🚀 強勢突破','🔥 主升段','🟢 守護生命線','⚠️ 大量換手高危','🔴 趨勢轉弱'])
markets=st.sidebar.multiselect('市場',['上市','上櫃','興櫃'],default=['上市','上櫃','興櫃'])
if st.sidebar.button('🔄 更新股票池與行情'):
    load_market_universe.clear()
    load_market_snapshot.clear()
    load_twse_daily_report.clear()
    load_fugle_snapshot.clear()
    fugle_intraday_quote.clear()
    get_stock_data.clear()
    st.rerun()
counts=UNIVERSE['市場'].value_counts().to_dict() if not UNIVERSE.empty else {}
st.sidebar.caption(f"官方股票池：上市 {counts.get('上市',0)}｜上櫃 {counts.get('上櫃',0)}｜興櫃 {counts.get('興櫃',0)}")
st.sidebar.caption(f"股票池最近同步（台灣）：{UNIVERSE_FETCH_TIME}｜每日 18:00 後首次執行自動換日同步，也可手動更新")
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
official_snapshot,official_status,OFFICIAL_SNAPSHOT_FETCH_TIME=load_market_snapshot()
twse_eod,twse_eod_status,TWSE_EOD_FETCH_TIME=load_twse_daily_report()
# 上市股票盤後優先使用 TWSE MI_INDEX；它直接提供完整 OHLCV
if twse_eod is not None and not twse_eod.empty:
    _other=official_snapshot[official_snapshot['市場']!='上市'].copy() if official_snapshot is not None and not official_snapshot.empty else pd.DataFrame()
    official_snapshot=pd.concat([twse_eod,_other],ignore_index=True,sort=False)
fugle_snapshot,fugle_status,FUGLE_SNAPSHOT_FETCH_TIME=load_fugle_snapshot(tuple(markets),FUGLE_API_KEY)
smart_snapshot=combine_quote_snapshots(fugle_snapshot,official_snapshot)
smart_status=(fugle_status if FUGLE_API_KEY else [])+official_status
smart_note=''
SMART_SNAPSHOT_FETCH_TIME=FUGLE_SNAPSHOT_FETCH_TIME if (fugle_snapshot is not None and not fugle_snapshot.empty) else OFFICIAL_SNAPSHOT_FETCH_TIME

if scan_mode.startswith('🧠'):
    base_universe=UNIVERSE[UNIVERSE['市場'].isin(markets)].copy() if not UNIVERSE.empty else pd.DataFrame()
    smart_pool,smart_note=smart_rank_universe(base_universe,smart_snapshot,markets,max_n,smart_min_volume,smart_min_value,smart_min_change)
    symbols=smart_pool['股票代號'].astype(str).str.zfill(4).tolist()
    market_map.update(dict(zip(smart_pool['股票代號'],smart_pool['市場'])))
else:
    symbols=symbols[:max_n]

quote_map={}
if smart_snapshot is not None and not smart_snapshot.empty and '股票代號' in smart_snapshot.columns:
    for _,_q in smart_snapshot.drop_duplicates('股票代號',keep='first').iterrows():
        quote_map[str(_q['股票代號']).zfill(4)]=_q.to_dict()

st.title('🖤 黑嚕嚕－台股盤中雷達')
st.caption('V3.6.13.1｜Gate D 資金容量健診：Gate D 條件已鎖定，本頁只做容量與資金壓力測試。')
st.markdown('**目前行情策略：B 模式｜🟢 即時優先 → 🔴 最新盤後價備援**')
_now_tw=taiwan_now();_session=taiwan_market_session(_now_tw)
a,b,c,d,e=st.columns(5)
a.metric('技術精掃',f'{len(symbols)} 檔');b.metric('全市場股票池',f'{len(UNIVERSE)} 檔')
c.metric('最低量比',f'{min_vr:.1f}x');d.metric('台股狀態',_session);e.metric('畫面更新（台灣）',_now_tw.strftime('%H:%M:%S'))
with st.expander('🕒 資料更新時間與價格來源',expanded=True):
    st.write(f"**台灣時間：** {taiwan_time_text(_now_tw)}")
    st.write(f"**股票池最近同步：** {UNIVERSE_FETCH_TIME}（台灣時間）")
    st.write(f"**Fugle 快照時間：** {FUGLE_SNAPSHOT_FETCH_TIME if (fugle_snapshot is not None and not fugle_snapshot.empty) else '未連線／無權限'}")
    st.write(f"**官方備援行情抓取：** {OFFICIAL_SNAPSHOT_FETCH_TIME}")
    st.write("**股票池更新規則：** 每日台灣時間 18:00 後首次執行自動同步；也可按側邊欄「更新股票池與行情」。")
    st.caption('B 模式原則：有即時價就用即時價；沒有即時價也不隱藏股票，仍顯示最新官方盤後價 / Yahoo 日K，並以紅色狀態清楚標示。')
    if fugle_snapshot is not None and not fugle_snapshot.empty:
        st.success('⚡ Fugle 即時行情已啟用：排行榜使用 Fugle 價格，並把今日 OHLCV 當成未完成日K注入技術計算。')
    elif FUGLE_API_KEY:
        st.warning('🔴 B模式：Fugle Key 已載入但即時 snapshot 未成功。排行榜仍顯示最新可用盤後價，但非即時資料會明確標紅。')
    else:
        st.info('🔴 B模式：尚未設定 Fugle API Key。排行榜仍顯示最新可用盤後價，但非即時資料會明確標紅。')
    if _session=='盤中' and (fugle_snapshot is None or fugle_snapshot.empty):
        st.warning('目前為盤中，但即時行情層未啟用：技術分數主要依完整日K，請勿把排行榜價格視為即時成交價。')
    elif _session!='盤中':
        st.info('目前非一般盤中時段；收盤後 Fugle 快照可用於確認當日最終行情，歷史回測仍以完整日K為準。')
st.divider()
if scan_mode.startswith('🧠') and not smart_pool.empty and '智能初篩分' in smart_pool.columns:
    with st.expander('🔎 查看 V3.3.2 智能候選池',expanded=False):
        preview=smart_pool[['股票代號','股票名稱','市場','智能初篩分','漲跌幅%','成交量','成交額']].copy()
        preview=preview.rename(columns={'股票代號':'股票','股票名稱':'名稱','漲跌幅%':'漲跌幅'})
        preview['漲跌幅']=preview['漲跌幅'].map(lambda x:f'{x:+.2f}%' if pd.notna(x) else '-')
        preview['成交量']=preview['成交量'].map(lambda x:f'{x:,.0f}')
        preview['成交額']=preview['成交額'].map(lambda x:f'{x:,.0f}')
        st.dataframe(preview,use_container_width=True,hide_index=True)
if fugle_snapshot is None or fugle_snapshot.empty:
    st.error('🚨 B模式：即時行情未啟用。排行榜仍會顯示最新可用的官方盤後價 / Yahoo 日K，但這些價格不是即時成交價，會以紅色狀態標示。')
else:
    _expected=expected_quote_date_tw()
    if _expected and '報價日期' in fugle_snapshot.columns:
        _dates=set(fugle_snapshot['報價日期'].dropna().astype(str))
        if _expected not in _dates:
            st.error(f'🚨 Fugle 快照日期異常：台灣預期 {_expected}，但回傳日期為 {sorted(_dates)[:5]}。本次不可當成當日行情。')

if scan_mode.startswith('🧠'):
    _prefilter_source='Fugle 即時快照' if (fugle_snapshot is not None and not fugle_snapshot.empty) else '官方日行情'
    st.info(f'🧠 智能掃描：先用 {_prefilter_source} 篩選，再對 {len(symbols)} 檔進行 2 年歷史＋今日即時技術分析。{smart_note}')
    if smart_status: st.caption('｜'.join(smart_status))

with st.expander('⚡ V3.4 即時行情診斷',expanded=False):
    st.write(f"**Fugle Key：** {'已載入' if FUGLE_API_KEY else '未設定'}")
    st.write(f"**Fugle 快照筆數：** {0 if fugle_snapshot is None else len(fugle_snapshot):,}")
    st.write(f"**官方備援筆數：** {0 if official_snapshot is None else len(official_snapshot):,}")
    st.write(f"**TWSE 每日收盤行情：** {twse_eod_status}｜抓取 {TWSE_EOD_FETCH_TIME or '—'}")
    if twse_eod is not None and not twse_eod.empty:
        _w=twse_eod[twse_eod['股票代號'].astype(str).str.zfill(4)=='2436']
        if not _w.empty:
            _w=_w.iloc[-1]
            st.write(f"**2436 偉詮電 TWSE驗證：** O {_w.get('開盤價','—')}｜H {_w.get('最高價','—')}｜L {_w.get('最低價','—')}｜C {_w.get('收盤價','—')}｜日期 {_w.get('報價日期','—')}")
    if fugle_status: st.write('**Fugle 狀態：** '+'｜'.join(fugle_status))
    if official_status: st.write('**官方狀態：** '+'｜'.join(official_status))
    test_code='3167'
    st.markdown('#### 🔬 3167 大量資料真偽檢查')
    st.write(f"**目前推估最近完整交易日：** {expected_completed_market_date().strftime('%Y-%m-%d')}")
    if test_code in quote_map:
        q=quote_map[test_code]
        st.write(f"**市場快照：** 價格 {q.get('收盤價','—')}｜來源 {q.get('行情來源','—')}｜日期 {q.get('報價日期','—')}｜時間 {q.get('報價時間','—')}")
        st.write(f"**日期健康度：** {quote_date_health(q.get('報價日期',''),q.get('行情來源',''))}")
    else:st.write('**市場快照：** 本次未包含 3167')
    iq,iq_status=fugle_intraday_quote(test_code,FUGLE_API_KEY)
    if iq:
        st.write(f"**Fugle 單檔 intraday：** lastPrice {iq.get('lastPrice','—')}｜closePrice {iq.get('closePrice','—')}｜previousClose {iq.get('previousClose','—')}｜日期 {iq.get('日期','—')}｜更新 {iq.get('lastUpdated','—')}｜isClose={iq.get('isClose','—')}")
        snap=pd.to_numeric(quote_map.get(test_code,{}).get('收盤價',np.nan),errors='coerce')
        exact=pd.to_numeric(iq.get('lastPrice'),errors='coerce')
        if pd.notna(snap) and pd.notna(exact) and abs(float(snap)-float(exact))>1e-9:
            st.error(f"⚠️ 3167 快照與單檔即時價不一致：快照 {snap:g} / intraday {exact:g}")
        elif pd.notna(exact):st.success('3167 快照與單檔 intraday 價格一致。')
        exp=expected_quote_date_tw()
        if exp and iq.get('日期')!=exp:st.error(f"🚨 單檔行情日期落後：預期 {exp}，實際 {iq.get('日期') or '未知'}")
    else:st.warning(f"**Fugle 單檔 intraday 測試失敗：** {iq_status}")
    st.caption('B模式判讀：若價格來源不是 Fugle 5秒快照，App 仍保留最新可用盤後價，但會標紅；這是刻意的備援顯示，不代表即時行情。')

rows=[];p=st.progress(0);status=st.empty()
for i,s in enumerate(symbols):
    status.text(f'正在掃描：{s} {stock_name(s)}　({i+1}/{len(symbols)})');df=get_stock_data(s, market_map.get(s))
    if df is None: p.progress((i+1)/max(len(symbols),1));continue
    r=build_row(s,df,quote_map.get(str(s).zfill(4)),SMART_SNAPSHOT_FETCH_TIME)
    if r:
        sig=r['訊號'];market_ok=(not markets or r['市場'] in markets or r['市場']=='未分類');change_ok=change_range[0]<=r['漲跌%']<=change_range[1];k_ok=pd.isna(r['K']) or k_range[0]<=r['K']<=k_range[1];base=r['黑嚕嚕分數']>=min_score and r['量比']>=min_vr and change_ok and k_ok and market_ok
        mode_ok={'全部股票':True,'🟣 黑嚕嚕超強':r['黑嚕嚕分數']>=90,'🔥 強勢股':r['黑嚕嚕分數']>=80 and r['漲跌%']>0,'🚀 強勢突破':'🚀 強勢突破' in sig,'🔥 主升段':'🔥 主升段' in sig,'🟢 守護生命線':'🟢 守護生命線' in sig,'⚠️ 大量換手高危':'⚠️ 爆量高危' in sig,'🔴 趨勢轉弱':'🔴 趨勢轉弱' in sig}.get(mode,True)
        if base and mode_ok:rows.append(r)
    p.progress((i+1)/max(len(symbols),1))
status.empty();p.empty()
if not rows:st.warning('目前沒有符合條件的股票。可以降低最低黑嚕嚕分數、量比、KD／漲跌幅，或增加股票池。');st.stop()
result=pd.DataFrame(rows);result=add_composite_columns(result);sort_col={'黑嚕嚕分數':'黑嚕嚕分數','漲跌幅':'漲跌%','量比':'量比','K值':'K','價格':'價格'}[sort_mode];result=result.sort_values(sort_col,ascending=False,na_position='last').reset_index(drop=True)
if '技術資料日' in result.columns and not result.empty:
    _dates=sorted(result['技術資料日'].dropna().astype(str).unique().tolist())
    _latest='、'.join(_dates[-3:]) if _dates else '未知'
    if (result['價格來源']=='Fugle 5秒快照').any():
        _src='⚡ Fugle 5秒快照＋盤中未完成日K'
    elif result['價格來源'].isin(['官方日行情','TWSE每日收盤行情']).any():
        _src='官方日行情'
    else:
        _src='Yahoo Finance 日K'
    st.caption(f"📅 技術最新資料日：{_latest}｜行情來源：{_src}")

strong=int((result['黑嚕嚕分數']>=80).sum());breakout=int(result['訊號'].str.contains('🚀 強勢突破',regex=False).sum());risk=int(result['訊號'].str.contains('⚠️ 爆量高危',regex=False).sum());weak=int(result['訊號'].str.contains('🔴 趨勢轉弱',regex=False).sum())
a,b,c,d,e=st.columns(5);a.metric('符合條件',f'{len(result)} 檔');b.metric('🔥 80分以上',f'{strong} 檔');c.metric('🚀 突破',f'{breakout} 檔');d.metric('⚠️ 高危',f'{risk} 檔');e.metric('🔴 轉弱',f'{weak} 檔');st.divider()

st.subheader('🖤 黑嚕嚕焦點');top=result.head(4);cols=st.columns(len(top))
for col,(_,r) in zip(cols,top.iterrows()):
    icon='🟢' if r['漲跌%']>0 else '🔴' if r['漲跌%']<0 else '⚪'
    with col:st.markdown(f'''<div class="radar-card"><div class="radar-title">{icon} {r['股票']} {r['名稱']}</div><div class="small">{r['市場']}</div><div class="radar-price">{r['價格']:.2f}</div><div>{r['漲跌%']:+.2f}%　量比 {r['量比']:.2f}x　KD K {r['K']:.1f} / D {r['D']:.1f}</div><div class="radar-score">🖤 {r['黑嚕嚕分數']} / 100</div><div>{r['等級']}</div><div class="signal">{r['訊號']}</div></div>''',unsafe_allow_html=True)

t1,t2,t3,t4,t5,t6=st.tabs([
    '📋 黑嚕嚕排行榜',
    '🚨 訊號中心',
    '📊 分數拆解',
    '📈 個股分析',
    '⭐ 自選股',
    '🧪 3.6.14 容量最佳化'
])

# V3.6.12：法人資料僅供閱讀，不改變排序分數。
result, _v360_chip_df, _v360_chip_date = v360_merge_chip_data(result, chip_days=15)
result = v358_attach_chip_labels(result)


with t1:
    st.caption('V3.6.12｜法人資料修正＋進出場風控研究。黑嚕嚕技術100分維持原模型；外資/投信僅作資訊標籤。')
    if isinstance(_v360_chip_date, str) and _v360_chip_date not in ('TWSE T86 無有效資料','日期未知'):
        _today_tw = taiwan_now().strftime('%Y-%m-%d')
        if _v360_chip_date == _today_tw:
            st.success(f'🏦 法人資料日：{_v360_chip_date}｜TWSE T86 已接入')
        else:
            st.warning(f'🏦 法人資料日：{_v360_chip_date}｜尚未更新至今日 {_today_tw}')
    else:
        st.warning(f'🏦 法人資料狀態：{_v360_chip_date}')

    with st.expander('🩺 法人資料管線診斷', expanded=False):
        _diag, _hist_diag, _cf_diag = v361_chip_diagnostics(result, chip_days=15)
        d1,d2,d3,d4 = st.columns(4)
        d1.metric('T86歷史筆數', _diag.get('T86歷史筆數',0))
        d2.metric('T86股票數', _diag.get('T86股票數',0))
        d3.metric('排行榜股票數', _diag.get('排行榜股票數',0))
        d4.metric('merge成功數', _diag.get('merge成功數',0))

        e1,e2,e3,e4 = st.columns(4)
        e1.metric('T86最新日期', _diag.get('T86最新日期','—'))
        e2.metric('chip_features筆數', _diag.get('chip_features筆數',0))
        e3.metric('外資有效檔數', _diag.get('外資有效檔數',0))
        e4.metric('投信有效檔數', _diag.get('投信有效檔數',0))

        st.caption(f"T86抓取狀態：{_diag.get('T86抓取狀態','—')}")
        if _diag.get('狀態') == 'OK':
            st.success('法人資料管線：OK')
        else:
            st.error(f"法人資料管線異常：{_diag.get('狀態','未知')}")
    if '外資狀態' in result.columns:
        with st.expander('🏦 法人籌碼資訊標籤', expanded=False):
            base_cols=[c for c in ['股票','名稱','綜合分數','黑嚕嚕分數','價格','外資狀態','投信狀態'] if c in result.columns]
            if base_cols:
                st.dataframe(result[base_cols],use_container_width=True,hide_index=True)
    show=result[['股票','名稱','市場','價格','漲跌%','量比','成交量','K','D','黑嚕嚕分數','綜合分數','綜合等級','日期檢查','行情狀態','行情時間','技術狀態','外資狀態','投信狀態','價格來源','訊號']].copy();show['價格']=show['價格'].map(lambda x:f'{x:.2f}');show['漲跌%']=show['漲跌%'].map(lambda x:f'{x:+.2f}%');show['量比']=show['量比'].map(lambda x:f'{x:.2f}x');show['成交量']=show['成交量'].map(lambda x:f'{x:,.0f}');show['K']=show['K'].map(lambda x:f'{x:.1f}' if pd.notna(x) else '-');show['D']=show['D'].map(lambda x:f'{x:.1f}' if pd.notna(x) else '-')
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
    _chart_date=pd.Timestamp(d.index[-1]).strftime('%Y-%m-%d') if not d.empty else '—'
    st.caption(f"📅 圖表最新K棒：{_chart_date}｜{r.get('技術狀態','—')}｜價格來源：{r.get('價格來源','—')}")
    # V3.6.12：標準K棒；若 Streamlit 尚未安裝 plotly，避免整個 App crash
    if PLOTLY_OK:
        fig = go.Figure()
        fig.add_trace(go.Candlestick(
            x=d.index,
            open=d['Open'],
            high=d['High'],
            low=d['Low'],
            close=d['Close'],
            name='日K',
            increasing_line_color='#e53935',
            increasing_fillcolor='#e53935',
            decreasing_line_color='#16a34a',
            decreasing_fillcolor='#16a34a',
            whiskerwidth=0.35
        ))
        if 'MA15' in d.columns:
            fig.add_trace(go.Scatter(x=d.index,y=d['MA15'],mode='lines',name='MA15',line=dict(width=1.5)))
        if 'MA60' in d.columns:
            fig.add_trace(go.Scatter(x=d.index,y=d['MA60'],mode='lines',name='MA60',line=dict(width=1.5)))
        if 'MA200' in d.columns:
            fig.add_trace(go.Scatter(x=d.index,y=d['MA200'],mode='lines',name='MA200',line=dict(width=1.8)))

        fig.update_layout(
            height=520,
            margin=dict(l=10,r=10,t=35,b=10),
            xaxis_rangeslider_visible=False,
            hovermode='x unified',
            legend=dict(orientation='h',yanchor='bottom',y=1.02,xanchor='left',x=0),
            xaxis_title='',
            yaxis_title='價格',
            dragmode='pan'
        )
        fig.update_xaxes(
            type='date',
            rangebreaks=[dict(bounds=['sat','mon'])],
            showspikes=True,
            spikemode='across',
            spikesnap='cursor'
        )
        fig.update_yaxes(fixedrange=False,showspikes=True,spikemode='across')
        st.plotly_chart(fig,use_container_width=True,config={'scrollZoom':True,'displaylogo':False})
    else:
        st.error('缺少 plotly 套件：請把 plotly 加入 requirements.txt。已暫時改用折線圖備援，避免整個 App 無法啟動。')
        st.line_chart(d[['Close','MA15','MA60','MA200']].rename(columns={'Close':'股價'}),height=420)

    a,b,c,d2,e=st.columns(5)
    a.metric('黑嚕嚕',f"{r['黑嚕嚕分數']}分")
    b.metric('量比',f"{r['量比']:.2f}x")
    c.metric('KD K',f"{r['K']:.1f}" if pd.notna(r['K']) else '-')
    d2.metric('MA15',f"{r['MA15']:.2f}")
    e.metric('MA200',f"{r['MA200']:.2f}")

    st.markdown('#### 🔊 成交量')
    if PLOTLY_OK:
        vol_fig=go.Figure()
        vol_colors=['#e53935' if c>=o else '#16a34a' for o,c in zip(d['Open'],d['Close'])]
        vol_fig.add_trace(go.Bar(x=d.index,y=d['Volume'],name='成交量',marker_color=vol_colors))
        if 'VOL_MA20' in d.columns:
            vol_fig.add_trace(go.Scatter(x=d.index,y=d['VOL_MA20'],mode='lines',name='20日均量',line=dict(width=1.6)))
        vol_fig.update_layout(
            height=260,
            margin=dict(l=10,r=10,t=30,b=10),
            xaxis_rangeslider_visible=False,
            hovermode='x unified',
            legend=dict(orientation='h',yanchor='bottom',y=1.02,xanchor='left',x=0),
            xaxis_title='',
            yaxis_title='成交量'
        )
        vol_fig.update_xaxes(type='date',rangebreaks=[dict(bounds=['sat','mon'])])
        st.plotly_chart(vol_fig,use_container_width=True,config={'displaylogo':False})
    else:
        st.line_chart(d[['Volume','VOL_MA20']].rename(columns={'Volume':'成交量','VOL_MA20':'20日均量'}),height=250)

    st.markdown('#### 🚨 目前訊號')
    st.info(r['訊號'])
    st.markdown('#### 🧠 黑嚕嚕判讀')
    st.write(r['判斷'] or '目前沒有額外判讀。')
with t5:
    st.subheader('⭐ 自選股');watch=st.multiselect('加入自選股',result['股票'].tolist(),default=[],key='watchlist')
    if not watch:st.info('請從上方選擇股票加入自選股。')
    else:
        q=result[result['股票'].isin(watch)].sort_values('黑嚕嚕分數',ascending=False)[['股票','名稱','價格','漲跌%','量比','K','D','黑嚕嚕分數','綜合分數','綜合等級','訊號']].copy();q['價格']=q['價格'].map(lambda x:f'{x:.2f}');q['漲跌%']=q['漲跌%'].map(lambda x:f'{x:+.2f}%');q['量比']=q['量比'].map(lambda x:f'{x:.2f}x');q['K']=q['K'].map(lambda x:f'{x:.1f}' if pd.notna(x) else '-');q['D']=q['D'].map(lambda x:f'{x:.1f}' if pd.notna(x) else '-');st.dataframe(q,use_container_width=True,hide_index=True)








with t6:
    st.subheader('🚦 V3.6.12 正式進場引擎｜Gate D 已鎖定')
    st.caption('V3.6.13.1｜Gate D 已完成正式驗證；本版主流程直接進入資金容量／同時持股壓力測試。')

    st.success('🔒 鎖定規則：90 ≤ 黑嚕嚕技術分數 < 95，且股價 ≥ MA200。40日持有＋12%硬停損沿用既有鎖定基準。')

    current=_v3611_current_candidates(result)
    st.markdown('### 🎯 ① 今日 Gate D 候選')
    if current.empty:
        st.info('目前掃描結果沒有符合 Gate D 的股票。這不是錯誤；正式引擎不會為了湊股票放寬條件。')
    else:
        c1,c2,c3,c4=st.columns(4)
        c1.metric('Gate D候選',len(current))
        if '市場' in current.columns:
            c2.metric('上市',int((current['市場']=='上市').sum()))
            c3.metric('上櫃',int((current['市場']=='上櫃').sum()))
            c4.metric('興櫃',int((current['市場']=='興櫃').sum()))
        st.dataframe(current,use_container_width=True,hide_index=True)
        st.download_button(
            '⬇️ 下載今日 Gate D 候選',
            current.to_csv(index=False,encoding='utf-8-sig').encode('utf-8-sig'),
            'V3.6.12_today_GateD_candidates.csv','text/csv',key='dl_v3611_today'
        )

    st.markdown('### 🧱 ② 全市場正式驗證')
    universe=_v3681_universe_symbols(result)
    total_u=len(universe)
    c1,c2,c3=st.columns(3)
    c1.metric('完整股票池',total_u)
    opts=[x for x in [1000,1500,2000,total_u] if 0<x<=max(total_u,1)]
    opts=sorted(set(opts)) or [0]
    default_idx=len(opts)-1  # 正式版預設全部
    cap=c2.selectbox('正式驗證股票數',opts,index=default_idx,key='v3611_cap')
    min_sample=c3.selectbox('最低有效樣本',[30,50,80,100],index=1,key='v3611_min')
    cooldown=st.radio('同股冷卻交易日',[20,30],horizontal=True,index=0,key='v3611_cd')

    if st.button('▶ 建立 Gate D 鎖定事件資料（首次 / 清快取後才需要）',type='primary',key='run_v3611'):
        with st.spinner('建立 Gate D 鎖定事件資料 → OOS/年度/分層資料同步建立，供容量引擎使用...'):
            events,diag=_v3610_prepare_events(result,cap,cooldown)
            pack=_v3611_locked_validation(events,min_sample)
            st.session_state['v3611_pack']=pack
            st.session_state['v3611_diag']=diag

    pack=st.session_state.get('v3611_pack',{})
    if pack:
        st.success('✅ Gate D 鎖定事件已載入，下方可直接執行 V3.6.13.1 容量健診。')
        with st.expander('📋 Gate D 既有驗證紀錄（預設收合）', expanded=False):
            overall=pack['overall']; oos=pack['oos']; year=pack['year']
            market=pack['market']; turnover=pack['turnover']; price=pack['price']
            stress=pack['stress']; rules=pack['rules']
    
            st.markdown('### 📊 ③ 鎖定 Gate D vs 85+基準')
            st.dataframe(overall,use_container_width=True,hide_index=True)
    
            st.markdown('### 🧪 ④ OOS｜開發70% vs OOS30%')
            st.dataframe(oos,use_container_width=True,hide_index=True)
    
            st.markdown('### 📅 ⑤ 年度穩定度')
            st.dataframe(year,use_container_width=True,hide_index=True)
    
            st.markdown('### 🏢 ⑥ 市場別壓力測試')
            st.dataframe(market,use_container_width=True,hide_index=True)
    
            c1,c2=st.columns(2)
            with c1:
                st.markdown('### 💰 ⑦ 成交額分層')
                st.dataframe(turnover,use_container_width=True,hide_index=True)
            with c2:
                st.markdown('### 💵 ⑧ 股價分層')
                st.dataframe(price,use_container_width=True,hide_index=True)
    
            st.markdown('### 💥 ⑨ 移除 Top5% 大贏家')
            st.dataframe(stress,use_container_width=True,hide_index=True)
    
            st.markdown('### 🧾 ⑩ 正式啟用判定')
            st.dataframe(rules,use_container_width=True,hide_index=True)
            verdict=pack['verdict']
            if verdict.startswith('🟢'):
                st.success(f"{verdict}｜{pack['passed']}/{pack['total']} 項通過。Gate D 可進入下一版資金/持倉引擎。")
            elif verdict.startswith('🟡'):
                st.warning(f"{verdict}｜{pack['passed']}/{pack['total']} 項通過。保留Gate D，但下一版先做資金曲線與持倉壓力測試。")
            else:
                st.error(f"{verdict}｜{pack['passed']}/{pack['total']} 項通過。Gate D 暫不進正式交易。")
    
            st.download_button(
                '⬇️ 下載 V3.6.12 正式驗證規則',
                rules.to_csv(index=False,encoding='utf-8-sig').encode('utf-8-sig'),
                'V3.6.12_GateD_formal_validation.csv','text/csv',key='dl_v3611_rules'
            )
    
            st.caption('本版刻意不加入成交額門檻：V3.6.10 雖然 Gate C/E 的平均報酬更高，但 Gate D 的 OOS證據、樣本量與壓力測試綜合排名第一，因此先鎖定 Gate D，避免再次資料探勘。')
    else:
        st.info('容量引擎需要 Gate D 歷史事件。第一次進入或清除 Streamlit 快取後，請先按上方「建立 Gate D 鎖定事件資料」一次；Gate D 規則仍固定為 90~94＋站上MA200，不重新選模。')


# ============================================================
# 📈 V3.6.15 Gate D 真實逐日 Mark-to-Market Portfolio Engine
# 重要修正：V3.6.14 在事件表缺少出場日時，曾以「進場日 + 持有天數(日曆日)」
# 近似資金出場日。V3.6.15 重新回抓每檔完整日K，依原始鎖定規則
# 「90~94 + 站上MA200；40交易日 + 盤中 -12% 硬停損」重建真正出場交易日，
# 再逐交易日把未實現損益納入權益，計算真正 Portfolio MDD。
# ============================================================

def _v3615_rebuild_trades_and_prices(events):
    if events is None or events.empty:
        return pd.DataFrame(),{},pd.DataFrame()
    base=events[_v3611_event_mask(events)].copy()
    if base.empty:return pd.DataFrame(),{},pd.DataFrame()
    base['進場日']=pd.to_datetime(base['進場日']).dt.normalize()
    rows=[]; price_map={}; diag=[]
    symbols=base['股票'].astype(str).str.zfill(4).drop_duplicates().tolist()
    prog=st.progress(0) if symbols else None
    for k,sym in enumerate(symbols):
        try:
            df,market_hint,ticker=_v369_download_with_meta(sym)
            if df is None or df.empty:
                diag.append({'股票':sym,'狀態':'K線失敗','重建交易':0});continue
            d=indicators(df.copy())
            d.index=pd.to_datetime(d.index).tz_localize(None) if getattr(pd.to_datetime(d.index),'tz',None) is not None else pd.to_datetime(d.index)
            d=d[~d.index.duplicated(keep='last')].sort_index()
            price_map[sym]=d[['Open','High','Low','Close','Volume']].copy()
            g=base[base['股票'].astype(str).str.zfill(4)==sym]
            cnt=0
            for _,r in g.iterrows():
                dt=pd.Timestamp(r['進場日']).normalize()
                loc=np.where(d.index.normalize()==dt)[0]
                if len(loc)==0:continue
                i=int(loc[-1])
                tr=_v368_locked_baseline_trade(d,i)
                if not tr:continue
                rr=r.to_dict()
                rr['進場日']=pd.Timestamp(tr['進場日'])
                rr['資金出場日']=pd.Timestamp(tr['出場日'])
                rr['持有交易日']=int(tr.get('持有天數',40))
                rr['重建進場價']=float(tr['進場價'])
                rr['重建出場價']=float(tr['出場價'])
                rr['重建毛報酬%']=float(tr['報酬%'])
                rr['重建出場原因']=str(tr.get('出場原因',''))
                rows.append(rr);cnt+=1
            diag.append({'股票':sym,'狀態':'OK','重建交易':cnt,'K線起日':d.index.min().strftime('%Y-%m-%d'),'K線迄日':d.index.max().strftime('%Y-%m-%d')})
        except Exception as e:
            diag.append({'股票':sym,'狀態':f'失敗:{type(e).__name__}','重建交易':0})
        finally:
            if prog is not None:prog.progress((k+1)/max(len(symbols),1))
    if prog is not None:prog.empty()
    z=pd.DataFrame(rows)
    if not z.empty:
        z=z.sort_values(['進場日','技術分數'],ascending=[True,False]).reset_index(drop=True)
    return z,price_map,pd.DataFrame(diag)

def _v3615_close_on(price_map,sym,dt, fallback=np.nan):
    d=price_map.get(str(sym).zfill(4))
    if d is None or d.empty:return fallback
    dt=pd.Timestamp(dt).normalize()
    try:
        if dt in d.index:
            v=pd.to_numeric(d.loc[dt,'Close'],errors='coerce')
            if isinstance(v,pd.Series):v=v.iloc[-1]
            return float(v) if pd.notna(v) else fallback
        prev=d.loc[d.index<=dt,'Close']
        return float(prev.iloc[-1]) if len(prev) else fallback
    except Exception:return fallback

def _v3615_true_mtm(trades,price_map,initial_capital=1_000_000,max_positions=25,position_pct=4.0,
                    fee_pct=0.1425,tax_pct=0.30,slippage_pct=0.10):
    if trades is None or trades.empty:return pd.DataFrame(),pd.DataFrame(),{}
    x=trades.copy().sort_values(['進場日','技術分數'],ascending=[True,False])
    buy_fee=fee_pct/100; sell_fee=(fee_pct+tax_pct)/100; slip=slippage_pct/100
    cash=float(initial_capital); open_pos=[]; logs=[]; curve=[]
    signal_count=accepted=rejected_slot=rejected_cash=rejected_same=0
    start=pd.Timestamp(x['進場日'].min()).normalize(); end=pd.Timestamp(x['資金出場日'].max()).normalize()
    all_dates=set()
    for sym in x['股票'].astype(str).str.zfill(4).unique():
        d=price_map.get(sym)
        if d is not None and not d.empty:
            all_dates.update(pd.Timestamp(v).normalize() for v in d.index if start<=pd.Timestamp(v).normalize()<=end)
    dates=sorted(all_dates)
    if not dates:return pd.DataFrame(),pd.DataFrame(),{}

    def mtm_equity(dt):
        mv=0.0
        for p in open_pos:
            px=_v3615_close_on(price_map,p['股票'],dt,p['last_px'])
            if np.isfinite(px):p['last_px']=px
            mv+=p['shares']*p['last_px']
        return cash+mv,mv

    for dt in dates:
        # 先以真實策略出場日平倉，當天資金可再使用
        closing=[p for p in open_pos if p['exit_date']<=dt]
        for p in closing:
            exit_raw=float(p['exit_price'])
            exit_exec=exit_raw*(1-slip)
            proceeds=p['shares']*exit_exec*(1-sell_fee)
            cash+=proceeds
            cost=p['shares']*p['entry_exec']*(1+buy_fee)
            pnl=proceeds-cost
            logs.append({'股票':p['股票'],'名稱':p['名稱'],'進場日':p['entry_date'],'出場日':p['exit_date'],
                         '技術分數':p['score'],'投入資金':cost,'進場價':p['entry_raw'],'出場價':exit_raw,
                         '出場原因':p['reason'],'淨損益':pnl,'淨報酬%':pnl/cost*100 if cost else 0,
                         '持有交易日':p['hold']})
            open_pos.remove(p)

        todays=x[x['進場日']==dt]
        held={p['股票'] for p in open_pos}
        for _,r in todays.iterrows():
            signal_count+=1;sym=str(r['股票']).zfill(4)
            if sym in held:rejected_same+=1;continue
            if len(open_pos)>=max_positions:rejected_slot+=1;continue
            eq_now,_=mtm_equity(dt)
            target=max(0.0,eq_now*position_pct/100)
            entry_raw=float(r['重建進場價']);entry_exec=entry_raw*(1+slip)
            per_share=entry_exec*(1+buy_fee)
            alloc=min(target,cash)
            if alloc<=0 or (target>0 and alloc<target*.20):rejected_cash+=1;continue
            shares=alloc/per_share
            actual_cost=shares*per_share
            cash-=actual_cost
            open_pos.append({'股票':sym,'名稱':r.get('名稱',''),'entry_date':dt,'exit_date':pd.Timestamp(r['資金出場日']),
                             'score':float(r['技術分數']),'shares':shares,'entry_raw':entry_raw,'entry_exec':entry_exec,
                             'exit_price':float(r['重建出場價']),'reason':str(r['重建出場原因']),
                             'hold':int(r['持有交易日']),'last_px':entry_raw})
            held.add(sym);accepted+=1
        eq,mv=mtm_equity(dt)
        curve.append({'日期':dt,'MTM權益':eq,'現金':cash,'持倉市值':mv,'持倉數':len(open_pos),
                      '資金使用率%':mv/eq*100 if eq>0 else np.nan})

    eq=pd.DataFrame(curve).sort_values('日期').drop_duplicates('日期',keep='last')
    lg=pd.DataFrame(logs)
    if eq.empty:return eq,lg,{}
    eq['權益高點']=eq['MTM權益'].cummax();eq['回撤%']=(eq['MTM權益']/eq['權益高點']-1)*100
    mdd=float(eq['回撤%'].min()); trough_i=eq['回撤%'].idxmin(); trough_date=eq.loc[trough_i,'日期']
    pre=eq.loc[:trough_i]; peak_i=pre['MTM權益'].idxmax(); peak_date=eq.loc[peak_i,'日期']
    final=float(eq.iloc[-1]['MTM權益']); total_ret=(final/initial_capital-1)*100
    years=max((eq.iloc[-1]['日期']-eq.iloc[0]['日期']).days/365.25,1/365.25)
    cagr=((final/initial_capital)**(1/years)-1)*100 if final>0 else -100.0
    calmar=cagr/abs(mdd) if mdd<0 else np.nan
    wins=(lg['淨損益']>0).mean()*100 if len(lg) else np.nan
    gp=lg.loc[lg['淨損益']>0,'淨損益'].sum() if len(lg) else 0;gl=-lg.loc[lg['淨損益']<0,'淨損益'].sum() if len(lg) else 0
    pf=gp/gl if gl>0 else (np.inf if gp>0 else 0)
    stats={'初始資金':initial_capital,'期末MTM權益':final,'總報酬%':total_ret,'CAGR%':cagr,'真實MTM_MDD%':mdd,
           'Calmar':calmar,'MDD高點日':peak_date,'MDD低點日':trough_date,'完成交易':len(lg),'淨勝率%':wins,'淨PF':pf,
           '最大同時持股':max_positions,'單筆目標資金%':position_pct,'實際最高持股':int(eq['持倉數'].max()),
           '平均持股數':float(eq['持倉數'].mean()),'總進場訊號':signal_count,'接受訊號':accepted,
           '訊號承接率%':accepted/signal_count*100 if signal_count else 0,'槽位不足淘汰':rejected_slot,
           '資金不足淘汰':rejected_cash,'同股重複略過':rejected_same,'平均資金使用率%':float(eq['資金使用率%'].mean()),
           '最高資金使用率%':float(eq['資金使用率%'].max())}
    return eq,lg,stats

def _v3615_candidate_grid(trades,price_map,capital,fee,tax,slip):
    candidates=[(20,5.0),(25,4.0),(25,3.33),(25,3.0),(30,3.33)]
    rows=[];packs={}
    for mp,pp in candidates:
        eq,lg,stt=_v3615_true_mtm(trades,price_map,capital,mp,pp,fee,tax,slip)
        if not stt:continue
        stt['配置']=f'{mp}檔 × {pp:g}%';stt['報酬/MDD']=stt['總報酬%']/abs(stt['真實MTM_MDD%']) if stt['真實MTM_MDD%']<0 else np.nan
        rows.append(stt);packs[(mp,pp)]=(eq,lg,stt)
    d=pd.DataFrame(rows)
    if not d.empty:d=d.sort_values(['Calmar','淨PF','總報酬%'],ascending=False).reset_index(drop=True)
    return d,packs

st.divider()
st.subheader('🧪 V3.6.14 可行資金容量 / 同時持股壓力測試')
st.caption('Gate D 繼續鎖定 90~94＋站上MA200。本版不改訊號，只把容量上限擴到 40 檔，量測槽位淘汰、訊號承接率、平均/最高持股與資金使用率。')

if pack:
    trades=_v3612_prepare_trades(pack['events'])
    if trades.empty:
        st.warning('目前沒有可供資金模擬的 Gate D 交易。')
    else:
        c1,c2,c3,c4=st.columns(4)
        capital=c1.number_input('初始資金',min_value=100000,max_value=10000000,value=1000000,step=100000,key='v3612_capital')
        fee=c2.number_input('單邊手續費%',min_value=0.0,max_value=1.0,value=0.1425,step=0.01,format='%.4f',key='v3612_fee')
        tax=c3.number_input('賣出交易稅%',min_value=0.0,max_value=1.0,value=0.30,step=0.05,format='%.2f',key='v3612_tax')
        slip=c4.number_input('單邊滑價%',min_value=0.0,max_value=2.0,value=0.10,step=0.05,format='%.2f',key='v3612_slip')

        grid=_v3613_scenario_grid(trades,capital,fee,tax,slip)
        st.markdown('### 🏆 ⑪ V3.6.14 可行容量甜蜜點｜5 → 40 檔')
        st.dataframe(grid,use_container_width=True,hide_index=True)
        bad_n=int((~grid['配置可行']).sum()) if not grid.empty else 0
        if bad_n:
            st.warning(f'容量真偽檢查：{bad_n} 組名目配置超過100%（例如25檔×5%=125%），V3.6.14保留顯示供比較，但不允許它們成為最佳容量。')

        if not grid.empty:
            best=grid[grid['配置可行']].iloc[0]
            st.success(
                f"目前可行容量首選：最多 {int(best['最大同時持股'])} 檔、每檔 {best['單筆目標資金%']:g}%｜名目需求 {best['名目資金需求%']:.1f}%｜"
                f"總報酬 {best['總報酬%']:.1f}%｜MDD {best['實現權益最大回撤%']:.1f}%｜淨PF {best['淨PF']:.2f}｜"
                f"承接率 {best['訊號承接率%']:.1f}%｜容量淘汰率 {best['容量淘汰率%']:.1f}%"
            )

        st.markdown('### 🔬 ⑫ 指定容量明細')
        a,b=st.columns(2)
        mp=a.select_slider('最大同時持股',[5,10,15,20,25,30,35,40],value=20,key='v3612_mp')
        pp=b.select_slider('單筆目標資金%',[2.0,2.5,3.0,3.33,4.0,5.0,7.5,10.0],value=5.0,key='v3612_pp')
        eq,lg,stats=_v3613_simulate(trades,capital,mp,pp,fee,tax,slip)

        s1,s2,s3,s4=st.columns(4)
        s1.metric('總報酬%',f"{stats['總報酬%']:.2f}")
        s2.metric('實現權益MDD%',f"{stats['實現權益最大回撤%']:.2f}")
        s3.metric('完成交易',stats['完成交易'])
        s4.metric('淨PF',f"{stats['淨PF']:.2f}")

        q1,q2,q3,q4=st.columns(4)
        q1.metric('實際最高持股',int(stats['實際最高持股']))
        q2.metric('平均持股數',f"{stats['平均持股數']:.2f}")
        q3.metric('訊號承接率%',f"{stats['訊號承接率%']:.1f}")
        q4.metric('容量淘汰率%',f"{stats['容量淘汰率%']:.1f}")
        r1,r2,r3,r4=st.columns(4)
        r1.metric('槽位不足淘汰',int(stats['槽位不足淘汰']))
        r2.metric('資金不足淘汰',int(stats['資金不足淘汰']))
        r3.metric('平均資金使用率%',f"{stats['平均資金使用率%']:.1f}")
        r4.metric('最高資金使用率%',f"{stats['最高資金使用率%']:.1f}")

        if not eq.empty:
            chart=eq.set_index('日期')[['實現權益']]
            st.line_chart(chart,use_container_width=True)
        with st.expander('查看交易明細'):
            st.dataframe(lg,use_container_width=True,hide_index=True)

        st.download_button(
            '⬇️ 下載 V3.6.14 容量情境',
            grid.to_csv(index=False,encoding='utf-8-sig').encode('utf-8-sig'),
            'V3.6.14_capacity_scenarios.csv','text/csv',key='dl_v3612_grid'
        )
        if not lg.empty:
            st.download_button(
                '⬇️ 下載 V3.6.14 交易明細',
                lg.to_csv(index=False,encoding='utf-8-sig').encode('utf-8-sig'),
                'V3.6.14_capacity_trades.csv','text/csv',key='dl_v3612_trades'
            )

        st.warning('V3.6.14 仍使用「實現權益最大回撤」，不是逐日 MTM MDD。本版目標是先找容量甜蜜點；容量鎖定後，下一版再回抓完整日K做逐日Portfolio MDD。')


# ============================================================
# 🧪 V3.6.15 真實逐日 MTM 驗證區
# ============================================================
st.divider()
st.subheader('📈 V3.6.15.1 真實逐日 MTM Portfolio MDD / 資金曲線')
st.caption('V3.6.15.1 修正版｜Gate D 完全不改：90~94＋站上MA200。修正 V3.6.15 容量參數作用域錯誤，並保留真實逐日 MTM 驗證。')
st.info('請先用 V3.6.14 的完整股票池建立 Gate D 事件，再按下方按鈕。第一次需要重新取得 Gate D 股票的歷史日K；之後會利用 Streamlit 快取。')

if pack:
    if st.button('▶ 執行 V3.6.15 真實 MTM 驗證',type='primary',key='run_v3615'):
        with st.spinner('正在重建真實出場交易日與逐日持倉價格…'):
            mtm_trades,mtm_prices,mtm_diag=_v3615_rebuild_trades_and_prices(pack['events'])
            st.session_state['v3615_trades']=mtm_trades
            st.session_state['v3615_prices']=mtm_prices
            st.session_state['v3615_diag']=mtm_diag
    mtm_trades=st.session_state.get('v3615_trades',pd.DataFrame())
    mtm_prices=st.session_state.get('v3615_prices',{})
    mtm_diag=st.session_state.get('v3615_diag',pd.DataFrame())
    if not mtm_trades.empty and mtm_prices:
        st.success(f'真實時間軸重建完成：{len(mtm_trades):,} 筆 Gate D 交易、{len(mtm_prices):,} 檔股票具完整日K。')
        with st.expander('🔎 查看 MTM K線 / 出場日重建診斷'):
            st.dataframe(mtm_diag,use_container_width=True,hide_index=True)
        grid15,packs15=_v3615_candidate_grid(mtm_trades,mtm_prices,capital,fee,tax,slip)
        show_cols=['配置','總報酬%','CAGR%','真實MTM_MDD%','Calmar','淨PF','淨勝率%','完成交易','訊號承接率%','實際最高持股','平均持股數','平均資金使用率%','最高資金使用率%','槽位不足淘汰','資金不足淘汰','報酬/MDD']
        st.markdown('### 🏆 ⑬ 五組候選｜真實 MTM 壓力測試')
        st.dataframe(grid15[[c for c in show_cols if c in grid15.columns]].round(4),use_container_width=True,hide_index=True)
        if not grid15.empty:
            best=grid15.iloc[0]
            st.success(f"目前 MTM 風險效率首選：{best['配置']}｜總報酬 {best['總報酬%']:.1f}%｜CAGR {best['CAGR%']:.1f}%｜真正MDD {best['真實MTM_MDD%']:.1f}%｜Calmar {best['Calmar']:.2f}｜淨PF {best['淨PF']:.2f}")
        st.markdown('### 🔬 ⑭ 指定配置逐日資金曲線')
        c1,c2=st.columns(2)
        mp15=c1.selectbox('MTM最大同時持股',[20,25,30],index=1,key='v3615_mp')
        allowed={20:[5.0],25:[3.0,3.33,4.0],30:[3.33]}
        pp15=c2.selectbox('MTM單筆目標資金%',allowed[mp15],index=0,key='v3615_pp')
        eq15,lg15,st15=_v3615_true_mtm(mtm_trades,mtm_prices,capital,mp15,pp15,fee,tax,slip)
        if st15:
            a,b,c,d=st.columns(4);a.metric('總報酬%',f"{st15['總報酬%']:.2f}");b.metric('CAGR%',f"{st15['CAGR%']:.2f}");c.metric('真正MTM MDD%',f"{st15['真實MTM_MDD%']:.2f}");d.metric('Calmar',f"{st15['Calmar']:.2f}")
            e,f,g,h=st.columns(4);e.metric('淨PF',f"{st15['淨PF']:.2f}");f.metric('完成交易',st15['完成交易']);g.metric('訊號承接率%',f"{st15['訊號承接率%']:.1f}");h.metric('最高資金使用率%',f"{st15['最高資金使用率%']:.1f}")
            st.caption(f"最大回撤區間：高點 {pd.Timestamp(st15['MDD高點日']).strftime('%Y-%m-%d')} → 低點 {pd.Timestamp(st15['MDD低點日']).strftime('%Y-%m-%d')}")
            if not eq15.empty:
                st.line_chart(eq15.set_index('日期')[['MTM權益']],use_container_width=True)
                st.line_chart(eq15.set_index('日期')[['回撤%']],use_container_width=True)
            with st.expander('查看 V3.6.15 真實成交明細'):
                st.dataframe(lg15,use_container_width=True,hide_index=True)
            st.download_button('⬇️ 下載 V3.6.15 MTM 資金曲線',eq15.to_csv(index=False,encoding='utf-8-sig').encode('utf-8-sig'),'V3.6.15_MTM_equity.csv','text/csv',key='dl_v3615_eq')
            st.download_button('⬇️ 下載 V3.6.15 MTM 交易明細',lg15.to_csv(index=False,encoding='utf-8-sig').encode('utf-8-sig'),'V3.6.15_MTM_trades.csv','text/csv',key='dl_v3615_trades')
        st.warning('判讀原則：V3.6.15 的「真正MTM MDD」才是主要風險指標；V3.6.14 的實現權益MDD保留作容量初篩，但不再作最終風險判定。')
    else:
        st.info('尚未建立 V3.6.15 MTM 資料。請按「▶ 執行 V3.6.15 真實 MTM 驗證」。')
else:
    st.info('請先建立 Gate D 鎖定事件資料，再執行 V3.6.15。')


# ============================================================
# 🔒 V3.6.16 Gate D 資金配置鎖定 / MTM 穩健度壓力測試
# 依 V3.6.15 實測：25檔×3.33% 在 Calmar、MDD、PF、資金可行性之間最平衡。
# 本版不再搜尋容量；固定 Gate D = 90~94 + 站上MA200，配置 = 25檔×3.33%，
# 專門驗證年度、月度、回撤持續時間與壓力成本敏感度。
# ============================================================

def _v3616_period_returns(eq, freq='YE'):
    if eq is None or eq.empty:return pd.DataFrame()
    z=eq[['日期','MTM權益']].copy().sort_values('日期')
    z['日期']=pd.to_datetime(z['日期']); z=z.set_index('日期')
    try:
        last=z['MTM權益'].resample(freq).last(); first=z['MTM權益'].resample(freq).first()
    except Exception:
        alias='Y' if freq=='YE' else freq
        last=z['MTM權益'].resample(alias).last(); first=z['MTM權益'].resample(alias).first()
    r=(last/first-1)*100
    out=pd.DataFrame({'期間':r.index,'報酬%':r.values}).dropna()
    return out

def _v3616_drawdown_episodes(eq):
    if eq is None or eq.empty:return pd.DataFrame(),{}
    z=eq[['日期','MTM權益']].copy().sort_values('日期').reset_index(drop=True)
    z['高水位']=z['MTM權益'].cummax();z['回撤%']=(z['MTM權益']/z['高水位']-1)*100
    episodes=[]; in_dd=False; start_i=None
    for i,row in z.iterrows():
        dd=float(row['回撤%'])
        if dd< -1e-10 and not in_dd:
            in_dd=True;start_i=max(i-1,0)
        if in_dd and (dd>=-1e-10 or i==len(z)-1):
            end_i=i; seg=z.iloc[start_i:end_i+1]
            trough_idx=seg['回撤%'].idxmin()
            recovered=bool(dd>=-1e-10)
            episodes.append({'高點日':z.loc[start_i,'日期'],'谷底日':z.loc[trough_idx,'日期'],
                             '恢復日':z.loc[end_i,'日期'] if recovered else pd.NaT,
                             '最大回撤%':float(z.loc[trough_idx,'回撤%']),
                             '回撤交易日':int(end_i-start_i),'已恢復':recovered})
            in_dd=False
    d=pd.DataFrame(episodes)
    stats={}
    if not d.empty:
        worst=d.loc[d['最大回撤%'].idxmin()]
        stats={'最深回撤%':float(worst['最大回撤%']),'最深回撤高點日':worst['高點日'],
               '最深回撤谷底日':worst['谷底日'],'最深回撤恢復日':worst['恢復日'],
               '最長回撤交易日':int(d['回撤交易日'].max()),'回撤事件數':len(d),
               '未恢復回撤數':int((~d['已恢復']).sum())}
    return d,stats

def _v3616_yearly(eq,logs):
    if eq is None or eq.empty:return pd.DataFrame()
    z=eq[['日期','MTM權益']].copy();z['日期']=pd.to_datetime(z['日期']);z['年度']=z['日期'].dt.year
    rows=[]
    for y,g in z.groupby('年度'):
        g=g.sort_values('日期');start=float(g.iloc[0]['MTM權益']);end=float(g.iloc[-1]['MTM權益'])
        peak=g['MTM權益'].cummax();mdd=float((g['MTM權益']/peak-1).min()*100)
        lg=logs.copy() if logs is not None else pd.DataFrame()
        if not lg.empty:
            lg['出場日']=pd.to_datetime(lg['出場日']);ly=lg[lg['出場日'].dt.year==y]
            gp=ly.loc[ly['淨損益']>0,'淨損益'].sum();gl=-ly.loc[ly['淨損益']<0,'淨損益'].sum()
            pf=gp/gl if gl>0 else (np.inf if gp>0 else 0);wr=(ly['淨損益']>0).mean()*100 if len(ly) else np.nan
        else: ly=pd.DataFrame();pf=np.nan;wr=np.nan
        rows.append({'年度':int(y),'年度報酬%':(end/start-1)*100 if start else np.nan,'年度MDD%':mdd,
                     '完成交易':len(ly),'淨勝率%':wr,'淨PF':pf,'年末權益':end})
    return pd.DataFrame(rows)

def _v3616_cost_stress(trades,prices,capital,base_fee,base_tax,base_slip):
    cases=[('基準成本',base_fee,base_tax,base_slip),('滑價0.20%',base_fee,base_tax,0.20),
           ('滑價0.30%',base_fee,base_tax,0.30),('手續費+滑價壓力',max(base_fee,0.20),base_tax,0.30)]
    rows=[]
    for name,fee16,tax16,slip16 in cases:
        eq,lg,stt=_v3615_true_mtm(trades,prices,capital,25,3.33,fee16,tax16,slip16)
        if stt:
            rows.append({'情境':name,'手續費%':fee16,'交易稅%':tax16,'滑價%':slip16,
                         '總報酬%':stt['總報酬%'],'CAGR%':stt['CAGR%'],'MTM_MDD%':stt['真實MTM_MDD%'],
                         'Calmar':stt['Calmar'],'淨PF':stt['淨PF'],'完成交易':stt['完成交易']})
    return pd.DataFrame(rows)

st.divider()
st.subheader('🔒 V3.6.16 Gate D 資金配置鎖定 / MTM 穩健度壓力測試')
st.caption('依 V3.6.15 結果鎖定第一版資金模型：Gate D＝90~94＋站上MA200；最大同時持股25檔；單筆目標資金3.33%。本區不再最佳化參數，只驗證穩健度。')

mtm_trades16=st.session_state.get('v3615_trades',pd.DataFrame())
mtm_prices16=st.session_state.get('v3615_prices',{})
if not mtm_trades16.empty and mtm_prices16:
    eq16,lg16,st16=_v3615_true_mtm(mtm_trades16,mtm_prices16,capital,25,3.33,fee,tax,slip)
    if st16:
        st.success(f"🔒 暫定鎖定：25檔 × 3.33%｜總報酬 {st16['總報酬%']:.1f}%｜CAGR {st16['CAGR%']:.1f}%｜MTM MDD {st16['真實MTM_MDD%']:.1f}%｜Calmar {st16['Calmar']:.2f}｜淨PF {st16['淨PF']:.2f}")
        a,b,c,d=st.columns(4)
        a.metric('鎖定最大持股','25檔');b.metric('鎖定單筆資金','3.33%');c.metric('名目滿倉','83.25%');d.metric('現金緩衝','16.75%')

        st.markdown('### 📅 ⑮ 年度 MTM 穩定度')
        yr16=_v3616_yearly(eq16,lg16);st.dataframe(yr16.round(4),use_container_width=True,hide_index=True)

        st.markdown('### 🩸 ⑯ 回撤深度 / 恢復時間')
        dd16,dds16=_v3616_drawdown_episodes(eq16)
        if dds16:
            c1,c2,c3,c4=st.columns(4)
            c1.metric('最深MDD%',f"{dds16['最深回撤%']:.2f}");c2.metric('最長回撤交易日',dds16['最長回撤交易日'])
            c3.metric('回撤事件數',dds16['回撤事件數']);c4.metric('未恢復回撤',dds16['未恢復回撤數'])
            st.caption(f"最深回撤：{pd.Timestamp(dds16['最深回撤高點日']).strftime('%Y-%m-%d')} → {pd.Timestamp(dds16['最深回撤谷底日']).strftime('%Y-%m-%d')}" + (f" → 恢復 {pd.Timestamp(dds16['最深回撤恢復日']).strftime('%Y-%m-%d')}" if pd.notna(dds16['最深回撤恢復日']) else '｜尚未恢復'))
        with st.expander('查看所有回撤事件'):
            st.dataframe(dd16.round(4),use_container_width=True,hide_index=True)

        st.markdown('### 🗓️ ⑰ 月報酬分布')
        mon16=_v3616_period_returns(eq16,'ME')
        if not mon16.empty:
            mon16['月份']=pd.to_datetime(mon16['期間']).dt.strftime('%Y-%m')
            neg=int((mon16['報酬%']<0).sum());pos=int((mon16['報酬%']>0).sum())
            q1,q2,q3,q4=st.columns(4)
            q1.metric('正報酬月',pos);q2.metric('負報酬月',neg);q3.metric('最佳月%',f"{mon16['報酬%'].max():.2f}");q4.metric('最差月%',f"{mon16['報酬%'].min():.2f}")
            st.dataframe(mon16[['月份','報酬%']].round(4),use_container_width=True,hide_index=True)

        st.markdown('### 🧯 ⑱ 交易成本 / 滑價壓力測試')
        cost16=_v3616_cost_stress(mtm_trades16,mtm_prices16,capital,fee,tax,slip)
        st.dataframe(cost16.round(4),use_container_width=True,hide_index=True)

        # 自動判定：不是重新最佳化，而是判斷是否可進實盤模擬層
        annual_ok=(not yr16.empty and (yr16['年度報酬%']>0).mean()>=2/3)
        pf_ok=float(st16['淨PF'])>=1.5
        mdd_ok=float(st16['真實MTM_MDD%'])>=-25
        calmar_ok=float(st16['Calmar'])>=1.5
        stress_ok=(not cost16.empty and float(cost16.iloc[-1]['淨PF'])>=1.3)
        st.markdown('### 🧾 ⑲ V3.6.16 自動判定')
        checks=pd.DataFrame([
            {'驗證':'至少2/3年度正報酬','結果':f"{(yr16['年度報酬%']>0).mean()*100:.1f}%" if not yr16.empty else 'N/A','通過':annual_ok},
            {'驗證':'淨PF ≥ 1.50','結果':f"{st16['淨PF']:.2f}",'通過':pf_ok},
            {'驗證':'真正MTM MDD ≥ -25%','結果':f"{st16['真實MTM_MDD%']:.2f}%",'通過':mdd_ok},
            {'驗證':'Calmar ≥ 1.50','結果':f"{st16['Calmar']:.2f}",'通過':calmar_ok},
            {'驗證':'高成本壓力淨PF ≥ 1.30','結果':f"{cost16.iloc[-1]['淨PF']:.2f}" if not cost16.empty else 'N/A','通過':stress_ok},
        ])
        st.dataframe(checks,use_container_width=True,hide_index=True)
        if bool(checks['通過'].all()):
            st.success('🟢 V3.6.16 通過：25檔×3.33% 可升級為 Gate D 第一版正式資金配置，下一階段應做訊號排序 / 槽位競爭與實盤執行規則。')
        else:
            st.warning('🟡 V3.6.16 尚有壓力條件未通過：先看未通過項目，不自動改 Gate D。')

        st.download_button('⬇️ 下載 V3.6.16 年度穩定度',yr16.to_csv(index=False,encoding='utf-8-sig').encode('utf-8-sig'),'V3.6.16_yearly.csv','text/csv',key='dl_v3616_year')
        st.download_button('⬇️ 下載 V3.6.16 回撤事件',dd16.to_csv(index=False,encoding='utf-8-sig').encode('utf-8-sig'),'V3.6.16_drawdowns.csv','text/csv',key='dl_v3616_dd')
else:
    st.info('請先在上方執行 V3.6.15 真實 MTM 驗證；完成後 V3.6.16 會直接沿用同一批完整日K，不需要再下載一次。')

# ============================================================
# 🏁 V3.6.17 Gate D 訊號排序 / 25槽位競爭驗證
# Gate D 與 25檔×3.33% 完全鎖定；只比較「同日訊號超過可用槽位時」的排序規則。
# 不新增篩選門檻，避免重新資料探勘。
# ============================================================

def _v3617_ranked_trades(trades, mode='分數→成交額→近MA200'):
    if trades is None or trades.empty:
        return pd.DataFrame()
    z=trades.copy()
    z['技術分數']=pd.to_numeric(z.get('技術分數'),errors='coerce')
    z['_liq']=pd.to_numeric(z.get('估算成交額',np.nan),errors='coerce').fillna(0)
    z['_vr']=pd.to_numeric(z.get('量比',np.nan),errors='coerce').fillna(0)
    px=pd.to_numeric(z.get('進場價',np.nan),errors='coerce')
    ma=pd.to_numeric(z.get('MA200',np.nan),errors='coerce')
    z['_dist']=((px/ma)-1).abs().replace([np.inf,-np.inf],np.nan).fillna(999)
    if mode=='分數→成交額→近MA200':
        cols=['進場日','技術分數','_liq','_dist']; asc=[True,False,False,True]
    elif mode=='分數→近MA200→成交額':
        cols=['進場日','技術分數','_dist','_liq']; asc=[True,False,True,False]
    elif mode=='分數→量比→成交額':
        cols=['進場日','技術分數','_vr','_liq']; asc=[True,False,False,False]
    elif mode=='成交額→分數→近MA200':
        cols=['進場日','_liq','技術分數','_dist']; asc=[True,False,False,True]
    else: # 純分數：股票代號只作 deterministic tie-break，不代表投資偏好
        z['_sym']=z['股票'].astype(str)
        cols=['進場日','技術分數','_sym']; asc=[True,False,True]
    return z.sort_values(cols,ascending=asc).reset_index(drop=True)


def _v3617_mtm(trades,price_map,initial_capital,rank_mode,fee_pct,tax_pct,slippage_pct):
    ranked=_v3617_ranked_trades(trades,rank_mode)
    # _v3615_true_mtm 會再依「進場日、技術分數」排序；為保留同分排序，
    # 加入極小且不改 Gate 區間的排序尾碼，只影響同分訊號的先後，不影響交易條件。
    if ranked.empty:return pd.DataFrame(),pd.DataFrame(),{}
    ranked=ranked.copy()
    ranked['_day_order']=ranked.groupby('進場日').cumcount()
    base_score=pd.to_numeric(ranked['技術分數'],errors='coerce').fillna(0)
    ranked['技術分數原值']=base_score
    ranked['技術分數']=base_score-ranked['_day_order']*1e-7
    eq,lg,stt=_v3615_true_mtm(ranked,price_map,initial_capital,25,3.33,fee_pct,tax_pct,slippage_pct)
    if stt: stt['排序規則']=rank_mode
    return eq,lg,stt


def _v3617_compare(trades,prices,capital,fee,tax,slip):
    modes=['純分數','分數→成交額→近MA200','分數→近MA200→成交額','分數→量比→成交額','成交額→分數→近MA200']
    rows=[]; packs={}
    for mode in modes:
        eq,lg,stt=_v3617_mtm(trades,prices,capital,mode,fee,tax,slip)
        if not stt:continue
        row={'排序規則':mode,'總報酬%':stt['總報酬%'],'CAGR%':stt['CAGR%'],'MTM_MDD%':stt['真實MTM_MDD%'],
             'Calmar':stt['Calmar'],'淨PF':stt['淨PF'],'淨勝率%':stt['淨勝率%'],'完成交易':stt['完成交易'],
             '訊號承接率%':stt['訊號承接率%'],'槽位不足淘汰':stt['槽位不足淘汰'],'資金不足淘汰':stt['資金不足淘汰']}
        rows.append(row);packs[mode]=(eq,lg,stt)
    return pd.DataFrame(rows),packs


def _v3617_year_compare(packs):
    rows=[]
    for mode,(eq,lg,stt) in packs.items():
        y=_v3616_yearly(eq,lg)
        if y.empty:continue
        for _,r in y.iterrows():
            rows.append({'排序規則':mode,'年度':int(r['年度']),'年度報酬%':r['年度報酬%'],'年度MDD%':r['年度MDD%'],
                         '淨PF':r['淨PF'],'淨勝率%':r['淨勝率%'],'完成交易':r['完成交易']})
    return pd.DataFrame(rows)

st.divider()
st.subheader('🏁 V3.6.17 訊號排序 / 25槽位競爭驗證')
st.caption('V3.6.16 五項全數通過後，Gate D＝90~94＋站上MA200、25檔×3.33% 正式鎖定。本版不改進場條件，只回答：同一天候選太多時，25個槽位應優先給誰。')

tr17=st.session_state.get('v3615_trades',pd.DataFrame())
px17=st.session_state.get('v3615_prices',{})
if not tr17.empty and px17:
    cmp17,packs17=_v3617_compare(tr17,px17,capital,fee,tax,slip)
    if not cmp17.empty:
        # 以純分數為中性基準，所有改善均顯示而不偷換基準
        b=cmp17[cmp17['排序規則']=='純分數'].iloc[0]
        cmp17['CAGR改善ppt']=cmp17['CAGR%']-float(b['CAGR%'])
        cmp17['MDD改善ppt']=cmp17['MTM_MDD%']-float(b['MTM_MDD%'])
        cmp17['PF改善']=cmp17['淨PF']-float(b['淨PF'])
        cmp17['Calmar改善']=cmp17['Calmar']-float(b['Calmar'])
        st.markdown('### 🏆 ⑳ 五種槽位排序規則 PK')
        st.dataframe(cmp17.round(4),use_container_width=True,hide_index=True)

        yr17=_v3617_year_compare(packs17)
        st.markdown('### 📅 ㉑ 排序規則年度穩定度')
        st.dataframe(yr17.round(4),use_container_width=True,hide_index=True)

        # 穩健排名：不只看總報酬；要求至少2/3年度報酬為正，再看 Calmar/PF/CAGR。
        rank=[]
        for _,r in cmp17.iterrows():
            mode=r['排序規則']; yy=yr17[yr17['排序規則']==mode]
            pos_ratio=float((yy['年度報酬%']>0).mean()) if len(yy) else 0
            rank.append({'排序規則':mode,'正報酬年度比例%':pos_ratio*100,'Calmar':r['Calmar'],'淨PF':r['淨PF'],
                         'CAGR%':r['CAGR%'],'MTM_MDD%':r['MTM_MDD%'],'完成交易':r['完成交易'],
                         '年度穩定通過':pos_ratio>=2/3})
        rank=pd.DataFrame(rank).sort_values(['年度穩定通過','Calmar','淨PF','CAGR%'],ascending=[False,False,False,False]).reset_index(drop=True)
        st.markdown('### 🧠 ㉒ V3.6.17 穩健排序')
        st.dataframe(rank.round(4),use_container_width=True,hide_index=True)
        winner=rank.iloc[0]
        wmode=winner['排序規則']; wr=cmp17[cmp17['排序規則']==wmode].iloc[0]

        # 正式採用門檻：相對純分數不能以更深超過2ppt的MDD換取報酬，且PF不得惡化。
        robust=bool(winner['年度穩定通過'])
        pf_nonworse=float(wr['淨PF'])>=float(b['淨PF'])-0.03
        mdd_nonworse=float(wr['MTM_MDD%'])>=float(b['MTM_MDD%'])-2.0
        calmar_nonworse=float(wr['Calmar'])>=float(b['Calmar'])
        checks17=pd.DataFrame([
            {'驗證':'至少2/3年度正報酬','結果':f"{winner['正報酬年度比例%']:.1f}%",'通過':robust},
            {'驗證':'淨PF不明顯劣於純分數','結果':f"{wr['淨PF']:.2f} vs {b['淨PF']:.2f}",'通過':pf_nonworse},
            {'驗證':'MDD不得比純分數惡化超過2ppt','結果':f"{wr['MTM_MDD%']:.2f}% vs {b['MTM_MDD%']:.2f}%",'通過':mdd_nonworse},
            {'驗證':'Calmar不低於純分數','結果':f"{wr['Calmar']:.2f} vs {b['Calmar']:.2f}",'通過':calmar_nonworse},
        ])
        st.markdown('### 🧾 ㉓ V3.6.17 自動判定')
        st.dataframe(checks17,use_container_width=True,hide_index=True)
        if bool(checks17['通過'].all()):
            st.success(f"🟢 排序規則可鎖定：{wmode}。下一版直接進入『訊號排隊 / 槽位釋放 / 實盤執行狀態機』。")
        else:
            st.warning('🟡 排序規則沒有形成足夠穩健優勢；正式執行先保留「純分數」中性排序，避免為了歷史報酬過度最佳化。下一版仍可進實盤狀態機。')

        st.download_button('⬇️ 下載 V3.6.17 排序PK',cmp17.to_csv(index=False,encoding='utf-8-sig').encode('utf-8-sig'),'V3.6.17_ranking_PK.csv','text/csv',key='dl_v3617_pk')
        st.download_button('⬇️ 下載 V3.6.17 年度穩定度',yr17.to_csv(index=False,encoding='utf-8-sig').encode('utf-8-sig'),'V3.6.17_ranking_yearly.csv','text/csv',key='dl_v3617_year')
else:
    st.info('請先完成上方 V3.6.15 真實 MTM 資料重建；V3.6.17 會直接沿用同一批 Gate D 交易與日K。')


# ============================================================
# 🚦 V3.6.18 訊號排隊 / 槽位釋放 / 實盤執行狀態機
# 已鎖定：
#   Gate D = 90~94 + 站上MA200
#   最大持股 = 25
#   單筆資金 = 3.33%
#   排序 = 分數→成交額→近MA200
#
# 本版只測「當日沒槽位的訊號，要不要短暫排隊等待槽位釋放」。
# 不改 Gate、不改分數、不改資金比例。
# ============================================================

V3618_LOCKED_RANK = '分數→成交額→近MA200'
V3618_MAX_POS = 25
V3618_POS_PCT = 3.33

def _v3618_rebuild_from_date(row, entry_date, price_map):
    """把原始 Gate D 訊號延後到指定交易日進場，重新依鎖定的40日/硬停損12%規則建立交易。
    只用當時之後的價格路徑，不偷用原本出場價。
    """
    sym = str(row['股票']).zfill(4)
    d = price_map.get(sym)
    if d is None or d.empty:
        return None
    dt = pd.Timestamp(entry_date).normalize()
    idx_norm = pd.DatetimeIndex(d.index).normalize()
    loc = np.where(idx_norm == dt)[0]
    if len(loc) == 0:
        return None
    i = int(loc[-1])
    tr = _v368_locked_baseline_trade(d, i)
    if not tr:
        return None
    rr = row.to_dict()
    rr['原始訊號日'] = pd.Timestamp(row['進場日']).normalize()
    rr['進場日'] = pd.Timestamp(tr['進場日']).normalize()
    rr['資金出場日'] = pd.Timestamp(tr['出場日']).normalize()
    rr['持有交易日'] = int(tr.get('持有天數', 40))
    rr['重建進場價'] = float(tr['進場價'])
    rr['重建出場價'] = float(tr['出場價'])
    rr['重建毛報酬%'] = float(tr['報酬%'])
    rr['重建出場原因'] = str(tr.get('出場原因', ''))
    return rr

def _v3618_trade_dates(price_map, trades):
    if trades is None or trades.empty:
        return []
    start = pd.Timestamp(trades['進場日'].min()).normalize()
    # 留足排隊與40日出場所需日期
    all_dates = set()
    for sym in trades['股票'].astype(str).str.zfill(4).unique():
        d = price_map.get(sym)
        if d is None or d.empty:
            continue
        for v in d.index:
            dt = pd.Timestamp(v).normalize()
            if dt >= start:
                all_dates.add(dt)
    return sorted(all_dates)

def _v3618_queue_mtm(trades, price_map, initial_capital, queue_days=0,
                     fee_pct=0.1425, tax_pct=0.30, slippage_pct=0.10):
    """事件驅動 MTM 狀態機。
    狀態：NEW_SIGNAL -> QUEUED -> FILLED / EXPIRED / DUPLICATE
    queue_days=0 等同「當日沒槽位直接放棄」。
    排隊天數以市場交易日計算；每天先出場釋放槽位，再處理舊排隊，再處理新訊號。
    """
    if trades is None or trades.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), {}

    ranked = _v3617_ranked_trades(trades, V3618_LOCKED_RANK).copy()
    ranked['進場日'] = pd.to_datetime(ranked['進場日']).dt.normalize()
    dates = _v3618_trade_dates(price_map, ranked)
    if not dates:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), {}

    date_pos = {d:i for i,d in enumerate(dates)}
    buy_fee = fee_pct/100
    sell_fee = (fee_pct+tax_pct)/100
    slip = slippage_pct/100

    cash = float(initial_capital)
    open_pos = []
    pending = []
    logs = []
    audit = []
    curve = []

    signal_count = accepted = rejected_cash = rejected_same = 0
    queued_count = expired_count = delayed_fills = 0
    slot_block_events = 0
    wait_days = []

    def mtm_equity(dt):
        mv = 0.0
        for p in open_pos:
            px = _v3615_close_on(price_map, p['股票'], dt, p['last_px'])
            if np.isfinite(px):
                p['last_px'] = px
            mv += p['shares'] * p['last_px']
        return cash + mv, mv

    def held_symbols():
        return {p['股票'] for p in open_pos}

    def pending_symbols():
        return {str(p['row']['股票']).zfill(4) for p in pending}

    def try_fill(row, dt, original_signal_date, waited):
        nonlocal cash, accepted, rejected_cash, rejected_same, delayed_fills
        sym = str(row['股票']).zfill(4)
        if sym in held_symbols():
            rejected_same += 1
            audit.append({'日期':dt,'股票':sym,'狀態':'DUPLICATE_HELD',
                          '原始訊號日':original_signal_date,'等待交易日':waited})
            return 'duplicate'

        rebuilt = _v3618_rebuild_from_date(row, dt, price_map)
        if rebuilt is None:
            audit.append({'日期':dt,'股票':sym,'狀態':'NO_PRICE',
                          '原始訊號日':original_signal_date,'等待交易日':waited})
            return 'no_price'

        eq_now, _ = mtm_equity(dt)
        target = max(0.0, eq_now * V3618_POS_PCT / 100)
        entry_raw = float(rebuilt['重建進場價'])
        entry_exec = entry_raw * (1 + slip)
        per_share = entry_exec * (1 + buy_fee)
        alloc = min(target, cash)
        if alloc <= 0 or (target > 0 and alloc < target * .20):
            rejected_cash += 1
            audit.append({'日期':dt,'股票':sym,'狀態':'NO_CASH',
                          '原始訊號日':original_signal_date,'等待交易日':waited})
            return 'cash'

        shares = alloc / per_share
        actual_cost = shares * per_share
        cash -= actual_cost
        open_pos.append({
            '股票':sym,'名稱':rebuilt.get('名稱',''),
            'entry_date':pd.Timestamp(rebuilt['進場日']).normalize(),
            'exit_date':pd.Timestamp(rebuilt['資金出場日']).normalize(),
            'score':float(pd.to_numeric(row.get('技術分數原值', row.get('技術分數',0)), errors='coerce')),
            'shares':shares,'entry_raw':entry_raw,'entry_exec':entry_exec,
            'exit_price':float(rebuilt['重建出場價']),
            'reason':str(rebuilt['重建出場原因']),
            'hold':int(rebuilt['持有交易日']),'last_px':entry_raw,
            'original_signal_date':pd.Timestamp(original_signal_date).normalize(),
            'waited':int(waited)
        })
        accepted += 1
        if waited > 0:
            delayed_fills += 1
            wait_days.append(waited)
        audit.append({'日期':dt,'股票':sym,'狀態':'FILLED_DELAYED' if waited>0 else 'FILLED_SAME_DAY',
                      '原始訊號日':original_signal_date,'等待交易日':waited})
        return 'filled'

    for dt in dates:
        # 1) EXIT：先出場，當天立即釋放槽位
        closing = [p for p in open_pos if p['exit_date'] <= dt]
        for p in closing:
            exit_raw = float(p['exit_price'])
            exit_exec = exit_raw * (1-slip)
            proceeds = p['shares'] * exit_exec * (1-sell_fee)
            cash += proceeds
            cost = p['shares'] * p['entry_exec'] * (1+buy_fee)
            pnl = proceeds - cost
            logs.append({
                '股票':p['股票'],'名稱':p['名稱'],
                '原始訊號日':p['original_signal_date'],
                '進場日':p['entry_date'],'出場日':p['exit_date'],
                '等待交易日':p['waited'],'技術分數':p['score'],
                '投入資金':cost,'進場價':p['entry_raw'],'出場價':exit_raw,
                '出場原因':p['reason'],'淨損益':pnl,
                '淨報酬%':pnl/cost*100 if cost else 0,
                '持有交易日':p['hold']
            })
            audit.append({'日期':dt,'股票':p['股票'],'狀態':'EXIT',
                          '原始訊號日':p['original_signal_date'],'等待交易日':p['waited']})
            open_pos.remove(p)

        # 2) QUEUE：先處理昨天以前排隊的訊號
        still_pending = []
        if pending:
            pending = sorted(
                pending,
                key=lambda p: (
                    date_pos.get(p['signal_date'], 10**9),
                    -float(pd.to_numeric(p['row'].get('技術分數',0),errors='coerce') or 0),
                    -float(pd.to_numeric(p['row'].get('_liq',0),errors='coerce') or 0),
                    float(pd.to_numeric(p['row'].get('_dist',999),errors='coerce') or 999),
                )
            )
        for p in pending:
            waited = date_pos.get(dt,0) - date_pos.get(p['signal_date'],0)
            sym = str(p['row']['股票']).zfill(4)
            if waited > queue_days:
                expired_count += 1
                audit.append({'日期':dt,'股票':sym,'狀態':'EXPIRED',
                              '原始訊號日':p['signal_date'],'等待交易日':waited})
                continue
            if sym in held_symbols():
                rejected_same += 1
                audit.append({'日期':dt,'股票':sym,'狀態':'DUPLICATE_HELD',
                              '原始訊號日':p['signal_date'],'等待交易日':waited})
                continue
            if len(open_pos) >= V3618_MAX_POS:
                still_pending.append(p)
                continue
            result = try_fill(p['row'], dt, p['signal_date'], waited)
            if result in ('cash','no_price'):
                still_pending.append(p)
        pending = still_pending

        # 3) NEW SIGNAL：處理今日新訊號
        todays = ranked[ranked['進場日'] == dt]
        for _, r in todays.iterrows():
            signal_count += 1
            sym = str(r['股票']).zfill(4)
            if sym in held_symbols() or sym in pending_symbols():
                rejected_same += 1
                audit.append({'日期':dt,'股票':sym,'狀態':'DUPLICATE_SIGNAL',
                              '原始訊號日':dt,'等待交易日':0})
                continue
            if len(open_pos) < V3618_MAX_POS:
                try_fill(r, dt, dt, 0)
            else:
                slot_block_events += 1
                if queue_days > 0:
                    pending.append({'row':r.copy(),'signal_date':dt})
                    queued_count += 1
                    audit.append({'日期':dt,'股票':sym,'狀態':'QUEUED',
                                  '原始訊號日':dt,'等待交易日':0})
                else:
                    expired_count += 1
                    audit.append({'日期':dt,'股票':sym,'狀態':'REJECTED_SLOT',
                                  '原始訊號日':dt,'等待交易日':0})

        eq, mv = mtm_equity(dt)
        curve.append({
            '日期':dt,'MTM權益':eq,'現金':cash,'持倉市值':mv,'持倉數':len(open_pos),
            '排隊數':len(pending),
            '資金使用率%':mv/eq*100 if eq>0 else np.nan
        })

    # 資料結尾仍在排隊者視為未成交到期
    for p in pending:
        expired_count += 1
        sym = str(p['row']['股票']).zfill(4)
        audit.append({'日期':dates[-1],'股票':sym,'狀態':'END_OF_DATA',
                      '原始訊號日':p['signal_date'],
                      '等待交易日':date_pos[dates[-1]]-date_pos.get(p['signal_date'],0)})

    eq = pd.DataFrame(curve).sort_values('日期').drop_duplicates('日期',keep='last')
    lg = pd.DataFrame(logs)
    au = pd.DataFrame(audit)
    if eq.empty:
        return eq, lg, au, {}

    eq['權益高點'] = eq['MTM權益'].cummax()
    eq['回撤%'] = (eq['MTM權益']/eq['權益高點']-1)*100
    mdd = float(eq['回撤%'].min())
    final = float(eq.iloc[-1]['MTM權益'])
    total_ret = (final/initial_capital-1)*100
    years = max((eq.iloc[-1]['日期']-eq.iloc[0]['日期']).days/365.25,1/365.25)
    cagr = ((final/initial_capital)**(1/years)-1)*100 if final>0 else -100.0
    calmar = cagr/abs(mdd) if mdd<0 else np.nan
    wins = (lg['淨損益']>0).mean()*100 if len(lg) else np.nan
    gp = lg.loc[lg['淨損益']>0,'淨損益'].sum() if len(lg) else 0
    gl = -lg.loc[lg['淨損益']<0,'淨損益'].sum() if len(lg) else 0
    pf = gp/gl if gl>0 else (np.inf if gp>0 else 0)

    stats = {
        '排隊有效交易日':int(queue_days),
        '總報酬%':total_ret,'CAGR%':cagr,'真實MTM_MDD%':mdd,'Calmar':calmar,
        '淨PF':pf,'淨勝率%':wins,'完成交易':len(lg),
        '總進場訊號':signal_count,'接受訊號':accepted,
        '訊號承接率%':accepted/signal_count*100 if signal_count else 0,
        '曾進排隊':queued_count,'延後成交':delayed_fills,'到期/槽位淘汰':expired_count,
        '槽位壓力事件':slot_block_events,'資金不足':rejected_cash,'同股重複':rejected_same,
        '平均等待交易日':float(np.mean(wait_days)) if wait_days else 0.0,
        '最高排隊數':int(eq['排隊數'].max()) if '排隊數' in eq else 0,
        '實際最高持股':int(eq['持倉數'].max()),
        '平均持股數':float(eq['持倉數'].mean()),
        '平均資金使用率%':float(eq['資金使用率%'].mean()),
        '最高資金使用率%':float(eq['資金使用率%'].max())
    }
    return eq, lg, au, stats

def _v3618_yearly(eq, lg):
    if eq is None or eq.empty:
        return pd.DataFrame()
    rows=[]
    z=eq.copy()
    z['年度']=pd.to_datetime(z['日期']).dt.year
    t=lg.copy() if lg is not None else pd.DataFrame()
    if not t.empty:
        t['年度']=pd.to_datetime(t['出場日']).dt.year
    for y,g in z.groupby('年度'):
        g=g.sort_values('日期')
        first=float(g.iloc[0]['MTM權益']); last=float(g.iloc[-1]['MTM權益'])
        ret=(last/first-1)*100 if first else np.nan
        peak=g['MTM權益'].cummax()
        mdd=float(((g['MTM權益']/peak)-1).min()*100)
        gy=t[t['年度']==y] if not t.empty else pd.DataFrame()
        gp=gy.loc[gy['淨損益']>0,'淨損益'].sum() if not gy.empty else 0
        gl=-gy.loc[gy['淨損益']<0,'淨損益'].sum() if not gy.empty else 0
        pf=gp/gl if gl>0 else (np.inf if gp>0 else 0)
        rows.append({'年度':int(y),'年度報酬%':ret,'年度MDD%':mdd,'淨PF':pf,
                     '淨勝率%':(gy['淨損益']>0).mean()*100 if not gy.empty else np.nan,
                     '完成交易':len(gy)})
    return pd.DataFrame(rows)

st.divider()
st.subheader('🚦 V3.6.18 訊號排隊 / 槽位釋放 / 實盤執行狀態機')
st.caption('V3.6.17 已鎖定「分數→成交額→近MA200」。本版不再改選股，只測：25槽位滿載時，被擋下的 Gate D 訊號是否值得排隊 1～3 個交易日。延後成交會用延後當天收盤重新建立40日/硬停損12%交易，不沿用原訊號日的進出場價。')

tr18 = st.session_state.get('v3615_trades', pd.DataFrame())
px18 = st.session_state.get('v3615_prices', {})

if not tr18.empty and px18:
    packs18={}
    rows18=[]
    for qd in [0,1,2,3]:
        eq18,lg18,au18,st18=_v3618_queue_mtm(tr18,px18,capital,qd,fee,tax,slip)
        if st18:
            rows18.append(st18)
            packs18[qd]=(eq18,lg18,au18,st18)
    cmp18=pd.DataFrame(rows18)

    if not cmp18.empty:
        b18=cmp18[cmp18['排隊有效交易日']==0].iloc[0]
        cmp18['CAGR改善ppt']=cmp18['CAGR%']-float(b18['CAGR%'])
        cmp18['MDD改善ppt']=cmp18['真實MTM_MDD%']-float(b18['真實MTM_MDD%'])
        cmp18['PF改善']=cmp18['淨PF']-float(b18['淨PF'])
        cmp18['Calmar改善']=cmp18['Calmar']-float(b18['Calmar'])

        st.markdown('### 🏆 ㉔ 排隊有效期 PK｜0 / 1 / 2 / 3 交易日')
        st.dataframe(cmp18.round(4),use_container_width=True,hide_index=True)

        yr_rows=[]
        for qd,(eq18,lg18,au18,st18) in packs18.items():
            yy=_v3618_yearly(eq18,lg18)
            for _,r in yy.iterrows():
                rr=r.to_dict();rr['排隊有效交易日']=qd;yr_rows.append(rr)
        yr18=pd.DataFrame(yr_rows)
        st.markdown('### 📅 ㉕ 排隊規則年度穩定度')
        st.dataframe(yr18.round(4),use_container_width=True,hide_index=True)

        # 排隊事件診斷
        diag=[]
        for qd,(eq18,lg18,au18,st18) in packs18.items():
            diag.append({
                '排隊有效交易日':qd,
                '總訊號':st18['總進場訊號'],'接受訊號':st18['接受訊號'],
                '承接率%':st18['訊號承接率%'],'曾進排隊':st18['曾進排隊'],
                '延後成交':st18['延後成交'],'到期/槽位淘汰':st18['到期/槽位淘汰'],
                '平均等待交易日':st18['平均等待交易日'],'最高排隊數':st18['最高排隊數'],
                '同股重複':st18['同股重複'],'資金不足':st18['資金不足']
            })
        diag18=pd.DataFrame(diag)
        st.markdown('### 🚥 ㉖ 狀態機 / 排隊壓力診斷')
        st.dataframe(diag18.round(4),use_container_width=True,hide_index=True)

        # 穩健判定：不以單純總報酬選 winner
        ranks=[]
        for _,r in cmp18.iterrows():
            qd=int(r['排隊有效交易日'])
            yy=yr18[yr18['排隊有效交易日']==qd]
            pos=float((yy['年度報酬%']>0).mean()) if len(yy) else 0
            ranks.append({
                '排隊有效交易日':qd,'正報酬年度比例%':pos*100,
                'Calmar':r['Calmar'],'淨PF':r['淨PF'],'CAGR%':r['CAGR%'],
                'MTM_MDD%':r['真實MTM_MDD%'],'承接率%':r['訊號承接率%'],
                '年度穩定通過':pos>=2/3
            })
        rank18=pd.DataFrame(ranks).sort_values(
            ['年度穩定通過','Calmar','淨PF','CAGR%'],
            ascending=[False,False,False,False]
        ).reset_index(drop=True)

        st.markdown('### 🧠 ㉗ V3.6.18 穩健排名')
        st.dataframe(rank18.round(4),use_container_width=True,hide_index=True)

        w18=rank18.iloc[0]
        qwin=int(w18['排隊有效交易日'])
        wr18=cmp18[cmp18['排隊有效交易日']==qwin].iloc[0]

        # 採用排隊的門檻比一般最佳化更嚴格：
        # 若排隊沒有明確改善，就維持 queue=0，實盤最簡單。
        annual_ok=bool(w18['年度穩定通過'])
        pf_ok=float(wr18['淨PF']) >= float(b18['淨PF']) - 0.03
        mdd_ok=float(wr18['真實MTM_MDD%']) >= float(b18['真實MTM_MDD%']) - 2.0
        calmar_ok=float(wr18['Calmar']) >= float(b18['Calmar'])
        meaningful = (qwin==0) or (
            float(wr18['Calmar']) >= float(b18['Calmar']) + 0.05
            or float(wr18['CAGR%']) >= float(b18['CAGR%']) + 1.0
            or float(wr18['淨PF']) >= float(b18['淨PF']) + 0.05
        )

        checks18=pd.DataFrame([
            {'驗證':'至少2/3年度正報酬','結果':f"{w18['正報酬年度比例%']:.1f}%",'通過':annual_ok},
            {'驗證':'淨PF不明顯劣於不排隊','結果':f"{wr18['淨PF']:.2f} vs {b18['淨PF']:.2f}",'通過':pf_ok},
            {'驗證':'MDD不得比不排隊惡化超過2ppt','結果':f"{wr18['真實MTM_MDD%']:.2f}% vs {b18['真實MTM_MDD%']:.2f}%",'通過':mdd_ok},
            {'驗證':'Calmar不低於不排隊','結果':f"{wr18['Calmar']:.2f} vs {b18['Calmar']:.2f}",'通過':calmar_ok},
            {'驗證':'若要排隊，至少有一項實質改善','結果':f"候選={qwin}交易日",'通過':meaningful},
        ])
        st.markdown('### 🧾 ㉘ V3.6.18 正式執行判定')
        st.dataframe(checks18,use_container_width=True,hide_index=True)

        if bool(checks18['通過'].all()):
            if qwin==0:
                st.success('🟢 正式狀態機鎖定：不排隊。槽位滿時直接略過，等待下一個新 Gate D 訊號；規則最單純，也最不容易產生延遲進場偏差。')
            else:
                st.success(f'🟢 正式狀態機候選：排隊最多 {qwin} 個交易日。下一版進入「實盤訂單生命週期 / 訊號去重 / 每日持倉帳本」驗證。')
        else:
            st.warning('🟡 排隊沒有形成足夠穩健優勢；正式規則維持「不排隊」，避免增加實盤複雜度。')

        # 指定一組查看逐日權益與狀態事件
        qsel=st.selectbox('查看哪一組排隊狀態機',options=[0,1,2,3],
                          format_func=lambda x:'不排隊' if x==0 else f'最多排隊{x}個交易日',
                          key='v3618_qsel')
        eqs,lgs,aus,sts=packs18[qsel]
        c1,c2,c3,c4=st.columns(4)
        c1.metric('總報酬%',f"{sts['總報酬%']:.2f}")
        c2.metric('真正MTM MDD%',f"{sts['真實MTM_MDD%']:.2f}")
        c3.metric('淨PF',f"{sts['淨PF']:.2f}")
        c4.metric('訊號承接率%',f"{sts['訊號承接率%']:.1f}")
        if not eqs.empty:
            st.line_chart(eqs.set_index('日期')[['MTM權益']])
        with st.expander('🔎 查看狀態機事件明細'):
            st.dataframe(aus.tail(1000),use_container_width=True,hide_index=True)

        st.download_button('⬇️ 下載 V3.6.18 排隊PK',
                           cmp18.to_csv(index=False,encoding='utf-8-sig').encode('utf-8-sig'),
                           'V3.6.18_queue_PK.csv','text/csv',key='dl_v3618_pk')
        st.download_button('⬇️ 下載 V3.6.18 年度穩定度',
                           yr18.to_csv(index=False,encoding='utf-8-sig').encode('utf-8-sig'),
                           'V3.6.18_queue_yearly.csv','text/csv',key='dl_v3618_year')
        st.download_button('⬇️ 下載目前狀態機事件',
                           aus.to_csv(index=False,encoding='utf-8-sig').encode('utf-8-sig'),
                           f'V3.6.18_queue_{qsel}_audit.csv','text/csv',key='dl_v3618_audit')
else:
    st.info('請先完成 V3.6.15 真實 MTM 資料重建；V3.6.18 直接沿用相同 Gate D 交易與完整日K。')


# ============================================================
# 📒 V3.6.19.1 實盤訂單生命週期 / 訊號去重 / 每日持倉帳本
# V3.6.18 正式結論：
#   1) 不排隊（queue=0）
#   2) Gate D = 90~94 + 站上MA200
#   3) 最大持股 = 25
#   4) 單筆目標資金 = 3.33%
#   5) 槽位排序 = 分數→成交額→近MA200
#
# 本版不再最佳化策略參數，只驗證「可執行性與帳務一致性」：
# NEW_SIGNAL -> ACCEPTED/REJECTED -> OPEN -> CLOSED
# 同股持有期間的新訊號一律去重；槽位滿一律 REJECTED_SLOT，不排隊。
# ============================================================

V3619_MAX_POS = 25
V3619_POS_PCT = 3.33
V3619_RANK = '分數→成交額→近MA200'

def _v3619_live_book(trades, price_map, initial_capital,
                     fee_pct=0.1425, tax_pct=0.30, slippage_pct=0.10):
    """以 V3.6.18 已鎖定規則重播一次「可實盤執行」的每日帳本。
    每日流程：
      1. 先依既定出場日平倉
      2. 釋放槽位
      3. 接收當日 Gate D 訊號
      4. 同股持有中 -> REJECTED_DUPLICATE
      5. 槽位已滿 -> REJECTED_SLOT（不排隊）
      6. 依 分數→成交額→近MA200 排序成交
      7. 收盤 MTM，建立每日現金/市值/權益/槽位/資金使用率帳本
    """
    if trades is None or trades.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), {}

    ranked = _v3617_ranked_trades(trades, V3619_RANK).copy()
    ranked['進場日'] = pd.to_datetime(ranked['進場日']).dt.normalize()
    ranked['資金出場日'] = pd.to_datetime(ranked['資金出場日']).dt.normalize()
    dates = _v3618_trade_dates(price_map, ranked)
    if not dates:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), {}

    buy_fee = fee_pct/100
    sell_fee = (fee_pct+tax_pct)/100
    slip = slippage_pct/100

    cash = float(initial_capital)
    open_pos = {}
    orders = []
    fills = []
    ledger = []
    positions_daily = []

    signal_count = 0
    accepted = 0
    reject_slot = 0
    reject_dup = 0
    reject_cash = 0
    reject_price = 0

    def current_equity(dt):
        mv = 0.0
        for sym,p in open_pos.items():
            px = _v3615_close_on(price_map, sym, dt, p['last_px'])
            if np.isfinite(px):
                p['last_px'] = float(px)
            mv += p['shares'] * p['last_px']
        return cash + mv, mv

    for dt in dates:
        # 1) CLOSE：先出場，當天釋放槽位
        closing = [sym for sym,p in open_pos.items() if p['exit_date'] <= dt]
        for sym in closing:
            p = open_pos[sym]
            exit_raw = float(p['exit_price'])
            exit_exec = exit_raw * (1-slip)
            gross = p['shares'] * exit_exec
            sell_cost = gross * sell_fee
            proceeds = gross - sell_cost
            cash += proceeds
            cost = p['entry_total_cost']
            pnl = proceeds - cost
            fills.append({
                '股票':sym,'名稱':p['名稱'],'狀態':'CLOSED',
                '訊號日':p['signal_date'],'進場日':p['entry_date'],'出場日':dt,
                '技術分數':p['score'],'進場價':p['entry_raw'],'出場價':exit_raw,
                '股數':p['shares'],'投入成本':cost,'賣出淨收入':proceeds,
                '淨損益':pnl,'淨報酬%':pnl/cost*100 if cost else 0,
                '出場原因':p['reason'],'持有交易日':p['hold']
            })
            orders.append({
                '日期':dt,'股票':sym,'事件':'CLOSE','結果':'FILLED',
                '原因':p['reason'],'技術分數':p['score'],
                '持倉數_事件前':len(open_pos),'現金_事件後':cash
            })
            del open_pos[sym]

        # 2) NEW SIGNAL：只處理當日訊號，不排隊
        todays = ranked[ranked['進場日'] == dt].copy()
        if not todays.empty:
            todays = _v3617_ranked_trades(todays, V3619_RANK)

        for _,r in todays.iterrows():
            signal_count += 1
            sym = str(r['股票']).zfill(4)
            score = float(pd.to_numeric(r.get('技術分數原值', r.get('技術分數',0)), errors='coerce') or 0)

            if sym in open_pos:
                reject_dup += 1
                orders.append({
                    '日期':dt,'股票':sym,'事件':'NEW_SIGNAL','結果':'REJECTED_DUPLICATE',
                    '原因':'同股仍在持有中','技術分數':score,
                    '持倉數_事件前':len(open_pos),'現金_事件後':cash
                })
                continue

            if len(open_pos) >= V3619_MAX_POS:
                reject_slot += 1
                orders.append({
                    '日期':dt,'股票':sym,'事件':'NEW_SIGNAL','結果':'REJECTED_SLOT',
                    '原因':'25槽位已滿；正式規則不排隊','技術分數':score,
                    '持倉數_事件前':len(open_pos),'現金_事件後':cash
                })
                continue

            d = price_map.get(sym)
            if d is None or d.empty:
                reject_price += 1
                orders.append({
                    '日期':dt,'股票':sym,'事件':'NEW_SIGNAL','結果':'REJECTED_NO_PRICE',
                    '原因':'缺少進場日價格','技術分數':score,
                    '持倉數_事件前':len(open_pos),'現金_事件後':cash
                })
                continue

            # V3.6.19.1：沿用 V3.6.15 真實MTM重建欄位。
            # 優先讀「重建進場價」，舊欄位僅作相容備援。
            entry_raw = pd.to_numeric(
                r.get('重建進場價', r.get('進場價', np.nan)),
                errors='coerce'
            )
            entry_raw = float(entry_raw) if pd.notna(entry_raw) else np.nan
            if not np.isfinite(entry_raw) or entry_raw <= 0:
                reject_price += 1
                orders.append({
                    '日期':dt,'股票':sym,'事件':'NEW_SIGNAL','結果':'REJECTED_BAD_PRICE',
                    '原因':'進場價無效','技術分數':score,
                    '持倉數_事件前':len(open_pos),'現金_事件後':cash
                })
                continue

            eq_now,_ = current_equity(dt)
            target = eq_now * V3619_POS_PCT/100
            entry_exec = entry_raw * (1+slip)
            unit_cost = entry_exec * (1+buy_fee)
            alloc = min(target, cash)

            # 若連目標部位的20%都無法建立，視為資金不足
            if alloc <= 0 or alloc < target*0.20:
                reject_cash += 1
                orders.append({
                    '日期':dt,'股票':sym,'事件':'NEW_SIGNAL','結果':'REJECTED_CASH',
                    '原因':'可用現金不足','技術分數':score,
                    '持倉數_事件前':len(open_pos),'現金_事件後':cash
                })
                continue

            shares = alloc/unit_cost
            entry_total_cost = shares*unit_cost
            cash -= entry_total_cost

            exit_price_val = pd.to_numeric(
                r.get('重建出場價', r.get('出場價', np.nan)),
                errors='coerce'
            )
            exit_date_val = pd.to_datetime(
                r.get('資金出場日', pd.NaT),
                errors='coerce'
            )
            if pd.isna(exit_price_val) or pd.isna(exit_date_val):
                reject_price += 1
                orders.append({
                    '日期':dt,'股票':sym,'事件':'NEW_SIGNAL','結果':'REJECTED_BAD_EXIT_DATA',
                    '原因':'缺少重建出場價或資金出場日','技術分數':score,
                    '持倉數_事件前':len(open_pos),'現金_事件後':cash
                })
                continue

            open_pos[sym] = {
                '股票':sym,'名稱':r.get('名稱',''),
                'signal_date':dt,'entry_date':dt,
                'exit_date':pd.Timestamp(exit_date_val).normalize(),
                'entry_raw':entry_raw,'entry_exec':entry_exec,
                'entry_total_cost':entry_total_cost,'shares':shares,
                # V3.6.19.1：V3.6.15 重建交易真正使用的欄位名稱
                'exit_price':float(exit_price_val),
                'reason':str(r.get('重建出場原因', r.get('出場原因',''))),
                'hold':int(pd.to_numeric(r.get('持有交易日',40),errors='coerce') or 40),
                'score':score,'last_px':entry_raw
            }
            accepted += 1
            orders.append({
                '日期':dt,'股票':sym,'事件':'NEW_SIGNAL','結果':'ACCEPTED_OPEN',
                '原因':'通過 Gate D / 排序 / 槽位 / 資金檢查','技術分數':score,
                '持倉數_事件前':len(open_pos)-1,'現金_事件後':cash
            })

        # 3) EOD MTM 帳本
        eq,mv = current_equity(dt)
        usage = mv/eq*100 if eq>0 else np.nan
        ledger.append({
            '日期':dt,'現金':cash,'持倉市值':mv,'MTM權益':eq,
            '持股檔數':len(open_pos),'可用槽位':V3619_MAX_POS-len(open_pos),
            '資金使用率%':usage
        })
        for sym,p in open_pos.items():
            market_value=p['shares']*p['last_px']
            unreal=market_value-p['entry_total_cost']
            positions_daily.append({
                '日期':dt,'股票':sym,'名稱':p['名稱'],'技術分數':p['score'],
                '進場日':p['entry_date'],'預定出場日':p['exit_date'],
                '股數':p['shares'],'進場成本':p['entry_total_cost'],
                '收盤價':p['last_px'],'持倉市值':market_value,
                '未實現損益':unreal,
                '未實現報酬%':unreal/p['entry_total_cost']*100 if p['entry_total_cost'] else 0
            })

    led = pd.DataFrame(ledger).sort_values('日期').drop_duplicates('日期',keep='last')
    od = pd.DataFrame(orders)
    fl = pd.DataFrame(fills)
    pdaily = pd.DataFrame(positions_daily)

    if led.empty:
        return led,od,fl,pdaily,{}

    led['權益高點'] = led['MTM權益'].cummax()
    led['回撤%'] = (led['MTM權益']/led['權益高點']-1)*100
    mdd = float(led['回撤%'].min())
    final = float(led.iloc[-1]['MTM權益'])
    total_ret = (final/initial_capital-1)*100
    years = max((led.iloc[-1]['日期']-led.iloc[0]['日期']).days/365.25,1/365.25)
    cagr = ((final/initial_capital)**(1/years)-1)*100 if final>0 else -100
    calmar = cagr/abs(mdd) if mdd<0 else np.nan

    gp = fl.loc[fl['淨損益']>0,'淨損益'].sum() if not fl.empty else 0
    gl = -fl.loc[fl['淨損益']<0,'淨損益'].sum() if not fl.empty else 0
    pf = gp/gl if gl>0 else (np.inf if gp>0 else 0)
    win = (fl['淨損益']>0).mean()*100 if not fl.empty else np.nan

    stats = {
        '總報酬%':total_ret,'CAGR%':cagr,'MTM_MDD%':mdd,'Calmar':calmar,
        '淨PF':pf,'淨勝率%':win,'完成交易':len(fl),
        '總訊號':signal_count,'接受訊號':accepted,
        '訊號承接率%':accepted/signal_count*100 if signal_count else 0,
        '槽位拒絕':reject_slot,'同股去重':reject_dup,
        '資金拒絕':reject_cash,'價格拒絕':reject_price,
        '最高持股':int(led['持股檔數'].max()),
        '平均持股':float(led['持股檔數'].mean()),
        '平均資金使用率%':float(led['資金使用率%'].mean()),
        '最高資金使用率%':float(led['資金使用率%'].max()),
        '最低可用槽位':int(led['可用槽位'].min()),
    }
    return led,od,fl,pdaily,stats

def _v3619_reconcile(ledger, orders, fills, stats, initial_capital):
    """帳務一致性檢查：用來抓狀態機/資金帳本的邏輯錯誤。"""
    rows=[]
    if ledger is None or ledger.empty:
        return pd.DataFrame()

    rows.append({
        '檢查':'MTM權益 = 現金 + 持倉市值',
        '結果':f"最大誤差 {(ledger['MTM權益']-(ledger['現金']+ledger['持倉市值'])).abs().max():.6f}",
        '通過':bool((ledger['MTM權益']-(ledger['現金']+ledger['持倉市值'])).abs().max() < 0.01)
    })
    rows.append({
        '檢查':'持股數不得超過25',
        '結果':f"最高 {int(ledger['持股檔數'].max())} 檔",
        '通過':bool(ledger['持股檔數'].max() <= V3619_MAX_POS)
    })
    rows.append({
        '檢查':'現金不得為負',
        '結果':f"最低現金 {ledger['現金'].min():,.2f}",
        '通過':bool(ledger['現金'].min() >= -0.01)
    })
    rows.append({
        '檢查':'資金使用率不得異常超過100.5%',
        '結果':f"最高 {ledger['資金使用率%'].max():.2f}%",
        '通過':bool(ledger['資金使用率%'].max() <= 100.5)
    })
    accepted = int((orders['結果']=='ACCEPTED_OPEN').sum()) if not orders.empty else 0
    rows.append({
        '檢查':'接受訊號數與 OPEN 訂單一致',
        '結果':f"{accepted} vs {int(stats.get('接受訊號',0))}",
        '通過':accepted == int(stats.get('接受訊號',0))
    })
    closed = int((orders['結果']=='FILLED').sum()) if not orders.empty else 0
    rows.append({
        '檢查':'CLOSE 成交與完成交易一致',
        '結果':f"{closed} vs {len(fills)}",
        '通過':closed == len(fills)
    })
    rows.append({
        '檢查':'正式規則沒有排隊狀態',
        '結果':'0 筆 QUEUED' if orders.empty or not orders['結果'].astype(str).str.contains('QUEUE').any() else '發現排隊狀態',
        '通過':bool(orders.empty or not orders['結果'].astype(str).str.contains('QUEUE').any())
    })
    return pd.DataFrame(rows)

def _v3619_yearly(ledger, fills):
    if ledger is None or ledger.empty:
        return pd.DataFrame()
    z=ledger.copy()
    z['年度']=pd.to_datetime(z['日期']).dt.year
    f=fills.copy() if fills is not None else pd.DataFrame()
    if not f.empty:
        f['年度']=pd.to_datetime(f['出場日']).dt.year
    rows=[]
    for y,g in z.groupby('年度'):
        g=g.sort_values('日期')
        first=float(g.iloc[0]['MTM權益']); last=float(g.iloc[-1]['MTM權益'])
        ret=(last/first-1)*100 if first else np.nan
        peak=g['MTM權益'].cummax()
        mdd=float(((g['MTM權益']/peak)-1).min()*100)
        fy=f[f['年度']==y] if not f.empty else pd.DataFrame()
        gp=fy.loc[fy['淨損益']>0,'淨損益'].sum() if not fy.empty else 0
        gl=-fy.loc[fy['淨損益']<0,'淨損益'].sum() if not fy.empty else 0
        pf=gp/gl if gl>0 else (np.inf if gp>0 else 0)
        rows.append({
            '年度':int(y),'年度報酬%':ret,'年度MDD%':mdd,
            '淨PF':pf,'淨勝率%':(fy['淨損益']>0).mean()*100 if not fy.empty else np.nan,
            '完成交易':len(fy),
            '平均持股':float(g['持股檔數'].mean()),
            '平均資金使用率%':float(g['資金使用率%'].mean())
        })
    return pd.DataFrame(rows)

st.divider()
st.subheader('📒 V3.6.19.1 實盤訂單生命週期 / 訊號去重 / 每日持倉帳本')
st.caption('V3.6.18 已正式淘汰排隊：queue=0。現在固定 Gate D、25檔、每筆3.33%、分數→成交額→近MA200；只驗證實盤狀態機與帳務一致性，不再調策略參數。')

tr19 = st.session_state.get('v3615_trades', pd.DataFrame())
px19 = st.session_state.get('v3615_prices', {})

if not tr19.empty and px19:
    required_cols_3619 = ['股票','進場日','資金出場日','重建進場價','重建出場價','重建出場原因','持有交易日']
    missing_cols_3619 = [c for c in required_cols_3619 if c not in tr19.columns]
    if missing_cols_3619:
        st.error('V3.6.19.1 資料契約檢查失敗：缺少欄位 ' + '、'.join(missing_cols_3619))
        st.stop()
    with st.expander('🔎 V3.6.19.1 交易資料欄位診斷', expanded=False):
        st.write('交易筆數：', len(tr19))
        st.write('必要欄位：', required_cols_3619)
        st.write('實際欄位：', list(tr19.columns))

    led19,ord19,fill19,pos19,stat19 = _v3619_live_book(
        tr19, px19, capital, fee, tax, slip
    )
    if stat19:
        st.markdown('### 🧾 ㉙ 正式規則執行摘要')
        c1,c2,c3,c4=st.columns(4)
        c1.metric('CAGR%',f"{stat19['CAGR%']:.2f}")
        c2.metric('真正 MTM MDD%',f"{stat19['MTM_MDD%']:.2f}")
        c3.metric('Calmar',f"{stat19['Calmar']:.2f}")
        c4.metric('淨PF',f"{stat19['淨PF']:.2f}")
        c5,c6,c7,c8=st.columns(4)
        c5.metric('總訊號',f"{stat19['總訊號']}")
        c6.metric('接受訊號',f"{stat19['接受訊號']}")
        c7.metric('槽位拒絕',f"{stat19['槽位拒絕']}")
        c8.metric('同股去重',f"{stat19['同股去重']}")

        summary19=pd.DataFrame([stat19])
        st.dataframe(summary19.round(4),use_container_width=True,hide_index=True)

        st.markdown('### 🧮 ㉚ 帳務一致性 / 狀態機驗證')
        rec19=_v3619_reconcile(led19,ord19,fill19,stat19,capital)
        st.dataframe(rec19,use_container_width=True,hide_index=True)

        st.markdown('### 📅 ㉛ 年度實盤帳本穩定度')
        yr19=_v3619_yearly(led19,fill19)
        st.dataframe(yr19.round(4),use_container_width=True,hide_index=True)

        st.markdown('### 📈 ㉜ 每日 MTM 權益 / 槽位使用')
        if not led19.empty:
            st.line_chart(led19.set_index('日期')[['MTM權益']])
            st.dataframe(
                led19[['日期','現金','持倉市值','MTM權益','持股檔數','可用槽位','資金使用率%','回撤%']]
                .tail(120).round(4),
                use_container_width=True,hide_index=True
            )

        st.markdown('### 🔁 ㉝ 訂單生命週期 / 拒絕原因')
        if not ord19.empty:
            reason19=(
                ord19.groupby(['事件','結果','原因'],dropna=False)
                .size().reset_index(name='筆數')
                .sort_values('筆數',ascending=False)
            )
            st.dataframe(reason19,use_container_width=True,hide_index=True)
            with st.expander('查看最近 500 筆訂單事件'):
                st.dataframe(ord19.tail(500),use_container_width=True,hide_index=True)

        st.markdown('### 🧠 ㉞ V3.6.19.1 正式可執行判定')
        checks19=pd.DataFrame([
            {'驗證':'帳務一致性全部通過','結果':f"{int(rec19['通過'].sum())}/{len(rec19)}",
             '通過':bool(rec19['通過'].all())},
            {'驗證':'最高持股≤25','結果':f"{stat19['最高持股']} 檔",
             '通過':stat19['最高持股']<=25},
            {'驗證':'沒有資金透支','結果':f"資金拒絕 {stat19['資金拒絕']} 筆",
             '通過':bool(led19['現金'].min()>=-0.01)},
            {'驗證':'至少2/3年度正報酬',
             '結果':f"{((yr19['年度報酬%']>0).mean()*100 if len(yr19) else 0):.1f}%",
             '通過':bool(len(yr19)>0 and (yr19['年度報酬%']>0).mean()>=2/3)},
            {'驗證':'淨PF≥1.50','結果':f"{stat19['淨PF']:.2f}",
             '通過':stat19['淨PF']>=1.50},
            {'驗證':'真正MTM MDD≥-25%','結果':f"{stat19['MTM_MDD%']:.2f}%",
             '通過':stat19['MTM_MDD%']>=-25},
            {'驗證':'Calmar≥1.50','結果':f"{stat19['Calmar']:.2f}",
             '通過':stat19['Calmar']>=1.50},
        ])
        st.dataframe(checks19,use_container_width=True,hide_index=True)

        if bool(checks19['通過'].all()):
            st.success('🟢 V3.6.19.1 通過：選股、槽位、資金、去重、進出場與每日 MTM 帳本可一致重播。下一階段可進入「Walk-Forward / 時間切割鎖參數」驗證，確認不是整段資料最佳化造成的結果。')
        else:
            st.warning('🟡 V3.6.19.1 尚有實盤帳務或穩健性條件未通過；先不要進 Walk-Forward，應先修正失敗項目。')

        st.download_button(
            '⬇️ 下載 V3.6.19.1 每日帳本',
            led19.to_csv(index=False,encoding='utf-8-sig').encode('utf-8-sig'),
            'V3.6.19.1_daily_ledger.csv','text/csv',key='dl_v3619_ledger'
        )
        st.download_button(
            '⬇️ 下載 V3.6.19.1 訂單事件',
            ord19.to_csv(index=False,encoding='utf-8-sig').encode('utf-8-sig'),
            'V3.6.19.1_order_events.csv','text/csv',key='dl_v3619_orders'
        )
        st.download_button(
            '⬇️ 下載 V3.6.19.1 完成交易',
            fill19.to_csv(index=False,encoding='utf-8-sig').encode('utf-8-sig'),
            'V3.6.19.1_closed_trades.csv','text/csv',key='dl_v3619_fills'
        )
        st.download_button(
            '⬇️ 下載 V3.6.19.1 每日持倉明細',
            pos19.to_csv(index=False,encoding='utf-8-sig').encode('utf-8-sig'),
            'V3.6.19.1_daily_positions.csv','text/csv',key='dl_v3619_positions'
        )
else:
    st.info('請先完成 V3.6.15 真實 MTM 資料重建；V3.6.19 直接沿用同一批 Gate D 交易與完整日K。')


# ============================================================
# 🧭 V3.6.20 Walk-Forward / 時間切割鎖參數驗證
# 固定正式規則，不再用測試區間重新最佳化：
# Gate D / 25檔 / 3.33% / 分數→成交額→近MA200 / queue=0
# ============================================================

def _v3620_period_stats(ledger, fills, initial_capital):
    if ledger is None or ledger.empty:
        return {}
    z = ledger.sort_values('日期').copy()
    start_eq = float(initial_capital)
    end_eq = float(z.iloc[-1]['MTM權益'])
    total_ret = (end_eq/start_eq - 1) * 100 if start_eq else np.nan
    days = max((pd.Timestamp(z.iloc[-1]['日期']) - pd.Timestamp(z.iloc[0]['日期'])).days, 1)
    years = days / 365.25
    cagr = ((end_eq/start_eq)**(1/years)-1)*100 if start_eq > 0 and end_eq > 0 else np.nan
    peak = z['MTM權益'].cummax()
    mdd = float(((z['MTM權益']/peak)-1).min()*100)
    f = fills.copy() if fills is not None else pd.DataFrame()
    if not f.empty and '淨損益' in f.columns:
        gp = float(f.loc[f['淨損益']>0,'淨損益'].sum())
        gl = float(-f.loc[f['淨損益']<0,'淨損益'].sum())
        pf = gp/gl if gl > 0 else (np.inf if gp > 0 else 0.0)
        win = float((f['淨損益']>0).mean()*100)
    else:
        pf, win = 0.0, np.nan
    calmar = cagr/abs(mdd) if np.isfinite(cagr) and mdd < 0 else np.nan
    return {
        '總報酬%': total_ret,
        'CAGR%': cagr,
        'MTM_MDD%': mdd,
        'Calmar': calmar,
        '淨PF': pf,
        '淨勝率%': win,
        '完成交易': len(f),
        '最高持股': int(z['持股檔數'].max()) if '持股檔數' in z else np.nan,
        '平均持股': float(z['持股檔數'].mean()) if '持股檔數' in z else np.nan,
        '平均資金使用率%': float(z['資金使用率%'].mean()) if '資金使用率%' in z else np.nan,
        '最高資金使用率%': float(z['資金使用率%'].max()) if '資金使用率%' in z else np.nan,
    }

def _v3620_halfyear_label(dt):
    dt = pd.Timestamp(dt)
    return f"{dt.year}-H{1 if dt.month <= 6 else 2}"

def _v3620_walkforward(trades, price_map, initial_capital, fee, tax, slip):
    """
    真正鎖參數的時間切割：
    - 不在任何 OOS 區間重新挑 Gate / 持股數 / 部位 / 排序。
    - 每個半年度獨立以同一初始資金重播，避免前段獲利放大後段結果。
    - 僅使用該 OOS 半年度『進場日』出現的訊號。
    """
    if trades is None or trades.empty:
        return pd.DataFrame(), {}

    x = trades.copy()
    x['進場日'] = pd.to_datetime(x['進場日'], errors='coerce').dt.normalize()
    x = x.dropna(subset=['進場日']).sort_values('進場日')
    if x.empty:
        return pd.DataFrame(), {}

    x['WF區間'] = x['進場日'].map(_v3620_halfyear_label)
    labels = list(dict.fromkeys(x['WF區間'].tolist()))
    rows = []
    detail = {}

    for label in labels:
        g = x[x['WF區間'] == label].copy()
        if g.empty:
            continue
        led, od, fl, pos, st = _v3619_live_book(
            g.drop(columns=['WF區間'], errors='ignore'),
            price_map, initial_capital, fee, tax, slip
        )
        if led is None or led.empty:
            continue
        ps = _v3620_period_stats(led, fl, initial_capital)
        signal_count = int(st.get('總訊號', 0))
        accepted = int(st.get('接受訊號', 0))
        row = {
            'OOS區間': label,
            '起始日': pd.Timestamp(g['進場日'].min()).date(),
            '最後訊號日': pd.Timestamp(g['進場日'].max()).date(),
            '總訊號': signal_count,
            '接受訊號': accepted,
            '承接率%': accepted/signal_count*100 if signal_count else 0,
            **ps
        }
        rows.append(row)
        detail[label] = {'ledger':led, 'orders':od, 'fills':fl, 'positions':pos, 'stats':st}

    out = pd.DataFrame(rows)
    if not out.empty:
        out['正報酬'] = out['總報酬%'] > 0
        out['PF>1'] = out['淨PF'] > 1
        out['MDD>-25%'] = out['MTM_MDD%'] >= -25
        out['Calmar>1'] = out['Calmar'] > 1
    return out, detail

def _v3620_expanding_oos(trades, price_map, initial_capital, fee, tax, slip):
    """
    Expanding Walk-Forward：
    前面區間只作『已知歷史』標記；正式參數完全鎖死，不用 train 重新最佳化。
    每個 OOS 半年獨立驗證。
    """
    wf, detail = _v3620_walkforward(trades, price_map, initial_capital, fee, tax, slip)
    if wf.empty:
        return wf
    z = wf.copy().reset_index(drop=True)
    z['先前可見區間數'] = range(len(z))
    z['是否純OOS'] = z['先前可見區間數'] >= 1
    return z

st.divider()
st.subheader('🧭 V3.6.20 Walk-Forward｜鎖參數時間切割驗證')
st.caption(
    'V3.6.19.1 已通過帳務與狀態機。V3.6.20 不再改 Gate D、不改 25 檔、不改每筆 3.33%、'
    '不改排序，也不排隊；只把歷史依半年度切開，逐段用同一套正式規則重播，檢查績效是否只集中在單一時期。'
)

if 'tr19' in globals() and isinstance(tr19, pd.DataFrame) and not tr19.empty and 'px19' in globals() and px19:
    wf20, wf20_detail = _v3620_walkforward(tr19, px19, capital, fee, tax, slip)
    exp20 = _v3620_expanding_oos(tr19, px19, capital, fee, tax, slip)

    if not wf20.empty:
        st.markdown('### 🏆 ㉟ 半年度 OOS｜固定參數逐段重播')
        show20 = wf20.copy()
        st.dataframe(show20.round(4), use_container_width=True, hide_index=True)

        # 真正 OOS：第一段視為形成期，第二段開始才列入正式 OOS 統計
        oos20 = exp20[exp20['是否純OOS']].copy()
        st.markdown('### 🧪 ㊱ Expanding Walk-Forward｜第一段形成、後續全為 OOS')
        st.dataframe(oos20.round(4), use_container_width=True, hide_index=True)

        if not oos20.empty:
            pos_ratio = float((oos20['總報酬%'] > 0).mean()*100)
            pf_ratio = float((oos20['淨PF'] > 1).mean()*100)
            mdd_ratio = float((oos20['MTM_MDD%'] >= -25).mean()*100)
            calmar_ratio = float((oos20['Calmar'] > 1).mean()*100)
            worst_mdd = float(oos20['MTM_MDD%'].min())
            median_ret = float(oos20['總報酬%'].median())
            median_pf = float(oos20['淨PF'].median())
            total_oos_trades = int(oos20['完成交易'].sum())

            c1,c2,c3,c4 = st.columns(4)
            c1.metric('OOS正報酬區間%', f'{pos_ratio:.1f}')
            c2.metric('OOS PF>1區間%', f'{pf_ratio:.1f}')
            c3.metric('OOS最差MDD%', f'{worst_mdd:.2f}')
            c4.metric('OOS完成交易', f'{total_oos_trades}')

            c5,c6,c7,c8 = st.columns(4)
            c5.metric('OOS中位報酬%', f'{median_ret:.2f}')
            c6.metric('OOS中位PF', f'{median_pf:.2f}')
            c7.metric('MDD合格區間%', f'{mdd_ratio:.1f}')
            c8.metric('Calmar>1區間%', f'{calmar_ratio:.1f}')

            st.markdown('### 📋 ㊲ V3.6.20 時間穩健性正式判定')
            min_segments = 3
            checks20 = pd.DataFrame([
                {
                    '驗證':'至少有3個純OOS半年度',
                    '結果':f'{len(oos20)} 段',
                    '通過':len(oos20) >= min_segments
                },
                {
                    '驗證':'至少2/3 OOS區間正報酬',
                    '結果':f'{pos_ratio:.1f}%',
                    '通過':pos_ratio >= 66.6667
                },
                {
                    '驗證':'至少2/3 OOS區間 PF>1',
                    '結果':f'{pf_ratio:.1f}%',
                    '通過':pf_ratio >= 66.6667
                },
                {
                    '驗證':'所有OOS區間 MDD 不低於 -25%',
                    '結果':f'最差 {worst_mdd:.2f}%',
                    '通過':worst_mdd >= -25
                },
                {
                    '驗證':'OOS中位數報酬 > 0',
                    '結果':f'{median_ret:.2f}%',
                    '通過':median_ret > 0
                },
                {
                    '驗證':'OOS中位數 PF > 1.20',
                    '結果':f'{median_pf:.2f}',
                    '通過':median_pf > 1.20
                },
                {
                    '驗證':'至少2/3 OOS區間 Calmar > 1',
                    '結果':f'{calmar_ratio:.1f}%',
                    '通過':calmar_ratio >= 66.6667
                },
            ])
            st.dataframe(checks20, use_container_width=True, hide_index=True)

            if bool(checks20['通過'].all()):
                st.success(
                    '🟢 V3.6.20 通過：Gate D 正式規則在時間切割後仍具一致性，'
                    '不是只靠整段資料或單一年份撐起績效。下一版直接進入「訊號日→下一交易日成交 / Look-ahead Bias 壓力測試」。'
                )
            else:
                st.warning(
                    '🟡 V3.6.20 有時間切割條件未通過。此處不自動調參；先辨識是哪個 OOS 半年度失效，'
                    '避免為了修漂亮數字重新最佳化正式規則。'
                )

            # 找最弱 / 最強 OOS 區間
            weakest = oos20.sort_values(['總報酬%','淨PF'], ascending=[True,True]).head(1)
            strongest = oos20.sort_values(['總報酬%','淨PF'], ascending=[False,False]).head(1)
            st.markdown('### 🔬 ㊳ 最弱 / 最強 OOS 區間')
            compare20 = pd.concat([
                weakest.assign(角色='最弱OOS'),
                strongest.assign(角色='最強OOS')
            ], ignore_index=True)
            cols20 = ['角色','OOS區間','總報酬%','CAGR%','MTM_MDD%','Calmar','淨PF','淨勝率%','完成交易','承接率%']
            st.dataframe(compare20[cols20].round(4), use_container_width=True, hide_index=True)

        st.download_button(
            '⬇️ 下載 V3.6.20 Walk-Forward 半年度結果',
            wf20.to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig'),
            'V3.6.20_walkforward_halfyear.csv',
            'text/csv',
            key='dl_v3620_wf'
        )
    else:
        st.warning('V3.6.20 無法建立半年度 Walk-Forward 結果，請確認 V3.6.19.1 交易資料日期。')
else:
    st.info('請先完成 V3.6.19.1；V3.6.20 直接沿用同一批正式 Gate D 交易與完整日K。')


# ============================================================
# 🛡️ V3.6.21 Look-ahead Bias 壓力測試
# 正式策略完全鎖定：
# Gate D / 25檔 / 3.33% / 分數→成交額→近MA200 / 不排隊
#
# 唯一改變：訊號在「訊號日收盤後」才視為成立，
# 因此不再允許用訊號日收盤成交。
# 比較：
# A. 原基準：訊號日收盤成交
# B. N+1 開盤成交（主要實盤壓力測試）
# C. N+1 收盤成交（更保守壓力測試）
# 出場仍固定 D40 / 盤中硬停損12%，並從新的實際進場日起重建。
# ============================================================

def _v3621_next_trade_date(df, signal_date):
    if df is None or df.empty:
        return None
    idx = pd.DatetimeIndex(df.index).normalize()
    s = pd.Timestamp(signal_date).normalize()
    pos = np.where(idx > s)[0]
    if len(pos) == 0:
        return None
    return pd.Timestamp(idx[int(pos[0])]).normalize()

def _v3621_trade_from_execution(df, entry_date, mode='open',
                                hard_stop=12, hold=40):
    """從真正可成交的 N+1 日期重建交易。
    mode=open  : N+1 開盤進場；當日 Low 即開始檢查 -12% 硬停損。
    mode=close : N+1 收盤進場；硬停損從下一交易日開始檢查。
    時間出場均以進場日起第40個交易日收盤。
    """
    if df is None or df.empty:
        return None
    d = df.copy()
    idx = pd.DatetimeIndex(d.index).normalize()
    dt = pd.Timestamp(entry_date).normalize()
    loc = np.where(idx == dt)[0]
    if len(loc) == 0:
        return None
    i = int(loc[-1])
    if i >= len(d)-1:
        return None

    col = 'Open' if mode == 'open' else 'Close'
    if col not in d.columns:
        return None
    entry = pd.to_numeric(d[col].iloc[i], errors='coerce')
    if pd.isna(entry) or not np.isfinite(float(entry)) or float(entry) <= 0:
        return None
    entry = float(entry)

    end = min(i + int(hold), len(d)-1)
    stop_price = entry * (1-hard_stop/100)
    stop_start = i if mode == 'open' else i+1

    for j in range(stop_start, end+1):
        low = pd.to_numeric(d['Low'].iloc[j], errors='coerce')
        if pd.notna(low) and float(low) <= stop_price:
            return {
                '進場日':pd.Timestamp(d.index[i]).normalize(),
                '出場日':pd.Timestamp(d.index[j]).normalize(),
                '進場價':entry,
                '出場價':stop_price,
                '報酬%':-float(hard_stop),
                '持有天數':j-i,
                '出場原因':f'N+1 {mode.upper()}｜硬停損{hard_stop:g}%'
            }

    exit_px = pd.to_numeric(d['Close'].iloc[end], errors='coerce')
    if pd.isna(exit_px) or not np.isfinite(float(exit_px)):
        return None
    exit_px = float(exit_px)
    return {
        '進場日':pd.Timestamp(d.index[i]).normalize(),
        '出場日':pd.Timestamp(d.index[end]).normalize(),
        '進場價':entry,
        '出場價':exit_px,
        '報酬%':(exit_px/entry-1)*100,
        '持有天數':end-i,
        '出場原因':f'N+1 {mode.upper()}｜D{hold}固定出場'
    }

def _v3621_shift_trades(trades, price_map, mode='open'):
    """保留原訊號日的 Gate D 分數/排序資訊，只把成交延後到下一交易日。"""
    if trades is None or trades.empty:
        return pd.DataFrame(), {'原訊號':0,'成功重建':0,'無下一交易日':0,'缺價格':0}

    rows = []
    stats = {'原訊號':len(trades),'成功重建':0,'無下一交易日':0,'缺價格':0}
    for _, r in trades.iterrows():
        sym = str(r['股票']).zfill(4)
        d = price_map.get(sym)
        if d is None or d.empty:
            stats['缺價格'] += 1
            continue

        signal_date = pd.Timestamp(r['進場日']).normalize()
        nd = _v3621_next_trade_date(d, signal_date)
        if nd is None:
            stats['無下一交易日'] += 1
            continue

        tr = _v3621_trade_from_execution(d, nd, mode=mode, hard_stop=12, hold=40)
        if tr is None:
            stats['缺價格'] += 1
            continue

        rr = r.to_dict()
        rr['原始訊號日_V3621'] = signal_date
        rr['進場日'] = pd.Timestamp(tr['進場日']).normalize()
        rr['資金出場日'] = pd.Timestamp(tr['出場日']).normalize()
        rr['重建進場價'] = float(tr['進場價'])
        rr['重建出場價'] = float(tr['出場價'])
        rr['重建毛報酬%'] = float(tr['報酬%'])
        rr['重建出場原因'] = str(tr['出場原因'])
        rr['持有交易日'] = int(tr['持有天數'])
        rr['V3621成交模式'] = 'N+1開盤' if mode == 'open' else 'N+1收盤'
        rows.append(rr)
        stats['成功重建'] += 1

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(['進場日','技術分數'], ascending=[True,False]).reset_index(drop=True)
    return out, stats

def _v3621_run_case(label, trades, price_map, initial_capital, fee, tax, slip):
    led, od, fl, pos, stx = _v3619_live_book(
        trades, price_map, initial_capital, fee, tax, slip
    )
    m = _v3620_period_stats(led, fl, initial_capital)
    return {
        '成交假設':label,
        **m,
        '總訊號':int(stx.get('總訊號',0)),
        '接受訊號':int(stx.get('接受訊號',0)),
        '槽位拒絕':int(stx.get('槽位拒絕',0)),
        '同股去重':int(stx.get('同股去重',0)),
        '資金拒絕':int(stx.get('資金拒絕',0)),
    }, (led,od,fl,pos,stx)

def _v3621_yearly(case_label, fills, ledger, initial_capital):
    if fills is None or fills.empty or ledger is None or ledger.empty:
        return pd.DataFrame()
    f = fills.copy()
    f['出場日'] = pd.to_datetime(f['出場日'])
    l = ledger.copy()
    l['日期'] = pd.to_datetime(l['日期'])
    rows = []
    for y in sorted(l['日期'].dt.year.unique()):
        ly = l[l['日期'].dt.year == y].copy()
        fy = f[f['出場日'].dt.year == y].copy()
        if ly.empty:
            continue
        # 年度報酬採該年第一個帳本權益為基準，避免把跨年本金誤當固定100萬。
        start_eq = float(ly.iloc[0]['MTM權益'])
        end_eq = float(ly.iloc[-1]['MTM權益'])
        ret = (end_eq/start_eq-1)*100 if start_eq else np.nan
        peak = ly['MTM權益'].cummax()
        mdd = float(((ly['MTM權益']/peak)-1).min()*100)
        gp = float(fy.loc[fy['淨損益']>0,'淨損益'].sum()) if not fy.empty else 0
        gl = float(-fy.loc[fy['淨損益']<0,'淨損益'].sum()) if not fy.empty else 0
        pf = gp/gl if gl>0 else (np.inf if gp>0 else 0)
        rows.append({
            '成交假設':case_label,'年度':int(y),'年度報酬%':ret,'年度MDD%':mdd,
            '淨PF':pf,'完成交易':len(fy),
            '淨勝率%':float((fy['淨損益']>0).mean()*100) if not fy.empty else np.nan
        })
    return pd.DataFrame(rows)

st.divider()
st.subheader('🛡️ V3.6.21 Look-ahead Bias｜下一交易日成交壓力測試')
st.caption(
    'V3.6.20 的 66.7% 邊界顯示曾因浮點數門檻被誤判；本版判定改用「通過段數 / 總段數」整數計數。'
    '策略參數仍完全鎖定。核心問題只有一個：若 Gate D 訊號必須等收盤後才確認，改成下一交易日才成交，優勢還剩多少？'
)

if 'tr19' in globals() and isinstance(tr19, pd.DataFrame) and not tr19.empty and 'px19' in globals() and px19:
    # 修正 V3.6.20 的 2/3 浮點邊界判斷
    wf21, _ = _v3620_walkforward(tr19, px19, capital, fee, tax, slip)
    exp21 = _v3620_expanding_oos(tr19, px19, capital, fee, tax, slip)
    pure21 = exp21[exp21['是否純OOS']].copy() if not exp21.empty else pd.DataFrame()
    if not pure21.empty:
        nseg = len(pure21)
        need = int(np.ceil(nseg * 2/3))
        positive_n = int((pure21['總報酬%'] > 0).sum())
        pf_n = int((pure21['淨PF'] > 1).sum())
        calmar_n = int((pure21['Calmar'] > 1).sum())
        st.info(
            f'🔧 V3.6.20 邊界修正：純OOS共 {nseg} 段，2/3門檻應為至少 {need} 段。'
            f'正報酬 {positive_n}/{nseg}、PF>1 {pf_n}/{nseg}、Calmar>1 {calmar_n}/{nseg}。'
        )

    next_open, rebuild_open = _v3621_shift_trades(tr19, px19, mode='open')
    next_close, rebuild_close = _v3621_shift_trades(tr19, px19, mode='close')

    base_row, base_pack = _v3621_run_case(
        '基準｜訊號日收盤', tr19, px19, capital, fee, tax, slip
    )
    open_row, open_pack = _v3621_run_case(
        '主要壓測｜N+1開盤', next_open, px19, capital, fee, tax, slip
    )
    close_row, close_pack = _v3621_run_case(
        '保守壓測｜N+1收盤', next_close, px19, capital, fee, tax, slip
    )

    pk21 = pd.DataFrame([base_row, open_row, close_row])
    b = pk21.iloc[0]
    for c in ['總報酬%','CAGR%','MTM_MDD%','Calmar','淨PF','淨勝率%']:
        pk21[f'{c}差異'] = pk21[c] - b[c]

    st.markdown('### 🏆 ㊴ 三種成交時點 PK')
    cols = [
        '成交假設','總報酬%','CAGR%','MTM_MDD%','Calmar','淨PF','淨勝率%',
        '完成交易','最高持股','平均持股','平均資金使用率%','最高資金使用率%',
        'CAGR%差異','MTM_MDD%差異','Calmar差異','淨PF差異'
    ]
    st.dataframe(pk21[cols].round(4), use_container_width=True, hide_index=True)

    st.markdown('### 🔄 ㊵ N+1 訊號重建完整度')
    rebuild_df = pd.DataFrame([
        {'模式':'N+1開盤', **rebuild_open},
        {'模式':'N+1收盤', **rebuild_close},
    ])
    rebuild_df['重建率%'] = np.where(
        rebuild_df['原訊號']>0,
        rebuild_df['成功重建']/rebuild_df['原訊號']*100, 0
    )
    st.dataframe(rebuild_df.round(4), use_container_width=True, hide_index=True)

    # 年度壓測
    yr21 = pd.concat([
        _v3621_yearly('基準｜訊號日收盤', base_pack[2], base_pack[0], capital),
        _v3621_yearly('N+1開盤', open_pack[2], open_pack[0], capital),
        _v3621_yearly('N+1收盤', close_pack[2], close_pack[0], capital),
    ], ignore_index=True)
    st.markdown('### 📅 ㊶ 成交延遲年度穩定度')
    st.dataframe(yr21.round(4), use_container_width=True, hide_index=True)

    # 正式判定：以 N+1 開盤為主要實盤假設
    op = open_row
    base_cagr = float(base_row.get('CAGR%', np.nan))
    op_cagr = float(op.get('CAGR%', np.nan))
    cagr_keep = op_cagr/base_cagr*100 if np.isfinite(base_cagr) and base_cagr>0 else np.nan
    base_pf = float(base_row.get('淨PF', np.nan))
    op_pf = float(op.get('淨PF', np.nan))
    op_mdd = float(op.get('MTM_MDD%', np.nan))
    op_calmar = float(op.get('Calmar', np.nan))
    op_trades = int(op.get('完成交易',0))

    yop = yr21[yr21['成交假設']=='N+1開盤'].copy()
    pos_years = int((yop['年度報酬%']>0).sum()) if not yop.empty else 0
    year_need = int(np.ceil(len(yop)*2/3)) if len(yop) else 999

    checks21 = pd.DataFrame([
        {'驗證':'N+1開盤 CAGR 仍為正','結果':f'{op_cagr:.2f}%','通過':op_cagr>0},
        {'驗證':'N+1開盤保留至少60%原CAGR','結果':f'{cagr_keep:.1f}%','通過':cagr_keep>=60},
        {'驗證':'N+1開盤淨PF ≥ 1.50','結果':f'{op_pf:.2f}','通過':op_pf>=1.50},
        {'驗證':'N+1開盤真正MTM MDD ≥ -25%','結果':f'{op_mdd:.2f}%','通過':op_mdd>=-25},
        {'驗證':'N+1開盤 Calmar ≥ 1.50','結果':f'{op_calmar:.2f}','通過':op_calmar>=1.50},
        {'驗證':'N+1開盤完成交易 ≥ 250','結果':f'{op_trades} 筆','通過':op_trades>=250},
        {'驗證':'至少2/3年度 N+1開盤正報酬',
         '結果':f'{pos_years}/{len(yop)} 年','通過':pos_years>=year_need},
    ])

    st.markdown('### 🧠 ㊷ V3.6.21 Look-ahead 正式判定')
    st.dataframe(checks21, use_container_width=True, hide_index=True)

    if bool(checks21['通過'].all()):
        st.success(
            '🟢 V3.6.21 通過：把成交延後到下一交易日開盤後，策略仍保有足夠風險調整後優勢。'
            '這代表原本績效不是主要靠「訊號日收盤同價成交」的前視偏誤撐起。'
        )
    else:
        st.warning(
            '🟡 V3.6.21 有條件未通過：先不要改 Gate D 或資金參數。'
            '應先判斷績效衰退來自隔夜跳空、延後一天錯過行情，或特定年度。'
        )

    # 隔夜成交價格偏移診斷
    diag_rows = []
    base_map = tr19.copy()
    base_map['股票'] = base_map['股票'].astype(str).str.zfill(4)
    base_map['原始訊號日_V3621'] = pd.to_datetime(base_map['進場日']).dt.normalize()
    base_map['_key'] = base_map['股票'] + '|' + base_map['原始訊號日_V3621'].astype(str)
    for mode_name, z in [('N+1開盤',next_open),('N+1收盤',next_close)]:
        if z is None or z.empty:
            continue
        zz = z.copy()
        zz['股票'] = zz['股票'].astype(str).str.zfill(4)
        zz['_key'] = zz['股票'] + '|' + pd.to_datetime(zz['原始訊號日_V3621']).dt.normalize().astype(str)
        bm = base_map[['_key','重建進場價']].rename(columns={'重建進場價':'訊號日收盤價'})
        mm = zz.merge(bm,on='_key',how='left')
        mm['成交價偏移%'] = (
            pd.to_numeric(mm['重建進場價'],errors='coerce') /
            pd.to_numeric(mm['訊號日收盤價'],errors='coerce') - 1
        )*100
        s = mm['成交價偏移%'].replace([np.inf,-np.inf],np.nan).dropna()
        if len(s):
            diag_rows.append({
                '模式':mode_name,'樣本':len(s),
                '平均成交價偏移%':s.mean(),'中位成交價偏移%':s.median(),
                'P90偏移%':s.quantile(.90),'P10偏移%':s.quantile(.10),
                '上漲跳空比例%':(s>0).mean()*100
            })
    st.markdown('### 🌙 ㊸ 隔夜 / 延遲成交價格偏移')
    if diag_rows:
        st.dataframe(pd.DataFrame(diag_rows).round(4), use_container_width=True, hide_index=True)

    st.download_button(
        '⬇️ 下載 V3.6.21 成交時點 PK',
        pk21.to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig'),
        'V3.6.21_execution_timing_PK.csv',
        'text/csv',
        key='dl_v3621_pk'
    )
    if not yr21.empty:
        st.download_button(
            '⬇️ 下載 V3.6.21 年度成交壓測',
            yr21.to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig'),
            'V3.6.21_execution_yearly.csv',
            'text/csv',
            key='dl_v3621_year'
        )
else:
    st.info('請先完成 V3.6.19.1 正式帳本；V3.6.21 會沿用同一批 Gate D 訊號與完整日K。')


# ============================================================
# V3.6.22 Capital Allocation Robustness
# 正式成交假設改採 V3.6.21 已通過的 N+1 開盤。
# 不改 Gate D、不改排序、不排隊；只驗證「最大持股數 × 單筆資金%」。
# ============================================================

def _v3622_run_allocation(trades, price_map, initial_capital,
                          max_pos, pos_pct, fee_pct, tax_pct, slippage_pct):
    """沿用 V3.6.19.1 正式帳本，但暫時替換容量參數；執行後立即還原全域設定。"""
    global V3619_MAX_POS, V3619_POS_PCT
    old_max = V3619_MAX_POS
    old_pct = V3619_POS_PCT
    try:
        V3619_MAX_POS = int(max_pos)
        V3619_POS_PCT = float(pos_pct)
        led, od, fl, pos, stx = _v3619_live_book(
            trades, price_map, initial_capital,
            fee_pct, tax_pct, slippage_pct
        )
        met = _v3620_period_stats(led, fl, initial_capital)
        met.update({
            '最大同時持股設定': int(max_pos),
            '單筆目標資金%': float(pos_pct),
            '名目最大投入%': float(max_pos) * float(pos_pct),
            '總訊號': int(stx.get('總訊號', 0)),
            '接受訊號': int(stx.get('接受訊號', 0)),
            '槽位拒絕': int(stx.get('槽位拒絕', 0)),
            '資金拒絕': int(stx.get('資金拒絕', 0)),
            '同股去重': int(stx.get('同股去重', 0)),
        })
        met['承接率%'] = (
            met['接受訊號'] / met['總訊號'] * 100
            if met['總訊號'] else 0.0
        )
        met['報酬/MDD'] = (
            float(met.get('CAGR%', 0)) / abs(float(met.get('MTM_MDD%', 0)))
            if float(met.get('MTM_MDD%', 0)) != 0 else np.nan
        )
        return met, (led, od, fl, pos, stx)
    finally:
        V3619_MAX_POS = old_max
        V3619_POS_PCT = old_pct


def _v3622_yearly(case_label, ledger, fills):
    if ledger is None or ledger.empty:
        return pd.DataFrame()
    z = _v3619_yearly(ledger, fills)
    if z is None or z.empty:
        return pd.DataFrame()
    z = z.copy()
    z.insert(0, '配置', case_label)
    return z


st.divider()
st.subheader('💰 V3.6.22 資金配置穩健度｜25檔 × 3.33% 是否真的是甜蜜點？')
st.caption(
    'V3.6.21 已證明 N+1 開盤成交仍可維持 CAGR 49.13%、淨PF 2.20、MTM MDD -19.92%。'
    '因此本版正式改用「N+1 開盤」作為成交假設；Gate D、排序規則、queue=0 全部鎖定，'
    '只壓測最大同時持股與單筆目標資金比例，避免把資金配置誤當成選股優勢。'
)

if 'next_open' in globals() and isinstance(next_open, pd.DataFrame) and not next_open.empty and 'px19' in globals() and px19:
    # 以 25×3.33 為中心，刻意測較保守與較積極配置。
    configs22 = [
        (20, 3.00), (20, 3.33), (20, 4.00), (20, 5.00),
        (25, 2.50), (25, 3.00), (25, 3.33), (25, 3.50), (25, 4.00),
        (30, 2.50), (30, 3.00), (30, 3.33),
    ]

    rows22 = []
    packs22 = {}
    for mp, pp in configs22:
        label = f'{mp}檔 × {pp:g}%'
        rr, pack = _v3622_run_allocation(
            next_open, px19, capital, mp, pp, fee, tax, slip
        )
        rr['配置'] = label
        rows22.append(rr)
        packs22[label] = pack

    grid22 = pd.DataFrame(rows22)

    # 正式基準：25 × 3.33
    base22 = grid22[
        (grid22['最大同時持股設定'] == 25) &
        (np.isclose(grid22['單筆目標資金%'], 3.33))
    ].iloc[0]

    for c in ['總報酬%','CAGR%','MTM_MDD%','Calmar','淨PF','淨勝率%','承接率%']:
        grid22[f'{c}差異'] = grid22[c] - float(base22[c])

    # 可行性：不允許名目配置 >100%，且實際最高資金使用率不可明顯穿越100.5%
    grid22['名目可行'] = grid22['名目最大投入%'] <= 100.0 + 1e-9
    grid22['實際資金可行'] = grid22['最高資金使用率%'] <= 100.5
    grid22['MDD合格'] = grid22['MTM_MDD%'] >= -25
    grid22['PF合格'] = grid22['淨PF'] >= 1.50
    grid22['Calmar合格'] = grid22['Calmar'] >= 1.50
    grid22['年度待驗'] = True

    st.markdown('### 🏆 ㊹ 資金配置 Grid PK｜全部採 N+1 開盤')
    show22 = [
        '配置','總報酬%','CAGR%','MTM_MDD%','Calmar','淨PF','淨勝率%',
        '完成交易','承接率%','最高持股','平均持股',
        '平均資金使用率%','最高資金使用率%','槽位拒絕','資金拒絕',
        '名目最大投入%','報酬/MDD'
    ]
    st.dataframe(
        grid22.sort_values(['Calmar','淨PF','CAGR%'], ascending=False)[show22].round(4),
        use_container_width=True, hide_index=True
    )

    # 只看 25 檔：回答使用者最直接的 3% vs 3.33% vs 4% 問題
    st.markdown('### 🔬 ㊺ 固定 25 檔｜單筆資金比例敏感度')
    fixed25 = grid22[grid22['最大同時持股設定'] == 25].copy()
    fixed25 = fixed25.sort_values('單筆目標資金%')
    fixed25_show = [
        '單筆目標資金%','名目最大投入%','CAGR%','MTM_MDD%','Calmar','淨PF',
        '承接率%','平均資金使用率%','最高資金使用率%',
        'CAGR%差異','MTM_MDD%差異','Calmar差異','淨PF差異'
    ]
    st.dataframe(fixed25[fixed25_show].round(4), use_container_width=True, hide_index=True)

    # 年度穩定度：針對核心候選，不讓總績效掩蓋單一年份失效
    core_labels22 = ['20檔 × 4%', '25檔 × 3%', '25檔 × 3.33%', '25檔 × 3.5%', '30檔 × 3%']
    yr_parts22 = []
    for lab in core_labels22:
        if lab in packs22:
            led22, od22, fl22, pos22, stx22 = packs22[lab]
            yy = _v3622_yearly(lab, led22, fl22)
            if not yy.empty:
                yr_parts22.append(yy)
    yr22 = pd.concat(yr_parts22, ignore_index=True) if yr_parts22 else pd.DataFrame()

    st.markdown('### 📅 ㊻ 核心配置年度穩定度')
    if not yr22.empty:
        st.dataframe(yr22.round(4), use_container_width=True, hide_index=True)

    # 穩健排名：不追求最高 CAGR，優先 Calmar / MDD / PF / 年度正報酬。
    robust_rows22 = []
    for _, r in grid22.iterrows():
        lab = r['配置']
        pack = packs22.get(lab)
        if pack is None:
            continue
        led22, od22, fl22, pos22, stx22 = pack
        yy = _v3622_yearly(lab, led22, fl22)
        pos_year_ratio = (
            float((yy['年度報酬%'] > 0).mean() * 100)
            if yy is not None and not yy.empty and '年度報酬%' in yy.columns else np.nan
        )
        worst_year = (
            float(yy['年度報酬%'].min())
            if yy is not None and not yy.empty and '年度報酬%' in yy.columns else np.nan
        )
        robust_rows22.append({
            '配置': lab,
            '正報酬年度比例%': pos_year_ratio,
            '最差年度報酬%': worst_year,
            'CAGR%': r['CAGR%'],
            'MTM_MDD%': r['MTM_MDD%'],
            'Calmar': r['Calmar'],
            '淨PF': r['淨PF'],
            '承接率%': r['承接率%'],
            '最高資金使用率%': r['最高資金使用率%'],
            '名目最大投入%': r['名目最大投入%'],
            '可行': bool(
                r['名目可行'] and r['實際資金可行'] and
                r['MDD合格'] and r['PF合格'] and r['Calmar合格']
            )
        })

    robust22 = pd.DataFrame(robust_rows22)
    if not robust22.empty:
        robust22 = robust22.sort_values(
            ['可行','正報酬年度比例%','Calmar','淨PF','CAGR%'],
            ascending=[False,False,False,False,False]
        ).reset_index(drop=True)

    st.markdown('### 🧠 ㊼ V3.6.22 穩健配置排名')
    if not robust22.empty:
        st.dataframe(robust22.round(4), use_container_width=True, hide_index=True)

    # 正式判定：25×3.33 不必「第一名」，只要位於穩健平台且沒有被鄰近參數明顯支配。
    b_cagr = float(base22['CAGR%'])
    b_mdd = float(base22['MTM_MDD%'])
    b_cal = float(base22['Calmar'])
    b_pf = float(base22['淨PF'])

    neigh = grid22[
        (grid22['最大同時持股設定'] == 25) &
        (grid22['單筆目標資金%'].isin([3.0, 3.33, 3.5, 4.0]))
    ].copy()

    best_cal = float(neigh['Calmar'].max()) if not neigh.empty else b_cal
    best_pf = float(neigh['淨PF'].max()) if not neigh.empty else b_pf
    cal_keep = b_cal / best_cal * 100 if best_cal > 0 else np.nan
    pf_keep = b_pf / best_pf * 100 if best_pf > 0 else np.nan

    # 3% 與 3.33% 的直接差異
    row3 = fixed25[np.isclose(fixed25['單筆目標資金%'], 3.0)]
    if not row3.empty:
        row3 = row3.iloc[0]
        cagr_gain_vs3 = b_cagr - float(row3['CAGR%'])
        mdd_cost_vs3 = b_mdd - float(row3['MTM_MDD%'])
        cal_diff_vs3 = b_cal - float(row3['Calmar'])
    else:
        cagr_gain_vs3 = mdd_cost_vs3 = cal_diff_vs3 = np.nan

    checks22 = pd.DataFrame([
        {'驗證':'25×3.33 名目投入 ≤ 100%','結果':f"{base22['名目最大投入%']:.2f}%",'通過':base22['名目最大投入%']<=100},
        {'驗證':'25×3.33 真正MTM MDD ≥ -25%','結果':f'{b_mdd:.2f}%','通過':b_mdd>=-25},
        {'驗證':'25×3.33 淨PF ≥ 1.50','結果':f'{b_pf:.2f}','通過':b_pf>=1.50},
        {'驗證':'25×3.33 Calmar ≥ 1.50','結果':f'{b_cal:.2f}','通過':b_cal>=1.50},
        {'驗證':'Calmar 至少保留鄰近最佳的90%','結果':f'{cal_keep:.1f}%','通過':cal_keep>=90},
        {'驗證':'PF 至少保留鄰近最佳的90%','結果':f'{pf_keep:.1f}%','通過':pf_keep>=90},
        {'驗證':'最高實際資金使用率 ≤ 100.5%','結果':f"{base22['最高資金使用率%']:.2f}%",'通過':base22['最高資金使用率%']<=100.5},
    ])

    st.markdown('### 📋 ㊽ V3.6.22 正式判定')
    st.dataframe(checks22, use_container_width=True, hide_index=True)

    if np.isfinite(cagr_gain_vs3):
        st.info(
            f'📌 25檔固定比較：3.33% 相對 3.00% 的 CAGR 差異 {cagr_gain_vs3:+.2f} ppt；'
            f'MDD 差異 {mdd_cost_vs3:+.2f} ppt；Calmar 差異 {cal_diff_vs3:+.2f}。'
            '這一列就是判斷「多投入 0.33%/筆是否值得」的核心數字。'
        )

    if bool(checks22['通過'].all()):
        st.success(
            '🟢 V3.6.22 通過：25檔 × 3.33% 位於可行且穩健的資金配置平台。'
            '後續不再因單次回測小幅差異調整 3.33%；除非更嚴格 OOS / Monte Carlo 顯示其被鄰近配置明顯支配。'
        )
    else:
        st.warning(
            '🟡 V3.6.22 有條件未通過：先不要微調 Gate D。'
            '下一步應判斷是持股上限造成槽位壓力，還是單筆資金比例造成 MDD / 資金使用率惡化。'
        )

    st.download_button(
        '⬇️ 下載 V3.6.22 資金配置 Grid',
        grid22.to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig'),
        'V3.6.22_capital_allocation_grid.csv',
        'text/csv',
        key='dl_v3622_grid'
    )
    if not yr22.empty:
        st.download_button(
            '⬇️ 下載 V3.6.22 年度配置穩定度',
            yr22.to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig'),
            'V3.6.22_capital_allocation_yearly.csv',
            'text/csv',
            key='dl_v3622_yearly'
        )
else:
    st.info('請先完成 V3.6.21；V3.6.22 會直接沿用已重建完成的 N+1 開盤 Gate D 訊號。')


# ============================================================
# 🎲 V3.6.23 Monte Carlo / Trade Sequence Robustness
# 正式規則完全鎖定：
# Gate D / N+1開盤 / 分數→成交額→近MA200 / queue=0 / 25檔 / 3.33%
#
# 本版不再最佳化任何參數，只回答：
# 1) 如果歷史報酬的出現順序改變，MDD/CAGR 還撐得住嗎？
# 2) 若遇到比較倒楣的報酬排列，虧損機率多高？
# 3) 目前漂亮的 PF / CAGR 是否高度依賴少數交易順序？
# ============================================================

def _v3623_daily_returns_from_ledger(ledger):
    if ledger is None or ledger.empty or 'MTM權益' not in ledger.columns:
        return pd.Series(dtype=float)
    z = ledger.sort_values('日期').copy()
    r = pd.to_numeric(z['MTM權益'], errors='coerce').pct_change()
    r = r.replace([np.inf, -np.inf], np.nan).dropna()
    return r.astype(float)

def _v3623_block_bootstrap_returns(ret, n_paths=3000, block=20, seed=3623):
    """Moving-block bootstrap daily portfolio returns.
    相較完全打散單日報酬，block bootstrap 能保留一部分趨勢/震盪連續性。
    """
    r = np.asarray(ret, dtype=float)
    n = len(r)
    if n < 10:
        return None
    block = int(max(1, min(block, n)))
    rng = np.random.default_rng(int(seed))
    max_start = max(1, n - block + 1)
    out = np.empty((int(n_paths), n), dtype=float)

    for i in range(int(n_paths)):
        parts = []
        need = n
        while need > 0:
            s = int(rng.integers(0, max_start))
            piece = r[s:s+block]
            if len(piece) == 0:
                piece = r
            take = min(need, len(piece))
            parts.append(piece[:take])
            need -= take
        out[i, :] = np.concatenate(parts)[:n]
    return out

def _v3623_path_metrics(paths, trading_days=252):
    """由模擬日報酬矩陣計算 CAGR / MDD / Calmar / 總報酬。"""
    if paths is None or len(paths) == 0:
        return pd.DataFrame()
    # 防止極端 bootstrap 造成 1+r <= 0
    p = np.clip(np.asarray(paths, dtype=float), -0.999, None)
    equity = np.cumprod(1.0 + p, axis=1)
    final = equity[:, -1]
    years = max(p.shape[1] / float(trading_days), 1/float(trading_days))
    cagr = (np.power(final, 1.0/years) - 1.0) * 100.0
    peaks = np.maximum.accumulate(equity, axis=1)
    dd = (equity / peaks - 1.0) * 100.0
    mdd = np.min(dd, axis=1)
    total_ret = (final - 1.0) * 100.0
    calmar = np.where(np.abs(mdd) > 1e-12, cagr / np.abs(mdd), np.nan)
    return pd.DataFrame({
        '總報酬%': total_ret,
        'CAGR%': cagr,
        'MTM_MDD%': mdd,
        'Calmar': calmar
    })

def _v3623_trade_bootstrap(fills, n_paths=3000, seed=3624):
    """封閉交易 bootstrap：主要觀察 PF / 勝率 / 平均交易報酬穩定度。
    此處不拿來代替逐日 MDD；MDD 仍以每日 MTM block bootstrap 為主。
    """
    if fills is None or fills.empty or '淨報酬%' not in fills.columns:
        return pd.DataFrame()
    rr = pd.to_numeric(fills['淨報酬%'], errors='coerce').replace([np.inf,-np.inf],np.nan).dropna().to_numpy(float)
    if len(rr) < 20:
        return pd.DataFrame()
    rng = np.random.default_rng(int(seed))
    n = len(rr)
    rows = []
    for _ in range(int(n_paths)):
        s = rng.choice(rr, size=n, replace=True)
        wins = s[s > 0]
        losses = s[s < 0]
        gp = float(wins.sum()) if len(wins) else 0.0
        gl = float(-losses.sum()) if len(losses) else 0.0
        pf = gp/gl if gl > 0 else (np.inf if gp > 0 else 0.0)
        rows.append({
            '淨PF': pf,
            '勝率%': float((s > 0).mean()*100),
            '平均交易報酬%': float(np.mean(s)),
            '中位交易報酬%': float(np.median(s))
        })
    return pd.DataFrame(rows)

def _v3623_quantile_table(df, cols):
    if df is None or df.empty:
        return pd.DataFrame()
    rows = []
    for c in cols:
        s = pd.to_numeric(df[c], errors='coerce').replace([np.inf,-np.inf],np.nan).dropna()
        if s.empty:
            continue
        rows.append({
            '指標': c,
            'P05': s.quantile(.05),
            'P10': s.quantile(.10),
            'P25': s.quantile(.25),
            'P50': s.quantile(.50),
            'P75': s.quantile(.75),
            'P90': s.quantile(.90),
            'P95': s.quantile(.95),
            '平均': s.mean()
        })
    return pd.DataFrame(rows)

def _v3623_risk_probabilities(path_df, trade_df):
    if path_df is None or path_df.empty:
        return pd.DataFrame()
    rows = [
        {'風險事件':'總報酬 < 0','機率%':float((path_df['總報酬%']<0).mean()*100)},
        {'風險事件':'CAGR < 0','機率%':float((path_df['CAGR%']<0).mean()*100)},
        {'風險事件':'MDD ≤ -20%','機率%':float((path_df['MTM_MDD%']<=-20).mean()*100)},
        {'風險事件':'MDD ≤ -25%','機率%':float((path_df['MTM_MDD%']<=-25).mean()*100)},
        {'風險事件':'MDD ≤ -30%','機率%':float((path_df['MTM_MDD%']<=-30).mean()*100)},
        {'風險事件':'MDD ≤ -35%','機率%':float((path_df['MTM_MDD%']<=-35).mean()*100)},
        {'風險事件':'Calmar < 1','機率%':float((path_df['Calmar']<1).mean()*100)},
        {'風險事件':'Calmar < 1.5','機率%':float((path_df['Calmar']<1.5).mean()*100)},
    ]
    if trade_df is not None and not trade_df.empty:
        rows.extend([
            {'風險事件':'Trade Bootstrap PF < 1','機率%':float((trade_df['淨PF']<1).mean()*100)},
            {'風險事件':'Trade Bootstrap PF < 1.5','機率%':float((trade_df['淨PF']<1.5).mean()*100)},
        ])
    return pd.DataFrame(rows)

st.divider()
st.subheader('🎲 V3.6.23 Monte Carlo｜正式策略隨機壓力測試')
st.caption(
    'V3.6.22 已鎖定 25檔 × 3.33%，V3.6.21 已鎖定 N+1 開盤成交。'
    '本版完全不改 Gate D、不改排序、不改資金配置；只把已實現的每日 MTM 報酬用 Moving-Block Bootstrap 重抽，'
    '並另外對封閉交易做 Bootstrap，觀察倒楣順序下的 CAGR、MDD、Calmar、PF 分布。'
)

if 'packs22' in globals() and isinstance(packs22, dict) and '25檔 × 3.33%' in packs22:
    led23, od23, fl23, pos23, st23 = packs22['25檔 × 3.33%']

    c1, c2, c3 = st.columns(3)
    sims23 = c1.select_slider(
        'Monte Carlo 模擬次數',
        options=[1000, 3000, 5000, 10000],
        value=3000,
        key='v3623_sims'
    )
    block23 = c2.select_slider(
        'Moving Block 長度（交易日）',
        options=[5, 10, 20, 40],
        value=20,
        key='v3623_block'
    )
    seed23 = c3.number_input(
        '隨機種子',
        min_value=1, max_value=999999,
        value=3623, step=1,
        key='v3623_seed'
    )

    daily_ret23 = _v3623_daily_returns_from_ledger(led23)

    if len(daily_ret23) >= 10:
        paths23 = _v3623_block_bootstrap_returns(
            daily_ret23,
            n_paths=int(sims23),
            block=int(block23),
            seed=int(seed23)
        )
        mc23 = _v3623_path_metrics(paths23)
        trade_mc23 = _v3623_trade_bootstrap(
            fl23,
            n_paths=int(sims23),
            seed=int(seed23)+1
        )

        # 實際正式策略數值
        actual23 = _v3620_period_stats(led23, fl23, capital)

        st.markdown('### 🏆 ㊾ Monte Carlo 分位數｜Daily MTM Block Bootstrap')
        q23 = _v3623_quantile_table(
            mc23, ['總報酬%','CAGR%','MTM_MDD%','Calmar']
        )
        st.dataframe(q23.round(4), use_container_width=True, hide_index=True)

        a1,a2,a3,a4 = st.columns(4)
        a1.metric('正式 CAGR%', f"{actual23.get('CAGR%',np.nan):.2f}")
        a2.metric('MC P10 CAGR%', f"{mc23['CAGR%'].quantile(.10):.2f}")
        a3.metric('正式 MTM MDD%', f"{actual23.get('MTM_MDD%',np.nan):.2f}")
        a4.metric('MC P10 MDD%', f"{mc23['MTM_MDD%'].quantile(.10):.2f}")

        st.markdown('### 🧪 ㊿ Trade Bootstrap｜PF / 勝率穩定度')
        tq23 = _v3623_quantile_table(
            trade_mc23, ['淨PF','勝率%','平均交易報酬%','中位交易報酬%']
        )
        st.dataframe(tq23.round(4), use_container_width=True, hide_index=True)

        st.markdown('### ⚠️ 51 極端風險機率')
        rp23 = _v3623_risk_probabilities(mc23, trade_mc23)
        st.dataframe(rp23.round(4), use_container_width=True, hide_index=True)

        # 實際值在 MC 分布中的百分位
        def pct_rank(series, value, higher_better=True):
            s = pd.to_numeric(series, errors='coerce').dropna()
            if s.empty or not np.isfinite(value):
                return np.nan
            if higher_better:
                return float((s <= value).mean()*100)
            return float((s >= value).mean()*100)

        actual_cagr = float(actual23.get('CAGR%', np.nan))
        actual_mdd = float(actual23.get('MTM_MDD%', np.nan))
        actual_cal = float(actual23.get('Calmar', np.nan))
        actual_pf = float(actual23.get('淨PF', np.nan))

        percentiles23 = pd.DataFrame([
            {'指標':'CAGR','正式值':actual_cagr,'正式值所在百分位%':pct_rank(mc23['CAGR%'],actual_cagr,True)},
            {'指標':'MTM MDD','正式值':actual_mdd,'正式值所在百分位%':pct_rank(mc23['MTM_MDD%'],actual_mdd,True)},
            {'指標':'Calmar','正式值':actual_cal,'正式值所在百分位%':pct_rank(mc23['Calmar'],actual_cal,True)},
            {'指標':'淨PF','正式值':actual_pf,'正式值所在百分位%':pct_rank(trade_mc23['淨PF'],actual_pf,True) if not trade_mc23.empty else np.nan},
        ])
        st.markdown('### 📍 52 正式績效在 Monte Carlo 分布的位置')
        st.dataframe(percentiles23.round(4), use_container_width=True, hide_index=True)

        # 正式判定：用保守 P10 / tail risk，不用平均值。
        p10_cagr = float(mc23['CAGR%'].quantile(.10))
        p10_mdd = float(mc23['MTM_MDD%'].quantile(.10))
        p10_cal = float(mc23['Calmar'].quantile(.10))
        p10_pf = float(trade_mc23['淨PF'].quantile(.10)) if not trade_mc23.empty else np.nan
        loss_prob = float((mc23['CAGR%']<0).mean()*100)
        mdd30_prob = float((mc23['MTM_MDD%']<=-30).mean()*100)

        checks23 = pd.DataFrame([
            {
                '驗證':'MC P10 CAGR > 0',
                '結果':f'{p10_cagr:.2f}%',
                '通過':p10_cagr > 0
            },
            {
                '驗證':'MC P10 MDD ≥ -35%',
                '結果':f'{p10_mdd:.2f}%',
                '通過':p10_mdd >= -35
            },
            {
                '驗證':'MC P10 Calmar ≥ 0.80',
                '結果':f'{p10_cal:.2f}',
                '通過':p10_cal >= 0.80
            },
            {
                '驗證':'Trade Bootstrap P10 PF ≥ 1.20',
                '結果':f'{p10_pf:.2f}' if np.isfinite(p10_pf) else 'NA',
                '通過':bool(np.isfinite(p10_pf) and p10_pf >= 1.20)
            },
            {
                '驗證':'模擬 CAGR < 0 機率 ≤ 10%',
                '結果':f'{loss_prob:.2f}%',
                '通過':loss_prob <= 10
            },
            {
                '驗證':'模擬 MDD ≤ -30% 機率 ≤ 25%',
                '結果':f'{mdd30_prob:.2f}%',
                '通過':mdd30_prob <= 25
            },
        ])

        st.markdown('### 🧠 53 V3.6.23 Monte Carlo 正式判定')
        st.dataframe(checks23, use_container_width=True, hide_index=True)

        if bool(checks23['通過'].all()):
            st.success(
                '🟢 V3.6.23 通過：25檔 × 3.33% 的正式策略在報酬順序被打亂、'
                '又保留部分時間連續性的 Monte Carlo 壓力下仍維持正期望。'
                '下一階段可以進入 Paper Trading / Forward Test 設計，不再回頭最佳化歷史參數。'
            )
        else:
            st.warning(
                '🟡 V3.6.23 有尾端風險條件未通過。此處不回頭調 Gate D 或3.33%；'
                '應先看是 MDD 尾部太厚、PF不穩，還是少數極端路徑拖累。'
            )

        # 簡單分布圖：只顯示 CAGR 與 MDD，避免過多圖表
        dist23 = pd.DataFrame({
            'CAGR%': mc23['CAGR%'],
            'MTM_MDD%': mc23['MTM_MDD%']
        })
        st.markdown('### 📊 54 Monte Carlo 分布')
        hist_choice23 = st.radio(
            '查看分布',
            ['CAGR%','MTM_MDD%'],
            horizontal=True,
            key='v3623_hist_choice'
        )
        st.bar_chart(
            np.histogram(
                dist23[hist_choice23].replace([np.inf,-np.inf],np.nan).dropna(),
                bins=40
            )[0]
        )

        st.download_button(
            '⬇️ 下載 V3.6.23 Monte Carlo 路徑統計',
            mc23.to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig'),
            'V3.6.23_monte_carlo_paths.csv',
            'text/csv',
            key='dl_v3623_mc'
        )
        if not trade_mc23.empty:
            st.download_button(
                '⬇️ 下載 V3.6.23 Trade Bootstrap',
                trade_mc23.to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig'),
                'V3.6.23_trade_bootstrap.csv',
                'text/csv',
                key='dl_v3623_trade'
            )
    else:
        st.warning('V3.6.23：每日 MTM 報酬樣本不足，無法進行 Monte Carlo。')
else:
    st.info('請先完成 V3.6.22；V3.6.23 會沿用 25檔 × 3.33% 的 N+1開盤正式帳本。')



# ============================================================
# 🧪 V3.6.24 Forward Test / Paper Trading
# 完全鎖定正式規則，不再回頭最佳化歷史：
#   Gate D = 90~94 + 站上 MA200
#   排序 = 分數 → 成交額 → 近 MA200
#   成交 = 訊號日收盤後成立，N+1 開盤
#   最大持股 = 25
#   單筆目標資金 = 3.33%
#   排隊 = 0（無槽位直接拒絕）
#   出場 = 進場日起 D40 收盤 / 盤中硬停損 -12%
#   成本 = 沿用 V3.6.23 畫面設定 fee / tax / slip
#
# 重要：
# 1) 只有「先封存的訊號」才可在未來成交，禁止事後補造訊號。
# 2) 忘記隔日開 App 沒關係：只要訊號已先封存，可用之後取得的歷史日K
#    重建真正 N+1 開盤成交與後續持倉路徑。
# 3) Streamlit Cloud 本機磁碟不是永久資料庫，所以同時提供 JSON 匯出/匯入。
# ============================================================

V3624_VERSION = 'V3.6.24'
V3624_STATE_FILE = 'paper_state_v3624.json'
V3624_INITIAL_CAPITAL = 1_000_000.0
V3624_MAX_POS = 25
V3624_POS_PCT = 3.33
V3624_HOLD = 40
V3624_HARD_STOP = 12.0

# V3.6.23 / V3.6.21 已驗證基準，用來監控 Forward 是否逐步偏離。
V3624_BACKTEST_CAGR = 49.1325
V3624_BACKTEST_MDD = -19.9192
V3624_BACKTEST_PF = 2.2044
V3624_MC_P10_CAGR = 19.05
V3624_MC_P10_MDD = -24.19
V3624_MC_P10_CALMAR = 0.876
V3624_MC_P10_PF = 1.6435

def _v3624_empty_state():
    return {
        'version': V3624_VERSION,
        'created_at': taiwan_time_text(),
        'initial_capital': V3624_INITIAL_CAPITAL,
        'cash': V3624_INITIAL_CAPITAL,
        'positions': {},
        'pending': [],
        'signals': [],
        'orders': [],
        'fills': [],
        'ledger': [],
        'signal_keys': [],
        'last_sync': '',
        'locked_rules': {
            'Gate': 'D｜90~94＋站上MA200',
            'rank': '分數→成交額→近MA200',
            'execution': 'N+1開盤',
            'max_positions': V3624_MAX_POS,
            'position_pct': V3624_POS_PCT,
            'queue_days': 0,
            'hold_days': V3624_HOLD,
            'hard_stop_pct': V3624_HARD_STOP,
        }
    }

def _v3624_json_default(x):
    if isinstance(x, (pd.Timestamp, datetime)):
        return pd.Timestamp(x).isoformat()
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating,)):
        return None if not np.isfinite(float(x)) else float(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    return str(x)

def _v3624_save_state(state):
    try:
        with open(V3624_STATE_FILE, 'w', encoding='utf-8') as f:
            _v3624_json.dump(state, f, ensure_ascii=False, indent=2, default=_v3624_json_default)
        return True, ''
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'

def _v3624_load_state():
    # session_state 優先，避免每次 rerun 都重複打 Google Drive API
    if 'v3624_state' in st.session_state and isinstance(st.session_state['v3624_state'], dict):
        return st.session_state['v3624_state']

    # V3.6.25：若已設定 Google Drive，優先把雲端帳本視為 Source of Truth。
    try:
        if '_v3625_drive_download_state' in globals():
            drive_state, drive_msg = _v3625_drive_download_state()
            if isinstance(drive_state, dict):
                st.session_state['v3624_state'] = drive_state
                st.session_state['v3625_last_drive_status'] = {
                    'ok': True, 'configured': True, 'message': drive_msg
                }
                _v3624_save_state(drive_state)
                return drive_state
    except Exception:
        pass

    # Drive 未設定或暫時無法讀取時，再使用本機備援。
    try:
        if os.path.exists(V3624_STATE_FILE):
            with open(V3624_STATE_FILE, 'r', encoding='utf-8') as f:
                state=_v3624_json.load(f)
            if isinstance(state, dict) and (
                str(state.get('version','')).startswith('V3.6.24') or
                str(state.get('version','')).startswith('V3.6.25')
            ):
                state['version'] = 'V3.6.25'
                st.session_state['v3624_state']=state
                return state
    except Exception:
        pass

    state=_v3624_empty_state()
    state['version']='V3.6.25'
    st.session_state['v3624_state']=state
    return state

def _v3624_commit(state):
    state['last_sync']=taiwan_time_text()
    state['version']='V3.6.25'
    st.session_state['v3624_state']=state
    local_ok, local_err = _v3624_save_state(state)

    # V3.6.25：若 Drive 已設定，每次封存 / 同步 / 重設後自動上傳。
    try:
        if '_v3625_drive_upload_state' in globals():
            cfg = _v3625_drive_config()
            if cfg.get('configured'):
                _v3625_drive_upload_state(state)
    except Exception as e:
        st.session_state['v3625_last_drive_status'] = {
            'ok': False, 'configured': True,
            'message': f'{type(e).__name__}: {e}'
        }
    return local_ok, local_err

def _v3624_num(v, default=np.nan):
    x=pd.to_numeric(v, errors='coerce')
    return float(x) if pd.notna(x) and np.isfinite(float(x)) else default

def _v3624_stock_df(symbol, market=''):
    try:
        d=get_stock_data(str(symbol).zfill(4), market or stock_market(symbol))
        if d is None or d.empty:
            return pd.DataFrame()
        z=d.copy()
        z.index=pd.DatetimeIndex(z.index).tz_localize(None).normalize()
        return z[~z.index.duplicated(keep='last')].sort_index()
    except Exception:
        return pd.DataFrame()

def _v3624_first_bar_after(df, signal_date):
    if df is None or df.empty:
        return None, None
    s=pd.Timestamp(signal_date).normalize()
    idx=pd.DatetimeIndex(df.index).normalize()
    pos=np.where(idx>s)[0]
    if len(pos)==0:
        return None, None
    i=int(pos[0])
    return i, pd.Timestamp(idx[i]).normalize()

def _v3624_price_on_or_before(df, dt, col='Close'):
    if df is None or df.empty or col not in df.columns:
        return np.nan
    t=pd.Timestamp(dt).normalize()
    z=df[pd.DatetimeIndex(df.index).normalize()<=t]
    if z.empty:
        return np.nan
    return _v3624_num(z[col].iloc[-1])

def _v3624_current_equity(state, asof=None):
    asof=pd.Timestamp(asof or taiwan_now().date()).normalize()
    mv=0.0
    for sym,p in state.get('positions',{}).items():
        d=_v3624_stock_df(sym,p.get('market',''))
        px=_v3624_price_on_or_before(d,asof,'Close')
        if not np.isfinite(px):
            px=_v3624_num(p.get('last_px'), _v3624_num(p.get('entry_exec'),0))
        p['last_px']=float(px)
        mv += float(p.get('shares',0))*float(px)
    return float(state.get('cash',0))+mv, mv

def _v3624_process_open_positions(state, fee, tax, slip):
    """只處理已成交部位；用已經發生的日K檢查 -12% 與 D40。"""
    today=pd.Timestamp(taiwan_now().date()).normalize()
    close_syms=[]
    for sym,p in list(state.get('positions',{}).items()):
        d=_v3624_stock_df(sym,p.get('market',''))
        if d.empty:
            continue
        entry_date=pd.Timestamp(p['entry_date']).normalize()
        idx=pd.DatetimeIndex(d.index).normalize()
        loc=np.where(idx==entry_date)[0]
        if len(loc)==0:
            continue
        i=int(loc[-1])
        last_done=np.where(idx<=today)[0]
        if len(last_done)==0:
            continue
        last_i=int(last_done[-1])
        end=min(i+V3624_HOLD,last_i)
        if end<i:
            continue

        stop_price=float(p['entry_raw'])*(1-V3624_HARD_STOP/100)
        exit_i=None; exit_raw=None; reason=''
        for j in range(i,end+1):
            low=_v3624_num(d['Low'].iloc[j]) if 'Low' in d.columns else np.nan
            op=_v3624_num(d['Open'].iloc[j]) if 'Open' in d.columns else np.nan
            if np.isfinite(low) and low<=stop_price:
                # 若隔日跳空低於停損價，以開盤價成交；否則以停損價成交。
                exit_raw=min(stop_price,op) if np.isfinite(op) and op<stop_price else stop_price
                exit_i=j
                reason='硬停損12%'
                break

        if exit_i is None and last_i>=i+V3624_HOLD:
            exit_i=i+V3624_HOLD
            exit_raw=_v3624_num(d['Close'].iloc[exit_i])
            reason='D40固定出場'

        if exit_i is None or not np.isfinite(exit_raw):
            p['last_px']=_v3624_num(d['Close'].iloc[last_i],p.get('last_px',p['entry_raw']))
            continue

        exit_date=pd.Timestamp(idx[exit_i]).normalize()
        exit_exec=float(exit_raw)*(1-slip/100)
        gross=float(p['shares'])*exit_exec
        sell_cost=gross*((fee+tax)/100)
        proceeds=gross-sell_cost
        state['cash']=float(state.get('cash',0))+proceeds
        pnl=proceeds-float(p['entry_total_cost'])
        state['fills'].append({
            '股票':sym,'名稱':p.get('name',''),'狀態':'CLOSED',
            '訊號日':p['signal_date'],'進場日':p['entry_date'],
            '出場日':exit_date.strftime('%Y-%m-%d'),
            '技術分數':p.get('score',np.nan),
            '進場價':p['entry_raw'],'出場價':float(exit_raw),
            '股數':p['shares'],'投入成本':p['entry_total_cost'],
            '賣出淨收入':proceeds,'淨損益':pnl,
            '淨報酬%':pnl/float(p['entry_total_cost'])*100 if p['entry_total_cost'] else 0,
            '出場原因':reason,'持有交易日':int(exit_i-i)
        })
        state['orders'].append({
            '日期':exit_date.strftime('%Y-%m-%d'),'股票':sym,
            '事件':'CLOSE','結果':'FILLED','原因':reason
        })
        close_syms.append(sym)

    for sym in close_syms:
        state['positions'].pop(sym,None)

def _v3624_process_pending(state, fee, tax, slip):
    """只成交「過去已封存」的訊號，成交價固定取下一交易日 Open。"""
    remain=[]
    # 固定正式排序；pending 在封存時已保存 rank_key，這裡仍重排一次。
    pend=sorted(
        state.get('pending',[]),
        key=lambda r:(-float(r.get('score',0)),-float(r.get('liq',0)),float(r.get('dist',999999)))
    )
    for r in pend:
        sym=str(r['symbol']).zfill(4)
        d=_v3624_stock_df(sym,r.get('market',''))
        i,entry_date=_v3624_first_bar_after(d,r['signal_date'])
        if i is None:
            remain.append(r)
            continue

        # 若同股仍持有，正式規則拒絕，不排隊。
        if sym in state.get('positions',{}):
            state['orders'].append({
                '日期':entry_date.strftime('%Y-%m-%d'),'股票':sym,
                '事件':'NEW_SIGNAL','結果':'REJECTED_DUPLICATE',
                '原因':'同股仍在持有中'
            })
            continue

        if len(state.get('positions',{}))>=V3624_MAX_POS:
            state['orders'].append({
                '日期':entry_date.strftime('%Y-%m-%d'),'股票':sym,
                '事件':'NEW_SIGNAL','結果':'REJECTED_SLOT',
                '原因':'25槽位已滿；正式規則 queue=0'
            })
            continue

        entry_raw=_v3624_num(d['Open'].iloc[i]) if 'Open' in d.columns else np.nan
        if not np.isfinite(entry_raw) or entry_raw<=0:
            state['orders'].append({
                '日期':entry_date.strftime('%Y-%m-%d'),'股票':sym,
                '事件':'NEW_SIGNAL','結果':'REJECTED_BAD_PRICE',
                '原因':'N+1開盤價無效'
            })
            continue

        eq,_=_v3624_current_equity(state,entry_date)
        target=eq*V3624_POS_PCT/100
        entry_exec=entry_raw*(1+slip/100)
        unit_cost=entry_exec*(1+fee/100)
        alloc=min(target,float(state.get('cash',0)))
        if alloc<=0 or alloc<target*0.20:
            state['orders'].append({
                '日期':entry_date.strftime('%Y-%m-%d'),'股票':sym,
                '事件':'NEW_SIGNAL','結果':'REJECTED_CASH',
                '原因':'可用現金不足'
            })
            continue

        shares=alloc/unit_cost
        total=shares*unit_cost
        state['cash']=float(state.get('cash',0))-total
        state.setdefault('positions',{})[sym]={
            'symbol':sym,'name':r.get('name',''),'market':r.get('market',''),
            'signal_date':r['signal_date'],'entry_date':entry_date.strftime('%Y-%m-%d'),
            'entry_raw':entry_raw,'entry_exec':entry_exec,
            'entry_total_cost':total,'shares':shares,
            'score':r.get('score',np.nan),'last_px':entry_raw
        }
        state['orders'].append({
            '日期':entry_date.strftime('%Y-%m-%d'),'股票':sym,
            '事件':'NEW_SIGNAL','結果':'ACCEPTED_OPEN',
            '原因':'Gate D / 正式排序 / N+1開盤 / 槽位 / 資金檢查'
        })
    state['pending']=remain

def _v3624_append_ledger(state):
    today=pd.Timestamp(taiwan_now().date()).normalize()
    eq,mv=_v3624_current_equity(state,today)
    row={
        '日期':today.strftime('%Y-%m-%d'),
        '現金':float(state.get('cash',0)),
        '持倉市值':mv,'MTM權益':eq,
        '持股檔數':len(state.get('positions',{})),
        '可用槽位':V3624_MAX_POS-len(state.get('positions',{})),
        '資金使用率%':mv/eq*100 if eq>0 else np.nan
    }
    led=[x for x in state.get('ledger',[]) if str(x.get('日期',''))!=row['日期']]
    led.append(row)
    state['ledger']=sorted(led,key=lambda x:x.get('日期',''))

def _v3624_today_candidates(result_df):
    """只有台灣交易日 13:30 後，且技術資料日=今天，才允許封存新訊號。"""
    now=taiwan_now()
    today=now.strftime('%Y-%m-%d')
    if now.weekday()>=5:
        return pd.DataFrame(), '今天是週末，不封存新訊號。'
    if now.hour*60+now.minute<810:
        return pd.DataFrame(), '尚未收盤；V3.6.24 必須等 13:30 後才封存 Gate D 訊號。'
    c=_v3611_current_candidates(result_df)
    if c is None or c.empty:
        return pd.DataFrame(), '今日沒有 Gate D 候選。'
    if '技術資料日' not in c.columns:
        return pd.DataFrame(), '缺少「技術資料日」，為避免 Look-ahead / 舊資料誤封存，本日不記錄。'
    dates=c['技術資料日'].astype(str).str[:10]
    c=c[dates==today].copy()
    if c.empty:
        return pd.DataFrame(), f'目前技術資料尚未更新到 {today}，不封存舊訊號。'
    return c, 'OK'

def _v3624_seal_today_signals(state, result_df):
    c,msg=_v3624_today_candidates(result_df)
    if c.empty:
        return 0,msg
    today=taiwan_now().strftime('%Y-%m-%d')
    keys=set(state.get('signal_keys',[]))
    n=0
    for _,r in c.iterrows():
        sym=str(r.get('股票','')).zfill(4)
        key=f'{today}|{sym}'
        if key in keys:
            continue
        score=_v3624_num(r.get('黑嚕嚕分數',r.get('綜合分數',np.nan)),0)
        liq=_v3624_num(r.get('估算成交額',0),0)
        dist=abs(_v3624_num(r.get('距MA200%',999999),999999))
        rec={
            'signal_date':today,'symbol':sym,'name':str(r.get('名稱','')),
            'market':str(r.get('市場','')),'score':score,'liq':liq,'dist':dist,
            'signal_price':_v3624_num(r.get('價格',np.nan)),
            'ma200':_v3624_num(r.get('MA200',np.nan)),
            'sealed_at':taiwan_time_text()
        }
        state.setdefault('signals',[]).append(rec)
        state.setdefault('pending',[]).append(rec.copy())
        keys.add(key); n+=1
    state['signal_keys']=sorted(keys)
    return n,f'已封存 {n} 筆今日 Gate D 訊號。'

def _v3624_metrics(state):
    led=pd.DataFrame(state.get('ledger',[]))
    fills=pd.DataFrame(state.get('fills',[]))
    initial=float(state.get('initial_capital',V3624_INITIAL_CAPITAL))
    out={
        '天數':0,'總報酬%':np.nan,'CAGR%':np.nan,'MDD%':np.nan,'Calmar':np.nan,
        'PF':np.nan,'勝率%':np.nan,'完成交易':0,'目前持股':len(state.get('positions',{}))
    }
    if not led.empty and 'MTM權益' in led.columns:
        led['日期']=pd.to_datetime(led['日期'],errors='coerce')
        led['MTM權益']=pd.to_numeric(led['MTM權益'],errors='coerce')
        led=led.dropna(subset=['日期','MTM權益']).sort_values('日期')
        if not led.empty:
            eq=led['MTM權益'].astype(float)
            out['天數']=max((led['日期'].iloc[-1]-led['日期'].iloc[0]).days,0)
            out['總報酬%']=(eq.iloc[-1]/initial-1)*100
            peak=eq.cummax()
            dd=(eq/peak-1)*100
            out['MDD%']=float(dd.min())
            if out['天數']>=30 and eq.iloc[-1]>0:
                years=max(out['天數']/365.25,1/365.25)
                out['CAGR%']=(eq.iloc[-1]/initial)**(1/years)*100-100
                out['Calmar']=out['CAGR%']/abs(out['MDD%']) if out['MDD%']<0 else np.nan
    if not fills.empty and '淨損益' in fills.columns:
        pnl=pd.to_numeric(fills['淨損益'],errors='coerce').dropna()
        out['完成交易']=len(pnl)
        if len(pnl):
            out['勝率%']=(pnl>0).mean()*100
            gp=pnl[pnl>0].sum(); gl=-pnl[pnl<0].sum()
            out['PF']=gp/gl if gl>0 else np.inf
    return out

def _v3624_forward_status(m):
    """早期樣本不夠時只做監控，不做策略生死判決。"""
    n=int(m.get('完成交易',0) or 0)
    days=int(m.get('天數',0) or 0)
    if n<30 or days<60:
        return '⚪ 累積期','至少累積 60 日且 30 筆完成交易後，再開始做 Forward 偏離判定。'
    pf=m.get('PF',np.nan); mdd=m.get('MDD%',np.nan)
    if (pd.notna(pf) and pf<1.0) or (pd.notna(mdd) and mdd<=-35):
        return '🔴 異常','已進入嚴格異常區：PF<1 或 MDD≤-35%。先停止加碼並檢查資料/執行/市場結構，不自動改參數。'
    if (pd.notna(pf) and pf<1.5) or (pd.notna(mdd) and mdd<=-30):
        return '🟠 警戒','已落入壓力區：PF<1.5 或 MDD≤-30%。保留策略參數，增加觀察。'
    if pd.notna(mdd) and mdd<=-25:
        return '🟡 注意','MDD 已低於 -25%，但仍未到 -30% 嚴格警戒線。'
    return '🟢 正常','Forward 尚未出現超出目前 Monte Carlo 壓力範圍的明顯異常。'



# ============================================================
# ☁️ V3.6.25 Google Drive 永久 Forward 帳本
# 以 Google Drive 作為 Forward JSON 的雲端 Source of Truth。
# Streamlit Cloud 本機 JSON 仍保留作為快取 / 第二層備援。
#
# 需要 requirements.txt：
# google-api-python-client
# google-auth
# google-auth-httplib2
#
# 需要 Streamlit secrets：
#
# [google_drive]
# folder_id = "你的Google Drive資料夾ID"
# state_filename = "V3.6.25_forward_state.json"
#
# [gdrive_service_account]
# type = "service_account"
# project_id = "..."
# private_key_id = "..."
# private_key = """-----BEGIN PRIVATE KEY-----
# ...
# -----END PRIVATE KEY-----
# """
# client_email = "...@...iam.gserviceaccount.com"
# client_id = "..."
# auth_uri = "https://accounts.google.com/o/oauth2/auth"
# token_uri = "https://oauth2.googleapis.com/token"
# auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
# client_x509_cert_url = "..."
#
# 最後要把 Google Drive 目標資料夾分享給 client_email，權限設「編輯者」。
# ============================================================

V3625_DRIVE_DEFAULT_FILENAME = 'V3.6.25_forward_state.json'

def _v3625_drive_config():
    try:
        gd = dict(st.secrets.get('google_drive', {}))
        sa = dict(st.secrets.get('gdrive_service_account', {}))
    except Exception:
        gd, sa = {}, {}
    folder_id = str(gd.get('folder_id', '')).strip()
    filename = str(gd.get('state_filename', V3625_DRIVE_DEFAULT_FILENAME)).strip() or V3625_DRIVE_DEFAULT_FILENAME
    configured = bool(folder_id and sa.get('client_email') and sa.get('private_key'))
    return {
        'configured': configured,
        'folder_id': folder_id,
        'filename': filename,
        'service_account': sa,
        'client_email': str(sa.get('client_email', '')).strip(),
    }

def _v3625_drive_imports_ok():
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseUpload
        return True, ''
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'

def _v3625_drive_service():
    cfg = _v3625_drive_config()
    if not cfg['configured']:
        raise RuntimeError('Google Drive 尚未完成 Streamlit Secrets 設定')
    ok, err = _v3625_drive_imports_ok()
    if not ok:
        raise RuntimeError(
            '缺少 Google Drive Python 套件。請更新 requirements.txt：'
            'google-api-python-client / google-auth / google-auth-httplib2。'
            f' 原始錯誤：{err}'
        )
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    scopes = ['https://www.googleapis.com/auth/drive']
    creds = Credentials.from_service_account_info(
        cfg['service_account'],
        scopes=scopes
    )
    return build('drive', 'v3', credentials=creds, cache_discovery=False), cfg

def _v3625_drive_find_state_file(service, cfg):
    safe_name = cfg['filename'].replace("'", "\\'")
    q = (
        f"name = '{safe_name}' and "
        f"'{cfg['folder_id']}' in parents and trashed = false"
    )
    resp = service.files().list(
        q=q,
        spaces='drive',
        fields='files(id,name,modifiedTime,size)',
        pageSize=10,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    files = resp.get('files', [])
    if not files:
        return None
    files = sorted(files, key=lambda x: x.get('modifiedTime', ''), reverse=True)
    return files[0]

def _v3625_drive_upload_state(state):
    import io
    cfg = _v3625_drive_config()
    if not cfg['configured']:
        st.session_state['v3625_last_drive_status'] = {
            'ok': False, 'configured': False,
            'message': '尚未設定 Google Drive 永久帳本'
        }
        return False, 'Drive not configured'
    try:
        service, cfg = _v3625_drive_service()
        payload = _v3624_json.dumps(
            state, ensure_ascii=False, indent=2,
            default=_v3624_json_default
        ).encode('utf-8')
        from googleapiclient.http import MediaIoBaseUpload
        media = MediaIoBaseUpload(
            io.BytesIO(payload),
            mimetype='application/json',
            resumable=False
        )
        existing = _v3625_drive_find_state_file(service, cfg)
        if existing:
            saved = service.files().update(
                fileId=existing['id'],
                media_body=media,
                fields='id,name,modifiedTime,size',
                supportsAllDrives=True
            ).execute()
            action = '更新'
        else:
            saved = service.files().create(
                body={
                    'name': cfg['filename'],
                    'parents': [cfg['folder_id']],
                    'mimeType': 'application/json'
                },
                media_body=media,
                fields='id,name,modifiedTime,size',
                supportsAllDrives=True
            ).execute()
            action = '建立'
        msg = (
            f"Google Drive {action}成功｜{saved.get('name', cfg['filename'])}｜"
            f"{saved.get('modifiedTime','')}"
        )
        st.session_state['v3625_last_drive_status'] = {
            'ok': True, 'configured': True,
            'message': msg,
            'file_id': saved.get('id',''),
            'modifiedTime': saved.get('modifiedTime','')
        }
        return True, msg
    except Exception as e:
        msg = f'{type(e).__name__}: {e}'
        st.session_state['v3625_last_drive_status'] = {
            'ok': False, 'configured': True,
            'message': msg
        }
        return False, msg

def _v3625_drive_download_state():
    cfg = _v3625_drive_config()
    if not cfg['configured']:
        return None, 'Drive not configured'
    try:
        service, cfg = _v3625_drive_service()
        existing = _v3625_drive_find_state_file(service, cfg)
        if not existing:
            return None, 'Drive 尚未有 Forward 帳本'
        content = service.files().get_media(
            fileId=existing['id'],
            supportsAllDrives=True
        ).execute()
        if isinstance(content, bytes):
            state = _v3624_json.loads(content.decode('utf-8'))
        else:
            state = _v3624_json.loads(bytes(content).decode('utf-8'))
        if not isinstance(state, dict):
            return None, 'Drive JSON 格式錯誤'
        # V3.6.24 / V3.6.25 共用同一 Forward 帳本 schema，允許版本升級延續。
        if not str(state.get('version','')).startswith('V3.6.24') and not str(state.get('version','')).startswith('V3.6.25'):
            return None, f"Drive 帳本版本不相容：{state.get('version')}"
        state['version'] = 'V3.6.25'
        return state, (
            f"Drive 讀取成功｜{existing.get('name','')}｜"
            f"{existing.get('modifiedTime','')}"
        )
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'

def _v3625_drive_test():
    cfg = _v3625_drive_config()
    if not cfg['configured']:
        return False, '尚未設定 Streamlit Secrets'
    try:
        service, cfg = _v3625_drive_service()
        folder = service.files().get(
            fileId=cfg['folder_id'],
            fields='id,name,mimeType',
            supportsAllDrives=True
        ).execute()
        existing = _v3625_drive_find_state_file(service, cfg)
        file_text = '尚未建立 Forward JSON' if not existing else (
            f"已存在 {existing.get('name')}｜{existing.get('modifiedTime','')}"
        )
        return True, f"資料夾：{folder.get('name','')}｜{file_text}"
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


st.divider()
st.subheader('☁️ V3.6.25 Forward Test / Paper Trading｜Google Drive 永久帳本')
st.success('✅ Build：V3.6.25｜Forward 規則鎖定｜支援 Google Drive 自動永久備份')

st.caption(
    'V3.6.23 已完成 Monte Carlo；本區不再最佳化 Gate D、排序、25檔、3.33% 或出場規則。'
    '只有「先封存」的訊號才能在 N+1 開盤模擬成交，避免任何事後補訊號。'
)

import json as _v3624_json


try:
    _v3624_json.dumps({'ok': True})
except Exception as _v3624_json_err:
    st.error(f'V3.6.24.2 JSON 自檢失敗：{type(_v3624_json_err).__name__}: {_v3624_json_err}')
    st.stop()

state24=_v3624_load_state()

st.markdown('### ☁️ Google Drive 永久帳本狀態')
cfg25=_v3625_drive_config()
g1,g2,g3=st.columns(3)
g1.metric('Drive永久備份','已設定' if cfg25['configured'] else '尚未設定')
g2.metric('雲端檔名',cfg25['filename'])
g3.metric('Service Account',cfg25['client_email'] if cfg25['client_email'] else '—')

pkg_ok25,pkg_err25=_v3625_drive_imports_ok()
if not pkg_ok25:
    st.error(
        'V3.6.25 尚缺 Google Drive 套件。請把我提供的 requirements.txt 一起覆蓋後重新部署。'
    )

last_drive25=st.session_state.get('v3625_last_drive_status',{})
if last_drive25:
    if last_drive25.get('ok'):
        st.success('Drive：'+str(last_drive25.get('message','同步成功')))
    elif last_drive25.get('configured'):
        st.warning('Drive：'+str(last_drive25.get('message','同步失敗')))

dc1,dc2,dc3=st.columns(3)
if dc1.button('🔌 測試 Google Drive 連線',key='v3625_drive_test',use_container_width=True):
    ok,msg=_v3625_drive_test()
    if ok: st.success(msg)
    else: st.error(msg)

if dc2.button('☁️ 立即備份目前帳本到 Drive',key='v3625_drive_upload',use_container_width=True):
    ok,msg=_v3625_drive_upload_state(state24)
    if ok: st.success(msg)
    else: st.error(msg)

if dc3.button('♻️ 從 Drive 還原帳本',key='v3625_drive_restore',use_container_width=True):
    ds,msg=_v3625_drive_download_state()
    if isinstance(ds,dict):
        st.session_state['v3624_state']=ds
        _v3624_save_state(ds)
        st.success(msg)
        st.rerun()
    else:
        st.error(msg)

if not cfg25['configured']:
    st.info(
        'Google Drive 要由「部署中的 Streamlit App」自動上傳，必須另外設定 Google Drive API / Service Account。'
        '你剛剛在 ChatGPT 連接的 Google Drive 是 ChatGPT 的連接權限，不會自動傳給 Streamlit Cloud。'
        '設定完成後，本區的「Drive永久備份」會變成已設定，而且每次封存與同步都會自動覆寫同一份 JSON。'
    )

# 匯入 / 匯出與帳本初始化
with st.expander('💾 Forward 帳本備份 / 還原（建議每次同步後下載一份）', expanded=False):
    up24=st.file_uploader('匯入 V3.6.24 JSON 帳本',type=['json'],key='v3624_upload')
    if up24 is not None and st.button('♻️ 還原這份 Forward 帳本',key='v3624_restore'):
        try:
            imported=_v3624_json.loads(up24.getvalue().decode('utf-8'))
            if imported.get('version')!=V3624_VERSION:
                st.error(f"版本不符：{imported.get('version')}，需要 {V3624_VERSION}")
            else:
                st.session_state['v3624_state']=imported
                _v3624_save_state(imported)
                st.success('Forward 帳本已還原。')
                st.rerun()
        except Exception as e:
            st.error(f'帳本匯入失敗：{type(e).__name__}: {e}')

    state_json=_v3624_json.dumps(state24,ensure_ascii=False,indent=2,default=_v3624_json_default).encode('utf-8')
    st.download_button(
        '⬇️ 下載 V3.6.24 Forward 完整帳本 JSON',
        state_json,
        'V3.6.24_forward_state.json',
        'application/json',
        key='v3624_download_state'
    )

    if st.button('🧨 清空 Forward 帳本重新開始',key='v3624_reset'):
        st.session_state['v3624_state']=_v3624_empty_state()
        _v3624_save_state(st.session_state['v3624_state'])
        st.success('已建立全新的 Forward 帳本；歷史回測參數沒有變動。')
        st.rerun()

# 沿用前面畫面的成本設定；若變數不存在才使用正式預設值
fee24=float(globals().get('fee',0.1425))
tax24=float(globals().get('tax',0.30))
slip24=float(globals().get('slip',0.10))

c1,c2,c3,c4=st.columns(4)
c1.metric('正式最大持股','25 檔')
c2.metric('單筆目標資金','3.33%')
c3.metric('成交假設','N+1 開盤')
c4.metric('出場規則','D40 / -12%')

cand24,msg24=_v3624_today_candidates(result if 'result' in globals() else pd.DataFrame())
if msg24=='OK':
    st.success(f'今日可封存 Gate D 訊號：{len(cand24)} 筆。封存後內容即固定，不會因之後價格變動回寫。')
    show24=[c for c in ['股票','名稱','黑嚕嚕分數','價格','MA200','距MA200%','估算成交額','技術資料日'] if c in cand24.columns]
    if show24:
        st.dataframe(cand24[show24],use_container_width=True,hide_index=True)
else:
    st.info(msg24)

b1,b2=st.columns(2)
if b1.button('🔒 封存今日 Gate D 訊號',key='v3624_seal',use_container_width=True):
    n24,txt24=_v3624_seal_today_signals(state24,result if 'result' in globals() else pd.DataFrame())
    _v3624_commit(state24)
    if n24>0: st.success(txt24)
    else: st.warning(txt24)
    st.rerun()

if b2.button('🔄 同步 Forward 帳本 / N+1成交 / MTM',key='v3624_sync',use_container_width=True):
    # 順序固定：先處理舊持倉出場，再處理先前已封存 pending 訊號，再做今日 MTM。
    _v3624_process_open_positions(state24,fee24,tax24,slip24)
    _v3624_process_pending(state24,fee24,tax24,slip24)
    _v3624_append_ledger(state24)
    ok24,err24=_v3624_commit(state24)
    if ok24: st.success('Forward 帳本同步完成。')
    else: st.warning(f'帳本已在本次工作階段更新，但本機 JSON 儲存失敗：{err24}')
    st.rerun()

m24=_v3624_metrics(state24)
status24,status_text24=_v3624_forward_status(m24)

st.markdown('### 📒 55 Forward 即時帳本')
a,b,c,d,e,f=st.columns(6)
a.metric('已封存訊號',len(state24.get('signals',[])))
b.metric('待 N+1 成交',len(state24.get('pending',[])))
c.metric('目前持股',len(state24.get('positions',{})))
d.metric('完成交易',m24['完成交易'])
e.metric('現金',f"{float(state24.get('cash',0)):,.0f}")
eq24,_mv24=_v3624_current_equity(state24)
f.metric('目前 MTM 權益',f'{eq24:,.0f}')

st.caption(
    f"帳本建立：{state24.get('created_at','')}｜最後同步：{state24.get('last_sync','尚未同步')}｜"
    f"成本：手續費 {fee24:g}% / 賣出稅 {tax24:g}% / 單邊滑價 {slip24:g}%"
)

st.markdown('### 📊 56 Forward 績效 vs 歷史 / Monte Carlo')
cmp24=pd.DataFrame([
    {'基準':'歷史正式回測','CAGR%':V3624_BACKTEST_CAGR,'MDD%':V3624_BACKTEST_MDD,'PF':V3624_BACKTEST_PF},
    {'基準':'Monte Carlo P10','CAGR%':V3624_MC_P10_CAGR,'MDD%':V3624_MC_P10_MDD,'PF':V3624_MC_P10_PF},
    {'基準':'Forward 實際','CAGR%':m24['CAGR%'],'MDD%':m24['MDD%'],'PF':m24['PF']},
])
st.dataframe(cmp24,use_container_width=True,hide_index=True)

x1,x2,x3,x4,x5=st.columns(5)
x1.metric('Forward 總報酬%',f"{m24['總報酬%']:.2f}" if pd.notna(m24['總報酬%']) else '-')
x2.metric('Forward CAGR%',f"{m24['CAGR%']:.2f}" if pd.notna(m24['CAGR%']) else '樣本累積中')
x3.metric('Forward MDD%',f"{m24['MDD%']:.2f}" if pd.notna(m24['MDD%']) else '-')
x4.metric('Forward PF',f"{m24['PF']:.2f}" if pd.notna(m24['PF']) and np.isfinite(m24['PF']) else '-')
x5.metric('Forward 勝率%',f"{m24['勝率%']:.1f}" if pd.notna(m24['勝率%']) else '-')

if status24.startswith('🟢'):
    st.success(f'{status24}｜{status_text24}')
elif status24.startswith('⚪'):
    st.info(f'{status24}｜{status_text24}')
elif status24.startswith('🟡') or status24.startswith('🟠'):
    st.warning(f'{status24}｜{status_text24}')
else:
    st.error(f'{status24}｜{status_text24}')

st.markdown('### 🧾 57 Forward 訂單生命週期')
od24=pd.DataFrame(state24.get('orders',[]))
if od24.empty:
    st.info('目前尚無 Forward 訂單事件。先在交易日收盤後封存 Gate D 訊號。')
else:
    st.dataframe(od24.tail(300).iloc[::-1],use_container_width=True,hide_index=True)
    st.download_button(
        '⬇️ 下載 Forward 訂單事件 CSV',
        od24.to_csv(index=False,encoding='utf-8-sig').encode('utf-8-sig'),
        'V3.6.24_forward_orders.csv','text/csv',key='v3624_dl_orders'
    )

st.markdown('### 💼 58 Forward 持股 / 已完成交易')
pos_rows=[]
for sym,p in state24.get('positions',{}).items():
    d=_v3624_stock_df(sym,p.get('market',''))
    px=_v3624_price_on_or_before(d,taiwan_now().date(),'Close')
    if not np.isfinite(px): px=_v3624_num(p.get('last_px'),p.get('entry_raw',np.nan))
    mv=float(p.get('shares',0))*px if np.isfinite(px) else np.nan
    pnl=mv-float(p.get('entry_total_cost',0)) if np.isfinite(mv) else np.nan
    pos_rows.append({
        '股票':sym,'名稱':p.get('name',''),'訊號日':p.get('signal_date',''),
        'N+1進場日':p.get('entry_date',''),'技術分數':p.get('score',np.nan),
        '進場價':p.get('entry_raw',np.nan),'最新收盤':px,
        '投入成本':p.get('entry_total_cost',np.nan),'持倉市值':mv,
        '未實現損益':pnl,
        '未實現報酬%':pnl/float(p.get('entry_total_cost',1))*100 if pd.notna(pnl) and p.get('entry_total_cost',0) else np.nan
    })
pos24=pd.DataFrame(pos_rows)
if not pos24.empty:
    st.dataframe(pos24,use_container_width=True,hide_index=True)
else:
    st.caption('目前無持股。')

fl24=pd.DataFrame(state24.get('fills',[]))
if not fl24.empty:
    with st.expander('查看已完成 Forward 交易',expanded=False):
        st.dataframe(fl24.iloc[::-1],use_container_width=True,hide_index=True)
        st.download_button(
            '⬇️ 下載 Forward 完成交易 CSV',
            fl24.to_csv(index=False,encoding='utf-8-sig').encode('utf-8-sig'),
            'V3.6.24_forward_fills.csv','text/csv',key='v3624_dl_fills'
        )

st.markdown('### 📈 59 Forward 每日 MTM 權益')
led24=pd.DataFrame(state24.get('ledger',[]))
if not led24.empty:
    led24['日期']=pd.to_datetime(led24['日期'],errors='coerce')
    led24=led24.dropna(subset=['日期']).sort_values('日期')
    st.line_chart(led24.set_index('日期')[['MTM權益']])
    st.dataframe(led24.tail(120).iloc[::-1],use_container_width=True,hide_index=True)
    st.download_button(
        '⬇️ 下載 Forward 每日 MTM CSV',
        led24.to_csv(index=False,encoding='utf-8-sig').encode('utf-8-sig'),
        'V3.6.24_forward_daily_mtm.csv','text/csv',key='v3624_dl_mtm'
    )
else:
    st.info('按一次「同步 Forward 帳本」後開始建立每日 MTM 序列。')

st.markdown('### 🧠 60 V3.6.25 前瞻驗證規則')
rules24=pd.DataFrame([
    {'項目':'策略參數','鎖定值':'Gate D｜90~94＋站上MA200','用途':'禁止 Forward 期間重新挑 Gate'},
    {'項目':'排序','鎖定值':'分數→成交額→近MA200','用途':'同日訊號固定優先順序'},
    {'項目':'成交','鎖定值':'訊號封存後 N+1 開盤','用途':'消除訊號日收盤 Look-ahead'},
    {'項目':'容量','鎖定值':'25檔 × 3.33%','用途':'沿用 V3.6.22 穩健平台'},
    {'項目':'排隊','鎖定值':'0交易日','用途':'槽位滿即拒絕，不事後補單'},
    {'項目':'出場','鎖定值':'D40 / 盤中硬停損12%','用途':'沿用正式歷史規則'},
    {'項目':'早期觀察期','鎖定值':'至少60日＋30筆完成交易','用途':'樣本不足時不因短期輸贏改策略'},
    {'項目':'警戒','鎖定值':'PF<1.5 或 MDD≤-30%','用途':'進入壓力觀察，不自動調參'},
    {'項目':'嚴格異常','鎖定值':'PF<1 或 MDD≤-35%','用途':'停止加碼並檢查資料/執行/市場結構'},
])
st.dataframe(rules24,use_container_width=True,hide_index=True)

st.success(
    'V3.6.25 的目的不是再找更漂亮的歷史數字，而是從部署日起留下不可回寫的 Forward 證據。'
    '建議交易日收盤後先按「封存今日 Gate D 訊號」，之後按「同步 Forward 帳本」；'
    '若已設定 Google Drive，封存與同步後會自動備份到 Drive；JSON 手動下載仍保留作第二層備援。'
)
