"""Runtime settings (playlist/EPG URLs, "skip movies/series"), kept in the
AppConfig table.

Precedence, per setting:  value saved in the web GUI  >  environment variable
>  default.  A URL's origin is tracked in a companion "<key>_source" row
('gui' or 'env') so the logs and the Settings page can say exactly which one
is in use.  Nothing here needs a schema change.
"""
import logging
import os
import re
from urllib.parse import urlparse

from django.utils import timezone

from .models import AppConfig

logger = logging.getLogger(__name__)

FILETYPES = ('m3u', 'epg')
GUI, ENV = 'gui', 'env'

_url_userinfo = re.compile(r'(?i)(://)[^/@\s]*@')
_url_secret_param = re.compile(r'(?i)([?&](?:password|pass|pwd|token|key|secret)=)[^&\s]*')
_url_user_param = re.compile(r'(?i)([?&]username=)[^&\s]*')


def mask_url(text):
    """Strip credentials (user:pass@host and ?username=/password=/token=)
    so URLs can be logged and displayed safely."""
    text = _url_userinfo.sub(r'\1***@', str(text))
    text = _url_secret_param.sub(r'\1***', text)
    return _url_user_param.sub(r'\1***', text)


def env_name(filetype):
    return f'IPTV_{filetype.upper()}_URL'


# --- low-level key/value helpers -------------------------------------------

def _get(key):
    row = AppConfig.objects.filter(key=key).order_by('-pk').first()
    return row.value if row else None


def _set(key, value):
    # AppConfig.key is not unique, so replace rather than add: older versions
    # added a new row on every start, and a stale first row could shadow it.
    AppConfig.objects.filter(key=key).delete()
    AppConfig.objects.create(key=key, value=value, last_updated=timezone.now())


def _delete(key):
    AppConfig.objects.filter(key=key).delete()


# --- URLs ------------------------------------------------------------------

def get_url(filetype):
    """-> (url or None, source) with source one of 'gui', 'env', 'stored', None."""
    url = _get(f'{filetype}_url')
    if not url:
        return None, None
    source = _get(f'{filetype}_url_source')
    return url, source if source in (GUI, ENV) else 'stored'


def validate_url(value):
    """Returns an error message, or None if the value is an acceptable http(s) URL."""
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return 'Must be a full http:// or https:// URL.'
    if any(c in value for c in ' \r\n\t'):
        return 'Must not contain spaces or line breaks.'
    return None


def set_gui_url(filetype, value):
    _set(f'{filetype}_url', value)
    _set(f'{filetype}_url_source', GUI)
    logger.info(f"Settings - {filetype.upper()} URL saved from the web GUI: {mask_url(value)}. "
                f"The GUI value now takes precedence over environment variable {env_name(filetype)}.")


def clear_gui_url(filetype):
    """Drop the GUI override and fall back to the environment variable, if set."""
    env = os.getenv(env_name(filetype))
    if env:
        _set(f'{filetype}_url', env)
        _set(f'{filetype}_url_source', ENV)
        logger.info(f"Settings - {filetype.upper()} URL cleared in the web GUI; "
                    f"using environment variable {env_name(filetype)} again.")
    else:
        _delete(f'{filetype}_url')
        _delete(f'{filetype}_url_source')
        logger.info(f"Settings - {filetype.upper()} URL cleared in the web GUI; no URL is configured now.")


# --- skip movies/series ------------------------------------------------------

def get_skip_vod():
    """-> (enabled, source) with source 'gui', 'env' or 'default'."""
    gui = _get('skip_vod')
    if gui is not None:
        return gui == '1', GUI
    if os.getenv('IPTV_SKIP_VOD'):
        return True, ENV
    return False, 'default'


def set_gui_skip_vod(enabled):
    _set('skip_vod', '1' if enabled else '0')
    logger.info(f"Settings - skip movies/series saved from the web GUI: {'ON' if enabled else 'OFF'} "
                f"(overrides environment variable IPTV_SKIP_VOD).")


# --- startup -----------------------------------------------------------------

def apply_at_startup():
    """Decide which source each setting comes from and say so, plainly, in the log."""
    for ft in FILETYPES:
        name = env_name(ft)
        env = os.getenv(name)
        url, source = get_url(ft)
        label = ft.upper()

        if source == GUI:
            note = f" (environment variable {name} is set but ignored - clear the URL on the Settings page to use it)" if env else ""
            logger.info(f"Startup - {label} URL: using the value configured in the web GUI: {mask_url(url)}{note}")
        elif env:
            _set(f'{ft}_url', env)
            _set(f'{ft}_url_source', ENV)
            logger.info(f"Startup - {label} URL: nothing saved in the web GUI, using environment variable {name}: {mask_url(env)}")
        elif url:
            logger.info(f"Startup - {label} URL: using the previously stored value: {mask_url(url)}")
        else:
            logger.warning(f"Startup - {label} URL: not configured. Set it on the Settings page of the web GUI or via environment variable {name}.")

    enabled, source = get_skip_vod()
    where = {'gui': 'the value configured in the web GUI', 'env': 'environment variable IPTV_SKIP_VOD',
             'default': 'default'}[source]
    logger.info(f"Startup - skip movies/series: {'ON' if enabled else 'OFF'} (from {where})")
