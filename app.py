import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import requests
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh

# ============================================================
# 🖤 黑嚕嚕－台股盤中雷達 V3.5.8
# V3.5.8：Fugle 5秒快照＋即時未完成日K注入＋Yahoo歷史日K＋官方行情備援＋A2.3.5可靠度驗證
# ============================================================

st.set_page_config(page_title='🖤 黑嚕嚕－台股盤中雷達', page_icon='🖤', layout='wide', initial_sidebar_state='expanded')

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
# ⚡ V3.5.8 Fugle 即時行情層
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
    return d, source_status, taiwan_time_text()


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
def load_twse_t86_day(date_yyyymmdd):
    cols=['日期','股票','名稱','外資買賣超股數','投信買賣超股數']
    try:
        r=requests.get('https://www.twse.com.tw/rwd/zh/fund/T86',params={'date':date_yyyymmdd,'response':'json','selectType':'ALLBUT0999'},headers={'User-Agent':'Mozilla/5.0'},timeout=20)
        r.raise_for_status();x=r.json();fields=x.get('fields',[]);data=x.get('data',[])
        def ix(*ks):
            for i,f in enumerate(fields):
                if all(k in f for k in ks):return i
            return None
        ic,inn,iff,itt=ix('證券代號'),ix('證券名稱'),ix('外陸資買賣超股數','不含外資自營商'),ix('投信買賣超股數')
        if None in (ic,inn,iff,itt):return pd.DataFrame(columns=cols)
        rows=[]
        for a in data:
            code=str(a[ic]).strip()
            if re.fullmatch(r'\d{4,6}',code):
                rows.append({'日期':pd.to_datetime(date_yyyymmdd),'股票':code.zfill(4),'名稱':str(a[inn]).strip(),'外資買賣超股數':_chip_num(a[iff]),'投信買賣超股數':_chip_num(a[itt])})
        return pd.DataFrame(rows,columns=cols)
    except Exception:return pd.DataFrame(columns=cols)
def recent_weekdays(n):
    d=taiwan_now().date();out=[]
    while len(out)<n:
        if d.weekday()<5:out.append(pd.Timestamp(d))
        d-=timedelta(days=1)
    return list(reversed(out))
@st.cache_data(ttl=1800, show_spinner=False)
def load_twse_chip_history(days=15):
    parts=[]
    for d in recent_weekdays(days+8):
        x=load_twse_t86_day(d.strftime('%Y%m%d'))
        if not x.empty:parts.append(x)
    if not parts:return pd.DataFrame()
    z=pd.concat(parts,ignore_index=True).sort_values(['股票','日期']);valid=sorted(z['日期'].unique())[-days:]
    return z[z['日期'].isin(valid)].copy()
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


# ===== V3.5.8 外資因子證明版 =====
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


# ===== V3.5.8 外資最佳權重驗證 =====
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


# ===== V3.5.8 外資 Gate 驗證 =====
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



# ===== V3.5.8 正式版：法人只做資訊標籤，不參與技術100分 =====
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

st.sidebar.title('🖤 黑嚕嚕－台股盤中雷達');st.sidebar.caption('V3.5.8｜B模式：即時優先＋最新盤後價備援')
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

st.title('🖤 黑嚕嚕－台股盤中雷達');st.caption('V3.5.8｜正式精簡版：保留日常雷達五頁；法人改為資訊標籤，不參與技術100分與Gate；行情核心沿用既有架構。')
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

t1,t2,t3,t4,t5=st.tabs([
    '📋 黑嚕嚕排行榜',
    '🚨 訊號中心',
    '📊 分數拆解',
    '📈 個股分析',
    '⭐ 自選股'
])

# V3.5.8：法人資料僅供閱讀，不改變排序分數。
result = v358_attach_chip_labels(result)


with t1:
    st.caption('V3.5.8 正式精簡版｜黑嚕嚕技術100分維持原模型；外資/投信僅作資訊標籤，不加分、不扣分、不作Gate。')
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
    st.line_chart(d[['Close','MA15','MA60','MA200']].rename(columns={'Close':'股價'}),height=420);a,b,c,d2,e=st.columns(5);a.metric('黑嚕嚕',f"{r['黑嚕嚕分數']}分");b.metric('量比',f"{r['量比']:.2f}x");c.metric('KD K',f"{r['K']:.1f}" if pd.notna(r['K']) else '-');d2.metric('MA15',f"{r['MA15']:.2f}");e.metric('MA200',f"{r['MA200']:.2f}");st.markdown('#### 🔊 成交量');st.line_chart(d[['Volume','VOL_MA20']].rename(columns={'Volume':'成交量','VOL_MA20':'20日均量'}),height=250);st.markdown('#### 🚨 目前訊號');st.info(r['訊號']);st.markdown('#### 🧠 黑嚕嚕判讀');st.write(r['判斷'] or '目前沒有額外判讀。')
with t5:
    st.subheader('⭐ 自選股');watch=st.multiselect('加入自選股',result['股票'].tolist(),default=[],key='watchlist')
    if not watch:st.info('請從上方選擇股票加入自選股。')
    else:
        q=result[result['股票'].isin(watch)].sort_values('黑嚕嚕分數',ascending=False)[['股票','名稱','價格','漲跌%','量比','K','D','黑嚕嚕分數','綜合分數','綜合等級','訊號']].copy();q['價格']=q['價格'].map(lambda x:f'{x:.2f}');q['漲跌%']=q['漲跌%'].map(lambda x:f'{x:+.2f}%');q['量比']=q['量比'].map(lambda x:f'{x:.2f}x');q['K']=q['K'].map(lambda x:f'{x:.1f}' if pd.notna(x) else '-');q['D']=q['D'].map(lambda x:f'{x:.1f}' if pd.notna(x) else '-');st.dataframe(q,use_container_width=True,hide_index=True)


