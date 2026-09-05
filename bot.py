import os
import json
import time
import requests
import numpy as np
import pandas as pd
from datetime import datetime
from flask import Flask

app = Flask(__name__)

# --- AYARLAR VE PARAMETRELER ---
SYMBOL = "GALAUSDT"
TIMEFRAME = "5m"

USE_BODY_FILTER = True
GOVDE_FILTRESI = 5.0
USE_PEAK_FILTER = True
ZIRVE_PERIYODU = 1

USE_SLOPE_FILTER = False
SLOPE_LEN = 21
NORM_LEN = 14

TRAILING_START_ORAN = 0.015
TRAILING_CIKIS_ORAN = 0.002
YENIDEN_GIRIS_ORAN = 0.002
STOP_LOSS_ORAN = 0.010

STATE_FILE = "state.json"

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {
        "state": "IDLE", 
        "entry_price": 0.0, 
        "peak_price": 0.0, 
        "trailing_armed": False, 
        "exit_price": 0.0
    }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def f_robustSlope(series, length):
    slopes = []
    vals = series.values
    for i in range(length - 1):
        for j in range(i + 1, length):
            d = vals[i] - vals[j]
            if not np.isnan(d):
                slopes.append(d / (j - i))
    if len(slopes) > 0:
        return np.median(slopes)
    return np.nan

def fetch_binance_data():
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": SYMBOL, "interval": TIMEFRAME, "limit": 100}
    
    for attempt in range(3):
        try:
            response = requests.get(url, params=params, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list) and len(data) > 0:
                    df = pd.DataFrame(data, columns=[
                        'open_time', 'open', 'high', 'low', 'close', 'volume',
                        'close_time', 'quote_vol', 'trades', 'taker_base_vol', 'taker_quote_vol', 'ignore'
                    ])
                    for col in ['open', 'high', 'low', 'close', 'volume']:
                        df[col] = df[col].astype(float)
                    df['timestamp'] = pd.to_datetime(df['open_time'], unit='ms')
                    df.set_index('timestamp', inplace=True)
                    return df[['open', 'high', 'low', 'close', 'volume']]
            time.sleep(2)
        except Exception as e:
            time.sleep(2)
    return pd.DataFrame()

def convert_to_heikin_ashi(df):
    ha_df = pd.DataFrame(index=df.index)
    ha_df['close'] = (df['open'] + df['high'] + df['low'] + df['close']) / 4
    
    ha_open = []
    first_open = (df['open'].iloc[0] + df['close'].iloc[0]) / 2
    ha_open.append(first_open)
    
    for i in range(1, len(df)):
        prev_ha_open = ha_open[i-1]
        prev_ha_close = ha_df['close'].iloc[i-1]
        curr_ha_open = (prev_ha_open + prev_ha_close) / 2
        ha_open.append(curr_ha_open)
        
    ha_df['open'] = ha_open
    ha_df['high'] = pd.concat([df['high'], ha_df['open'], ha_df['close']], axis=1).max(axis=1)
    ha_df['low'] = pd.concat([df['low'], ha_df['open'], ha_df['close']], axis=1).min(axis=1)
    ha_df['volume'] = df['volume']
    return ha_df

def calculate_indicators(df):
    df = convert_to_heikin_ashi(df)
    df['safe_close'] = df['close'].apply(lambda x: max(float(x), 1e-10))
    df['log_close'] = np.log(df['safe_close'])
    
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(window=NORM_LEN).mean()
    df['norm_unit'] = df['atr'] / df['safe_close']
    
    slopes = []
    for i in range(len(df)):
        if i < SLOPE_LEN:
            slopes.append(np.nan)
        else:
            segment = df['log_close'].iloc[i-SLOPE_LEN+1:i+1].iloc[::-1]
            slopes.append(f_robustSlope(segment, SLOPE_LEN))
            
    df['raw_mid'] = slopes
    df['slope_val'] = df.apply(lambda row: row['raw_mid'] / row['norm_unit'] if row['norm_unit'] > 0 else 0, axis=1)
    df['slope_gecerli'] = (not USE_SLOPE_FILTER) | (df['slope_val'] > 0.0)
    
    df['body_size'] = (df['close'] - df['open']).abs()
    df['total_range'] = df['high'] - df['low']
    df['guclu_govde'] = (not USE_BODY_FILTER) | ((df['body_size'] > df['total_range'] * (GOVDE_FILTRESI / 100.0)) & (df['close'] > df['open']))
    
    df['onceki_zirve'] = df['close'].shift(1).rolling(window=ZIRVE_PERIYODU).max()
    df['kirilim_var'] = (not USE_PEAK_FILTER) | (df['close'] > df['onceki_zirve'])
    
    df['sinyal_al'] = df['guclu_govde'] & df['kirilim_var'] & df['slope_gecerli']
    return df

def send_webhook(action, price):
    webhook_url = os.environ.get("BROKER_WEBHOOK_URL")
    if not webhook_url:
        return
    payload = {"action": action, "symbol": SYMBOL, "price": price}
    try:
        requests.post(webhook_url, json=payload, timeout=5)
    except Exception as e:
        print(f"Webhook hata: {e}")

def run_bot_cycle():
    state = load_state()
    df_raw = fetch_binance_data()
    if df_raw.empty or 'close' not in df_raw.columns:
        return "Veri alinamadi"
        
    current_price = float(df_raw['close'].iloc[-1])
    df_ha = calculate_indicators(df_raw)
    latest_ha = df_ha.iloc[-1]
    
    current_state = state.get("state", "IDLE")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    log_msg = f"[{timestamp}] {SYMBOL} Fiyat: {current_price:.7f} | Durum: {current_state} | HA Sinyal: {latest_ha['sinyal_al']}"
    print(log_msg)
    
    if current_state == "IDLE":
        if latest_ha['sinyal_al']:
            send_webhook("BUY", current_price)
            state["state"] = "IN_POSITION"
            state["entry_price"] = current_price
            state["peak_price"] = current_price
            state["trailing_armed"] = False

    elif current_state == "IN_POSITION":
        stop_loss_limiti = state["entry_price"] * (1.0 - STOP_LOSS_ORAN)
        if current_price <= stop_loss_limiti:
            send_webhook("SELL", current_price)
            state["state"] = "WAITING_REENTRY"
            state["exit_price"] = current_price
            state["peak_price"] = 0.0
            state["trailing_armed"] = False
        else:
            if current_price > state["peak_price"]:
                state["peak_price"] = current_price
                
            if not state["trailing_armed"] and current_price >= state["entry_price"] * (1.0 + TRAILING_START_ORAN):
                state["trailing_armed"] = True
                
            if state["trailing_armed"]:
                cikis_limiti = state["peak_price"] * (1.0 - TRAILING_CIKIS_ORAN)
                if current_price <= cikis_limiti:
                    send_webhook("SELL", current_price)
                    state["state"] = "WAITING_REENTRY"
                    state["exit_price"] = current_price
                    state["peak_price"] = 0.0
                    state["trailing_armed"] = False

    elif current_state == "WAITING_REENTRY":
        reentry_limiti = state["exit_price"] * (1.0 - YENIDEN_GIRIS_ORAN)
        if current_price <= reentry_limiti:
            send_webhook("BUY", current_price)
            state["state"] = "IN_POSITION"
            state["entry_price"] = current_price
            state["peak_price"] = current_price
            state["trailing_armed"] = False
            state["exit_price"] = 0.0
        elif latest_ha['sinyal_al']:
            send_webhook("BUY", current_price)
            state["state"] = "IN_POSITION"
            state["entry_price"] = current_price
            state["peak_price"] = current_price
            state["trailing_armed"] = False
            state["exit_price"] = 0.0

    save_state(state)
    return log_msg

@app.route("/")
def home():
    result = run_bot_cycle()
    return f"Bot Calisti: {result}"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)