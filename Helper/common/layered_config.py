"""Two-layer config: a TRACKED defaults file under an UNTRACKED local overlay.

Why this exists
---------------
`Helper/config/` is excluded wholesale by `.gitignore`, so the thresholds that
decide what eleven subsystems do have no history at all — you cannot ask when a
number changed, or what it was before. They also arrive on the Pi over Google
Drive rather than over git, which is a sync, not a delivery.

They are untracked for one good reason: three of those files carry live secrets
(two Telegram bot tokens, a Drive folder id, credential paths containing a real
home directory, two account ids) and **the GitHub repo is PUBLIC**. So the fix
is not "track config/" — it is to split each file in two:

    config/<name>.defaults.json   TRACKED    everything that is not a secret
    config/<name>.json            UNTRACKED  the secrets, and any local override

`load('bcs_config')` deep-merges the second over the first.

Why the overlay wins, given that it is the direction with a footgun
-------------------------------------------------------------------
Overlay-wins means a threshold edited in the tracked file does nothing on a box
whose overlay still carries the old value — silently, which is the failure the
kill switch was explicitly designed against. Defaults-win would avoid that.

It is still the right way round, because of one fact about this fleet: **SSH to
the Pi has been broken since 2026-08-12.** The tracked file therefore may not
take the name of a file that already exists there untracked — `git pull` refuses
to clobber one, and the owner would have to fix a stopped money system standing
at the box. A new name (`.defaults.json`) is the only shape that pulls cleanly,
and a new name can only be the lower layer.

The footgun is answered rather than accepted: every overlay leaf that shadows a
DIFFERENT value in the defaults is logged at WARNING with its path and both
values. On a box whose overlay has been trimmed to secrets that is silent. On
one that has not, it names precisely what to delete. A secret is *added* by the
overlay, not shadowed, so it never warns.

Missing files are not an error in either layer. Missing defaults means a
subsystem not yet split; missing overlay means a fresh checkout with no secrets
yet, which must still start.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent / 'config'

#: Suffix of the tracked layer. Deliberately NOT the bare name — see the
#: module docstring on why a name collision with an untracked file on the Pi is
#: a stopped money system rather than an inconvenience.
DEFAULTS_SUFFIX = '.defaults.json'


def _default_warn(msg: str) -> None:
    logger.warning('%s', msg)


def _read(path: Path, warn) -> Optional[dict]:
    """Parsed JSON object, or None if absent/unreadable/not an object.

    A corrupt layer returns None rather than raising: with two layers, refusing
    to start because ONE of them is malformed would turn a bad overlay into a
    total outage. The warning says which file, and the other layer still loads.
    """
    if not path.exists():
        return None
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        warn(f"config layer {path} is unreadable ({e}); ignoring it")
        return None
    if not isinstance(data, dict):
        warn(f"config layer {path} is a {type(data).__name__}, not an object; "
             f"ignoring it")
        return None
    return data


def _merge(base: dict, over: dict, path: str = '') -> Tuple[dict, List[str]]:
    """Deep-merge `over` onto `base`. Returns (merged, shadow warnings).

    Dicts merge key-by-key; every other type replaces wholesale. A list is NOT
    merged element-wise — a threshold list or a universe list is one value, and
    element-wise merging would make it impossible for an overlay to REMOVE an
    entry.
    """
    out = dict(base)
    shadows: List[str] = []
    for k, v in over.items():
        here = f'{path}{k}'
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            out[k], sub = _merge(base[k], v, here + '.')
            shadows.extend(sub)
        else:
            if k in base and base[k] != v:
                shadows.append(f'{here}: defaults {base[k]!r} -> overlay {v!r}')
            out[k] = v
    return out, shadows


def load(name: str, warn_on_shadow: bool = True, warn=None) -> Dict[str, Any]:
    """Merged config for `name` (e.g. 'bcs_config'), overlay over defaults.

    Returns `{}` when neither layer exists — the same thing every current
    loader does for a missing file, so a subsystem that has not been split yet
    behaves exactly as before.

    `warn` takes one string. It exists because the kill switch reads its config
    through here and its "unreadable, staying ARMED" notice has to reach the
    monitor's own session log, where somebody actually looks — a `logging`
    record in a cron job goes nowhere.
    """
    warn = warn or _default_warn
    name = name[:-5] if name.endswith('.json') else name
    base = _read(CONFIG_DIR / (name + DEFAULTS_SUFFIX), warn) or {}
    over = _read(CONFIG_DIR / (name + '.json'), warn)
    if over is None:
        return base
    merged, shadows = _merge(base, over)
    if shadows and warn_on_shadow:
        warn(f"{name}.json overrides {len(shadows)} value(s) from "
             f"{name}{DEFAULTS_SUFFIX} — an edit to the tracked file will NOT "
             f"take effect for these. Trim the overlay to secrets only. "
             + '; '.join(shadows))
    return merged
