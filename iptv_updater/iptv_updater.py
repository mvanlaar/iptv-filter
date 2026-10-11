import contextlib
import io
import os
import sys
import threading
import zlib
import requests
import re
import time
from datetime import timedelta
import xml.etree.ElementTree as ET
from django.db import OperationalError, connection, transaction
from django.utils import timezone
from iptv_filter import settings_store, logo_cache, epg_quality
from iptv_filter.models import AppConfig, CachedFile, PlaylistChannel, EpgChannel, EpgProgramme

# import the logging library / Get an instance of a logger
import logging
logger = logging.getLogger(__name__)

# In-memory record of the most recent error per filetype, surfaced via the
# /status endpoint. Intentionally not persisted to the DB - it's meant to
# answer "is this currently healthy", which naturally resets on restart.
_last_error = {}

def _record_error(filetype, message):
    _last_error[filetype] = {'time': timezone.now(), 'message': message}

def get_last_error(filetype):
    return _last_error.get(filetype)

def update_all():
    try:
        update_m3u()
    except Exception as e:
        logger.exception("Unhandled error during startup M3U update.")
        _record_error('m3u', str(e))
    try:
        update_epg()
    except Exception as e:
        logger.exception("Unhandled error during startup EPG update.")
        _record_error('epg', str(e))

# Only one download/processing run per file type at a time: the startup run,
# the scheduler and the Settings page buttons could otherwise overlap and
# fight over the same tables.
_busy = {}
_busy_lock = threading.Lock()

@contextlib.contextmanager
def _exclusive(filetype):
    with _busy_lock:
        acquired = not _busy.get(filetype)
        if acquired:
            _busy[filetype] = True
    try:
        yield acquired
    finally:
        if acquired:
            with _busy_lock:
                _busy[filetype] = False

def is_busy(filetype):
    return bool(_busy.get(filetype))

def _run(filetype, download):
    with _exclusive(filetype) as ok:
        if not ok:
            logger.warning(f"A {filetype} update is already running - skipping this request.")
            return
        if download:
            if _retrieve(filetype):
                _update_tables(filetype)
            else:
                logger.warning(f"Skipping {filetype} table update because retrieval failed.")
        else:
            _update_tables(filetype)
        if filetype == 'm3u':
            # The selection may have new/changed logos: refresh the local cache.
            logo_cache.request_sync()

def update_m3u():
    _run('m3u', download=True)

def update_epg():
    _run('epg', download=True)

def reprocess(filetype):
    """Re-read the stored file with the current settings - no download."""
    _run(filetype, download=False)

def start_in_background(filetype, download):
    """Run an update on a daemon thread; False if one is already running."""
    if is_busy(filetype):
        return False
    def work():
        try:
            _run(filetype, download)
        except Exception as e:
            logger.exception(f"Unhandled error during manual {filetype} update.")
            _record_error(filetype, _mask(e))
    threading.Thread(target=work, name=f'iptv-manual-{filetype}', daemon=True).start()
    return True

def update_m3u_scheduled():
    # TODO: Assuming 4am, make configurable.
    next_m3u_loadtime = timezone.now().replace(hour=4,minute=0,second=0,microsecond=0)
    while True:
        while next_m3u_loadtime < timezone.now():
            next_m3u_loadtime = next_m3u_loadtime + timedelta(days=1)

        logging.info(f"Next M3U Load scheduled for {next_m3u_loadtime}")
        time.sleep((next_m3u_loadtime-timezone.now()).total_seconds())
        try:
            update_m3u()
        except Exception as e:
            # A single bad cycle shouldn't permanently kill the scheduler -
            # log it and try again at the next scheduled time.
            logger.exception("Unhandled error during scheduled M3U update; will retry next cycle.")
            _record_error('m3u', str(e))

def update_epg_scheduled():
    # TODO: Assuming every half hour, make configurable
    next_epg_loadtime = timezone.now().replace(minute=30,second=0,microsecond=0)
    while True:
        # give us plenty of lead time
        while next_epg_loadtime < timezone.now() + timedelta(minutes=35):
            next_epg_loadtime = next_epg_loadtime + timedelta(hours=1)
        
        logging.info(f"Next EPG Load scheduled for {next_epg_loadtime}")
        time.sleep((next_epg_loadtime-timezone.now()).total_seconds())
        try:
            update_epg()
        except Exception as e:
            logger.exception("Unhandled error during scheduled EPG update; will retry next cycle.")
            _record_error('epg', str(e))


# Many IPTV providers (Xtream-codes panels in particular) reset the connection
# for the default "python-requests/x.y" User-Agent. Send a browser-like one
# instead; override with IPTV_USER_AGENT if a provider wants something else.
DEFAULT_USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'

# (connect, read) timeouts. The read timeout is per-chunk / time-to-first-byte,
# not total: providers often take minutes to generate a big playlist.
CONNECT_TIMEOUT = 10
READ_TIMEOUT = int(os.environ.get('IPTV_READ_TIMEOUT') or 300)

_mask = settings_store.mask_url  # kept under this name for existing callers/tests

MAX_UNPACKED_BYTES = int(os.environ.get('IPTV_MAX_UNPACKED_MB') or 1024) * 1024 * 1024

def _gunzip(data):
    """Unpack gzip data, refusing to expand beyond MAX_UNPACKED_BYTES (a tiny
    file can otherwise decompress into gigabytes). Raises ValueError."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = d.decompress(data, MAX_UNPACKED_BYTES + 1)
        if len(out) > MAX_UNPACKED_BYTES:
            raise ValueError(f"unpacked file is larger than {MAX_UNPACKED_BYTES // (1024*1024)} MB")
        if not d.eof:
            raise ValueError("gzip data is truncated")
    except zlib.error as e:
        raise ValueError(f"not valid gzip data: {e}")
    return out

# "m3u" and "epg" for now...
def _retrieve(filetype):
    url, source = settings_store.get_url(filetype)
    if not url:
        logger.warning(f"No {filetype} URL configured yet (set it on the Settings page or via IPTV_{filetype.upper()}_URL) - skipping retrieval.")
        return False
    safe_url = _mask(url)
    origin = {'gui': 'configured in the web GUI', 'env': 'from environment variable', 'stored': 'stored value'}[source]

    logger.info(f"Retrieving fresh {filetype} file from {safe_url} ({origin})")

    headers = {'User-Agent': os.environ.get('IPTV_USER_AGENT') or DEFAULT_USER_AGENT}
    started = time.monotonic()
    try:
        r = requests.get(url, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        # Covers connection errors (DNS failures, refused connections, timeouts)
        # and non-2xx responses (raise_for_status) without crashing the caller.
        msg = _mask(e)
        logger.warning(f"Failed to retrieve {filetype} file from {safe_url}: {msg}")
        _record_error(filetype, msg)
        return False

    # Store the raw bytes as received (no decode/encode round trip: a provider
    # playlist can be hundreds of MB, and r.text would also run slow charset
    # detection or mis-decode when the server defaults to ISO-8859-1).
    data = r.content
    # Many EPG sources publish a gzipped file (epg.xml.gz). That is the *file* being
    # gzip, not HTTP Content-Encoding (which requests already undoes), so it has to be
    # recognised by its content: gzip always starts with the bytes 1f 8b.
    if data[:2] == b'\x1f\x8b':
        packed = len(data)
        try:
            data = _gunzip(data)
        except ValueError as e:
            logger.warning(f"Failed to unpack gzipped {filetype} file from {safe_url}: {e}")
            _record_error(filetype, f"Could not unpack gzip: {e}")
            return False
        logger.info(f"Unpacked gzipped {filetype} file: {packed} -> {len(data)} bytes")
    now = timezone.now()
    CachedFile.objects.update_or_create(file_type=filetype, defaults={'file':data, 'last_updated':now})
    logger.info(f"Received fresh {filetype} file, size {len(data)} in {time.monotonic()-started:.1f}s")
    _last_error.pop(filetype, None)
    return True

def _update_tables(filetype):
    # EPG is small enough to apply in one transaction. The M3U update is applied
    # in small batches instead (see _update_tables_impl): a single transaction
    # over a very large playlist holds SQLite's write lock for minutes and makes
    # every other request fail with "database is locked".
    if filetype == 'm3u':
        return _update_tables_impl(filetype)
    # The whole EPG is one transaction. If SQLite still reports the database as
    # locked (another writer held it past the timeout), nothing was applied, so
    # simply try again a few times before giving up.
    for attempt in range(1, EPG_ATTEMPTS + 1):
        try:
            with transaction.atomic():
                return _update_tables_impl(filetype)
        except OperationalError as e:
            if 'locked' not in str(e).lower() or attempt == EPG_ATTEMPTS:
                raise
            wait = 5 * attempt
            logger.warning(f"EPG update hit a locked database (attempt {attempt} of {EPG_ATTEMPTS}); retrying in {wait}s.")
            time.sleep(wait)

WRITE_BATCH = 2000
EPG_ATTEMPTS = 4
SKIP_VOD_RE = re.compile(r'(?i)^[a-z]+://[^/]+/(?:[^/]+/)?(?:movie|series)/')

def _update_tables_impl(filetype):
    now = timezone.now()
    cached = CachedFile.objects.filter(file_type=filetype)
    if not cached:
        logger.warning(f"No cached {filetype} file to process yet - skipping table update.")
        return False
    blob = cached[0].file
    if filetype != 'm3u':
        file = blob.decode()

    if filetype == 'm3u':
        # Real-world M3U sources don't agree on attribute order (or even which
        # attributes are present - tvg-id in particular is often missing), so
        # match each "key=value" pair independently instead of assuming a
        # fixed sequence like 'tvg-id="..." tvg-name="..." tvg-logo="..." group-title="..."'.
        extinf_pattern = re.compile(r'^#EXTINF:', re.IGNORECASE)
        attr_pattern = re.compile(r'([\w-]+)="(.*?)"')
        # Stream URLs in the wild aren't always http(s) - rtsp/rtmp/rtmps/udp/rtp
        # all show up in real IPTV playlists.
        urlpattern = re.compile(r'(?i)^(https?|rtsps?|rtmps?|udp|rtp)://')

        start_perftime = time.perf_counter()

        # Parse the file into a dict keyed by tvg_name (the natural/unique key).
        # A dict naturally de-dupes if the source has the same tvg_name twice
        # (last one wins), which also protects bulk_create from the unique
        # constraint on tvg_name.
        parsed = {}
        skip_vod, skip_source = settings_store.get_skip_vod()
        # Channels already ticked are kept even if they're movies/series, so
        # turning VOD on, ticking a few, and turning it off again keeps them.
        keep_names = set(PlaylistChannel.objects.filter(included=True).values_list('tvg_name', flat=True)) if skip_vod else set()
        skipped_vod = 0
        pending_extinf = None  # Reset so a stray/duplicate URL line can't reuse a stale channel from a previous iteration.
        # Iterate the raw bytes line by line instead of decoding + splitlines():
        # that would hold 2-3 extra copies of a (possibly 300+ MB) playlist in memory.
        for raw in io.BytesIO(blob):
            line = raw.decode('utf-8', errors='replace').rstrip('\r\n')
            if extinf_pattern.match(line):
                # This was an #EXTINF line.
                attrs = dict(attr_pattern.findall(line))
                # The display name is whatever follows the last comma on the
                # line (the M3U convention), used as a fallback if tvg-name
                # itself is missing.
                display_name = line.rsplit(',', 1)[-1].strip() if ',' in line else ''
                tvg_name = attrs.get('tvg-name') or display_name
                if not tvg_name:
                    # Nothing usable to key this channel on - skip it.
                    pending_extinf = None
                    continue
                # (name, tvg_id, tvg_logo, group_title) - the stream URL is on the next line.
                # group_title is interned: the same few hundred strings repeat across a million rows.
                pending_extinf = (tvg_name, attrs.get('tvg-id', ''), attrs.get('tvg-logo', ''),
                                  sys.intern(attrs.get('group-title', '')))

            else:
                if urlpattern.match(line) and pending_extinf is not None:
                    if skip_vod and SKIP_VOD_RE.match(line) and pending_extinf[0] not in keep_names:
                        skipped_vod += 1
                        pending_extinf = None
                        continue
                    # This is the URL line.
                    # Stored as plain tuples, not dicts: ~3x less memory per entry,
                    # which matters at a million+ entries.
                    parsed[pending_extinf[0]] = pending_extinf[1:] + (line,)
                    pending_extinf = None  # Consumed; don't let a duplicate/extra URL line re-add this channel.
                # else: not a recognized stream URL (e.g. a "[NO PUBLIC STREAM]"
                # placeholder) - the pending channel has no usable stream and
                # is dropped when the next #EXTINF line overwrites pending_extinf.

        parse_secs = time.perf_counter() - start_perftime
        logger.info(f"Parsed {len(parsed)} playlist entries in {parse_secs:.1f}s.")

        # Diff against what's already in the DB instead of deleting everything
        # and recreating it: most channels are unchanged between pulls, so we
        # only need to write the rows that are actually new, changed, or gone.
        # .values() avoids the cost of instantiating a full model object per
        # row just to compare a few fields.
        diff_start = time.perf_counter()
        existing = {}
        # .iterator() streams rows instead of caching a second copy of them.
        for pk, name, tid, logo, group, url in PlaylistChannel.objects.values_list(
                'pk', 'tvg_name', 'tvg_id', 'tvg_logo', 'group_title', 'stream_url').iterator(chunk_size=5000):
            existing[name] = (pk, tid, logo, group, url)

        new_names = []  # names to insert; their data is read back from `parsed` while writing
        to_update = []  # (tvg_id, tvg_logo, group_title, stream_url, pk)
        for tvg_name, pdata in parsed.items():
            ex = existing.get(tvg_name)
            if ex is not None:
                if ex[1:] != pdata:
                    to_update.append(pdata + (ex[0],))
                # else: unchanged, nothing to write.
            else:
                # TODO: Whitelists/blacklists for whether to include newly found channels (movies/tv series probably)
                new_names.append(tvg_name)

        stale_pks = [ex[0] for name, ex in existing.items() if name not in parsed]
        unchanged = len(parsed) - len(new_names) - len(to_update)
        existing = None  # free memory before the (long) write phase
        logger.info(f"Compared against the database in {time.perf_counter()-diff_start:.1f}s: "
                    f"{len(new_names)} to add, {len(to_update)} to update, {len(stale_pks)} to remove.")

        # Write with plain executemany() instead of the ORM. For bulk inserts the
        # ORM spends most of its time converting every single value (~3x slower
        # than the database itself on a million-row playlist). Batches are small
        # and each is its own transaction, so the web UI can keep using the
        # database while a big update runs.
        write_start = time.perf_counter()
        table = connection.ops.quote_name(PlaylistChannel._meta.db_table)
        now_db = connection.ops.adapt_datetimefield_value(now)
        for i in range(0, len(stale_pks), WRITE_BATCH):
            with transaction.atomic():
                PlaylistChannel.objects.filter(pk__in=stale_pks[i:i+WRITE_BATCH]).delete()
        insert_sql = (f"INSERT INTO {table} (tvg_id, tvg_name, tvg_logo, stream_url, group_title, "
                      f"first_seen, last_updated, included) VALUES (%s, %s, %s, %s, %s, %s, %s, 0)")
        for i in range(0, len(new_names), WRITE_BATCH):
            rows = []
            for name in new_names[i:i+WRITE_BATCH]:
                tid, logo, group, url = parsed[name]
                rows.append((tid, name, logo, url, group, now_db, now_db))
            with transaction.atomic(), connection.cursor() as cur:
                cur.executemany(insert_sql, rows)
        update_sql = (f"UPDATE {table} SET tvg_id = %s, tvg_logo = %s, group_title = %s, stream_url = %s, "
                      f"last_updated = '{now_db}' WHERE id = %s")
        for i in range(0, len(to_update), WRITE_BATCH):
            with transaction.atomic(), connection.cursor() as cur:
                cur.executemany(update_sql, to_update[i:i+WRITE_BATCH])
        logger.info(f"Wrote changes in {time.perf_counter()-write_start:.1f}s.")
        if skipped_vod:
            logger.info(f"Skipped {skipped_vod} movie/series entries (skip movies/series is ON, set via {skip_source}); channels you already ticked are kept.")

        logging.info(
            f"Done with Channels in {time.perf_counter()-start_perftime} seconds. "
            f"({len(new_names)} new, {len(to_update)} changed, {len(stale_pks)} removed, "
            f"{unchanged} unchanged)"
        )

    elif filetype == 'epg':
        root = ET.fromstring(file)
        included_channel_ids = set(PlaylistChannel.objects.filter(included=True).values_list('tvg_id', flat=True))

        # channel/programme are direct children of the root <tv> element in
        # XMLTV files, so a plain (non-recursive) findall avoids walking every
        # descendant element (title/desc/category/credits/etc. inside every
        # programme) that './/' would otherwise visit.
        start_perftime = time.perf_counter()
        parsed_channels = {}
        for channel in root.findall('channel'):
            ch_id = channel.get('id')
            if not ch_id or len(ch_id) == 0:
                continue

            # Reset per-channel so a channel missing one of these elements
            # doesn't silently inherit the previous channel's value.
            display_name = ch_id
            icon = None

            for child in channel:
                if child.tag == 'display-name':
                    display_name = child.text or ch_id
                elif child.tag == 'icon':
                    icon = child.get('src')

            parsed_channels[ch_id] = {'display_name': display_name, 'icon': icon}

        existing_channels = {
            v['channel_id']: v for v in
            EpgChannel.objects.all().values('pk', 'channel_id', 'display_name', 'icon', 'included')
        }

        to_create = []
        to_update = []
        for ch_id, pdata in parsed_channels.items():
            included = ch_id in included_channel_ids
            if ch_id in existing_channels:
                ex = existing_channels[ch_id]
                if ex['display_name'] != pdata['display_name'] or ex['icon'] != pdata['icon'] or ex['included'] != included:
                    to_update.append(EpgChannel(pk=ex['pk'], display_name=pdata['display_name'], icon=pdata['icon'], included=included, last_updated=now))
            else:
                to_create.append(EpgChannel(channel_id=ch_id, display_name=pdata['display_name'], icon=pdata['icon'], included=included, last_updated=now))

        stale_channel_ids = existing_channels.keys() - parsed_channels.keys()
        if stale_channel_ids:
            EpgChannel.objects.filter(channel_id__in=stale_channel_ids).delete()
        if to_create:
            EpgChannel.objects.bulk_create(to_create)
        if to_update:
            EpgChannel.objects.bulk_update(to_update, ['display_name', 'icon', 'included', 'last_updated'])

        logging.info(
            f"Done with EPG Channels in {time.perf_counter()-start_perftime} seconds. "
            f"({len(to_create)} new, {len(to_update)} changed, {len(stale_channel_ids)} removed)"
        )

        start_perftime = time.perf_counter()
        parsed_programmes = {}
        for programme in root.findall('programme'):
            ch_id = programme.get('channel')
            if not ch_id or len(ch_id) == 0:
                continue

            start = programme.get('start')
            stop = programme.get('stop')

            # Reset per-programme so a programme missing one of these
            # elements doesn't silently inherit the previous one's value.
            title = ''
            desc = ''

            for child in programme:
                if child.tag == 'title':
                    title = child.text or ''
                elif child.tag == 'desc':
                    desc = child.text or ''

            # (channel, start) is the natural key for a programme slot.
            parsed_programmes[(ch_id, start)] = {'stop': stop, 'title': title, 'desc': desc}

        existing_programmes = {
            (v['channel'], v['start']): v for v in
            EpgProgramme.objects.all().values('pk', 'channel', 'start', 'stop', 'title', 'desc', 'included')
        }

        to_create = []
        to_update = []
        for key, pdata in parsed_programmes.items():
            ch_id, start = key
            included = ch_id in included_channel_ids
            if key in existing_programmes:
                ex = existing_programmes[key]
                if (ex['stop'] != pdata['stop'] or ex['title'] != pdata['title']
                        or ex['desc'] != pdata['desc'] or ex['included'] != included):
                    to_update.append(EpgProgramme(pk=ex['pk'], stop=pdata['stop'], title=pdata['title'], desc=pdata['desc'], included=included, last_updated=now))
            else:
                to_create.append(EpgProgramme(channel=ch_id, start=start, stop=pdata['stop'], title=pdata['title'], desc=pdata['desc'], included=included, last_updated=now))

        stale_keys = existing_programmes.keys() - parsed_programmes.keys()
        if stale_keys:
            stale_pks = [existing_programmes[k]['pk'] for k in stale_keys]
            EpgProgramme.objects.filter(pk__in=stale_pks).delete()
        if to_create:
            EpgProgramme.objects.bulk_create(to_create)
        if to_update:
            EpgProgramme.objects.bulk_update(to_update, ['stop', 'title', 'desc', 'included', 'last_updated'])

        logging.info(
            f"Done with EPG Programmes in {time.perf_counter()-start_perftime} seconds. "
            f"({len(to_create)} new, {len(to_update)} changed, {len(stale_keys)} removed, "
            f"{len(parsed_programmes)-len(to_create)-len(to_update)} unchanged)"
        )

        # Channels whose playlist id differs from the EPG id only in letter case
        # are served with the corrected id; make sure their guide is included.
        fixed = epg_quality.sync_flags()
        logging.info(f"EPG selection synced with the ticked channels ({fixed} rows flagged differently).")

    return True
