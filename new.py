from flask import Flask, render_template, jsonify, request
import glob
import os
from datetime import datetime
import json
import threading
import pytz

import config
import scanner

app = Flask(__name__)
JSON_DIR = config.JSON_DIR
IST = pytz.timezone('Asia/Kolkata')

os.makedirs(JSON_DIR, exist_ok=True)

# Fetch NSE every 3 minutes in the background (one scheduler per machine)
scanner.start_background_scanner()

def load_symbols():
    try:
        with open("symbols.txt", "r") as f:
            symbols = [line.strip() for line in f if line.strip()]
        return list(dict.fromkeys(symbols))
    except:
        return ["NIFTY", "BANKNIFTY", "RELIANCE", "TCS", "INFY"]

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
            if isinstance(data, dict):
                return data, date_loaded
    except Exception as e:
        print(f"Error loading JSON: {e}")
    return None, None

# ==================== ROUTES ====================
@app.route('/')
def index():
    symbols = load_symbols()
    return render_template('top_gainer.html', symbols=symbols)

@app.route('/scanner')
def scanner_page():
    return render_template('scanner.html')

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

@app.route('/api/oi-series')
def api_oi_series():
    """OI time series for one stock from the stored 3-minute NSE snapshots."""
    stock = request.args.get('stock')
    date = request.args.get('date')
    time_str = request.args.get('time', '15:30')
    if not stock or not date:
        return jsonify({'success': False, 'error': 'Missing parameters'})

    data, _ = load_json_data(date)
    if not data:
        return jsonify({'success': False, 'error': 'No data'})

    cutoff = time_str.replace(':', '')
    rows = sorted((r for r in data.get('combined_data', [])
                   if r.get('Stock') == stock and r.get('Time_Code', '') <= cutoff),
                  key=lambda r: r['Time_Code'])
    if not rows:
        return jsonify({'success': False, 'error': 'No data for this stock'})

    last = rows[-1]
    return jsonify({
        'success': True,
        'stock': stock,
        'times': [f"{r['Time_Code'][:2]}:{r['Time_Code'][2:]}" for r in rows],
        'price': [r.get('Cash_Price') for r in rows],
        'oi': [r.get('OI') for r in rows],
        'ce_oi': [r.get('CE_OI') for r in rows],
        'pe_oi': [r.get('PE_OI') for r in rows],
        'atm': last.get('ATM'),
        'pcr': last.get('PCR'),
        'oi_change_pct': last.get('OI_Change_%'),
    })

@app.route('/api/scan/latest')
def api_scan_latest():
    date = request.args.get('date') or datetime.now(IST).strftime('%Y-%m-%d')
    path = scanner.scan_file(date)
    if not os.path.exists(path):
        return jsonify({'success': False, 'error': f'No scan for {date} yet', 'status': scanner.STATE})
    with open(path) as f:
        scans = json.load(f)
    return jsonify({'success': True, 'date': date, 'status': scanner.STATE, **scans})

@app.route('/api/scan/run', methods=['POST'])
def api_scan_run():
    """Trigger one NSE fetch + scoring cycle now (runs in the background)."""
    if scanner.STATE['running']:
        return jsonify({'success': False, 'error': 'A scan is already running'})
    threading.Thread(target=scanner.Scanner().run_cycle, daemon=True).start()
    return jsonify({'success': True, 'message': 'Scan started'})

@app.route('/api/health')
def health():
    return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat(), 'scanner': scanner.STATE})

if __name__ == '__main__':
    print("\n" + "="*60)
    print("🚀 DASHBOARD RUNNING")
    print("="*60)
    print(f"📁 JSON directory: {JSON_DIR}")
    print(f"🌐 Dashboard: http://localhost:5000/")
    print(f"🎯 Scanner:   http://localhost:5000/scanner")
    print("="*60 + "\n")
    app.run(debug=False, host='0.0.0.0', port=5000, threaded=True)
