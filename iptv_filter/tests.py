import json
import re
import xml.etree.ElementTree as ET

from django.test import TestCase, Client
from django.utils import timezone

from .models import AppConfig, CachedFile, PlaylistChannel, EpgChannel, EpgProgramme
from iptv_updater import iptv_updater


def _seed_cached_file(filetype, content):
    """Simulate a successful _retrieve() by writing straight to CachedFile,
    the way _update_tables() expects to find it."""
    CachedFile.objects.update_or_create(
        file_type=filetype,
        defaults={'file': content.encode(), 'last_updated': timezone.now()},
    )


class M3UParsingTests(TestCase):
    """The M3U parser has to tolerate real-world provider quirks: attribute
    order/set varies between providers and tvg-id is often missing entirely,
    and stream URLs aren't always http(s). A real provider file with
    reordered attributes once caused 100% of channels to be silently
    dropped - these guard against that regressing."""

    def test_reordered_attributes_are_parsed(self):
        m3u = (
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-name="Reordered Channel" tvg-logo="http://x/1.png" group-title="Test",Reordered Channel\n'
            'http://stream/1\n'
        )
        _seed_cached_file('m3u', m3u)
        iptv_updater._update_tables('m3u')
        ch = PlaylistChannel.objects.get(tvg_name='Reordered Channel')
        self.assertEqual(ch.group_title, 'Test')
        self.assertEqual(ch.stream_url, 'http://stream/1')

    def test_missing_tvg_id_does_not_drop_the_channel(self):
        m3u = (
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-logo="http://x/2.png" tvg-name="No ID Channel" group-title="Test",No ID Channel\n'
            'http://stream/2\n'
        )
        _seed_cached_file('m3u', m3u)
        iptv_updater._update_tables('m3u')
        ch = PlaylistChannel.objects.get(tvg_name='No ID Channel')
        self.assertEqual(ch.tvg_id, '')

    def test_non_http_stream_protocols_are_recognized(self):
        m3u = (
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-name="RTSP Channel" group-title="Test",RTSP Channel\n'
            'rtsp://stream/3\n'
            '#EXTINF:-1 tvg-name="RTMP Channel" group-title="Test",RTMP Channel\n'
            'rtmp://stream/4\n'
        )
        _seed_cached_file('m3u', m3u)
        iptv_updater._update_tables('m3u')
        self.assertEqual(PlaylistChannel.objects.get(tvg_name='RTSP Channel').stream_url, 'rtsp://stream/3')
        self.assertEqual(PlaylistChannel.objects.get(tvg_name='RTMP Channel').stream_url, 'rtmp://stream/4')

    def test_channel_with_no_working_stream_is_skipped(self):
        m3u = (
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-name="No Stream Channel" group-title="Test",No Stream Channel\n'
            '[NO PUBLIC STREAM]\n'
        )
        _seed_cached_file('m3u', m3u)
        iptv_updater._update_tables('m3u')
        self.assertFalse(PlaylistChannel.objects.filter(tvg_name='No Stream Channel').exists())

    def test_orphan_url_line_without_a_preceding_extinf_is_ignored(self):
        m3u = (
            '#EXTM3U\n'
            'http://orphan-url-no-extinf/should-be-ignored\n'
            '#EXTINF:-1 tvg-name="Real Channel" group-title="Test",Real Channel\n'
            'http://stream/5\n'
        )
        _seed_cached_file('m3u', m3u)
        iptv_updater._update_tables('m3u')
        self.assertEqual(PlaylistChannel.objects.count(), 1)
        self.assertEqual(PlaylistChannel.objects.first().tvg_name, 'Real Channel')

    def test_duplicate_url_line_does_not_duplicate_the_channel(self):
        m3u = (
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-name="Dup Channel" group-title="Test",Dup Channel\n'
            'http://stream/6\n'
            'http://stream/6-duplicate-should-be-ignored\n'
        )
        _seed_cached_file('m3u', m3u)
        iptv_updater._update_tables('m3u')
        self.assertEqual(PlaylistChannel.objects.filter(tvg_name='Dup Channel').count(), 1)
        self.assertEqual(PlaylistChannel.objects.get(tvg_name='Dup Channel').stream_url, 'http://stream/6')


class OutputEscapingTests(TestCase):
    """Channel names and programme titles come from an untrusted upstream
    feed. A crafted name used to be able to break the output format
    outright: an embedded CR/LF could inject fake extra channel entries into
    the M3U, and an EPG title containing <, >, or & produced invalid XML."""

    def test_m3u_quote_in_name_does_not_break_attribute_quoting(self):
        PlaylistChannel.objects.create(
            tvg_id='x1', tvg_name='Sports "Extra" HD', tvg_logo='', group_title='Sports',
            stream_url='http://x/1', included=True, first_seen=timezone.now(), last_updated=timezone.now())
        m3u = Client().get('/m3u').content.decode()
        lines = [l for l in m3u.split('\r\n') if l.startswith('#EXTINF')]
        self.assertEqual(len(lines), 1)
        # round-trip through the app's own attribute parser to prove the line is well-formed
        attrs = dict(re.findall(r'([\w-]+)="(.*?)"', lines[0]))
        self.assertIn('Extra', attrs['tvg-name'])

    def test_m3u_embedded_newline_cannot_inject_extra_lines(self):
        PlaylistChannel.objects.create(
            tvg_id='legit', tvg_name='Legit\r\n#EXTINF:-1,Injected\r\nhttp://evil.example/stream',
            tvg_logo='', group_title='News', stream_url='http://real/1',
            included=True, first_seen=timezone.now(), last_updated=timezone.now())
        m3u = Client().get('/m3u').content.decode()
        extinf_lines = [l for l in m3u.split('\r\n') if l.startswith('#EXTINF')]
        self.assertEqual(len(extinf_lines), 1,
            "a single malicious channel name must not be able to inject extra #EXTINF lines")

    def test_epg_special_characters_produce_valid_xml(self):
        EpgChannel.objects.create(channel_id='x1', display_name='News <Live>', included=True, last_updated=timezone.now())
        EpgProgramme.objects.create(channel='x1', start='20260101000000 +0000', stop='20260101010000 +0000',
            title='Report: A & B < C', desc='', included=True, last_updated=timezone.now())

        resp = Client().get('/epg')
        content = b''.join(resp.streaming_content) if resp.streaming else resp.content

        root = ET.fromstring(content)  # raises ET.ParseError if the output is invalid XML
        title = root.find('programme/title').text
        self.assertEqual(title, 'Report: A & B < C')  # round-trips correctly, not double-escaped


class DiffingTests(TestCase):
    """_update_tables() diffs against existing rows instead of deleting and
    recreating the whole table on every scheduled refresh. The one thing
    that must never regress: a channel's 'included' (the user's own
    subscription choice) has to survive a refresh even when other fields on
    that channel change."""

    def test_included_flag_survives_a_refresh(self):
        _seed_cached_file('m3u', (
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-id="ch1" tvg-name="Channel One" tvg-logo="http://x/1.png" group-title="News",Channel One\n'
            'http://stream/1\n'
        ))
        iptv_updater._update_tables('m3u')
        PlaylistChannel.objects.filter(tvg_name='Channel One').update(included=True)

        # second pull: same channel, but the logo URL changed upstream
        _seed_cached_file('m3u', (
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-id="ch1" tvg-name="Channel One" tvg-logo="http://x/1-NEW.png" group-title="News",Channel One\n'
            'http://stream/1\n'
        ))
        iptv_updater._update_tables('m3u')

        ch = PlaylistChannel.objects.get(tvg_name='Channel One')
        self.assertTrue(ch.included, "the user's inclusion choice must survive a refresh")
        self.assertEqual(ch.tvg_logo, 'http://x/1-NEW.png')

    def test_channel_removed_from_source_is_deleted(self):
        _seed_cached_file('m3u', (
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-name="Channel A" group-title="News",Channel A\n'
            'http://stream/a\n'
            '#EXTINF:-1 tvg-name="Channel B" group-title="News",Channel B\n'
            'http://stream/b\n'
        ))
        iptv_updater._update_tables('m3u')
        self.assertEqual(PlaylistChannel.objects.count(), 2)

        # Channel B disappears from the next pull
        _seed_cached_file('m3u', (
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-name="Channel A" group-title="News",Channel A\n'
            'http://stream/a\n'
        ))
        iptv_updater._update_tables('m3u')
        self.assertEqual(PlaylistChannel.objects.count(), 1)
        self.assertFalse(PlaylistChannel.objects.filter(tvg_name='Channel B').exists())

    def test_new_channel_is_created_with_included_false(self):
        _seed_cached_file('m3u', '#EXTM3U\n')
        iptv_updater._update_tables('m3u')
        self.assertEqual(PlaylistChannel.objects.count(), 0)

        _seed_cached_file('m3u', (
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-name="Brand New" group-title="News",Brand New\n'
            'http://stream/new\n'
        ))
        iptv_updater._update_tables('m3u')
        ch = PlaylistChannel.objects.get(tvg_name='Brand New')
        self.assertFalse(ch.included, "newly discovered channels must default to excluded")


class StatusEndpointTests(TestCase):
    def test_status_reports_unhealthy_when_unconfigured(self):
        body = json.loads(Client().get('/status').content)
        self.assertFalse(body['healthy'])
        self.assertFalse(body['m3u']['url_configured'])
        self.assertIsNone(body['m3u']['last_successful_fetch'])

    def test_status_reports_healthy_after_successful_load(self):
        AppConfig.objects.create(key='m3u_url', value='http://provider.example/playlist.m3u')
        AppConfig.objects.create(key='epg_url', value='http://provider.example/epg.xml')
        _seed_cached_file('m3u', '#EXTM3U\n')
        _seed_cached_file('epg', '<tv></tv>')
        iptv_updater._update_tables('m3u')
        iptv_updater._update_tables('epg')

        body = json.loads(Client().get('/status').content)
        self.assertTrue(body['healthy'])
        self.assertIsNotNone(body['m3u']['last_successful_fetch'])

    def test_status_surfaces_and_then_clears_a_retrieval_error(self):
        iptv_updater._record_error('m3u', 'simulated failure')
        body = json.loads(Client().get('/status').content)
        self.assertEqual(body['m3u']['last_error']['message'], 'simulated failure')

        iptv_updater._last_error.pop('m3u', None)  # what a successful _retrieve() does internally
        body = json.loads(Client().get('/status').content)
        self.assertIsNone(body['m3u']['last_error'])


class ChannelApiTests(TestCase):
    def test_post_with_malformed_body_returns_400_and_leaves_channel_unchanged(self):
        ch = PlaylistChannel.objects.create(
            tvg_id='x', tvg_name='Test', tvg_logo='', group_title='Test',
            stream_url='http://x/1', included=False, first_seen=timezone.now(), last_updated=timezone.now())
        resp = Client().post(f'/channels/{ch.pk}', data=b'not valid json', content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        ch.refresh_from_db()
        self.assertFalse(ch.included)

    def test_post_with_valid_request_updates_the_channel(self):
        ch = PlaylistChannel.objects.create(
            tvg_id='x', tvg_name='Test2', tvg_logo='', group_title='Test',
            stream_url='http://x/1', included=False, first_seen=timezone.now(), last_updated=timezone.now())
        resp = Client().post(f'/channels/{ch.pk}', data=json.dumps({'included': True}), content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        ch.refresh_from_db()
        self.assertTrue(ch.included)


class RetrieveTests(TestCase):
    """_retrieve() against a local HTTP server. Many IPTV panels reset the
    connection for the default python-requests User-Agent, so we must send a
    browser-like one, and credentials in the URL must never reach logs/status."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        body = b'#EXTM3U\n#EXTINF:-1 tvg-name="A" group-title="G",A\nhttp://s/1\n'
        cls.seen_agents = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                ua = self.headers.get('User-Agent', '')
                cls.seen_agents.append(ua)
                if ua.lower().startswith('python-requests'):
                    self.connection.setsockopt(
                        __import__('socket').SOL_SOCKET, __import__('socket').SO_LINGER,
                        b'\x01\x00\x00\x00\x00\x00\x00\x00')
                    self.connection.close()
                    return
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        cls.server = HTTPServer(('127.0.0.1', 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        super().tearDownClass()

    def _url(self):
        return f'http://127.0.0.1:{self.port}/get.php?username=SECRETUSER&password=SECRETPASS&type=m3u_plus'

    def test_sends_browser_user_agent_and_succeeds(self):
        AppConfig.objects.update_or_create(key='m3u_url', defaults={'value': self._url()})
        self.assertTrue(iptv_updater._retrieve('m3u'))
        self.assertFalse(self.seen_agents[-1].lower().startswith('python-requests'))
        self.assertIn(b'#EXTM3U', CachedFile.objects.get(file_type='m3u').file)

    def test_custom_user_agent_env(self):
        import os
        from unittest import mock
        AppConfig.objects.update_or_create(key='m3u_url', defaults={'value': self._url()})
        with mock.patch.dict(os.environ, {'IPTV_USER_AGENT': 'MyAgent/1.0'}):
            self.assertTrue(iptv_updater._retrieve('m3u'))
        self.assertEqual(self.seen_agents[-1], 'MyAgent/1.0')

    def test_failure_masks_credentials(self):
        from unittest import mock
        AppConfig.objects.update_or_create(key='m3u_url', defaults={'value': self._url()})
        with mock.patch.dict('os.environ', {'IPTV_USER_AGENT': 'python-requests/2'}):
            with self.assertLogs('iptv_updater.iptv_updater', level='WARNING') as cm:
                self.assertFalse(iptv_updater._retrieve('m3u'))
        logged = '\n'.join(cm.output)
        self.assertNotIn('SECRETPASS', logged)
        self.assertNotIn('SECRETUSER', logged)
        err = iptv_updater.get_last_error('m3u')
        self.assertIsNotNone(err)
        self.assertNotIn('SECRETPASS', err['message'])

    def test_mask_helper(self):
        m = iptv_updater._mask
        self.assertEqual(m('http://u:p@h/x'), 'http://***@h/x')
        self.assertNotIn('abc', m('http://h/get.php?username=bob&password=abc&type=m3u'))
        self.assertIn('type=m3u', m('http://h/get.php?username=bob&password=abc&type=m3u'))

    def test_body_stored_as_raw_bytes(self):
        AppConfig.objects.update_or_create(key='m3u_url', defaults={'value': self._url()})
        self.assertTrue(iptv_updater._retrieve('m3u'))
        self.assertEqual(bytes(CachedFile.objects.get(file_type='m3u').file),
                         b'#EXTM3U\n#EXTINF:-1 tvg-name="A" group-title="G",A\nhttp://s/1\n')


class LargeUpdateTests(TestCase):
    """M3U updates are applied in batches (not one giant transaction) so a big
    playlist can't lock the database for the web UI."""

    def _m3u(self, n, vod=0):
        lines = ['#EXTM3U']
        for i in range(n):
            lines.append(f'#EXTINF:-1 tvg-name="Live {i}" group-title="L",Live {i}')
            lines.append(f'http://h:80/user/pass/{i}')
        for i in range(vod):
            lines.append(f'#EXTINF:-1 tvg-name="Movie {i}" group-title="M",Movie {i}')
            lines.append(f'http://h:80/movie/user/pass/{i}.mp4')
        lines.append('#EXTINF:-1 tvg-name="Show" group-title="S",Show')
        lines.append('http://h:80/series/user/pass/1.mkv')
        return '\n'.join(lines) + '\n'

    def test_batched_create_update_delete(self):
        orig = iptv_updater.WRITE_BATCH
        iptv_updater.WRITE_BATCH = 7  # force several batches
        try:
            _seed_cached_file('m3u', self._m3u(50))
            iptv_updater._update_tables('m3u')
            self.assertEqual(PlaylistChannel.objects.count(), 51)
            # drop 20 live channels, change one stream URL
            _seed_cached_file('m3u', self._m3u(30).replace('/user/pass/3\n', '/user/pass/NEW\n'))
            iptv_updater._update_tables('m3u')
            self.assertEqual(PlaylistChannel.objects.count(), 31)
            self.assertEqual(PlaylistChannel.objects.get(tvg_name='Live 3').stream_url, 'http://h:80/user/pass/NEW')
        finally:
            iptv_updater.WRITE_BATCH = orig

    def test_skip_vod_is_opt_in(self):
        from unittest import mock
        _seed_cached_file('m3u', self._m3u(5, vod=5))
        iptv_updater._update_tables('m3u')
        self.assertEqual(PlaylistChannel.objects.count(), 11)  # default: keep everything
        PlaylistChannel.objects.all().delete()
        with mock.patch.dict('os.environ', {'IPTV_SKIP_VOD': '1'}):
            iptv_updater._update_tables('m3u')
        self.assertEqual(PlaylistChannel.objects.count(), 5)
        self.assertFalse(PlaylistChannel.objects.filter(stream_url__contains='/movie/').exists())

    def test_non_utf8_bytes_do_not_crash(self):
        CachedFile.objects.update_or_create(file_type='m3u', defaults={
            'file': b'#EXTM3U\n#EXTINF:-1 tvg-name="Caf\xe9" group-title="G",Caf\xe9\nhttp://s/1\n',
            'last_updated': timezone.now()})
        iptv_updater._update_tables('m3u')
        self.assertEqual(PlaylistChannel.objects.count(), 1)

    def test_channels_api_survives_busy_database(self):
        from unittest import mock
        from django.db import OperationalError
        with mock.patch.object(AppConfig.objects, 'update_or_create', side_effect=OperationalError('database is locked')):
            r = Client().get('/channels')
        self.assertEqual(r.status_code, 200)
        b''.join(r.streaming_content)


class UiTests(TestCase):
    """The AdminLTE-based pages: shared layout, dashboard, bundled assets."""

    def test_pages_render_with_shared_layout(self):
        for url in ('/', '/dashboard', '/configure'):
            r = Client().get(url)
            self.assertEqual(r.status_code, 200, url)
            body = r.content.decode()
            self.assertIn('app-sidebar', body, url)
            self.assertIn('/static/vendor/adminlte/adminlte.min.css', body, url)
            self.assertIn('iptv-filter v', body, url)

    def test_active_nav_item_is_highlighted(self):
        body = Client().get('/configure').content.decode()
        self.assertRegex(body, r'href="/configure" class="nav-link active"')
        self.assertNotRegex(body, r'href="/" class="nav-link active"')

    def test_bundled_assets_exist_and_no_external_cdn(self):
        import re
        from django.contrib.staticfiles import finders
        for url in ('/', '/dashboard', '/configure'):
            body = Client().get(url).content.decode()
            self.assertNotIn('cdn.jsdelivr.net', body)
            for path in re.findall(r'/static/(vendor/[^"\']+)', body):
                self.assertIsNotNone(finders.find(path), f'{path} missing (referenced by {url})')

    def test_dashboard_shows_counts_and_masked_error(self):
        now = timezone.now()
        PlaylistChannel.objects.create(tvg_name='A', tvg_id='a', group_title='G', stream_url='http://s/1',
                                       last_updated=now, first_seen=now, included=True)
        PlaylistChannel.objects.create(tvg_name='B', tvg_id='b', group_title='G', stream_url='http://s/2',
                                       last_updated=now, first_seen=now, included=False)
        iptv_updater._record_error('epg', 'boom <script>alert(1)</script>')
        try:
            body = Client().get('/dashboard').content.decode()
        finally:
            iptv_updater._last_error.pop('epg', None)
        self.assertIn('Not ready yet', body)
        self.assertRegex(body, r'1 <small[^>]*>/ 2</small>')
        self.assertIn('boom', body)
        self.assertNotIn('<script>alert(1)</script>', body)  # escaped

    def test_dashboard_output_urls_use_request_host(self):
        body = Client().get('/dashboard', HTTP_HOST='tv.lan:8000').content.decode()
        self.assertIn('http://tv.lan:8000/m3u', body)
        self.assertIn('http://tv.lan:8000/epg', body)

    def test_channels_page_sets_csrf_cookie(self):
        # The grid POSTs toggles with a token read from this cookie.
        r = Client().get('/')
        self.assertIn('csrftoken', r.cookies)

    def test_group_save_redirects_back_with_confirmation(self):
        now = timezone.now()
        PlaylistChannel.objects.create(tvg_name='A', tvg_id='a', group_title='G1', stream_url='http://s/1',
                                       last_updated=now, first_seen=now)
        r = Client().post('/configure', {'G1': 'G1'})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r['Location'].startswith('/configure?saved='))
        self.assertTrue(PlaylistChannel.objects.get(tvg_name='A').included)
        self.assertIn('Groups saved', Client().get(r['Location']).content.decode())


class SettingsPrecedenceTests(TestCase):
    """GUI-saved settings win over environment variables, and the logs say
    which one is in use."""

    LOGGER = 'iptv_filter.settings_store'

    def _env(self, **kw):
        from unittest import mock
        base = {'IPTV_M3U_URL': '', 'IPTV_EPG_URL': '', 'IPTV_SKIP_VOD': ''}
        base.update(kw)
        return mock.patch.dict('os.environ', base)

    def _startup(self, **env):
        from iptv_filter import settings_store
        with self._env(**env), self.assertLogs(self.LOGGER, level='INFO') as cm:
            settings_store.apply_at_startup()
        return '\n'.join(cm.output)

    def test_env_used_when_nothing_saved_in_gui(self):
        from iptv_filter import settings_store
        log = self._startup(IPTV_M3U_URL='http://p.example/get.php?username=u&password=SECRET')
        self.assertIn('nothing saved in the web GUI, using environment variable IPTV_M3U_URL', log)
        self.assertNotIn('SECRET', log)
        self.assertEqual(settings_store.get_url('m3u')[1], 'env')

    def test_gui_value_wins_and_log_says_so(self):
        from iptv_filter import settings_store
        with self._env():
            settings_store.set_gui_url('m3u', 'http://gui.example/list.m3u')
        log = self._startup(IPTV_M3U_URL='http://env.example/other.m3u')
        self.assertIn('M3U URL: using the value configured in the web GUI', log)
        self.assertIn('IPTV_M3U_URL is set but ignored', log)
        self.assertEqual(settings_store.get_url('m3u'), ('http://gui.example/list.m3u', 'gui'))

    def test_gui_value_survives_restart_without_env(self):
        from iptv_filter import settings_store
        settings_store.set_gui_url('epg', 'http://gui.example/epg.xml')
        log = self._startup()
        self.assertIn('EPG URL: using the value configured in the web GUI', log)
        self.assertNotIn('ignored', log)

    def test_changed_env_is_picked_up_and_rows_do_not_pile_up(self):
        from iptv_filter import settings_store
        self._startup(IPTV_M3U_URL='http://old.example/a.m3u')
        self._startup(IPTV_M3U_URL='http://new.example/b.m3u')
        self.assertEqual(settings_store.get_url('m3u')[0], 'http://new.example/b.m3u')
        self.assertEqual(AppConfig.objects.filter(key='m3u_url').count(), 1)

    def test_not_configured_is_logged_as_warning(self):
        from iptv_filter import settings_store
        with self._env(), self.assertLogs(self.LOGGER, level='WARNING') as cm:
            settings_store.apply_at_startup()
        self.assertIn('M3U URL: not configured', '\n'.join(cm.output))

    def test_clearing_gui_value_falls_back_to_env(self):
        from iptv_filter import settings_store
        with self._env(IPTV_EPG_URL='http://env.example/epg.xml'):
            settings_store.set_gui_url('epg', 'http://gui.example/epg.xml')
            settings_store.clear_gui_url('epg')
        self.assertEqual(settings_store.get_url('epg'), ('http://env.example/epg.xml', 'env'))

    def test_clearing_gui_value_without_env_unsets(self):
        from iptv_filter import settings_store
        with self._env():
            settings_store.set_gui_url('epg', 'http://gui.example/epg.xml')
            settings_store.clear_gui_url('epg')
        self.assertEqual(settings_store.get_url('epg'), (None, None))

    def test_skip_vod_precedence(self):
        from iptv_filter import settings_store
        with self._env():
            self.assertEqual(settings_store.get_skip_vod(), (False, 'default'))
        with self._env(IPTV_SKIP_VOD='1'):
            self.assertEqual(settings_store.get_skip_vod(), (True, 'env'))
            settings_store.set_gui_skip_vod(False)  # GUI says off: wins over env
            self.assertEqual(settings_store.get_skip_vod(), (False, 'gui'))

    def test_retrieve_log_names_the_source(self):
        from iptv_filter import settings_store
        settings_store.set_gui_url('m3u', 'http://127.0.0.1:1/x.m3u?password=SECRET')
        with self.assertLogs('iptv_updater.iptv_updater', level='INFO') as cm:
            iptv_updater._retrieve('m3u')  # connection refused: only the log matters
        out = '\n'.join(cm.output)
        self.assertIn('(configured in the web GUI)', out)
        self.assertNotIn('SECRET', out)


class SettingsPageTests(TestCase):
    def setUp(self):
        from unittest import mock
        p = mock.patch.dict('os.environ', {'IPTV_M3U_URL': '', 'IPTV_EPG_URL': '', 'IPTV_SKIP_VOD': ''})
        p.start()
        self.addCleanup(p.stop)

    def test_page_renders(self):
        r = Client().get('/settings')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'not set')
        self.assertContains(r, 'app-sidebar')

    def test_save_urls_marks_them_as_gui(self):
        from iptv_filter import settings_store
        r = Client().post('/settings', {'m3u_url': 'http://a.example/x.m3u', 'epg_url': 'https://b.example/e.xml', 'action': 'save'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(settings_store.get_url('m3u'), ('http://a.example/x.m3u', 'gui'))
        self.assertEqual(settings_store.get_url('epg'), ('https://b.example/e.xml', 'gui'))
        self.assertContains(Client().get('/settings'), 'saved in the GUI')

    def test_invalid_url_saves_nothing(self):
        from iptv_filter import settings_store
        c = Client()
        c.post('/settings', {'m3u_url': 'http://ok.example/x.m3u', 'epg_url': 'ftp://nope', 'action': 'save'})
        self.assertEqual(settings_store.get_url('m3u'), (None, None))
        self.assertContains(c.get('/settings'), 'Must be a full http')

    def test_unchanged_env_value_is_not_turned_into_a_gui_value(self):
        from unittest import mock
        from iptv_filter import settings_store
        with mock.patch.dict('os.environ', {'IPTV_M3U_URL': 'http://env.example/x.m3u'}):
            settings_store.apply_at_startup()
            Client().post('/settings', {'m3u_url': 'http://env.example/x.m3u', 'epg_url': '', 'action': 'save'})
        self.assertEqual(settings_store.get_url('m3u')[1], 'env')

    def test_blank_field_clears_gui_value(self):
        from iptv_filter import settings_store
        settings_store.set_gui_url('m3u', 'http://a.example/x.m3u')
        Client().post('/settings', {'m3u_url': '', 'epg_url': '', 'action': 'save'})
        self.assertEqual(settings_store.get_url('m3u'), (None, None))

    def test_skip_vod_switch_saved(self):
        from iptv_filter import settings_store
        Client().post('/settings', {'m3u_url': '', 'epg_url': '', 'skip_vod': 'on', 'action': 'save'})
        self.assertEqual(settings_store.get_skip_vod(), (True, 'gui'))
        Client().post('/settings', {'m3u_url': '', 'epg_url': '', 'action': 'save'})
        self.assertEqual(settings_store.get_skip_vod(), (False, 'gui'))

    def test_fetch_only_downloads_changed_urls(self):
        from unittest import mock
        settings_store_url = 'http://a.example/x.m3u'
        with mock.patch.object(iptv_updater, 'start_in_background', return_value=True) as m:
            Client().post('/settings', {'m3u_url': settings_store_url, 'epg_url': '', 'action': 'fetch'})
        m.assert_called_once_with('m3u', download=True)

    def test_reprocess_does_not_download(self):
        from unittest import mock
        with mock.patch.object(iptv_updater, 'start_in_background', return_value=True) as m:
            Client().post('/settings', {'m3u_url': '', 'epg_url': '', 'action': 'reprocess'})
        m.assert_called_once_with('m3u', download=False)

    def test_page_does_not_start_second_run_when_busy(self):
        iptv_updater._busy['m3u'] = True
        try:
            self.assertFalse(iptv_updater.start_in_background('m3u', download=False))
            with self.assertLogs('iptv_updater.iptv_updater', level='WARNING'):
                iptv_updater.update_m3u()  # skipped, not run twice
        finally:
            iptv_updater._busy['m3u'] = False


class SkipVodKeepsTickedTests(TestCase):
    def test_ticked_vod_survives_skip_but_unticked_vod_goes(self):
        from unittest import mock
        m3u = ('#EXTM3U\n'
               '#EXTINF:-1 tvg-name="Live" group-title="L",Live\nhttp://h/u/p/1\n'
               '#EXTINF:-1 tvg-name="Movie A" group-title="M",A\nhttp://h/movie/u/p/1.mp4\n'
               '#EXTINF:-1 tvg-name="Movie B" group-title="M",B\nhttp://h/movie/u/p/2.mp4\n')
        _seed_cached_file('m3u', m3u)
        with mock.patch.dict('os.environ', {'IPTV_SKIP_VOD': ''}):
            iptv_updater._update_tables('m3u')  # everything imported
            self.assertEqual(PlaylistChannel.objects.count(), 3)
            PlaylistChannel.objects.filter(tvg_name='Movie A').update(included=True)
            from iptv_filter import settings_store
            settings_store.set_gui_skip_vod(True)
            iptv_updater.reprocess('m3u')  # no download involved
        names = set(PlaylistChannel.objects.values_list('tvg_name', flat=True))
        self.assertEqual(names, {'Live', 'Movie A'})
        self.assertTrue(PlaylistChannel.objects.get(tvg_name='Movie A').included)
