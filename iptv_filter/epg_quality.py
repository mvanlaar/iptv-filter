"""Keeping the EPG in step with the playlist selection, and reporting how well
the two actually line up.

The link between a playlist channel and the guide is an exact, case-sensitive
match between the M3U ``tvg-id`` and the XMLTV channel ``id``. A channel with
an empty or non-matching ``tvg-id`` simply gets no guide data; the coverage
report shows which ones and why.
"""
import logging
import re
from datetime import datetime

from django.db.models import Count, Max, Min, Q
from django.utils import timezone

from .models import EpgChannel, EpgProgramme, PlaylistChannel

logger = logging.getLogger(__name__)

LIST_LIMIT = 500   # rows shown per problem list


def _selected_ids():
    """Subquery of the tvg-ids of ticked playlist channels (empty ids ignored)."""
    return PlaylistChannel.objects.filter(included=True).exclude(tvg_id='').values('tvg_id')


def sync_flags():
    """Set EpgChannel/EpgProgramme.included to match the ticked playlist channels,
    right now. Only rows that actually change are written, using set-based
    UPDATEs (no per-channel queries). Returns the number of rows changed."""
    changed = 0
    for model, column in ((EpgChannel, 'channel_id'), (EpgProgramme, 'channel')):
        selected = {f'{column}__in': _selected_ids()}
        # NULL counts as "not included" for both directions.
        changed += model.objects.filter(**selected).exclude(included=True).update(included=True)
        changed += model.objects.filter(~Q(**selected)).exclude(included=False).update(included=False)
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

    matched = 0
    problems = {'no_id': [], 'case_only': [], 'missing': [], 'empty': []}
    for pk, name, tvg_id, group in ticked:
        row = {'pk': pk, 'name': name, 'tvg_id': tvg_id, 'group': group}
        if not tvg_id:
            problems['no_id'].append(row)
        elif tvg_id in epg_ids:
            if programme_counts.get(tvg_id, 0) == 0:
                problems['empty'].append(row)
            else:
                matched += 1
        elif tvg_id.lower() in by_lower:
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
        'problems': {k: v[:LIST_LIMIT] for k, v in problems.items()},
        'truncated': any(len(v) > LIST_LIMIT for v in problems.values()),
        'horizon': horizon,
    }
