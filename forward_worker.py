#!/usr/bin/env python3
# 黑嚕嚕 V3.6.29 Headless Forward Runner
# 目的：GitHub Actions 不再依賴 Playwright/瀏覽器。
# 以 Streamlit 官方 AppTest headless 執行 app.py，之後直接讀 Google Drive 正式帳本驗證成功。
import os, sys, json, time, requests
from datetime import datetime, timezone, timedelta

TW = timezone(timedelta(hours=8))
TODAY = datetime.now(TW).strftime("%Y-%m-%d")
STATE_FILENAME = "V3.6.25.1_forward_state.json"
FOLDER_NAME = "黑嚕嚕_Forward帳本"

def tg(msg):
    token=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
    chat=os.getenv("TELEGRAM_CHAT_ID","").strip()
    if not token or not chat:
        print("Telegram secrets missing")
        return
    try:
        r=requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id":chat,"text":msg},timeout=20)
        print("Telegram:", r.status_code)
    except Exception as e:
        print("Telegram error:", type(e).__name__, e)

def access_token():
    r=requests.post("https://oauth2.googleapis.com/token",data={
        "client_id":os.environ["GOOGLE_CLIENT_ID"],
        "client_secret":os.environ["GOOGLE_CLIENT_SECRET"],
        "refresh_token":os.environ["GOOGLE_REFRESH_TOKEN"],
        "grant_type":"refresh_token",
    },timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"OAuth refresh failed HTTP {r.status_code}: {r.text[:300]}")
    return r.json()["access_token"]

def drive_headers():
    return {"Authorization":f"Bearer {access_token()}"}

def find_one(name, parent=None, folder=False):
    q=[f"name='{name.replace(chr(39), chr(92)+chr(39))}'","trashed=false"]
    if parent: q.append(f"'{parent}' in parents")
    if folder: q.append("mimeType='application/vnd.google-apps.folder'")
    params={"q":" and ".join(q),"fields":"files(id,name,modifiedTime)","pageSize":20}
    r=requests.get("https://www.googleapis.com/drive/v3/files",headers=drive_headers(),params=params,timeout=25)
    r.raise_for_status()
    fs=r.json().get("files",[])
    return fs[0] if fs else None

def read_cloud_state():
    folder=find_one(FOLDER_NAME, folder=True)
    if not folder: raise RuntimeError("Drive folder not found")
    f=find_one(STATE_FILENAME,parent=folder["id"])
    if not f: raise RuntimeError("Drive state file not found")
    r=requests.get(f"https://www.googleapis.com/drive/v3/files/{f['id']}",
                   headers=drive_headers(),params={"alt":"media"},timeout=25)
    r.raise_for_status()
    return r.json()

def verify_state(before_rev):
    s=read_cloud_state()
    rev=int(s.get("cloud_revision",0) or 0)
    auto_date=str(s.get("last_auto_date","") or "")
    status=str(s.get("last_auto_status","") or "")
    print(f"CLOUD_VERIFY date={auto_date} revision={rev} status={status}")
    if auto_date != TODAY:
        raise RuntimeError(f"Auto Forward 未完成：last_auto_date={auto_date}, expected={TODAY}")
    if not status.startswith("🟢"):
        raise RuntimeError(f"Auto Forward 狀態不是成功：{status}")
    if rev < before_rev:
        raise RuntimeError(f"Drive revision 倒退：before={before_rev}, after={rev}")
    return s

def main():
    before=read_cloud_state()
    before_rev=int(before.get("cloud_revision",0) or 0)
    print(f"HEADLESS_START today={TODAY} base_revision={before_rev}")

    # 如果今天已完成，直接驗證成功，不重複交易/推播。
    if str(before.get("last_auto_date","")) == TODAY and str(before.get("last_auto_status","")).startswith("🟢"):
        print("FORWARD_WORKER_SUCCESS already completed today")
        return 0

    try:
        from streamlit.testing.v1 import AppTest
        at=AppTest.from_file("app.py", default_timeout=300)
        at.run(timeout=300)
        if at.exception:
            details=" | ".join(str(x.value) for x in at.exception)
            raise RuntimeError("AppTest exception: "+details[:1000])
        # 給 Drive 最後寫入少量緩衝
        time.sleep(3)
        after=verify_state(before_rev)
        print("FORWARD_WORKER_SUCCESS")
        print(json.dumps({
            "date":after.get("last_auto_date"),
            "revision":after.get("cloud_revision"),
            "status":after.get("last_auto_status"),
            "positions":len(after.get("positions",{}) or {}),
            "pending":len(after.get("pending",[]) or []),
        },ensure_ascii=False))
        return 0
    except Exception as e:
        msg=f"🚨 黑嚕嚕 V3.6.29 Worker 失敗\n日期：{TODAY}\n{type(e).__name__}: {e}\n正式帳本採 Fail-Closed，請查看 GitHub Actions。"
        print(msg)
        tg(msg)
        return 1

if __name__=="__main__":
    sys.exit(main())
