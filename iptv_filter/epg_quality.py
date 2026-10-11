"""Keeping the EPG in step with the playlist selection, and reporting how well
the two actually line up.

The link between a playlist channel and the guide is an exact, case-sensitive
match between the M3U ``tvg-id`` and the XMLTV channel ``id``. Providers often
differ from the EPG only in letter case (``npo1.nl`` vs ``NPO1.nl``); players
match exactly, so those channels would get nothing. Such ids are corrected in
the M3U we serve (see ``id_overrides``), but only when the channel's name
agrees with the EPG channel, because providers also hand out plain wrong ids.
A channel with an empty or non-matching ``tvg-id`` gets no guide data; the
coverage report shows which ones and why.
"""
import logging
import os
import re
from datetime import datetime

from django.db.models import Count, Max, Min, Q
from django.utils import timezone

from .models import EpgChannel, EpgProgramme, PlaylistChannel

logger = logging.getLogger(__name__)

LIST_LIMIT = 500   # rows shown per problem list

# Words that say nothing about *which* channel it is (quality tags, country,
# generic words). Ignored when comparing a playlist name with an EPG id.
_NOISE = {'nl', 'hd', 'fhd', 'uhd', 'sd', 'hevc', '4k', 'tv', 'fm', 'omroep', 'rtv', 'radio', 'channel', 'live'}


def fix_enabled():
    """Case-correction of tvg-ids in the M3U output. On unless IPTV_FIX_EPG_IDS is 0/false/no/off."""
    return (os.environ.get('IPTV_FIX_EPG_IDS') or '1').strip().lower() not in ('0', 'false', 'no', 'off')


def _alnum(text):
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


def names_agree(channel_name, epg_id, epg_display_name=''):
    """Does the playlist channel name plausibly belong to this EPG channel?
    Guards against providers that put another channel's id on a channel
    (e.g. 'TV NOORD' carrying the id of 'RTV Utrecht'). Quality tags and generic
    words are ignored; numbers must match exactly (NPO 1 is not npo2); at
    least one distinctive word must occur in the EPG id or display name."""
    stem = epg_id.rsplit('.', 1)[0] if '.' in epg_id else epg_id
    hay = _alnum(stem) + ' ' + _alnum(epg_display_name)
    tokens = [t for t in re.split(r'[^a-z0-9]+', (channel_name or '').lower()) if t and t not in _NOISE]
    if not tokens:
        return True
    for t in tokens:
        if t.isdigit() and t not in hay:
            return False
    words = [t for t in tokens if not t.isdigit()]
    if not words:
        return True
    for t in words:
        if t in hay or (len(t) >= 5 and t[:4] in hay):
            return True
    return False


def id_overrides():
    """Which ticked channels get a corrected tvg-id in the M3U output.

    Returns (overrides, conflicts). ``overrides`` maps PlaylistChannel pk -> the
    EPG channel id to write. ``conflicts`` lists channels whose id matches an EPG
    channel only when case is ignored, but whose name does not fit that EPG
    channel; those are left alone. Both are empty when the fix is switched off.
    """
    if not fix_enabled():
        return {}, []
    epg, exact = {}, set()
    for cid, name in EpgChannel.objects.values_list('channel_id', 'display_name'):
        epg.setdefault(cid.lower(), (cid, name))
        exact.add(cid)
    overrides, conflicts = {}, []
    for pk, name, tvg_id, group in (PlaylistChannel.objects.filter(included=True).exclude(tvg_id='')
                                    .values_list('pk', 'tvg_name', 'tvg_id', 'group_title')):
        if tvg_id in exact:
            continue
        hit = epg.get(tvg_id.lower())
        if not hit:
            continue
        if names_agree(name, hit[0], hit[1]):
            overrides[pk] = hit[0]
        else:
            conflicts.append({'pk': pk, 'name': name, 'tvg_id': tvg_id, 'group': group, 'epg_id': hit[0],
                              'epg_name': hit[1]})
    return overrides, conflicts


def _selected_ids():
    """Subquery of the tvg-ids of ticked playlist channels (empty ids ignored)."""
    return PlaylistChannel.objects.filter(included=True).exclude(tvg_id='').values('tvg_id')


def sync_flags():
    """Set EpgChannel/EpgProgramme.included to match the ticked playlist channels,
    right now. Only rows that actually change are written, using set-based
    UPDATEs (no per-channel queries). Returns the number of rows changed."""
    changed = 0
    corrected = sorted(set(id_overrides()[0].values()))   # EPG ids reached through the case fix
    for model, column in ((EpgChannel, 'channel_id'), (EpgProgramme, 'channel')):
        selected = Q(**{f'{column}__in': _selected_ids()})
        if corrected:
            selected |= Q(**{f'{column}__in': corrected})
        # NULL counts as "not included" for both directions.
        changed += model.objects.filter(selected).exclude(included=True).update(included=True)
        changed += model.objects.filter(~selected).exclude(included=False).update(included=False)
    return changed


def _parse_xmltv_time(value):
    """XMLTV times look like '20261011123000 +0200'. -> aware datetime or None."""
    value = (value or '').strip()
    for fmt in ('%Y%m%d%H%M%S %z', '%Y%m%d%H%M%S'):
        try:
            dt = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return dt if dt.tzinfo else timezone.make_aware(dt)
    return None


def coverage():
    """How many ticked channels get guide data, and why the others don't."""
    ticked = list(PlaylistChannel.objects.filter(included=True)
                  .values_list('pk', 'tvg_name', 'tvg_id', 'group_title').order_by('group_title', 'tvg_name'))
    epg_ids = set(EpgChannel.objects.values_list('channel_id', flat=True))
    by_lower = {}
    for i in epg_ids:
        by_lower.setdefault(i.lower(), i)
    programme_counts = dict(EpgProgramme.objects.filter(included=True).values('channel')
                            .annotate(n=Count('id')).values_list('channel', 'n'))

    overrides, conflicts = id_overrides()
    conflict_pks = {c['pk'] for c in conflicts}
    matched = 0
    problems = {'no_id': [], 'case_only': [], 'conflict': [], 'missing': [], 'empty': []}
    fixed = []
    for pk, name, tvg_id, group in ticked:
        row = {'pk': pk, 'name': name, 'tvg_id': tvg_id, 'group': group}
        if not tvg_id:
            problems['no_id'].append(row)
        elif tvg_id in epg_ids:
            if programme_counts.get(tvg_id, 0) == 0:
                problems['empty'].append(row)
            else:
                matched += 1
        elif pk in overrides:
            row['epg_id'] = overrides[pk]
            if programme_counts.get(overrides[pk], 0) == 0:
                problems['empty'].append(row)
            else:
                matched += 1
                fixed.append(row)
        elif pk in conflict_pks:
            c = next(c for c in conflicts if c['pk'] == pk)
            row['epg_id'] = c['epg_id']
            row['epg_name'] = c['epg_name']
            problems['conflict'].append(row)
        elif tvg_id.lower() in by_lower:       # fix switched off
            row['epg_id'] = by_lower[tvg_id.lower()]
            problems['case_only'].append(row)
        else:
            problems['missing'].append(row)

    horizon = None
    agg = EpgProgramme.objects.filter(included=True).aggregate(first=Min('start'), last=Max('stop'))
    first, last = _parse_xmltv_time(agg['first']), _parse_xmltv_time(agg['last'])
    if first and last:
        horizon = {'first': first, 'last': last, 'hours_ahead': round((last - timezone.now()).total_seconds() / 3600, 1)}

    total = len(ticked)
    return {
        'ticked': total,
        'matched': matched,
        'percent': round(100 * matched / total) if total else 0,
        'epg_channels': len(epg_ids),
        'counts': {k: len(v) for k, v in problems.items()},
        'fixed': fixed[:LIST_LIMIT],
        'fixed_count': len(fixed),
        'fix_enabled': fix_enabled(),
        'problems': {k: v[:LIST_LIMIT] for k, v in problems.items()},
        'truncated': any(len(v) > LIST_LIMIT for v in problems.values()),
        'horizon': horizon,
    }
