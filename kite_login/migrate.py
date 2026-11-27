"""One-time cutover helper: build data/kite_credentials.json from the retired bots' files.

  personal <- SNAIL/config/creds.env  (ZERODHA_API_KEY ... ZERODHA_TOTP_SECRET)
  company  <- FIFTY/data/kite_credentials.json

Run ON THE PI, once, before SNAIL/FIFTY are deleted. Never prints a value; refuses
to overwrite an existing credentials file; writes it 0600.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Tuple

from .core import BOTS_ROOT, CRED_KEYS, resolve

SNAIL_ENV = 'SNAIL/config/creds.env'
FIFTY_JSON = 'FIFTY/data/kite_credentials.json'
ENV_KEYS = {'api_key': 'ZERODHA_API_KEY', 'api_secret': 'ZERODHA_API_SECRET',
            'user_id': 'ZERODHA_USER_ID', 'password': 'ZERODHA_PASSWORD',
            'totp_secret': 'ZERODHA_TOTP_SECRET'}


def read_env(path: Path) -> Dict[str, str]:
    out = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        k = k.strip()
        if k.startswith('export '):
            k = k[len('export '):].strip()
        out[k] = v.strip().strip('"').strip("'")
    return out


def build(root: Path = BOTS_ROOT) -> Dict[str, Dict[str, str]]:
    env = read_env(root / SNAIL_ENV)
    personal = {k: env.get(v, '') for k, v in ENV_KEYS.items()}
    with open(str(root / FIFTY_JSON), encoding='utf-8') as f:
        fifty = json.load(f)
    company = {k: str(fifty.get(k) or '') for k in CRED_KEYS}
    out = {'personal': personal, 'company': company}
    for alias, acc in out.items():
        missing = [k for k in CRED_KEYS if not acc[k].strip()]
        if missing:
            raise ValueError('%s: missing %s in the legacy file' % (alias, ', '.join(missing)))
    if personal['user_id'] == company['user_id']:
        raise ValueError('personal and company carry the SAME user_id -- legacy files mixed up')
    return out


def import_legacy(cfg: Dict, root: Path = BOTS_ROOT) -> Tuple[bool, str]:
    target = resolve(cfg['credentials_file'], root)
    if target.exists():
        return False, '%s already exists -- not overwritten' % target
    try:
        data = build(root)
    except (OSError, ValueError) as e:
        return False, 'legacy credentials unusable: %s' % e
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    return True, 'wrote %s (0600): personal + company' % target
