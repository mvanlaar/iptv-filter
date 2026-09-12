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
