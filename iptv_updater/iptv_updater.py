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

def update_all():
    update_m3u()
    update_epg()

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
        update_m3u()

def update_epg_scheduled():
    # TODO: Assuming every half hour, make configurable
    next_epg_loadtime = timezone.now().replace(minute=30,second=0,microsecond=0)
    while True:
        # give us plenty of lead time
        while next_epg_loadtime < timezone.now() + timedelta(minutes=35):
            next_epg_loadtime = next_epg_loadtime + timedelta(hours=1)
        
        logging.info(f"Next EPG Load scheduled for {next_epg_loadtime}")
        time.sleep((next_epg_loadtime-timezone.now()).total_seconds())
        update_epg()


# "m3u" and "epg" for now...
def _retrieve(filetype):
    # TODO: What if there is no URL?
    configs = AppConfig.objects.filter(key=filetype+"_url")
    url = configs[0].value

    logging.info(f"Retrieving fresh {filetype} file from {url}")

    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        # Covers connection errors (DNS failures, refused connections, timeouts)
        # and non-2xx responses (raise_for_status) without crashing the caller.
        logger.warning(f"Failed to retrieve {filetype} file from {url}: {e}")
        return False

    now = timezone.now()
    CachedFile.objects.update_or_create(file_type=filetype, defaults={'file':r.text.encode(), 'last_updated':now})
    logging.info(f"Received fresh {filetype} file, size {len(r.text.encode())}")
    return True

@transaction.atomic
def _update_tables(filetype):
    now = timezone.now()
    # TODO: what if file doesn't exist?
    file = CachedFile.objects.filter(file_type=filetype)[0].file.decode()

    if filetype == 'm3u':
        # TODO: store regex in AppConfig, as well as relative index/position of id, name, etc. in case other providers format this differently.
        infopattern = re.compile('(?i)#EXTINF:-1 tvg-id="(.*?)" tvg-name="(.*?)" tvg-logo="(.*?)" group-title="(.*?)",(.*?)')
        urlpattern = re.compile('(?i)^http')

        start_perftime = time.perf_counter()

        # Parse the file into a dict keyed by tvg_name (the natural/unique key).
        # A dict naturally de-dupes if the source has the same tvg_name twice
        # (last one wins), which also protects bulk_create from the unique
        # constraint on tvg_name.
        parsed = {}
        pending_extinf = None  # Reset so a stray/duplicate URL line can't reuse a stale channel from a previous iteration.
        for line in file.splitlines():
            m = infopattern.findall(line)
            if len(m) > 0:
                # This was an #EXTINF line.
                pending_extinf = {'tvg_id': m[0][0], 'tvg_name': m[0][1], 'tvg_logo': m[0][2], 'group_title': m[0][3]}
                # stream_url gets filled in on the next line.

            else:
                if urlpattern.match(line) and pending_extinf is not None:
                    # This is the URL line.
                    pending_extinf['stream_url'] = line
                    parsed[pending_extinf['tvg_name']] = pending_extinf
                    pending_extinf = None  # Consumed; don't let a duplicate/extra URL line re-add this channel.

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
