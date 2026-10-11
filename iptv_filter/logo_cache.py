"""Local cache for the channel logos of *ticked* channels.

Kodi (and most IPTV clients) download every tvg-logo themselves, straight from
the provider's/CDN's host, the first time they load a playlist - which is slow
when that host is slow. Here the logos of the channels you actually selected
are downloaded once, in the background, and the generated M3U points at this
server instead. Only logos that really are cached get rewritten; everything
else keeps its original URL, so a client never receives a dead link.

Files live in IPTV_LOGO_DIR (default: <project>/logo_cache), named by the
sha1 of the logo URL. State is tracked in the LogoCache table.
"""
import base64
import hashlib
import ipaddress
import logging
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import unquote_to_bytes, urljoin, urlparse

import requests
from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from . import settings_store
from .models import LogoCache, PlaylistChannel

logger = logging.getLogger(__name__)

MAX_BYTES = 2 * 1024 * 1024          # a logo bigger than this is not a logo
CONNECT_TIMEOUT, READ_TIMEOUT = 5, 10
MAX_REDIRECTS = 3
WORKERS = 4
REFRESH_AFTER = timedelta(days=30)   # re-download cached logos this old
UNUSED_AFTER = timedelta(days=30)    # delete logos no ticked channel has used for this long
RETRY_BACKOFF = [timedelta(hours=1), timedelta(hours=6), timedelta(hours=24)]
DEBOUNCE_SECONDS = 5

EXT_BY_TYPE = {
    'image/png': 'png', 'image/jpeg': 'jpg', 'image/jpg': 'jpg', 'image/gif': 'gif',
    'image/webp': 'webp', 'image/svg+xml': 'svg', 'image/x-icon': 'ico',
    'image/vnd.microsoft.icon': 'ico', 'image/bmp': 'bmp',
}
_HASH_RE = re.compile(r'^[0-9a-f]{40}$')


def logo_dir():
    return os.environ.get('IPTV_LOGO_DIR') or str(settings.BASE_DIR / 'logo_cache')


def logo_hash(url):
    return hashlib.sha1(url.encode('utf-8', 'replace')).hexdigest()


def valid_hash(value):
    return bool(_HASH_RE.match(value or ''))


def file_path(url_hash):
    return os.path.join(logo_dir(), url_hash)


def ext_for(content_type):
    return EXT_BY_TYPE.get((content_type or '').lower(), 'img')


def enabled():
    return settings_store.get_logo_cache()[0]


# --- safety: the app downloads URLs that come from a playlist ----------------

def _allow_private():
    return (os.environ.get('IPTV_LOGO_ALLOW_PRIVATE') or '').lower() in ('1', 'true', 'yes', 'on')


def check_url_allowed(url):
    """Raises ValueError unless url is http(s) and does not point at this
    machine or a private network (so a hostile playlist can't use us to probe
    the LAN). IPTV_LOGO_ALLOW_PRIVATE=1 lifts the address restriction."""
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('only http/https logo URLs are fetched')
    if _allow_private():
        return
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f'cannot resolve host: {e}')
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f'refusing non-public address {ip}')


def _sniff(data):
    if data.startswith(b'\x89PNG'):
        return 'image/png'
    if data.startswith(b'\xff\xd8'):
        return 'image/jpeg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data.startswith(b'\x00\x00\x01\x00'):
        return 'image/x-icon'
    if data.startswith(b'BM'):
        return 'image/bmp'
    return None


def _decode_data_uri(url):
    header, _, payload = url.partition(',')
    meta = header[5:]
    if ';base64' in meta:
        raw = base64.b64decode(payload, validate=False)
    else:
        raw = unquote_to_bytes(payload)
    return raw, meta.split(';')[0].strip().lower()


# --- downloading ---------------------------------------------------------------

def _download(url):
    """-> (bytes, content_type). Raises on any problem."""
    if url.lower().startswith('data:'):
        raw, ctype = _decode_data_uri(url)
        if len(raw) > MAX_BYTES:
            raise ValueError('embedded image too large')
        return raw, ctype

    headers = {'User-Agent': os.environ.get('IPTV_USER_AGENT') or
               'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'}
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        check_url_allowed(current)
        r = requests.get(current, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                         stream=True, allow_redirects=False)
        try:
            if r.is_redirect and r.headers.get('Location'):
                current = urljoin(current, r.headers['Location'])  # validated at the top of the loop
                continue
            r.raise_for_status()
            declared = r.headers.get('Content-Length')
            if declared and declared.isdigit() and int(declared) > MAX_BYTES:
                raise ValueError('image too large')
            buf = bytearray()
            for chunk in r.iter_content(16384):
                buf.extend(chunk)
                if len(buf) > MAX_BYTES:
                    raise ValueError('image too large')
            return bytes(buf), (r.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        finally:
            r.close()
    raise ValueError('too many redirects')


def _store(row, data, declared_type):
    ctype = declared_type if declared_type.startswith('image/') else None
    sniffed = _sniff(data)
    if sniffed and (ctype is None or ctype == 'application/octet-stream'):
        ctype = sniffed
    if not ctype or not data:
        raise ValueError('not an image')
    os.makedirs(logo_dir(), exist_ok=True)
    final = file_path(row.url_hash)
    tmp = final + '.part'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, final)  # atomic: a client never sees a half-written file
    return ctype


def _attempt(row):
    """Network + disk work for one logo. Runs in worker threads, so it must not
    touch the database. -> (content_type, size, error_message_or_None)"""
    try:
        data, declared = _download(row.url)
        return _store(row, data, declared), len(data), None
    except Exception as e:  # network errors, bad images, blocked addresses ...
        return None, 0, settings_store.mask_url(str(e))[:200]


def _record(row, ctype, size, error):
    """Save the outcome of an attempt on its row (main thread only)."""
    now = timezone.now()
    if error is None:
        row.status = LogoCache.OK
        row.content_type = ctype
        row.size = size
        row.fetched_at = now
        row.fail_count = 0
        row.last_error = ''
        row.next_attempt = None
    else:
        row.fail_count += 1
        row.status = LogoCache.FAILED
        row.last_error = error
        row.next_attempt = now + RETRY_BACKOFF[min(row.fail_count, len(RETRY_BACKOFF)) - 1]
        logger.warning(f"Logo cache: could not fetch {settings_store.mask_url(row.url)[:120]}: {error}")
    row.save()
    return row.status == LogoCache.OK


def fetch_logo(row):
    return _record(row, *_attempt(row))


# --- selection -> cache ----------------------------------------------------------

def selected_logo_urls():
    return set(PlaylistChannel.objects.filter(included=True).exclude(tvg_logo='')
               .values_list('tvg_logo', flat=True).distinct())


def sync():
    """Make the cache match the current selection: register logos of ticked
    channels, download what is missing or stale, and delete what is long unused.
    Returns a small stats dict."""
    now = timezone.now()
    urls = selected_logo_urls()
    hashes = {logo_hash(u): u for u in urls}

    existing = set(LogoCache.objects.filter(url_hash__in=list(hashes)).values_list('url_hash', flat=True)) if hashes else set()
    LogoCache.objects.bulk_create(
        [LogoCache(url_hash=h, url=u) for h, u in hashes.items() if h not in existing], batch_size=500)
    if hashes:
        for i in range(0, len(hashes), 500):
            LogoCache.objects.filter(url_hash__in=list(hashes)[i:i+500]).update(last_selected=now)

    due = []
    for row in LogoCache.objects.filter(url_hash__in=list(hashes)) if hashes else []:
        if row.status == LogoCache.OK:
            if row.fetched_at and now - row.fetched_at > REFRESH_AFTER or not os.path.exists(file_path(row.url_hash)):
                due.append(row)
        elif row.status == LogoCache.PENDING or row.next_attempt is None or row.next_attempt <= now:
            due.append(row)

    ok = 0
    if due:
        logger.info(f"Logo cache: downloading {len(due)} logo(s) for ticked channels.")
        started = time.perf_counter()
        # Downloads run in parallel; results are saved here, on one thread,
        # so the database is only ever used from the calling thread.
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(_attempt, due))
        ok = sum(_record(row, *res) for row, res in zip(due, results))
        logger.info(f"Logo cache: fetched {ok} of {len(due)} in {time.perf_counter()-started:.1f}s.")

    removed = _purge_unused(now)
    return {'selected': len(hashes), 'downloaded': ok, 'attempted': len(due), 'removed': removed}


def _purge_unused(now):
    stale = list(LogoCache.objects.filter(last_selected__lt=now - UNUSED_AFTER).values_list('pk', 'url_hash'))
    for _, h in stale:
        try:
            os.remove(file_path(h))
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning(f"Logo cache: could not remove {h}: {e}")
    if stale:
        LogoCache.objects.filter(pk__in=[pk for pk, _ in stale]).delete()
        logger.info(f"Logo cache: removed {len(stale)} logo(s) unused for {UNUSED_AFTER.days} days.")
    return len(stale)


# --- M3U output ---------------------------------------------------------------------

def cached_lookup():
    """{url_hash: extension} for logos that are really on disk and ready."""
    return {h: ext_for(ct) for h, ct in LogoCache.objects.filter(status=LogoCache.OK).values_list('url_hash', 'content_type')}


def local_logo_url(base, logo_url, lookup):
    """The local URL for a logo if it is cached, else None."""
    if not logo_url:
        return None
    h = logo_hash(logo_url)
    ext = lookup.get(h)
    return f"{base}{h}.{ext}" if ext else None


def stats():
    agg = LogoCache.objects.aggregate(total=Sum('size'))
    counts = {s: LogoCache.objects.filter(status=s).count() for s in (LogoCache.OK, LogoCache.PENDING, LogoCache.FAILED)}
    return {'ok': counts['ok'], 'pending': counts['pending'], 'failed': counts['failed'],
            'bytes': agg['total'] or 0, 'running': is_running()}


# --- background worker -----------------------------------------------------------------

_wake = threading.Event()
_thread = None
_thread_lock = threading.Lock()
_running = False


def is_running():
    return _running


def request_sync():
    """Ask the background worker to sync soon. Requests that arrive close
    together (e.g. ticking several channels in a row) are combined into one run."""
    if os.environ.get('IPTV_SAFE_START') or not enabled():
        return False
    global _thread
    with _thread_lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_worker, name='iptv-logo-cache', daemon=True)
            _thread.start()
    _wake.set()
    return True


def _worker():
    global _running
    while True:
        _wake.wait()
        time.sleep(DEBOUNCE_SECONDS)   # let a burst of ticks settle
        _wake.clear()
        _running = True
        try:
            if enabled():
                sync()
        except Exception:
            logger.exception("Logo cache: sync failed; will retry on the next request.")
        finally:
            _running = False
