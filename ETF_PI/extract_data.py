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

# ****************************************************************
# ****************************************************************

pd.set_option('display.max_rows', 500)
pd.set_option('display.max_columns', 500)
pd.set_option('display.width', 1000)


def send_msg(df):
    token = etf_creds.get('extract_data_telegram')['token']
    chatid = etf_creds.get('extract_data_telegram')['chatid']
    msg = ''

    for index, row in df.iterrows():
        msg = ''
        if row['w_per'] >= -2 and row['w_per'] <= 2 and row['L_W'] == 'UP':
            msg = '[ETF_W] ' + str(row['Symbol']) + ' ' + str(row['LTP'])

        if row['m_per'] >= -2 and row['m_per'] <= 2  and row['L_M'] == 'UP':
            msg = '[ETF_M] ' + str(row['Symbol']) + ' ' + str(row['LTP'])

        if msg != '':
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
        start_date = end_date - timedelta(days=4 * 365)
    elif timeframe == 'weekly':
        start_date = end_date - timedelta(days=3 * 90)
    else:
        print('Invalid timeframe')
        return None

    # Initialize an empty list to store the daily data
    daily_data_list = []

    # Loop through each year and get the daily data
    for year in range(start_date.year, end_date.year + 1):
        # Define the start and end dates for the current year
        year_start_date = datetime(year, 1, 1).date()
        year_end_date = datetime(year, 12, 31).date()

        # print(year_start_date, year_end_date)

        # Get the historical data for the current year
        try:
            historical_data = shared_kite.candles(instrument_token, 'day', str(year_start_date), str(year_end_date))
        except Exception as e:
            # print(f"Error occurred: {e}")
            continue

        # Append the historical data to the daily data list
        daily_data_list.extend(historical_data)
        # print(daily_data_list)

    # Create a DataFrame from the daily data list
    daily_df = pd.DataFrame(daily_data_list, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
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

    df = indicators.SuperTrend(monthly_df, 10, 3)
    # print(df)

    return df

def get_index():
    # instruments = ['256265', '260105']
    df = get_historical_data('256265', 'NIFTY50', 'weekly')
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
    pat = os.path.dirname(os.path.realpath(__file__)) + '/secret.json'
    cred = ServiceAccountCredentials.from_json_keyfile_name(pat, scope)

    client = gspread.authorize(cred)
    gsheet = client.open_by_key("1z6kHM4v-BeQYyixM2J7Q6bJunM_S27IRfkpGLmXnUik")

    empty = []
    for i in range(100):
        if i == 0:
            empty.append(
                ['Symbol', ' LTP', 'Weekly ST', 'W_Dist', 'W_Dir', 'Monthly ST', 'M_Dist', 'M_Dir', 'W_Per', 'M_Per'])
        empty.append(['', '', '', '', '', '', '', '', '', ''])

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
    pat = os.path.dirname(os.path.realpath(__file__)) + '/secret.json'
    cred = ServiceAccountCredentials.from_json_keyfile_name(pat, scope)

    client = gspread.authorize(cred)
    # print(client)
    gsheet = client.open_by_key("1z6kHM4v-BeQYyixM2J7Q6bJunM_S27IRfkpGLmXnUik")
    exist = gsheet.worksheet(sheet_name).get_all_values()
    l = []
    for i, j in enumerate(exist):
        if i > 0:
            l.append(j[0])
    # print(l)
    return l

def get_token_list(symbol_list):
    pat = os.path.dirname(os.path.realpath(__file__)) + '/instruments_etf.csv'
    df = pd.read_csv(pat)
    # Remove duplicates, keeping first occurrence
    df = df.drop_duplicates(subset='tradingsymbol', keep='first')
    df.set_index('tradingsymbol', inplace=True)
    result_list = []

    for text in symbol_list:
        if text in df.index:
            value = df.loc[text, 'instrument_token']
            # print(f"Found {text}: {value}")
            result_list.append(value)
        else:
            print(f"Warning: {text} not found in index")

    return result_list

# Get date and time
def date_time():
    now = datetime.now()
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

#
# import datetime
#
# # Open the file in append mode
# with open("output.txt", "a") as file:
#     # Get current time
#     current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
#
#     # Write current time to the file
#     file.write(current_time + "\n")
#
# print("Current time written to the file.")

weekday, currDate, currTime = date_time()
check_weekend(weekday)

r = check_run_time(9, 15, 15, 30)
if r != 0:
    sys.exit('Out of time range')

# The ONE shared Kite token (kite_login, 2026-11-27). No login here: no data =
# one alert and a non-zero exit, never a silent run on stale data.
try:
    length = get_index()
except Exception as e:  # token missing/expired, network
    print('ETF scanner: data fetch failed: %s' % e)
    length = 0
if length == 0:
    _t = etf_creds.get('extract_data_telegram')
    requests.post('https://api.telegram.org/bot{}/sendMessage'.format(_t['token']),
                  data={'chat_id': _t['chatid'],
                        'text': 'ETF scanner: Data extraction failed (shared Kite token - '
                                'run `python -m kite_login status` on the Pi)'})
    sys.exit(1)

# define list of symbols and instrument tokens
symbol_list = get_trade_gsheet('ETF')
# symbol_list = ['ICICIFMCG']
print(symbol_list)
instrument_token_list = get_token_list(symbol_list)
print(instrument_token_list)

result = []
# get historical data for each symbol in the list
for i, token in enumerate(instrument_token_list):
    symbol = symbol_list[i]

    monthly_data = get_historical_data(token, symbol, 'monthly')
    # print(monthly_data)
    if len(monthly_data) > 0:
        last_dir_m = monthly_data.iloc[-2]['STX']
        m_close = monthly_data.iloc[-1]['Close']
        m_st = round(monthly_data.iloc[-1]['ST'], 2)
        m_dir = monthly_data.iloc[-1]['STX']
        # print(m_close, m_st, m_dir)
    else:
        m_close, m_st = 0
        m_dir = ''
    time.sleep(0.5)

    monthly_data = get_historical_data(token, symbol, 'weekly')
    # print(monthly_data)
    if len(monthly_data) > 0:
        last_dir_w = monthly_data.iloc[-2]['STX']
        w_close = monthly_data.iloc[-1]['Close']
        w_st = round(monthly_data.iloc[-1]['ST'], 2)
        w_dir = monthly_data.iloc[-1]['STX']
        # print(w_close, w_st, w_dir)
    else:
        w_close, w_st = 0
        w_dir = ''
    time.sleep(0.5)

    result.append([symbol, w_close, w_st, (w_close - w_st), w_dir, m_st, (m_close - m_st), m_dir, last_dir_m, last_dir_w])

if len(result) > 0:
    df = pd.DataFrame(result,
                      columns=['Symbol', 'LTP', 'Weekly ST', 'W_Dist', 'W_Dir', 'Monthly ST', 'M_Dist', 'M_Dir', 'L_M',
                               'L_W'])
    df = df.sort_values(by=['M_Dist', 'W_Dist'])

    df['w_per'] = round((df['W_Dist'] / df['LTP']) * 100, 1)
    df['m_per'] = round((df['M_Dist'] / df['LTP']) * 100, 1)

    df_comp = df
    df = df.drop(columns=['L_M', 'L_W'])
    print(df)

    update_trade_gsheet(df, 'ETF')

    print(df_comp)
    send_msg(df_comp)

print(time.perf_counter())
