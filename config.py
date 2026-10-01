import os

# =========================
# NSE FETCH SCHEDULE (IST)
# =========================
FETCH_INTERVAL_MINUTES = 3        # pull fresh NSE data every 3 minutes
PREOPEN_FETCH_TIME = "09:08"      # pre-open session snapshot (IEP / gap)
MARKET_OPEN_TIME = "09:15"
MARKET_CLOSE_TIME = "15:30"
FINAL_PICK_TIME = "09:54"         # the cycle that publishes the morning pick (lands before 10:00)

# =========================
# UNIVERSE
# =========================
# NSE index whose constituents are scanned. "SECURITIES IN F&O" = every F&O stock.
SCAN_INDEX = os.environ.get("SCAN_INDEX", "SECURITIES IN F&O")

# =========================
# FILTERS
# =========================
MIN_PRICE = 50                    # skip penny stocks
MIN_TURNOVER_CR = 5               # min traded value so far today (₹ crore)
MAX_GAP_PCT = 6                   # skip stocks that gapped more than this (chasing risk)

# =========================
# SCORING
# =========================
TOP_N = 10                        # picks per side (long / short)
OPTION_CHAIN_LIMIT = 30           # option chains fetched per cycle (top candidates only)

# =========================
# ALERT TOGGLES
# =========================
ENABLE_SCANNER = os.environ.get("ENABLE_SCANNER", "1") == "1"
ENABLE_TELEGRAM = True

# =========================
# DATA STORAGE
# =========================
JSON_DIR = "oi_data_json"
SCAN_DIR = "scan_results"

# ===============================
# TELEGRAM SETTINGS
# ===============================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8467935956:AAFyIwMCpFkkReR94RJflO6uZmk5d0tDOO8")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "2063497381")
