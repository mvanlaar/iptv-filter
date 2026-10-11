from django.db import models
from xml.sax.saxutils import escape as _xml_escape

# M3U has no formal escaping convention, so a value that would break line
# framing (CR/LF - which could inject extra fake #EXTINF/URL lines into the
# output) or attribute quoting (an embedded ") has to be sanitized by hand.
def _m3u_safe(value):
    if value is None:
        return ''
    return str(value).replace('\r', ' ').replace('\n', ' ').replace('"', "'")

# XML text content only needs &, <, > escaped; attribute values (which this
# code always double-quotes) also need " escaped so an embedded quote can't
# terminate the attribute early.
def _xml_text(value):
    return _xml_escape(value or '')

def _xml_attr(value):
    return _xml_escape(value or '', {'"': '&quot;'})

# this will be configs like the URL to pull from, how often to refresh etc
class AppConfig(models.Model):
    key = models.CharField(max_length=50)
    value = models.TextField(max_length=100)
    last_updated = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return '[' + self.key + ': ' + self.value + ']' 

# Some notes about this model:
# - no foreign keys so we can do fast bulk inserts when we import source files
# - expectation is for app to manage changes to inclusion across the 3 types 

class PlaylistChannel(models.Model):
    tvg_id = models.CharField(max_length=50)
    tvg_name = models.CharField(max_length=100)  # indexed via the UniqueConstraint in Meta (a second db_index here would just duplicate that index)
    tvg_logo = models.TextField() #this is sometimes an embedded image, which is large.
    stream_url = models.URLField()
    first_seen = models.DateTimeField(null=True,blank=True, db_index=True)
    last_updated = models.DateTimeField(null=True,blank=True, db_index=True)
    included = models.BooleanField(default=None,null=True, db_index=True) #None = inherit from PlaylistGroup, False = force no, True = force yes.
    group_title = models.CharField(max_length=50)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['tvg_name'],name="unique name for PlaylistChannel")
        ]

    def __str__(self):
        return self.to_m3u()

    def to_m3u(self, logo_override=None, id_override=None):
        """The #EXTINF + URL lines for this channel. logo_override replaces the
        stored logo URL (used to point at the local logo cache); id_override
        replaces the tvg-id (letter-case correction towards the EPG)."""
        tvg_id = _m3u_safe(self.tvg_id if id_override is None else id_override)
        tvg_name = _m3u_safe(self.tvg_name)
        tvg_logo = _m3u_safe(self.tvg_logo if logo_override is None else logo_override)
        group_title = _m3u_safe(self.group_title)
        stream_url = _m3u_safe(self.stream_url)
        text = f"#EXTINF:-1 tvg-id=\"{tvg_id}\" tvg-name=\"{tvg_name}\" tvg-logo=\"{tvg_logo}\" group-title=\"{group_title}\",{tvg_name}\r\n"
        text += stream_url
        return text

class EpgChannel(models.Model):
    channel_id = models.CharField(max_length=50, db_index=True)
    display_name = models.CharField(max_length=50)
    icon = models.TextField(null=True)
    last_updated = models.DateTimeField(null=True,blank=True, db_index=True)
    included = models.BooleanField(default=None,null=True, db_index=True) #None = inherit from PlaylistGroup, False = force no, True = force yes.
    def __str__(self):
        text =  f'<channel id="{_xml_attr(self.channel_id)}">\n'
        text += f'  <display-name>{_xml_text(self.display_name)}</display-name>\n'
        if self.icon:
            text += f'  <icon src="{_xml_attr(self.icon)}"/>\n'
        text += '</channel>'
        return text

class EpgProgramme(models.Model):
    start = models.CharField(max_length=20)
    stop = models.CharField(max_length=20)
    title = models.CharField(max_length=100)
    desc = models.TextField(blank=True)
    channel = models.CharField(max_length=50, db_index=True)
    last_updated = models.DateTimeField(null=True,blank=True, db_index=True)
    included = models.BooleanField(default=None,null=True, db_index=True) #None = inherit from PlaylistGroup, False = force no, True = force yes.
    def __str__(self):
        text =  f'<programme start="{_xml_attr(self.start)}" stop="{_xml_attr(self.stop)}" channel="{_xml_attr(self.channel)}">\n'
        text += f'  <title>{_xml_text(self.title)}</title>\n'
        if self.desc:
            text += f'  <desc>{_xml_text(self.desc)}</desc>\n'
        text += '</programme>'
        return text

class CachedFile(models.Model):
    file_type = models.TextField(max_length=5)
    file = models.BinaryField()
    last_updated = models.DateTimeField(null=True,blank=True, db_index=True)


class LogoCache(models.Model):
    """One row per distinct logo URL used by a ticked channel. The image itself
    lives on disk (see iptv_filter/logo_cache.py); this tracks its state."""
    PENDING, OK, FAILED = 'pending', 'ok', 'failed'
    url_hash = models.CharField(max_length=40, unique=True)  # sha1 of the url
    url = models.TextField()
    status = models.CharField(max_length=10, default='pending', db_index=True)
    content_type = models.CharField(max_length=60, blank=True, default='')
    size = models.IntegerField(default=0)
    fetched_at = models.DateTimeField(null=True, blank=True)
    next_attempt = models.DateTimeField(null=True, blank=True)
    fail_count = models.IntegerField(default=0)
    last_error = models.CharField(max_length=200, blank=True, default='')
    last_selected = models.DateTimeField(null=True, blank=True)  # last time a ticked channel used it
