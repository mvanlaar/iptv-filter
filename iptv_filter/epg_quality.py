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

from difflib import SequenceMatcher

from .models import EpgChannel, EpgMapping, EpgProgramme, PlaylistChannel

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


def manual_mappings():
    """{tvg_name: (epg_id, no_guide)} for the decisions made on the coverage page."""
    return {n: (e, g) for n, e, g in EpgMapping.objects.values_list('tvg_name', 'epg_id', 'no_guide')}


def id_overrides():
    """Which ticked channels get a different tvg-id in the M3U output.

    Returns (overrides, conflicts). ``overrides`` maps PlaylistChannel pk -> the
    EPG channel id to write. Two sources, the manual one winning:

    * a mapping you made by hand on the coverage page (any channel, also ones
      without an id), as long as that EPG channel exists;
    * the automatic letter-case correction (unless IPTV_FIX_EPG_IDS=0), applied
      only when the channel name fits the EPG channel.

    ``conflicts`` lists channels whose id matches an EPG channel only when case
    is ignored, but whose name does not fit; those are left alone.
    """
    epg, exact = {}, set()
    for cid, name in EpgChannel.objects.values_list('channel_id', 'display_name'):
        epg.setdefault(cid.lower(), (cid, name))
        exact.add(cid)
    manual = manual_mappings()
    auto = fix_enabled()
    overrides, conflicts = {}, []
    for pk, name, tvg_id, group in (PlaylistChannel.objects.filter(included=True)
                                    .values_list('pk', 'tvg_name', 'tvg_id', 'group_title')):
        m = manual.get(name)
        if m and not m[1] and m[0] in exact:
            overrides[pk] = m[0]
            continue
        if m and m[1]:
            continue                      # marked "no guide": leave it alone
        if not auto or not tvg_id or tvg_id in exact:
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


def _norm_name(text):
    """Comparable form of a channel name: lower case, quality tags and
    generic words dropped, only letters and digits left."""
    tokens = [t for t in re.split(r'[^a-z0-9]+', (text or '').lower()) if t and t not in _NOISE]
    return ''.join(tokens)


def _digits(text):
    return re.findall(r'\d+', text)


def _is_radio(text):
    return bool(re.search(r'\b(radio|fm)\b', (text or '').lower()))


def suggest(rows, epg_channels, threshold=0.88):
    """Fill in row['suggest'] = {'epg_id', 'name', 'score'} for rows whose name
    resembles an EPG channel closely (numbers must be identical). Names are
    de-duplicated first, so HD/FHD/HEVC variants cost one comparison."""
    cands = []
    for cid, disp in epg_channels:
        keys = {_norm_name(disp), _norm_name(cid.rsplit('.', 1)[0])} - {''}
        cands.append((cid, disp, keys, _is_radio(disp) or _is_radio(cid)))
    cache = {}
    for row in rows:
        key = _norm_name(row['name'])
        if len(key) < 3:
            continue
        if key not in cache:
            best = None
            radio = _is_radio(row['name'])
            for cid, disp, keys, c_radio in cands:
                if c_radio != radio:       # a radio station is not its TV namesake
                    continue
                for k in keys:
                    sm = SequenceMatcher(None, key, k, autojunk=False)
                    if sm.real_quick_ratio() < threshold or sm.quick_ratio() < threshold:
                        continue
                    score = sm.ratio()
                    if score >= threshold and _digits(k) == _digits(key) and (best is None or score > best[2]):
                        best = (cid, disp, score)
            cache[key] = best
        best = cache[key]
        if best:
            row['suggest'] = {'epg_id': best[0], 'name': best[1], 'score': round(best[2] * 100)}


def _selected_ids():
    """Subquery of the tvg-ids of ticked playlist channels (empty ids ignored)."""
    return PlaylistChannel.objects.filter(included=True).exclude(tvg_id='').values('tvg_id')


def sync_flags(only=None):
    """Set EpgChannel/EpgProgramme.included to match the ticked playlist channels,
    right now. Only rows that actually change are written, using set-based
    UPDATEs (no per-channel queries). Returns the number of rows changed.

    ``only`` limits the work to the given EPG channel ids (used after a single
    manual mapping, where only a couple of ids can have changed)."""
    if only is not None:
        only = sorted({i for i in only if i})
        if not only:
            return 0
    changed = 0
    corrected = sorted(set(id_overrides()[0].values()))   # EPG ids reached through a mapping or the case fix
    if only is not None:
        # A few ids: decide them here with one small query instead of letting every
        # UPDATE re-scan the (huge) playlist table through a subquery.
        wanted = set(PlaylistChannel.objects.filter(included=True, tvg_id__in=only).values_list('tvg_id', flat=True))
        wanted |= set(only) & set(corrected)
        for model, column in ((EpgChannel, 'channel_id'), (EpgProgramme, 'channel')):
            on = [i for i in only if i in wanted]
            off = [i for i in only if i not in wanted]
            if on:
                changed += model.objects.filter(**{f'{column}__in': on}).exclude(included=True).update(included=True)
            if off:
                changed += model.objects.filter(**{f'{column}__in': off}).exclude(included=False).update(included=False)
        return changed
    for model, column in ((EpgChannel, 'channel_id'), (EpgProgramme, 'channel')):
        selected = Q(**{f'{column}__in': _selected_ids()})
        if corrected:
            selected |= Q(**{f'{column}__in': corrected})
        rows = model.objects.all()
        # NULL counts as "not included" for both directions.
        changed += rows.filter(selected).exclude(included=True).update(included=True)
        changed += rows.filter(~selected).exclude(included=False).update(included=False)
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


def coverage(with_suggestions=False):
    """How many ticked channels get guide data, and why the others don't."""
    ticked = list(PlaylistChannel.objects.filter(included=True)
                  .values_list('pk', 'tvg_name', 'tvg_id', 'group_title').order_by('group_title', 'tvg_name'))
    epg_rows = list(EpgChannel.objects.values_list('channel_id', 'display_name'))
    epg_ids = {c for c, _ in epg_rows}
    by_lower = {}
    for i in epg_ids:
        by_lower.setdefault(i.lower(), i)
    programme_counts = dict(EpgProgramme.objects.filter(included=True).values('channel')
                            .annotate(n=Count('id')).values_list('channel', 'n'))

    overrides, conflicts = id_overrides()
    conflicts_by_pk = {c['pk']: c for c in conflicts}
    manual = manual_mappings()
    matched = 0
    problems = {'no_id': [], 'case_only': [], 'conflict': [], 'missing': [], 'empty': []}
    fixed, mapped, no_guide = [], [], []
    for pk, name, tvg_id, group in ticked:
        row = {'pk': pk, 'name': name, 'tvg_id': tvg_id, 'group': group}
        m = manual.get(name)
        if m and m[1]:
            no_guide.append(row)
        elif pk in overrides:
            row['epg_id'] = overrides[pk]
            if programme_counts.get(overrides[pk], 0) == 0:
                problems['empty'].append(row)
            else:
                matched += 1
                (mapped if m else fixed).append(row)
        elif m and m[0]:                       # mapped by hand, but that EPG channel is gone
            row['epg_id'] = m[0]
            row['stale'] = True
            mapped.append(row)
        elif not tvg_id:
            problems['no_id'].append(row)
        elif tvg_id in epg_ids:
            if programme_counts.get(tvg_id, 0) == 0:
                problems['empty'].append(row)
            else:
                matched += 1
        elif pk in conflicts_by_pk:
            c = conflicts_by_pk[pk]
            row['epg_id'], row['epg_name'] = c['epg_id'], c['epg_name']
            problems['conflict'].append(row)
        elif tvg_id.lower() in by_lower:       # automatic fix switched off
            row['epg_id'] = by_lower[tvg_id.lower()]
            problems['case_only'].append(row)
        else:
            problems['missing'].append(row)

    horizon = None
    agg = EpgProgramme.objects.filter(included=True).aggregate(first=Min('start'), last=Max('stop'))
    first, last = _parse_xmltv_time(agg['first']), _parse_xmltv_time(agg['last'])
    if first and last:
        horizon = {'first': first, 'last': last, 'hours_ahead': round((last - timezone.now()).total_seconds() / 3600, 1)}

    if with_suggestions:
        epg_rows.sort()
        for key in ('no_id', 'missing', 'conflict', 'empty', 'case_only'):
            suggest(problems[key][:LIST_LIMIT], epg_rows)

    total = len(ticked)
    expected = total - len(no_guide)       # channels you said have no guide don't count against the score
    return {
        'ticked': total,
        'expected': expected,
        'matched': matched,
        'percent': round(100 * matched / expected) if expected else 0,
        'epg_channels': len(epg_ids),
        'epg_list': sorted(epg_rows, key=lambda r: r[1].lower()) if with_suggestions else [],
        'counts': {k: len(v) for k, v in problems.items()},
        'problems': {k: v[:LIST_LIMIT] for k, v in problems.items()},
        'fixed': fixed[:LIST_LIMIT],
        'fixed_count': len(fixed),
        'mapped': mapped[:LIST_LIMIT],
        'mapped_count': len(mapped),
        'no_guide': no_guide[:LIST_LIMIT],
        'no_guide_count': len(no_guide),
        'fix_enabled': fix_enabled(),
        'truncated': any(len(v) > LIST_LIMIT for v in list(problems.values()) + [fixed, mapped, no_guide]),
        'horizon': horizon,
    }
