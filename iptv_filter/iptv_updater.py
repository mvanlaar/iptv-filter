import requests
import re
import time
from datetime import timedelta
import xml.etree.ElementTree as ET
from django.db import transaction
from django.utils import timezone
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

def update_m3u():
    if _retrieve('m3u'):
        _update_tables('m3u')
    else:
        logger.warning("Skipping m3u table update because retrieval failed.")

def update_epg():
    if _retrieve('epg'):
        _update_tables('epg')
    else:
        logger.warning("Skipping epg table update because retrieval failed.")

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


# "m3u" and "epg" for now...
def _retrieve(filetype):
    configs = AppConfig.objects.filter(key=filetype+"_url")
    if not configs:
        logger.warning(f"No {filetype} URL configured yet (set IPTV_{filetype.upper()}_URL or configure it) - skipping retrieval.")
        return False
    url = configs[0].value

    logging.info(f"Retrieving fresh {filetype} file from {url}")

    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        # Covers connection errors (DNS failures, refused connections, timeouts)
        # and non-2xx responses (raise_for_status) without crashing the caller.
        logger.warning(f"Failed to retrieve {filetype} file from {url}: {e}")
        _record_error(filetype, str(e))
        return False

    now = timezone.now()
    CachedFile.objects.update_or_create(file_type=filetype, defaults={'file':r.text.encode(), 'last_updated':now})
    logging.info(f"Received fresh {filetype} file, size {len(r.text.encode())}")
    _last_error.pop(filetype, None)
    return True

@transaction.atomic
def _update_tables(filetype):
    now = timezone.now()
    cached = CachedFile.objects.filter(file_type=filetype)
    if not cached:
        logger.warning(f"No cached {filetype} file to process yet - skipping table update.")
        return False
    file = cached[0].file.decode()

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
        pending_extinf = None  # Reset so a stray/duplicate URL line can't reuse a stale channel from a previous iteration.
        for line in file.splitlines():
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
                pending_extinf = {
                    'tvg_id': attrs.get('tvg-id', ''),
                    'tvg_name': tvg_name,
                    'tvg_logo': attrs.get('tvg-logo', ''),
                    'group_title': attrs.get('group-title', ''),
                }
                # stream_url gets filled in on the next line.

            else:
                if urlpattern.match(line) and pending_extinf is not None:
                    # This is the URL line.
                    pending_extinf['stream_url'] = line
                    parsed[pending_extinf['tvg_name']] = pending_extinf
                    pending_extinf = None  # Consumed; don't let a duplicate/extra URL line re-add this channel.
                # else: not a recognized stream URL (e.g. a "[NO PUBLIC STREAM]"
                # placeholder) - the pending channel has no usable stream and
                # is dropped when the next #EXTINF line overwrites pending_extinf.

        # Diff against what's already in the DB instead of deleting everything
        # and recreating it: most channels are unchanged between pulls, so we
        # only need to write the rows that are actually new, changed, or gone.
        # .values() avoids the cost of instantiating a full model object per
        # row just to compare a few fields.
        existing = {
            v['tvg_name']: v for v in
            PlaylistChannel.objects.all().values('pk', 'tvg_name', 'tvg_id', 'tvg_logo', 'group_title', 'stream_url')
        }

        to_create = []
        to_update = []
        for tvg_name, pdata in parsed.items():
            if tvg_name in existing:
                ex = existing[tvg_name]
                if (ex['tvg_id'] != pdata['tvg_id'] or ex['tvg_logo'] != pdata['tvg_logo']
                        or ex['group_title'] != pdata['group_title'] or ex['stream_url'] != pdata['stream_url']):
                    to_update.append(PlaylistChannel(
                        pk=ex['pk'], tvg_id=pdata['tvg_id'], tvg_logo=pdata['tvg_logo'],
                        group_title=pdata['group_title'], stream_url=pdata['stream_url'], last_updated=now))
                # else: unchanged, nothing to write.
            else:
                # TODO: Whitelists/blacklists for whether to include newly found channels (movies/tv series probably)
                to_create.append(PlaylistChannel(
                    tvg_id=pdata['tvg_id'], tvg_name=tvg_name, tvg_logo=pdata['tvg_logo'],
                    group_title=pdata['group_title'], stream_url=pdata['stream_url'],
                    last_updated=now, first_seen=now, included=False))

        stale_names = existing.keys() - parsed.keys()
        if stale_names:
            PlaylistChannel.objects.filter(tvg_name__in=stale_names).delete()
        if to_create:
            PlaylistChannel.objects.bulk_create(to_create)
        if to_update:
            PlaylistChannel.objects.bulk_update(to_update, ['tvg_id', 'tvg_logo', 'group_title', 'stream_url', 'last_updated'])

        logging.info(
            f"Done with Channels in {time.perf_counter()-start_perftime} seconds. "
            f"({len(to_create)} new, {len(to_update)} changed, {len(stale_names)} removed, "
            f"{len(parsed)-len(to_create)-len(to_update)} unchanged)"
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

    return True
