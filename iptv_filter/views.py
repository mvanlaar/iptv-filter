import copy
import os
import time
import json
import xml.etree.ElementTree as ET
from django.contrib import messages
from django.db.models.functions import Length
from django.shortcuts import render, redirect
from django.http import JsonResponse, HttpResponse, StreamingHttpResponse, FileResponse, Http404, HttpResponseNotModified
from django.db import OperationalError
from django.views.decorators.csrf import ensure_csrf_cookie
from django.db.models import Count, Q
from django.core import serializers
from iptv_filter.models import PlaylistChannel, CachedFile, EpgChannel, EpgProgramme, EpgMapping, AppConfig, LogoCache
from iptv_filter import __version__, settings_store, logo_cache, epg_quality
from iptv_updater import iptv_updater
from django.utils import timezone
from django.utils.dateparse import parse_datetime


# import the logging library / Get an instance of a logger
import logging
logger = logging.getLogger(__name__)

def playlistChannel2json(pc, last_visit):
    if 'first_seen' in pc:
        new = pc['first_seen'] > last_visit
    else:
        new = True

    return {
        'pk': pc['pk'],
        'group_title': pc['group_title'],
        'tvg_id': pc['tvg_id'],
        'tvg_name' : pc['tvg_name'],
        'included' : pc['included'],
        'new' : new
    }

def _channel_json_stream(last_visit):
    # .iterator() reads rows from the DB cursor in chunks instead of Django
    # caching the entire queryset result in memory, and yielding one row at a
    # time (rather than building a full list + a full JSON string first)
    # keeps peak memory proportional to chunk_size instead of table size.
    qs = (PlaylistChannel.objects.all()
          .values('pk', 'group_title', 'tvg_id', 'tvg_name', 'included', 'first_seen')
          .iterator(chunk_size=2000))
    yield '['
    first = True
    for obj in qs:
        if not first:
            yield ','
        first = False
        yield json.dumps(playlistChannel2json(obj, last_visit))
    yield ']'

def channel_api(request, id=-1):
    if request.method == 'GET':
        ac_lv = AppConfig.objects.filter(key='last_visit')
        if len(ac_lv) > 0:
            last_visit = parse_datetime(ac_lv[0].value)
        else:
            last_visit = timezone.now()

        try:
            AppConfig.objects.update_or_create(key='last_visit', defaults={'value':timezone.now(), 'last_updated':timezone.now()})
        except OperationalError:
            # Only bookkeeping for the "new" column; never fail the page because
            # the database is busy (e.g. during a large background update).
            logger.warning("Could not record last_visit (database busy).")

        return StreamingHttpResponse(_channel_json_stream(last_visit), content_type='application/json')

    # I wanted this to be a PUT but Django doesnt support it??
    elif request.method == 'POST':
        if id != -1:
            chs = PlaylistChannel.objects.filter(pk=id)
            if len(chs) == 1:
                ch = chs[0]
                try:
                    changeset = json.loads(request.body)
                    ch.included = changeset['included']
                except (json.JSONDecodeError, KeyError) as e:
                    return HttpResponse(json.dumps({'result': 'error', 'message': f'Invalid request body: {e}'}), status=400, content_type='application/json')
                ch.save()
                try:
                    # Guide data follows the selection immediately (not at the next hourly EPG refresh).
                    epg_quality.sync_flags()
                except OperationalError:
                    logger.warning("Could not update EPG flags right now (database busy); the next EPG refresh will.")
                logo_cache.request_sync()  # (debounced) cache the logo of a newly ticked channel

        # TODO: Error class for consistent json responses?
        return HttpResponse(json.dumps({'result':'success'}))

# The page's JavaScript posts channel toggles with an X-CSRFToken header read from
# the csrftoken cookie; without this decorator that cookie is only ever set by
# visiting /configure, so toggling a channel first would be rejected with a 403.
@ensure_csrf_cookie
def index(request):
    return render(request, 'iptv_filter/index.html')

_FAVICON_PATH = os.path.join(os.path.dirname(__file__), 'static', 'favicon.ico')

def favicon(request):
    # Served directly rather than via the staticfiles app so it works the
    # same way regardless of DEBUG/collectstatic configuration - browsers
    # request this at the site root, not under STATIC_URL.
    response = FileResponse(open(_FAVICON_PATH, 'rb'), content_type='image/x-icon')
    response['Cache-Control'] = 'public, max-age=604800'  # a week - favicons rarely change
    return response

def m3u_api(request):
    logger.info("Received m3u API call")
    included_channels = PlaylistChannel.objects.filter(included = True)

    # Logos that are cached locally are served from this server (same host the
    # client used to fetch this playlist); the rest keep their original URL.
    lookup = logo_cache.cached_lookup() if logo_cache.enabled() else {}
    base = request.build_absolute_uri('/logo/')

    try:
        id_fix = epg_quality.id_overrides()[0]
    except Exception:
        logger.exception("Could not work out tvg-id corrections; serving the ids as given")
        id_fix = {}

    parts = ["#EXTM3U\r\n"]
    for c in included_channels:
        parts.append(c.to_m3u(logo_cache.local_logo_url(base, c.tvg_logo, lookup) if lookup else None,
                              id_fix.get(c.pk)))
        parts.append("\r\n")
    m3u = "".join(parts)

    logger.info("Responded to m3u API call")
    return HttpResponse(m3u)

def epg_api(request):
    logger.info("Received epg API call")
    parts = ["""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE tv SYSTEM "xmltv.dtd">
<tv>
"""]
    included_channels = EpgChannel.objects.filter(included = True)
    for c in included_channels:
        parts.append(str(c))
        parts.append("\r\n")

    included_programmes = EpgProgramme.objects.filter(included = True)
    for p in included_programmes:
        parts.append(str(p))
        parts.append("\r\n")

    parts.append("</tv>")
    epg = "".join(parts)
    logger.info("Responded to epg API call")
    return HttpResponse(epg, content_type="text/xml")

@ensure_csrf_cookie
def configure(request):
    if request.method == 'GET':
        # One row per group with how many of its channels are ticked; the page
        # lists the channels of a group on demand (see groups_channels).
        rows = (PlaylistChannel.objects.values('group_title')
                .annotate(total=Count('id'), selected=Count('id', filter=Q(included=True)))
                .order_by('group_title'))
        groups = [{'name': r['group_title'], 'total': r['total'], 'selected': r['selected'],
                   'state': 'all' if r['selected'] == r['total'] else ('some' if r['selected'] else 'none')}
                  for r in rows]
        return render(request, 'iptv_filter/select_groups.html', {
            'groups': groups,
            'saved': request.GET.get('saved'),
        })

    elif request.method == 'POST':
        start_perftime = time.perf_counter()

        PlaylistChannel.objects.all().update(included=False)

        for group_name in request.POST:
            if group_name == 'csrfmiddlewaretoken':
                continue

            logger.info(f'About to include items for group "{group_name}"')
            PlaylistChannel.objects.filter(group_title=group_name).update(included=True)

        # One set-based update for the guide data instead of a few queries per channel.
        epg_quality.sync_flags()

        logo_cache.request_sync()

        # Post/Redirect/Get: back to the form with a confirmation instead of
        # leaving the browser on a bare text response.
        return redirect(f'/configure?saved={time.perf_counter()-start_perftime:.1f}')


# ---------------------------------------------
# Groups page: look into a group and tick/untick single channels, or a whole
# group (optionally only the channels matching a filter). Every change is saved
# at once; the guide selection and logo cache follow it.

PAGE_LIMIT = 200


def _group_counts(names):
    out = {}
    rows = (PlaylistChannel.objects.filter(group_title__in=list(names)).values('group_title')
            .annotate(total=Count('id'), selected=Count('id', filter=Q(included=True))))
    for r in rows:
        out[r['group_title']] = {'total': r['total'], 'selected': r['selected']}
    return out


def _selection_changed():
    try:
        epg_quality.sync_flags()
    except OperationalError:
        logger.warning("Could not update EPG flags right now (database busy); the next EPG refresh will.")
    logo_cache.request_sync()


def _json_body(request):
    try:
        data = json.loads(request.body or b'{}')
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def groups_channels(request):
    """GET ?group=NAME&q=TEXT&offset=0&limit=200 -> one page of a group's channels."""
    group = request.GET.get('group')
    if group is None:
        return JsonResponse({'ok': False, 'error': 'group is required'}, status=400)
    try:
        offset = max(0, int(request.GET.get('offset', 0)))
        limit = min(PAGE_LIMIT, max(1, int(request.GET.get('limit', 100))))
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'bad offset or limit'}, status=400)
    qs = PlaylistChannel.objects.filter(group_title=group)
    text = (request.GET.get('q') or '').strip()
    if text:
        qs = qs.filter(tvg_name__icontains=text)
    total = qs.count()
    items = [{'pk': pk, 'name': name, 'included': bool(inc)}
             for pk, name, inc in qs.order_by('tvg_name').values_list('pk', 'tvg_name', 'included')[offset:offset + limit]]
    counts = _group_counts([group]).get(group, {'total': 0, 'selected': 0})
    return JsonResponse({'ok': True, 'total': total, 'items': items, 'group': counts,
                         'matching_selected': qs.filter(included=True).count() if text else counts['selected']})


def groups_set(request):
    """POST {groups: [names], included: bool, q?: text} -> tick/untick whole groups.
    With q (and exactly one group) only the channels whose name contains q."""
    if request.method != 'POST':
        return HttpResponse(status=405)
    data = _json_body(request)
    if data is None or not isinstance(data.get('groups'), list) or not isinstance(data.get('included'), bool):
        return JsonResponse({'ok': False, 'error': 'Expected {groups: [...], included: true|false}.'}, status=400)
    groups = [g for g in data['groups'] if isinstance(g, str)]
    qs = PlaylistChannel.objects.filter(group_title__in=groups)
    text = str(data.get('q') or '').strip()
    if text:
        if len(groups) != 1:
            return JsonResponse({'ok': False, 'error': 'A filter text needs exactly one group.'}, status=400)
        qs = qs.filter(tvg_name__icontains=text)
    changed = qs.exclude(included=data['included']).update(included=data['included'])
    logger.info(f"Groups {'ticked' if data['included'] else 'unticked'}: {len(groups)} group(s)"
                f"{' (filter ' + repr(text) + ')' if text else ''}, {changed} channel(s) changed")
    if changed:
        _selection_changed()
    return JsonResponse({'ok': True, 'changed': changed, 'groups': _group_counts(groups)})


def groups_toggle(request):
    """POST {pks: [ids], included: bool} -> tick/untick single channels in one go."""
    if request.method != 'POST':
        return HttpResponse(status=405)
    data = _json_body(request)
    pks = data.get('pks') if data else None
    if not isinstance(pks, list) or not all(isinstance(i, int) for i in pks) or not isinstance(data.get('included'), bool):
        return JsonResponse({'ok': False, 'error': 'Expected {pks: [...], included: true|false}.'}, status=400)
    pks = pks[:5000]
    affected = set(PlaylistChannel.objects.filter(pk__in=pks).values_list('group_title', flat=True))
    changed = PlaylistChannel.objects.filter(pk__in=pks).exclude(included=data['included']).update(included=data['included'])
    if changed:
        _selection_changed()
    return JsonResponse({'ok': True, 'changed': changed, 'groups': _group_counts(affected)})


def _status_payload():
    now = timezone.now()

    def file_status(filetype):
        url_configured = AppConfig.objects.filter(key=f'{filetype}_url').exists()
        cached = CachedFile.objects.filter(file_type=filetype).values('last_updated').first()
        last_error = iptv_updater.get_last_error(filetype)
        return {
            'url_configured': url_configured,
            'last_successful_fetch': cached['last_updated'].isoformat() if cached else None,
            'last_error': {
                'time': last_error['time'].isoformat(),
                'message': last_error['message'],
            } if last_error else None,
        }

    m3u_status = file_status('m3u')
    m3u_status['channel_count'] = PlaylistChannel.objects.count()
    m3u_status['included_channel_count'] = PlaylistChannel.objects.filter(included=True).count()

    epg_status = file_status('epg')
    epg_status['channel_count'] = EpgChannel.objects.count()
    epg_status['included_channel_count'] = EpgChannel.objects.filter(included=True).count()
    epg_status['programme_count'] = EpgProgramme.objects.count()
    epg_status['included_programme_count'] = EpgProgramme.objects.filter(included=True).count()

    # "healthy" means both sources are configured and have loaded at least
    # once - it doesn't demand a *recent* fetch, since m3u/epg are only
    # scheduled to refresh daily/hourly and being between cycles is normal.
    healthy = (
        m3u_status['url_configured'] and m3u_status['last_successful_fetch'] is not None and
        epg_status['url_configured'] and epg_status['last_successful_fetch'] is not None
    )

    return {
        'version': __version__,
        'healthy': healthy,
        'server_time': now.isoformat(),
        'm3u': m3u_status,
        'epg': epg_status,
    }

def status_api(request):
    return HttpResponse(json.dumps(_status_payload(), indent=2), content_type='application/json')

def dashboard(request):
    status = _status_payload()

    def source(key, title, icon):
        data = status[key]
        last_error = iptv_updater.get_last_error(key)
        cached = CachedFile.objects.filter(file_type=key).values('last_updated').first()
        return {
            'title': title, 'icon': icon, 'data': data,
            'fetched': cached['last_updated'] if cached else None,
            # last_error messages are already credential-masked by the updater.
            'error': last_error,
        }

    output_urls = [
        {'key': 'm3u', 'label': 'M3U playlist', 'url': request.build_absolute_uri('/m3u')},
        {'key': 'epg', 'label': 'EPG guide (XMLTV)', 'url': request.build_absolute_uri('/epg')},
    ]
    return render(request, 'iptv_filter/dashboard.html', {
        'status': status,
        'sources': [source('m3u', 'Playlist (M3U)', 'bi-file-earmark-play'),
                    source('epg', 'Guide (EPG)', 'bi-calendar3')],
        'output_urls': output_urls,
        'logo_stats': logo_cache.stats(),
        'logo_enabled': logo_cache.enabled(),
        'coverage': epg_quality.coverage(),
    })


# ---------------------------------------------
# These are to allow forcing an action.

def _retrieve_m3u(request):
    start_perftime = time.perf_counter()
    iptv_updater._retrieve('m3u')
    return HttpResponse(f'Completed in {time.perf_counter()-start_perftime} seconds.')

def _retrieve_epg(request):
    start_perftime = time.perf_counter()
    iptv_updater._retrieve('epg')
    return HttpResponse(f'Completed in {time.perf_counter()-start_perftime} seconds.')

def _update_m3u_tables(request):
    start_perftime = time.perf_counter()
    iptv_updater._update_tables('m3u')
    return HttpResponse(f'Completed in {time.perf_counter()-start_perftime} seconds.')

def _update_epg_tables(request):
    start_perftime = time.perf_counter()
    iptv_updater._update_tables('epg')
    return HttpResponse(f'Completed in {time.perf_counter()-start_perftime} seconds.')

# ---------------------------------------------
# Settings page: playlist/EPG URLs and the "skip movies/series" switch.

def _settings_context():
    sources = []
    for ft, title, icon in (('m3u', 'Playlist (M3U)', 'bi-file-earmark-play'), ('epg', 'Guide (EPG, XMLTV)', 'bi-calendar3')):
        url, source = settings_store.get_url(ft)
        cached = CachedFile.objects.filter(file_type=ft).annotate(size=Length('file')).values('size', 'last_updated').first()
        sources.append({
            'key': ft, 'title': title, 'icon': icon,
            'url': url or '', 'source': source,
            'env_name': settings_store.env_name(ft),
            'env_set': bool(os.environ.get(settings_store.env_name(ft))),
            'busy': iptv_updater.is_busy(ft),
            'cached': cached,
        })
    skip, skip_source = settings_store.get_skip_vod()
    logo_on, logo_source = settings_store.get_logo_cache()
    return {'sources': sources, 'skip_vod': skip, 'skip_vod_source': skip_source,
            'logo_cache': logo_on, 'logo_cache_source': logo_source, 'logo_stats': logo_cache.stats()}

def settings_page(request):
    if request.method == 'POST':
        action = request.POST.get('action', 'save')

        # Validate everything first so a typo doesn't leave a half-saved form.
        wanted = {}
        errors = []
        for ft in settings_store.FILETYPES:
            value = (request.POST.get(f'{ft}_url') or '').strip()
            if value:
                err = settings_store.validate_url(value)
                if err:
                    errors.append(f'{ft.upper()} URL: {err}')
            wanted[ft] = value
        if errors:
            for e in errors:
                messages.error(request, e)
            return redirect('/settings')

        changed = []
        for ft, value in wanted.items():
            current, source = settings_store.get_url(ft)
            if value and value != current:
                settings_store.set_gui_url(ft, value)
                changed.append(ft)
            elif not value and source == settings_store.GUI:
                settings_store.clear_gui_url(ft)
                changed.append(ft)

        skip_wanted = request.POST.get('skip_vod') == 'on'
        skip_changed = skip_wanted != settings_store.get_skip_vod()[0]
        if skip_changed:
            settings_store.set_gui_skip_vod(skip_wanted)

        logo_wanted = request.POST.get('logo_cache') == 'on'
        logo_changed = logo_wanted != settings_store.get_logo_cache()[0]
        if logo_changed:
            settings_store.set_gui_logo_cache(logo_wanted)
            if logo_wanted:
                logo_cache.request_sync()

        if changed or skip_changed or logo_changed:
            messages.success(request, 'Settings saved.')
        else:
            messages.info(request, 'Nothing changed.')

        if action == 'fetch':
            targets = changed or list(settings_store.FILETYPES)
            started = [ft for ft in targets if iptv_updater.start_in_background(ft, download=True)]
            busy = [ft for ft in targets if ft not in started]
            if started:
                messages.info(request, 'Downloading and processing ' + ' and '.join(t.upper() for t in started) +
                              ' in the background - see the Dashboard for progress.')
            if busy:
                messages.warning(request, ' and '.join(t.upper() for t in busy) + ' is already being updated.')
        elif action == 'logos':
            if logo_cache.request_sync():
                messages.info(request, 'Checking and downloading logos of ticked channels in the background.')
            else:
                messages.warning(request, 'The local logo cache is switched off.')
        elif action == 'reprocess':
            if iptv_updater.start_in_background('m3u', download=False):
                messages.info(request, 'Re-processing the stored playlist in the background (no download).')
            else:
                messages.warning(request, 'The playlist is already being updated.')
        elif skip_changed:
            messages.info(request, 'Click "Save & re-process playlist" to apply the movies/series setting to the stored playlist.')
        return redirect('/settings')

    return render(request, 'iptv_filter/settings.html', _settings_context())


# ---------------------------------------------
# Locally cached channel logos (see iptv_filter/logo_cache.py).

def logo_file(request, name):
    h = name.split('.', 1)[0]
    if not logo_cache.valid_hash(h):
        raise Http404()
    row = LogoCache.objects.filter(url_hash=h, status=LogoCache.OK).first()
    path = logo_cache.file_path(h)
    if row is None or not os.path.isfile(path):
        raise Http404()

    etag = f'"{h}-{int(row.fetched_at.timestamp()) if row.fetched_at else 0}"'
    if request.headers.get('If-None-Match') == etag:
        response = HttpResponseNotModified()
    else:
        response = FileResponse(open(path, 'rb'), content_type=row.content_type or 'application/octet-stream')
    response['ETag'] = etag
    response['Cache-Control'] = 'public, max-age=2592000'
    # Files come from third parties: never let a browser run them as a page (e.g. an SVG with script).
    response['X-Content-Type-Options'] = 'nosniff'
    response['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    return response


@ensure_csrf_cookie
def epg_coverage(request):
    return render(request, 'iptv_filter/coverage.html', {'coverage': epg_quality.coverage(with_suggestions=True)})


def epg_map_api(request):
    """POST {pk, action: 'map'|'none'|'clear', epg_id?}: a manual decision about
    one playlist channel's guide. Takes effect immediately."""
    if request.method != 'POST':
        return HttpResponse(status=405)
    try:
        data = json.loads(request.body or b'{}')
        action = data.get('action')
        channel = PlaylistChannel.objects.get(pk=int(data.get('pk')))
    except (ValueError, TypeError, PlaylistChannel.DoesNotExist):
        return JsonResponse({'ok': False, 'error': 'Unknown channel or bad request.'}, status=400)
    before = epg_quality.id_overrides()[0].get(channel.pk)
    if action == 'map':
        epg_id = str(data.get('epg_id') or '').strip()
        if not EpgChannel.objects.filter(channel_id=epg_id).exists():
            return JsonResponse({'ok': False, 'error': f'"{epg_id}" is not a channel in the EPG file.'}, status=400)
        EpgMapping.objects.update_or_create(tvg_name=channel.tvg_name, defaults={'epg_id': epg_id, 'no_guide': False})
    elif action == 'none':
        EpgMapping.objects.update_or_create(tvg_name=channel.tvg_name, defaults={'epg_id': '', 'no_guide': True})
    elif action == 'clear':
        EpgMapping.objects.filter(tvg_name=channel.tvg_name).delete()
    else:
        return JsonResponse({'ok': False, 'error': 'Unknown action.'}, status=400)
    logger.info(f'EPG mapping for "{channel.tvg_name}": {action}' + (f' -> {epg_id}' if action == 'map' else ''))
    try:
        # Only the EPG channels this channel pointed to before / points to now (or carries as its own id) can change.
        after = epg_quality.id_overrides()[0].get(channel.pk)
        epg_quality.sync_flags(only={before, after, channel.tvg_id})
    except OperationalError:
        logger.warning("Could not sync the guide selection right now; it follows at the next change or EPG refresh.")
    return JsonResponse({'ok': True})
