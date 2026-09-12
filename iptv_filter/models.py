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
    tvg_name = models.CharField(max_length=100, db_index=True)
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
        tvg_id = _m3u_safe(self.tvg_id)
        tvg_name = _m3u_safe(self.tvg_name)
        tvg_logo = _m3u_safe(self.tvg_logo)
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
