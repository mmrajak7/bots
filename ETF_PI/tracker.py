import pandas as pd
import shared_kite  # the ONE shared Kite token (python -m kite_login); ETF_PI never logs in
from datetime import datetime, timedelta
import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests
import json
import random
import time
import indicators
import pytz
import calendar
import sys
import etf_creds  # secrets live in the untracked credentials.json

mode = 'U'
folder = "\\"

if mode == 'W':
    folder = "\\"
elif mode == 'U':
    folder = '/'

# ****************************************************************
# ****************************************************************

pd.set_option('display.max_rows', 500)
pd.set_option('display.max_columns', 500)
pd.set_option('display.width', 1000)


def send_msg(msg):
    token = etf_creds.get('tracker_telegram')['token']
    chatid = etf_creds.get('tracker_telegram')['chatid']

    payload = {
        'chat_id': chatid,
        'text': msg,
        'parse_mode': 'HTML'
    }
    requests.post("https://api.telegram.org/bot{token}/sendMessage".format(token=token), data=payload)


# define function to get historical data
def get_historical_data(instrument_token, symbol, timeframe):
    # set end date as today
    end_date = datetime.now().date()

    # set start date based on timeframe
    if timeframe == 'monthly':
        start_date = end_date - timedelta(days=5 * 365)
    elif timeframe == 'weekly':
        start_date = end_date - timedelta(days=3 * 90)
    else:
        print('Invalid timeframe')
        return None

    daily_data_list = []
    # Same span as the old one-call-per-calendar-year loop (from 1 Jan of the start
    # year: the SuperTrend warm-up is unchanged), fetched in as few calls as Kite
    # allows -- these calls now spend the SHARED API key's historical budget.
    try:
        daily_data_list = shared_kite.daily(instrument_token, datetime(start_date.year, 1, 1).date(), end_date)
    except Exception as e:  # token missing -> caller sees empty data and alerts
        print('historical %s: %s' % (symbol, e))

    # Create a DataFrame from the daily data list
    daily_df = pd.DataFrame(daily_data_list, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])

    if instrument_token == '256265':
        # In case of NIFTY, just exit- is just to get length
        return daily_df
    # print(daily_df)
    # exit()

    # Convert the 'date' column to a datetime object
    daily_df['Date'] = pd.to_datetime(daily_df['Date'])

    # Set the 'date' column as the index of the DataFrame
    daily_df.set_index('Date', inplace=True)

    # Resample the DataFrame to monthly/weekly frequency
    if timeframe == 'monthly':
        f = 'M'
    elif timeframe == 'weekly':
        f = 'W'

    monthly_df = daily_df.resample(f).agg(
        {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'})

    monthly_df = monthly_df[monthly_df['Volume'] != 0]
    monthly_df = monthly_df.reset_index(drop=True)
    # print(monthly_df)

    df = indicators.SuperTrend(monthly_df, 10, 3)
    # print(df)

    return df


def get_index():
    # instruments = ['256265', '260105']
    df = get_historical_data('256265', 'NIFTY50', 'weekly')
    print(df.tail(5))
    return len(df)
    # d = kite.ltp(instruments)
    # # print(d)
    # n50 = int(d['256265']['last_price'])
    # bn = int(d['260105']['last_price'])
    #
    # n50 = round(n50, -2)
    # bn = round(bn, -2)
    #
    # return n50, bn


def update_trade_gsheet(df, sheet_name):
    scope = ['https://www.googleapis.com/auth/drive', 'https://spreadsheets.google.com/feeds']
    pat = os.path.dirname(os.path.realpath(__file__)) + str(folder) + 'secret.json'
    cred = ServiceAccountCredentials.from_json_keyfile_name(pat, scope)

    client = gspread.authorize(cred)
    gsheet = client.open_by_key("1z6kHM4v-BeQYyixM2J7Q6bJunM_S27IRfkpGLmXnUik")

    empty = []
    for i in range(100):
        if i == 0:
            empty.append(
                ['W_M', 'Script', 'Entry', 'Qty', 'LTP', 'Change', 'Direction', 'SL/TSL', 'Alert'])
        empty.append(['', '', '', '', '', '', '', '', ''])

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
    pat = os.path.dirname(os.path.realpath(__file__)) + str(folder) + 'secret.json'
    cred = ServiceAccountCredentials.from_json_keyfile_name(pat, scope)

    client = gspread.authorize(cred)
    # print(client)
    gsheet = client.open_by_key("1z6kHM4v-BeQYyixM2J7Q6bJunM_S27IRfkpGLmXnUik")
    exist = gsheet.worksheet(sheet_name).get_all_values()
    # print(exist)
    columns = [row for row in exist[0]]
    # print(columns)

    df = pd.DataFrame(exist, columns=columns)
    if len(df) > 0:
        df = df.drop(0)
        df = df.reset_index(drop=True)

    l = []
    d = []
    for i, j in enumerate(exist):
        if i > 0:
            l.append(j[1])
            d.append(j[0])
    # print(l)
    return d, l, df


def get_token_list(symbol_list):
    pat = os.path.dirname(os.path.realpath(__file__)) + str(folder) + 'instruments.csv'
    df = pd.read_csv(pat)
    df.set_index('tradingsymbol', inplace=True)
    result_list = []
    # Loop through the list of texts
    for text in symbol_list:
        value = df.loc[text, 'instrument_token']
        result_list.append(value)
    return result_list


# Get date and time
def date_time():
    now = datetime.now(pytz.timezone('Asia/Calcutta'))
    day = now.weekday()
    weekday = calendar.day_name[day]
    currDate = now.date()
    currTime = now.time()

    return weekday, currDate, currTime


def check_weekend(weekday):
    if weekday == 'Saturday' or weekday == 'Sunday':
        sys.exit("Weekend")


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


# weekday, currDate, currTime = date_time()
# check_weekend(weekday)
#
# r = check_run_time(9, 15, 15, 30)
# if r != 0:
#     sys.exit('Out of time range')

# The ONE shared Kite token (kite_login, 2026-11-27). No login here: no data =
# one alert and a non-zero exit, never a silent run on stale data.
try:
    length = get_index()
except Exception as e:  # token missing/expired, network
    print('Tracker: data fetch failed: %s' % e)
    length = 0
if length == 0:
    send_msg('Tracker: Data extraction failed (shared Kite token - run '
             '`python -m kite_login status` on the Pi)')
    sys.exit(1)

# define list of symbols and instrument tokens
dire, symbol_list, df = get_trade_gsheet('Track')
# print(symbol_list)
# print(df)
# print(dir)

# symbol_list = ['IRISDOREME']
# print(symbol_list)
instrument_token_list = get_token_list(symbol_list)
# print(instrument_token_list)
#
result = []
# get historical data for each symbol in the list
for i, token in enumerate(instrument_token_list):
    # if i > 0:
    #     exit()
    symbol = symbol_list[i]
    c_dir = dire[i]

    side = 'weekly'
    if c_dir == 'W':
        side = 'weekly'
    elif c_dir == 'M':
        side = 'monthly'

    data = get_historical_data(token, symbol, side)
    # print(data)
    if len(data) > 0:
        last_dir_m = data.iloc[-2]['STX']
        last_close = round(round(data.iloc[-2]['Close'] / 0.05) * 0.05, 2)
        ltp = round(round(data.iloc[-1]['Close'] / 0.05) * 0.05, 2)
        low = round(round(data.iloc[-2]['Low'] / 0.05) * 0.05, 2)
        st = round(round(data.iloc[-2]['ST'] / 0.05) * 0.05, 2)
        dir = data.iloc[-2]['STX']

        tsl = 0.0
        # print(last_close, tsl, low, st)
        if last_close < st:
            tsl = low
        elif low > st:
            tsl = st
        elif last_close > st:
            tsl = st

        # print(symbol, ltp, st, dir, low, tsl)
        msg = ''
        if ltp < tsl and dir == 'UP':
            msg = '[TSL] ' + symbol + ' at ' + str(ltp) + ' | TSL / Pyramid'
        elif ltp < tsl and dir == 'DOWN':
            msg = '[SL] ' + symbol + ' at ' + str(ltp) + ' | Stop-Loss'
        elif dir == 'DOWN':
            msg = '[DOWN]'
        result.append([symbol, ltp, st, dir, tsl, msg, c_dir])

# print(result)

for index, row in df.iterrows():
    symbol = df.values[index][1]
    direc = df.values[index][0]

    for i, j in enumerate(result):
        # print(symbol, j[0], direc, j[6])
        if symbol == j[0] and direc == j[6]:
            df.values[index][4] = j[1]
            df.values[index][5] = str(
                round(((float(df.values[index][4]) - float(df.values[index][2])) / float(
                    df.values[index][2])) * 100)) + '%'
            df.values[index][6] = j[3]
            df.values[index][7] = j[4]
            # if j[3] == 'UP':
            #     df.values[index][9] = j[2]
            # else:
            #     df.values[index][9] = ''
            if df.values[index][8] == '':
                if '[SL]' in j[5]:
                    df.values[index][8] = j[5]
                    send_msg(j[5])
                elif '[TSL]' in j[5]:
                    abs_val = ((j[2] - float(df.values[index][2])) / float(df.values[index][2])) * 100
                    # print(abs_val)
                    if abs_val > 1:
                        df.values[index][8] = j[5]
                        send_msg(j[5])
                elif '[DOWN]' in j[5]:
                    # print(j[1], float(df.values[index][2]))
                    if j[1] >= float(df.values[index][2]):
                        j[5] = '[EXIT] ' + symbol + ' at ' + str(j[1]) + ' | Close position'
                        df.values[index][8] = j[5]
                        send_msg(j[5])

print(df)
if len(df) > 0:
    weekday, currDate, currTime = date_time()
    check_weekend(weekday)
    # print(weekday)

    r = check_run_time(16, 31, 16, 50)
    if r == 0 and weekday == 'Friday':
        df = df.assign(Alert='')

    update_trade_gsheet(df, 'Track')
