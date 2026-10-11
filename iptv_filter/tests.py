import json
import os
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


class SchemaTests(TestCase):
    def test_models_and_migrations_are_in_sync(self):
        from django.core.management import call_command
        # Fails if a model change was made without its migration.
        call_command('makemigrations', '--check', '--dry-run', verbosity=0)

    def test_name_lookup_uses_the_unique_index_only(self):
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=%s",
                        [PlaylistChannel._meta.db_table])
            cols = []
            for (name,) in cur.fetchall():
                cur.execute(f'PRAGMA index_info("{name}")')
                cols.append(tuple(r[2] for r in cur.fetchall()))
        # one index on tvg_name (the unique constraint's), not two
        self.assertEqual(sum(1 for c in cols if c == ('tvg_name',)), 1)

    def test_updates_and_inserts_use_correct_values(self):
        _seed_cached_file('m3u', '#EXTM3U\n#EXTINF:-1 tvg-id="i1" tvg-name="N1" tvg-logo="L" group-title="G",N1\nhttp://s/1\n')
        iptv_updater._update_tables('m3u')
        ch = PlaylistChannel.objects.get(tvg_name='N1')
        self.assertEqual((ch.tvg_id, ch.tvg_logo, ch.group_title, ch.stream_url, ch.included), ('i1', 'L', 'G', 'http://s/1', False))
        self.assertIsNotNone(ch.first_seen)
        first_seen = ch.first_seen
        PlaylistChannel.objects.filter(pk=ch.pk).update(included=True)
        _seed_cached_file('m3u', '#EXTM3U\n#EXTINF:-1 tvg-id="i2" tvg-name="N1" tvg-logo="L2" group-title="G2",N1\nhttp://s/2\n')
        iptv_updater._update_tables('m3u')
        ch = PlaylistChannel.objects.get(tvg_name='N1')
        self.assertEqual((ch.tvg_id, ch.tvg_logo, ch.group_title, ch.stream_url), ('i2', 'L2', 'G2', 'http://s/2'))
        self.assertTrue(ch.included)             # user's tick survives an update
        self.assertEqual(ch.first_seen, first_seen)  # first_seen is not reset
        self.assertGreaterEqual(ch.last_updated, first_seen)


import tempfile
from datetime import timedelta
from unittest import mock

from iptv_filter import logo_cache
from iptv_filter.models import LogoCache

PNG = b'\x89PNG\r\n\x1a\n' + b'x' * 200


class LogoCacheTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith('/ok'):
                    self._send(200, 'image/png', PNG)
                elif self.path.startswith('/octet'):
                    self._send(200, 'application/octet-stream', PNG)   # type only detectable by content
                elif self.path.startswith('/big'):
                    self._send(200, 'image/png', b'\x89PNG' + b'x' * (3 * 1024 * 1024))
                elif self.path.startswith('/html'):
                    self._send(200, 'text/html', b'<html>nope</html>')
                elif self.path.startswith('/redir'):
                    self.send_response(302); self.send_header('Location', '/ok2.png'); self.send_header('Content-Length', '0'); self.end_headers()
                else:
                    self._send(404, 'text/plain', b'no')

            def _send(self, code, ctype, body):
                self.send_response(code)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        cls.server = HTTPServer(('127.0.0.1', 0), Handler)
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        super().tearDownClass()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict('os.environ', {'IPTV_LOGO_DIR': self.tmp.name, 'IPTV_LOGO_ALLOW_PRIVATE': '1',
                                             'IPTV_LOGO_CACHE': '', 'IPTV_SAFE_START': '1'})
        env.start()
        self.addCleanup(env.stop)

    def _channel(self, name, logo, included=True):
        now = timezone.now()
        return PlaylistChannel.objects.create(tvg_name=name, tvg_id=name, tvg_logo=logo, group_title='G',
                                              stream_url=f'http://s/{name}', last_updated=now, first_seen=now, included=included)

    def _m3u(self):
        return Client().get('/m3u', HTTP_HOST='kodi.lan:8000').content.decode()

    def test_only_ticked_channels_logos_are_downloaded(self):
        self._channel('A', f'{self.base}/ok-a.png')
        self._channel('B', f'{self.base}/ok-b.png', included=False)
        stats = logo_cache.sync()
        self.assertEqual((stats['selected'], stats['downloaded']), (1, 1))
        self.assertEqual(LogoCache.objects.count(), 1)
        self.assertTrue(os.path.exists(logo_cache.file_path(logo_cache.logo_hash(f'{self.base}/ok-a.png'))))

    def test_m3u_points_at_local_cache_once_cached(self):
        url = f'{self.base}/ok-a.png'
        self._channel('A', url)
        self.assertIn(f'tvg-logo="{url}"', self._m3u())            # not cached yet: original URL
        logo_cache.sync()
        h = logo_cache.logo_hash(url)
        out = self._m3u()
        self.assertIn(f'tvg-logo="http://kodi.lan:8000/logo/{h}.png"', out)
        self.assertNotIn(url, out)

    def test_uncached_logo_keeps_original_url_even_when_others_are_cached(self):
        self._channel('A', f'{self.base}/ok-a.png')
        self._channel('B', f'{self.base}/html-b.png')              # not an image -> fails
        logo_cache.sync()
        out = self._m3u()
        self.assertIn('/logo/', out)
        self.assertIn(f'tvg-logo="{self.base}/html-b.png"', out)

    def test_logo_is_served_with_safe_headers_and_etag(self):
        url = f'{self.base}/ok-a.png'
        self._channel('A', url)
        logo_cache.sync()
        h = logo_cache.logo_hash(url)
        c = Client()
        r = c.get(f'/logo/{h}.png')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b''.join(r.streaming_content), PNG)
        self.assertEqual(r['Content-Type'], 'image/png')
        self.assertEqual(r['X-Content-Type-Options'], 'nosniff')
        self.assertIn('sandbox', r['Content-Security-Policy'])
        self.assertIn('max-age', r['Cache-Control'])
        r2 = c.get(f'/logo/{h}.png', HTTP_IF_NONE_MATCH=r['ETag'])
        self.assertEqual(r2.status_code, 304)

    def test_logo_view_rejects_unknown_and_malformed_names(self):
        c = Client()
        for name in ('0' * 40 + '.png', 'nothex.png', '../../etc/passwd', '..%2f..%2fsecret', 'A' * 40):
            self.assertEqual(c.get(f'/logo/{name}').status_code, 404, name)

    def test_non_image_is_not_cached_and_retry_is_backed_off(self):
        url = f'{self.base}/html-x.png'
        self._channel('A', url)
        logo_cache.sync()
        row = LogoCache.objects.get()
        self.assertEqual((row.status, row.fail_count), ('failed', 1))
        self.assertGreater(row.next_attempt, timezone.now())
        self.assertEqual(logo_cache.sync()['attempted'], 0)         # not retried before next_attempt
        self.assertEqual(Client().get(f'/logo/{row.url_hash}.png').status_code, 404)

    def test_failed_logo_is_retried_when_due(self):
        url = f'{self.base}/html-x.png'
        self._channel('A', url)
        logo_cache.sync()
        LogoCache.objects.update(next_attempt=timezone.now() - timedelta(minutes=1))
        self.assertEqual(logo_cache.sync()['attempted'], 1)
        self.assertEqual(LogoCache.objects.get().fail_count, 2)

    def test_oversized_image_is_rejected(self):
        self._channel('A', f'{self.base}/big.png')
        logo_cache.sync()
        row = LogoCache.objects.get()
        self.assertEqual(row.status, 'failed')
        self.assertIn('too large', row.last_error)

    def test_type_is_detected_from_content_when_server_says_octet_stream(self):
        self._channel('A', f'{self.base}/octet.bin')
        logo_cache.sync()
        row = LogoCache.objects.get()
        self.assertEqual((row.status, row.content_type), ('ok', 'image/png'))

    def test_redirects_are_followed(self):
        self._channel('A', f'{self.base}/redir')
        logo_cache.sync()
        self.assertEqual(LogoCache.objects.get().status, 'ok')

    def test_embedded_data_uri_is_stored_without_download(self):
        import base64
        uri = 'data:image/png;base64,' + base64.b64encode(PNG).decode()
        self._channel('A', uri)
        logo_cache.sync()
        self.assertEqual(LogoCache.objects.get().status, 'ok')
        self.assertNotIn('data:image', self._m3u())                 # the huge inline logo became a short URL

    def test_unused_logos_are_purged_after_30_days(self):
        ch = self._channel('A', f'{self.base}/ok-a.png')
        logo_cache.sync()
        row = LogoCache.objects.get()
        path = logo_cache.file_path(row.url_hash)
        ch.included = False
        ch.save()
        logo_cache.sync()
        self.assertTrue(os.path.exists(path))                       # kept for a while after unticking
        LogoCache.objects.update(last_selected=timezone.now() - timedelta(days=31))
        logo_cache.sync()
        self.assertFalse(os.path.exists(path))
        self.assertEqual(LogoCache.objects.count(), 0)

    def test_switch_off_restores_original_urls(self):
        from iptv_filter import settings_store
        url = f'{self.base}/ok-a.png'
        self._channel('A', url)
        logo_cache.sync()
        self.assertIn('/logo/', self._m3u())
        settings_store.set_gui_logo_cache(False)
        self.assertIn(f'tvg-logo="{url}"', self._m3u())

    def test_settings_page_toggles_cache_and_shows_stats(self):
        from iptv_filter import settings_store
        c = Client()
        c.post('/settings', {'m3u_url': '', 'epg_url': '', 'action': 'save'})   # checkbox absent = off
        self.assertEqual(settings_store.get_logo_cache(), (False, 'gui'))
        c.post('/settings', {'m3u_url': '', 'epg_url': '', 'logo_cache': 'on', 'action': 'save'})
        self.assertEqual(settings_store.get_logo_cache(), (True, 'gui'))
        self.assertContains(c.get('/settings'), 'cached (')
        self.assertContains(c.get('/dashboard'), 'Logo cache')

    def test_env_can_disable_and_gui_wins(self):
        from iptv_filter import settings_store
        with mock.patch.dict('os.environ', {'IPTV_LOGO_CACHE': '0'}):
            self.assertEqual(settings_store.get_logo_cache(), (False, 'env'))
            settings_store.set_gui_logo_cache(True)
            self.assertEqual(settings_store.get_logo_cache(), (True, 'gui'))

    def test_request_sync_is_inert_in_safe_mode(self):
        self.assertFalse(logo_cache.request_sync())

    def test_ticking_a_channel_asks_for_a_sync(self):
        ch = self._channel('A', f'{self.base}/ok-a.png', included=False)
        with mock.patch.object(logo_cache, 'request_sync') as m:
            Client().post(f'/channels/{ch.pk}', data=json.dumps({'included': True}), content_type='application/json')
        m.assert_called()


class LogoSafetyTests(TestCase):
    """The app downloads URLs from a playlist; it must not be usable to reach
    the local machine or private network."""

    def test_blocks_private_loopback_linklocal_and_odd_schemes(self):
        for url in ('http://127.0.0.1/x.png', 'http://localhost/x.png', 'http://10.0.0.5/x.png',
                    'http://192.168.1.1/x.png', 'http://169.254.169.254/latest/meta-data/',
                    'http://[::1]/x.png', 'file:///etc/passwd', 'ftp://example.com/x.png', 'gopher://x/'):
            with self.assertRaises(ValueError, msg=url):
                logo_cache.check_url_allowed(url)

    def test_allows_public_address(self):
        with mock.patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 80))]):
            logo_cache.check_url_allowed('http://img.example.com/x.png')   # no exception

    def test_hostname_resolving_to_private_address_is_blocked(self):
        with mock.patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('10.1.2.3', 80))]):
            with self.assertRaises(ValueError):
                logo_cache.check_url_allowed('http://looks-public.example.com/x.png')

    def test_private_logo_url_is_recorded_as_failed_not_fetched(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict('os.environ', {'IPTV_LOGO_DIR': d, 'IPTV_LOGO_ALLOW_PRIVATE': ''}):
            now = timezone.now()
            PlaylistChannel.objects.create(tvg_name='A', tvg_id='a', tvg_logo='http://127.0.0.1:9/x.png', group_title='G',
                                           stream_url='http://s/1', last_updated=now, first_seen=now, included=True)
            with mock.patch('requests.get') as get:
                logo_cache.sync()
            get.assert_not_called()
            self.assertIn('non-public', LogoCache.objects.get().last_error)


from iptv_filter import epg_quality


class EpgTestBase(TestCase):
    def _pc(self, name, tvg_id, included=False, group='G'):
        now = timezone.now()
        return PlaylistChannel.objects.create(tvg_name=name, tvg_id=tvg_id, group_title=group, stream_url=f'http://s/{name}',
                                              last_updated=now, first_seen=now, included=included)

    def _epg(self, channel_id, programmes=1, included=None, start='20261011120000 +0200', stop='20261011130000 +0200'):
        EpgChannel.objects.create(channel_id=channel_id, display_name=channel_id, included=included, last_updated=timezone.now())
        for i in range(programmes):
            EpgProgramme.objects.create(start=start, stop=stop, title=f'P{i}', channel=channel_id, included=included,
                                        last_updated=timezone.now())



class EpgFlagSyncTests(EpgTestBase):
    def test_ticking_a_channel_updates_guide_flags_immediately(self):
        ch = self._pc('A', 'a.nl')
        self._epg('a.nl', programmes=3)
        self._epg('b.nl', programmes=2)
        r = Client().post(f'/channels/{ch.pk}', data=json.dumps({'included': True}), content_type='application/json')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(EpgChannel.objects.get(channel_id='a.nl').included)
        self.assertEqual(EpgProgramme.objects.filter(included=True).count(), 3)
        self.assertFalse(EpgChannel.objects.get(channel_id='b.nl').included)

    def test_unticking_removes_guide_data_again(self):
        ch = self._pc('A', 'a.nl', included=True)
        self._epg('a.nl', programmes=2, included=True)
        Client().post(f'/channels/{ch.pk}', data=json.dumps({'included': False}), content_type='application/json')
        self.assertFalse(EpgChannel.objects.get(channel_id='a.nl').included)
        self.assertEqual(EpgProgramme.objects.filter(included=True).count(), 0)

    def test_shared_tvg_id_stays_included_while_any_channel_is_ticked(self):
        a = self._pc('A HD', 'a.nl', included=True)
        self._pc('A SD', 'a.nl', included=True)
        self._epg('a.nl', included=True)
        a.included = False
        a.save()
        epg_quality.sync_flags()
        self.assertTrue(EpgChannel.objects.get(channel_id='a.nl').included)

    def test_empty_tvg_id_matches_nothing_and_null_flags_are_handled(self):
        self._pc('NoId', '', included=True)
        self._epg('x.nl', included=None)                      # NULL flag, not selected
        epg_quality.sync_flags()
        self.assertFalse(EpgChannel.objects.get(channel_id='x.nl').included)
        self._pc('X', 'x.nl', included=True)
        epg_quality.sync_flags()
        self.assertTrue(EpgChannel.objects.get(channel_id='x.nl').included)

    def test_sync_only_writes_rows_that_change(self):
        self._pc('A', 'a.nl', included=True)
        self._epg('a.nl', programmes=4)
        self.assertEqual(epg_quality.sync_flags(), 5)         # 1 channel + 4 programmes
        self.assertEqual(epg_quality.sync_flags(), 0)         # nothing left to do

    def test_group_save_updates_guide_flags(self):
        self._pc('A', 'a.nl', group='G1')
        self._pc('B', 'b.nl', group='G2')
        self._epg('a.nl')
        self._epg('b.nl')
        Client().post('/configure', {'G1': 'G1'})
        self.assertTrue(EpgChannel.objects.get(channel_id='a.nl').included)
        self.assertFalse(EpgChannel.objects.get(channel_id='b.nl').included)

    def test_sync_is_fast_on_a_large_guide(self):
        import time
        for i in range(200):
            self._pc(f'C{i}', f'id{i}', included=(i % 2 == 0))
        EpgProgramme.objects.bulk_create([
            EpgProgramme(start='20261011120000 +0200', stop='20261011130000 +0200', title='t', channel=f'id{i % 200}', last_updated=timezone.now())
            for i in range(30000)])
        EpgChannel.objects.bulk_create([EpgChannel(channel_id=f'id{i}', display_name='x', last_updated=timezone.now()) for i in range(200)])
        t = time.time()
        epg_quality.sync_flags()
        self.assertLess(time.time() - t, 5)
        self.assertEqual(EpgProgramme.objects.filter(included=True).count(), 15000)


class EpgIdFixTests(EpgTestBase):
    def test_names_agree(self):
        a = epg_quality.names_agree
        self.assertTrue(a('NL| NPO 1 HD', 'NPO1.nl'))
        self.assertTrue(a('NL| RTL Z FHD', 'RTLZ.nl'))
        self.assertTrue(a('NL| L1 LIMBURG HD', 'L1TV.nl'))
        self.assertTrue(a('NL| OMROEP FRYSLAND HD', 'OmropFryslan.nl'))
        self.assertTrue(a('NL| TV GELDERLAND FHD', 'TVGelderland.nl'))
        self.assertFalse(a('NL| NPO 1 HD', 'NPO2.nl'))                 # number differs
        self.assertFalse(a('NL| TV NOORD HD', 'RTVUtrecht.nl'))
        self.assertFalse(a('NL| OMROEP BRABANT FHD', 'OmroepGelderland.nl'))
        self.assertTrue(a('NL| EUROSPORT 1 HD', 'x.nl', 'Eurosport 1'))  # display name counts too

    def test_m3u_uses_epg_spelling_and_epg_is_included(self):
        self._pc('NL| NPO 1 HD', 'npo1.nl', included=True)
        self._epg('NPO1.nl', programmes=2)
        epg_quality.sync_flags()
        body = Client().get('/m3u').content.decode()
        self.assertIn('tvg-id="NPO1.nl"', body)
        self.assertNotIn('tvg-id="npo1.nl"', body)
        self.assertEqual(EpgProgramme.objects.filter(channel='NPO1.nl', included=True).count(), 2)
        self.assertIn('<channel id="NPO1.nl">', Client().get('/epg').content.decode())

    def test_conflicting_name_is_left_alone(self):
        self._pc('NL| TV NOORD HD', 'rtvutrecht.nl', included=True)
        self._epg('RTVUtrecht.nl', programmes=1)
        epg_quality.sync_flags()
        self.assertIn('tvg-id="rtvutrecht.nl"', Client().get('/m3u').content.decode())
        self.assertEqual(EpgProgramme.objects.filter(included=True).count(), 0)
        cov = epg_quality.coverage()
        self.assertEqual((cov['matched'], cov['counts']['conflict']), (0, 1))
        self.assertContains(Client().get('/coverage'), 'another channel')

    def test_exact_match_wins_and_switch_off(self):
        import os
        from unittest import mock
        self._pc('NL| NPO 1 HD', 'npo1.nl', included=True)
        self._epg('NPO1.nl', programmes=1)
        with mock.patch.dict(os.environ, {'IPTV_FIX_EPG_IDS': '0'}):
            epg_quality.sync_flags()
            self.assertIn('tvg-id="npo1.nl"', Client().get('/m3u').content.decode())
            self.assertEqual(epg_quality.coverage()['counts']['case_only'], 1)
        self._epg('npo1.nl', programmes=1)                              # exact id now exists
        epg_quality.sync_flags()
        self.assertIn('tvg-id="npo1.nl"', Client().get('/m3u').content.decode())


class EpgCoverageTests(EpgTestBase):
    def test_categories(self):
        self._pc('Good', 'good.nl', included=True)
        self._pc('NoId', '', included=True)
        self._pc('Missing', 'missing.nl', included=True)
        self._pc('Case', 'case.NL', included=True)
        self._pc('Empty', 'empty.nl', included=True)
        self._pc('Unticked', 'nope.nl', included=False)
        self._epg('good.nl', programmes=2)
        self._epg('case.nl', programmes=1)
        self._epg('empty.nl', programmes=0)
        epg_quality.sync_flags()
        cov = epg_quality.coverage()
        self.assertEqual((cov['ticked'], cov['matched'], cov['percent']), (5, 2, 40))   # 'Case' is corrected automatically
        self.assertEqual(cov['counts'], {'no_id': 1, 'case_only': 0, 'conflict': 0, 'missing': 1, 'empty': 1})
        self.assertEqual(cov['fixed'][0]['epg_id'], 'case.nl')
        self.assertEqual(cov['problems']['missing'][0]['name'], 'Missing')

    def test_horizon_from_programmes_of_ticked_channels(self):
        self._pc('A', 'a.nl', included=True)
        self._epg('a.nl', start='20990101120000 +0000', stop='20990101130000 +0000')
        epg_quality.sync_flags()
        h = epg_quality.coverage()['horizon']
        self.assertEqual(h['last'].year, 2099)
        self.assertGreater(h['hours_ahead'], 1000)

    def test_empty_selection_does_not_crash(self):
        cov = epg_quality.coverage()
        self.assertEqual((cov['ticked'], cov['percent'], cov['horizon']), (0, 0, None))

    def test_pages_render(self):
        self._pc('Missing', 'missing.nl', included=True)
        r = Client().get('/coverage')
        self.assertContains(r, 'Id not in the EPG file')
        self.assertContains(r, 'missing.nl')
        self.assertContains(Client().get('/dashboard'), 'EPG coverage')
        self.assertContains(Client().get('/coverage'), 'app-sidebar')

    def test_names_are_escaped(self):
        self._pc('<script>x</script>', 'm.nl', included=True)
        self.assertNotIn('<script>x</script>', Client().get('/coverage').content.decode())


import gzip as _gzip


class GzipRetrieveTests(TestCase):
    """EPG sources often publish epg.xml.gz - the gzip is the file itself, not
    HTTP Content-Encoding."""

    XML = ('<?xml version="1.0" encoding="utf-8"?><tv>'
           '<channel id="a.nl"><display-name>A</display-name></channel>'
           '<programme start="20261011120000 +0200" stop="20261011130000 +0200" channel="a.nl"><title>Show é</title></programme>'
           '</tv>').encode()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        xml = cls.XML

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith('/epg.xml.gz'):          # gzip file, served as a binary download
                    self._send(_gzip.compress(xml), 'application/gzip')
                elif self.path.startswith('/epg-ce'):             # plain XML with Content-Encoding: gzip
                    self._send(_gzip.compress(xml), 'application/xml', {'Content-Encoding': 'gzip'})
                elif self.path.startswith('/plain'):
                    self._send(xml, 'application/xml')
                elif self.path.startswith('/truncated'):
                    self._send(_gzip.compress(xml)[:-12], 'application/gzip')
                elif self.path.startswith('/garbage'):
                    self._send(b'\x1f\x8b' + b'not really gzip', 'application/gzip')
                elif self.path.startswith('/bomb'):
                    self._send(_gzip.compress(b'0' * (3 * 1024 * 1024)), 'application/gzip')
                else:
                    self._send(b'no', 'text/plain', code=404)

            def _send(self, body, ctype, extra=None, code=200):
                self.send_response(code)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        cls.server = HTTPServer(('127.0.0.1', 0), Handler)
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        super().tearDownClass()

    def _fetch(self, path):
        from iptv_filter import settings_store
        settings_store.set_gui_url('epg', f'{self.base}{path}')
        return iptv_updater._retrieve('epg')

    def test_gzipped_file_is_unpacked_and_processed(self):
        self.assertTrue(self._fetch('/epg.xml.gz'))
        self.assertEqual(bytes(CachedFile.objects.get(file_type='epg').file), self.XML)
        iptv_updater._update_tables('epg')
        self.assertEqual(EpgChannel.objects.filter(channel_id='a.nl').count(), 1)
        self.assertEqual(EpgProgramme.objects.get(channel='a.nl').title, 'Show é')

    def test_http_content_encoding_gzip_still_works(self):
        self.assertTrue(self._fetch('/epg-ce'))
        self.assertEqual(bytes(CachedFile.objects.get(file_type='epg').file), self.XML)

    def test_plain_xml_is_untouched(self):
        self.assertTrue(self._fetch('/plain'))
        self.assertEqual(bytes(CachedFile.objects.get(file_type='epg').file), self.XML)

    def test_corrupt_or_truncated_gzip_is_a_warning_not_an_exception(self):
        for path in ('/truncated', '/garbage'):
            with self.assertLogs('iptv_updater.iptv_updater', level='WARNING'):
                self.assertFalse(self._fetch(path), path)
            self.assertIn('unpack', iptv_updater.get_last_error('epg')['message'])
        self.assertFalse(CachedFile.objects.filter(file_type='epg').exists())  # nothing half-written

    def test_decompression_bomb_is_refused(self):
        with mock.patch.object(iptv_updater, 'MAX_UNPACKED_BYTES', 1024 * 1024):
            with self.assertLogs('iptv_updater.iptv_updater', level='WARNING'):
                self.assertFalse(self._fetch('/bomb'))
        self.assertIn('larger than', iptv_updater.get_last_error('epg')['message'])

    def test_gzip_also_works_for_the_playlist(self):
        m3u = b'#EXTM3U\n#EXTINF:-1 tvg-name="A" group-title="G",A\nhttp://s/1\n'
        data = _gzip.compress(m3u)
        r = mock.Mock(content=data, status_code=200)
        r.raise_for_status = lambda: None
        from iptv_filter import settings_store
        settings_store.set_gui_url('m3u', 'http://example.com/list.m3u.gz')
        with mock.patch('requests.get', return_value=r):
            self.assertTrue(iptv_updater._retrieve('m3u'))
        self.assertEqual(bytes(CachedFile.objects.get(file_type='m3u').file), m3u)
