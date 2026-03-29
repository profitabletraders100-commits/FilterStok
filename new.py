from flask import Flask, render_template, jsonify, request
import pandas as pd
import glob
import os
from datetime import datetime, timedelta
import json
from fyers_apiv3 import fyersModel
from config import FYERS_CLIENT_ID, FYERS_ACCESS_TOKEN
import pytz
import numpy as np
import traceback

app = Flask(__name__)
DATA_DIR = "oi_data"
JSON_DIR = "oi_data_json"
IST = pytz.timezone('Asia/Kolkata')

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(JSON_DIR, exist_ok=True)

class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (pd.Timestamp, datetime)):
            return obj.strftime('%Y-%m-%d %H:%M:%S')
        return super(NpEncoder, self).default(obj)

app.json_encoder = NpEncoder

def init_fyers():
    return fyersModel.FyersModel(
        client_id=FYERS_CLIENT_ID,
        token=FYERS_ACCESS_TOKEN,
        log_path=""
    )

fyers = init_fyers()

def load_symbols():
    try:
        with open("symbols.txt", "r") as f:
            symbols = [line.strip() for line in f if line.strip()]
        return list(dict.fromkeys(symbols))
    except:
        return ["NIFTY", "BANKNIFTY", "RELIANCE", "TCS", "INFY"]

def get_all_strikes(stock):
    """Get all available strikes for a stock"""
    try:
        symbol = f"NSE:{stock}-EQ"
        opt_response = fyers.optionchain({"symbol": symbol, "strikecount": 50})
        if opt_response and opt_response.get("s") == "ok":
            data = opt_response.get("data", {})
            options_chain = data.get("optionsChain", [])
            strikes = []
            for opt in options_chain:
                strike = opt.get("strike_price", 0)
                if strike > 0 and strike not in strikes:
                    strikes.append(strike)
            return sorted(strikes)
    except:
        pass
    return []

def get_atm_strike_from_json(stock, time_str, json_data):
    """Get ATM strike using price from JSON data"""
    try:
        # Convert time format: "10:00" -> "1000"
        time_code = time_str.replace(':', '')
        
        # Find price in JSON
        price = None
        for record in json_data.get('combined_data', []):
            if record.get('Stock') == stock and record.get('Time_Code') == time_code:
                price = record.get('Cash_Price')
                print(f"💰 Price from JSON for {stock} at {time_str}: {price}")
                break
        
        if not price:
            print(f"⚠️ No price in JSON for {stock} at {time_str}")
            return None, None
        
        # Get strikes from option chain
        symbol = f"NSE:{stock}-EQ"
        opt_response = fyers.optionchain({"symbol": symbol, "strikecount": 50})
        
        if opt_response and opt_response.get("s") == "ok":
            data = opt_response.get("data", {})
            options_chain = data.get("optionsChain", [])
            strikes = []
            for opt in options_chain:
                strike = opt.get("strike_price", 0)
                if strike > 0:
                    strikes.append(strike)
            
            if strikes:
                strikes = sorted(set(strikes))
                atm_strike = min(strikes, key=lambda x: abs(x - price))
                print(f"🎯 ATM strike for {stock}: {atm_strike} (Price: {price})")
                return atm_strike, price
            else:
                print(f"⚠️ No strikes found for {stock}")
        else:
            print(f"⚠️ Option chain failed for {stock}")
                    
    except Exception as e:
        print(f"Error getting ATM strike: {e}")
        traceback.print_exc()
    
    return None, None

def get_oi_data(stock, expiry, strike, option_type, date):
    """Fetch OI data for specific strike"""
    try:
        symbol = f"NSE:{stock}{expiry}{strike}{option_type}"
        date_str = date.strftime("%Y-%m-%d")
        
        data = {
            "symbol": symbol, 
            "resolution": "5", 
            "date_format": "1",
            "range_from": date_str, 
            "range_to": date_str,
            "cont_flag": "1", 
            "oi_flag": "1"
        }
        
        response = fyers.history(data=data)
        
        if not response or response.get("s") != "ok":
            return []
        
        if not response.get("candles"):
            return []
        
        candles = response["candles"]
        df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
        
        df['oi'] = df['oi'].fillna(0)
        df['close'] = df['close'].fillna(0)
        
        df['time_utc'] = pd.to_datetime(df['timestamp'], unit='s', utc=True)
        df['time_ist'] = df['time_utc'].dt.tz_convert(IST)
        df['time_str'] = df['time_ist'].dt.strftime('%H:%M')
        df['hour'] = df['time_ist'].dt.hour
        df['minute'] = df['time_ist'].dt.minute
        
        # Filter market hours
        df = df[
            ((df['hour'] == 9) & (df['minute'] >= 15)) |
            ((df['hour'] >= 10) & (df['hour'] <= 14)) |
            ((df['hour'] == 15) & (df['minute'] <= 30))
        ]
        
        if df.empty:
            return []
        
        df = df.sort_values('time_str').reset_index(drop=True)
        records = df[['time_str', 'oi', 'close']].to_dict('records')
        return records
        
    except Exception as e:
        print(f"Error fetching {option_type}: {e}")
        return []

# ==================== JSON DATA LOADING ====================
def load_json_data(date_str=None):
    try:
        if date_str is None:
            json_files = glob.glob(os.path.join(JSON_DIR, "*.json"))
            if not json_files:
                return None, None
            json_files.sort(reverse=True)
            json_file = json_files[0]
            date_loaded = os.path.basename(json_file).replace('.json', '')
        else:
            json_file = os.path.join(JSON_DIR, f"{date_str}.json")
            date_loaded = date_str
        
        if os.path.exists(json_file):
            with open(json_file, 'r') as f:
                data = json.load(f)
            return data, date_loaded
    except Exception as e:
        print(f"Error loading JSON: {e}")
    return None, None

# ==================== ROUTES ====================
@app.route('/')
def index():
    symbols = load_symbols()
    return render_template('top_gainer.html', symbols=symbols)

@app.route('/api/available-dates')
def api_available_dates():
    json_files = glob.glob(os.path.join(JSON_DIR, "*.json"))
    dates = [os.path.basename(f).replace('.json', '') for f in json_files]
    dates.sort(reverse=True)
    return jsonify({'success': True, 'dates': dates})

@app.route('/api/daily-data/<date>')
def api_daily_data(date):
    data, date_loaded = load_json_data(date)
    if not data:
        return jsonify({'success': False, 'error': 'No data'})
    
    times = sorted(set([d.get('Time_Code') for d in data.get('combined_data', []) if d.get('Time_Code')]))
    times_display = [f"{t[:2]}:{t[2:]}" for t in times]
    
    return jsonify({
        'success': True,
        'date': date_loaded,
        'times': times,
        'times_display': times_display,
        'snapshots': data.get('combined_data', [])
    })

@app.route('/api/snapshot')
def api_snapshot():
    date = request.args.get('date')
    time_code = request.args.get('time')
    limit = request.args.get('limit', 20, type=int)
    
    if not date or not time_code:
        return jsonify({'success': False, 'error': 'Missing params'})
    
    data, date_loaded = load_json_data(date)
    if not data:
        return jsonify({'success': False, 'error': 'No data'})
    
    time_data = [d for d in data.get('combined_data', []) if d.get('Time_Code') == time_code]
    
    if not time_data:
        return jsonify({'success': False, 'error': 'No data for this time'})
    
    gainers = []
    losers = []
    
    for row in time_data:
        change = row.get('Cash_Change_%_Open', 0)
        stock = row.get('Stock', '')
        price = row.get('Cash_Price', 0)
        
        if change > 0:
            gainers.append({
                'symbol': stock,
                'change': round(change, 2),
                'current_price': price
            })
        else:
            losers.append({
                'symbol': stock,
                'change': round(change, 2),
                'current_price': price
            })
    
    gainers.sort(key=lambda x: x['change'], reverse=True)
    losers.sort(key=lambda x: x['change'])
    
    return jsonify({
        'success': True,
        'date': date_loaded,
        'time': time_code,
        'time_display': f"{time_code[:2]}:{time_code[2:]}",
        'gainers': gainers[:limit],
        'losers': losers[:limit],
        'gainers_count': len(gainers),
        'losers_count': len(losers),
        'total_stocks': len(gainers) + len(losers)
    })

@app.route('/api/fetch-oi')
def fetch_oi():
    try:
        stock = request.args.get('stock')
        date_str = request.args.get('date')
        time_str = request.args.get('time')
        expiry = request.args.get('expiry', '26MAR')
        strike_param = request.args.get('strike', type=int)
        
        print(f"\n{'='*50}")
        print(f"🔍 FETCH OI: {stock} | {date_str} | {time_str} | {expiry}")
        print(f"{'='*50}")
        
        if not all([stock, date_str, time_str]):
            return jsonify({'success': False, 'error': 'Missing parameters'})
        
        date = datetime.strptime(date_str, "%Y-%m-%d")
        
        # Load JSON data for this date
        json_data, _ = load_json_data(date_str)
        
        # Get ATM strike using JSON price
        selected_strike = strike_param
        ltp = None
        
        if not selected_strike:
            print(f"🎯 Finding ATM strike for {stock} at {time_str} from JSON...")
            selected_strike, ltp = get_atm_strike_from_json(stock, time_str, json_data)
            if not selected_strike:
                print(f"❌ Could not determine ATM strike for {stock}")
                return jsonify({'success': False, 'error': 'Could not determine ATM strike'})
        
        print(f"🎯 Using strike: {selected_strike}, LTP: {ltp}")
        
        # Fetch OI data
        ce_data = get_oi_data(stock, expiry, selected_strike, "CE", date)
        pe_data = get_oi_data(stock, expiry, selected_strike, "PE", date)
        
        if not ce_data or not pe_data:
            return jsonify({'success': False, 'error': 'No OI data available'})
        
        # Get closest time
        time_int = int(time_str.replace(':', ''))
        available_times = [c.get('time_str') for c in ce_data if c.get('time_str')]
        
        if not available_times:
            return jsonify({'success': False, 'error': 'No time data'})
        
        closest = min(available_times, key=lambda x: abs(int(x.replace(':', '')) - time_int))
        
        ce_at = next((c for c in ce_data if c.get('time_str') == closest), ce_data[0])
        pe_at = next((p for p in pe_data if p.get('time_str') == closest), pe_data[0])
        
        pcr = pe_at.get('oi', 0) / ce_at.get('oi', 0) if ce_at.get('oi', 0) > 0 else 0
        sentiment = "Bullish" if pcr > 1.2 else "Bearish" if pcr < 0.8 else "Neutral"
        
        # Get all strikes for dropdown
        all_strikes = get_all_strikes(stock)
        
        print(f"✅ Success for {stock} - PCR: {pcr:.2f}, Sentiment: {sentiment}")
        
        return jsonify({
            'success': True,
            'stock': stock,
            'strike': selected_strike,
            'selected_time': closest,
            'ltp': ltp or 0,
            'ce_data': ce_data,
            'pe_data': pe_data,
            'ce_at': ce_at,
            'pe_at': pe_at,
            'pcr': round(pcr, 2),
            'sentiment': sentiment,
            'strikes': all_strikes
        })
        
    except Exception as e:
        print(f"❌ Error in fetch_oi: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/health')
def health():
    return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat()})

if __name__ == '__main__':
    print("\n" + "="*60)
    print("🚀 DASHBOARD RUNNING")
    print("="*60)
    print(f"📁 JSON directory: {JSON_DIR}")
    print(f"🌐 Dashboard: http://localhost:5000/")
    print("="*60 + "\n")
    app.run(debug=True, host='0.0.0.0', port=5000, threaded=True)