"""ETF_PI secrets, read from the UNTRACKED credentials.json beside this file.

The repo is PUBLIC: no token, password, TOTP key or chat id may sit in code.
`credentials.json` is gitignored by the root `**/credentials.json` rule and is
created on the Pi with mode 0600. Shape (values never in git):

  {
    "telegram_main":     {"token": "...", "chatid": "..."},
    "telegram_error":    {"token": "...", "chatid": "..."},
    "extract_data_kite": {"userid": "...", "password": "...", "totp_key": "..."},
    "extract_data_telegram": {"token": "...", "chatid": "..."},
    "tracker_kite":      {"userid": "...", "password": "...", "totp_key": "..."},
    "tracker_telegram":  {"token": "...", "chatid": "..."},
    "backtest_kite":     {"api_key": "...", "api_secret": "...", "user_id": "...",
                          "password": "...", "totp_secret": "..."}
  }

A missing file or section raises at the point of use with a message that names
the file -- never a value.
"""
import json
import os

_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'credentials.json')
_cache = None


def get(section):
    global _cache
    if _cache is None:
        try:
            with open(_PATH, encoding='utf-8') as f:
                _cache = json.load(f)
        except (OSError, ValueError) as e:
            raise RuntimeError('ETF_PI credentials unreadable at %s (%s)' % (_PATH, type(e).__name__))
    if section not in _cache:
        raise RuntimeError('ETF_PI credentials.json has no %r section' % section)
    return _cache[section]
