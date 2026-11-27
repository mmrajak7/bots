from datetime import datetime
import pytz
import calendar
import requests
import random
import json
import sys
import pandas as pd
import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import time
import etf_creds  # secrets live in the untracked credentials.json

# Get the current directory
CURRENT_DIR = os.path.dirname(os.path.realpath(__file__))


def set_up_logger(currDate):
    import logging
    logfile = os.path.join(CURRENT_DIR, 'logs', f't_{str(currDate)}.log')
    # Create and configure logger
    logging.basicConfig(filename=logfile,
                        format='%(asctime)s %(message)s',
                        filemode='a')
    # Creating an object
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    return logger


def get_token(path):
    with open(path, "r") as f:
        token = f.read()
    return token


def generate_token():
    import get_kite_token


def sendMsgTelegram(message, type, max_retries=3):
    """
    Send message to Telegram with retry logic and error handling.

    Args:
        message: Text to send (HTML format supported)
        type: 'E' for error channel, anything else for info channel
        max_retries: Number of retry attempts for transient failures

    Raises:
        Exception: If message fails to send after all retries
    """
    # Telegram message limit is 4096 characters
    MAX_MSG_LENGTH = 4096

    if type == 'E':  # Error
        token = etf_creds.get('telegram_error')['token']
        chatid = etf_creds.get('telegram_error')['chatid']
    else:
        token = etf_creds.get('telegram_main')['token']
        chatid = etf_creds.get('telegram_main')['chatid']

    # Truncate message if too long
    if len(message) > MAX_MSG_LENGTH:
        truncate_notice = "\n\n... [truncated]"
        message = message[:MAX_MSG_LENGTH - len(truncate_notice)] + truncate_notice

    payload = {
        'chat_id': chatid,
        'text': message,
        'parse_mode': 'HTML'
    }

    last_error = None
    for attempt in range(max_retries):
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=payload,
                timeout=10  # 10 second timeout
            )

            # Check HTTP status
            if response.status_code == 429:  # Rate limited
                retry_after = int(response.headers.get('Retry-After', 5))
                time.sleep(retry_after)
                continue

            response.raise_for_status()

            # Check Telegram API response
            result = response.json()
            if not result.get('ok'):
                error_desc = result.get('description', 'Unknown error')
                # If HTML parsing failed, retry without HTML
                if 'parse' in error_desc.lower() or 'html' in error_desc.lower():
                    payload['parse_mode'] = None  # Disable HTML parsing
                    continue
                raise Exception(f"Telegram API error: {error_desc}")

            return True  # Success

        except requests.exceptions.Timeout:
            last_error = "Request timed out"
            time.sleep(2)
        except requests.exceptions.ConnectionError:
            last_error = "Connection error"
            time.sleep(2)
        except requests.exceptions.HTTPError as e:
            last_error = f"HTTP error: {e}"
            if response.status_code >= 500:  # Server error, retry
                time.sleep(2)
                continue
            raise  # Client error, don't retry
        except Exception as e:
            last_error = str(e)
            raise

    raise Exception(f"Failed to send Telegram after {max_retries} attempts: {last_error}")


# Set date and time
def log_time(logger):
    now = datetime.now(pytz.timezone('Asia/Calcutta'))
    currDate = now.date()
    currTime = now.time()
    logger.info(f"{str(currDate)} / {str(currTime)}")


# Get date and time
def date_time():
    now = datetime.now(pytz.timezone('Asia/Calcutta'))
    day = now.weekday()
    weekday = calendar.day_name[day]
    currDate = now.date()
    currTime = now.time()
    return weekday, currDate, currTime


# Get date and time
def check_valid_datetime(logger, d, t):
    now = datetime.now(pytz.timezone('Asia/Calcutta'))
    expDate = str(now.date())
    expTime = '10:15'
    if (expDate == d) and (expTime in str(t)):
        return 0
    else:
        logger.info(f'Date or Time not matching {str(d)} {str(t)}')
        return 1


def get_instruments():
    df = pd.read_csv(os.path.join(CURRENT_DIR, 'instruments.csv'))
    return df


def get_instrument_stock(df, script):
    df = df[df['instrument_type'] == 'EQ']
    df = df[df['exchange'] == 'NSE']
    df = df[['tradingsymbol', 'instrument_token']]
    ins = df.loc[df['tradingsymbol'] == script, 'instrument_token'].iloc[0]
    return ins, script, 0


def check_which_expiry(expiry):
    i = 0
    try:
        expiry = datetime.strptime(expiry, '%Y-%m-%d')
        eWeek = expiry.strftime("%V")
        weekday, currDate, currTime = date_time()
        cWeek = currDate.strftime("%V")
    except:
        i = 0
    if cWeek == eWeek:
        i = 1
    return i


def get_instrument_fut(df, script):
    df = df[(df['name'] == script) & (df['instrument_type'] == 'FUT')]
    df = df.sort_values(by=['expiry'])
    df = df.reset_index(drop=True)
    df = df[['tradingsymbol', 'instrument_token', 'lot_size', 'expiry']]
    edt = df.values[0][3]
    i = check_which_expiry(edt)
    sym = df.values[i][0]
    ins = df.values[i][1]
    lot = df.values[i][2]
    return ins, sym, lot


def get_first_ohlc(df):
    d = str(df.values[1][0])
    t = str(df.values[1][1])
    o = df.values[1][2]
    h = df.values[1][3]
    l = df.values[1][4]
    c = df.values[1][5]
    return d, t, o, h, l, c


# check if script can be run in the current time period
def check_run_time(from_hour, from_minutes, to_hour, to_minutes, to_seconds=0):
    now = datetime.now(pytz.timezone('Asia/Calcutta'))
    currTime = now.replace(second=0, microsecond=0)
    sessStart = now.replace(hour=from_hour, minute=from_minutes, second=0, microsecond=0)
    sessClose = now.replace(hour=to_hour, minute=to_minutes, second=to_seconds, microsecond=0)
    if currTime < sessStart or currTime > sessClose:
        return 1
    else:
        return 0


def check_weekend(weekday):
    if weekday == 'Saturday' or weekday == 'Sunday':
        sys.exit("Weekend")


def get_header(tkn):
    auth = f'enctoken {tkn}'
    headers = {
        'authority': 'kite.zerodha.com',
        'cache-control': 'private, max-age=0, no-cache',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.163 Safari/537.36',
        'accept': '*/*',
        'authorization': auth,
        'accept-encoding': 'gzip, deflate',
        'accept-language': 'en-US,en;q=0.9,tr-TR;q=0.8,tr;q=0.7',
    }
    return headers


def get_data_kite(tkn, instrument, ab, bis, interval='60minute'):
    time.sleep(0.35)  # Reduced from 1s - Zerodha allows ~3 req/sec
    randomNo = random.randint(1000000, 9999999)
    link = f'https://kite.zerodha.com/oms/instruments/historical/{instrument}/{interval}?oi=1&from={ab}&to={bis}&ciqrandom={randomNo}'
    headers = get_header(tkn)
    res = requests.get(link, headers=headers)
    if res.status_code == 200:
        data = json.loads(res.text)
        df = pd.DataFrame.from_dict(data['data']['candles'])
        df = df.iloc[:, :-1]
        if len(df) == 0:
            print('No data found')
            df = pd.DataFrame()
            # msg = f'{script}: No reply from server'
            # sendMsgTelegram(msg, 'E')
        df.columns = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume']
        # if setIndex is True:
        #     df = df.set_index(['Date'])
        return df
    else:
        return pd.DataFrame()


def process_kite_data(df):
    df[["Date", "Time"]] = df["Date"].str.split("T", expand=True, )
    df = df.drop(['Volume'], axis=1)
    df['Time'] = df['Time'].str[:8]
    columnSeq = ['Date', 'Time', 'Open', 'High', 'Low', 'Close']
    df['Date'] = pd.to_datetime(df['Date']).dt.date
    df['Time'] = pd.to_datetime(df['Time'], format='%H:%M:%S').dt.time
    df = df[columnSeq]
    return df


def update_trade_gsheet(df, sheet_name):
    scope = ['https://www.googleapis.com/auth/drive', 'https://spreadsheets.google.com/feeds']
    path = os.path.join(CURRENT_DIR, 'secret.json')
    cred = ServiceAccountCredentials.from_json_keyfile_name(path, scope)
    client = gspread.authorize(cred)
    gsheet = client.open_by_key("1z6kHM4v-BeQYyixM2J7Q6bJunM_S27IRfkpGLmXnUik")
    empty = []
    for i in range(20000):
        if i == 0:
            empty.append(
                ['Date', 'Entry Time', 'Type', 'Script', 'Quantity', 'Entry Price', 'Exit Price', 'Exit Time', 'Points',
                 'Profit/Loss', 'PnL %', 'Cum. PnL', 'DD', 'DD %'])
        empty.append(['', '', '', '', '', '', '', '', '', '', '', '', '', ''])
    gsheet.values_update(
        sheet_name,
        params={'valueInputOption': 'USER_ENTERED'},
        body={'values': empty}
    )
    gsheet.values_append(
        sheet_name,
        params={'valueInputOption': 'USER_ENTERED'},
        body={'values': df.values.tolist()}
    )
    print("Sheet Updated!")


def get_trade_gsheet(sheet_name):
    scope = ['https://www.googleapis.com/auth/drive', 'https://spreadsheets.google.com/feeds']
    path = os.path.join(CURRENT_DIR, 'secret.json')
    cred = ServiceAccountCredentials.from_json_keyfile_name(path, scope)
    client = gspread.authorize(cred)
    gsheet = client.open_by_key("1z6kHM4v-BeQYyixM2J7Q6bJunM_S27IRfkpGLmXnUik")
    exist = gsheet.worksheet(sheet_name).get_all_values()
    df = pd.DataFrame(exist)
    df = df.drop(df.index[0])
    df = df.reset_index(drop=True)
    df.drop(df.columns[[11, 12, 13]], axis=1, inplace=True)
    return df
