# iptv-filter

I was sick of trying to parse through 13000+ "Channels" on an IPTV service so I decided to write this and share it.
I've never used Django before (and I'm a python beginner) so I'm sure there is improvement available in the codebase.

# Concept
This application pulls an m3u file and an epg file and stores their components in a database; users tag the groups they wish to subscribe to within that subset and the app serves up an m3u file on the fly composed of the segments that are included.

# Launching iptv-filter
There are two options for running this: Docker, and Plain Python.
In both cases you will setting up the prerequisite libraries and initial backing database via the below commands.

## Docker
### Initialization

* Run `docker-compose build` to build the application
* Edit `docker-compose.yml` and insert your own personal URLs for retrieving the M3U and EPG files.  

### Running

Run the container: `docker-compose up`

## Python

### Linux  

#### Initialization

* Install `python3 python3-pip git` via `apt`/`yum`/whatever package manager
* Clone this repo into a folder
* `cd` into the new folder (`iptv-filter` by default)
* From the root folder, run `pip3 install -r requirements.txt`
* Run `IPTV_SAFE_START=1 python3 manage.py migrate`

#### Running

* `export IPTV_M3U_URL="<your_m3u_url>"`
* `export IPTV_EPG_URL="<your_epg_url>"`
* `export TZ="Europe/Amsterdam"` (optional - controls how times are displayed, e.g. in the admin site and the `runserver` startup banner; defaults to `Europe/Amsterdam` if not set)
* `export IPTV_USER_AGENT="..."` (optional - the User-Agent sent when downloading; defaults to a browser-like one because many IPTV panels reset connections from `python-requests`)
* `export IPTV_READ_TIMEOUT=300` (optional - seconds to wait for the provider to start/continue sending; raise it if your provider is slow to generate the playlist)
* `export IPTV_SKIP_VOD=1` (optional - skip movie/series entries (Xtream-style `/movie/` and `/series/` URLs) when loading the M3U. Provider playlists that include a VOD library can be hundreds of MB and millions of entries; this keeps only live channels and greatly cuts memory use and load time)
* `python3 manage.py runserver 0:8000 --noreload` (this is what `./run.sh` does)

#### Upgrading

* `IPTV_SAFE_START=1 python3 manage.py makemigrations`
* `IPTV_SAFE_START=1 python3 manage.py migrate`

### Windows

#### Initialization

* Install python 3 via `apt`/`yum`/whatever package manager
* From the root folder, run `pip3 install -r requirements.txt`
* `SET IPTV_SAFE_START=1`
* `python manage.py migrate`
* `SET IPTV_SAFE_START=`

### Running

* `SET IPTV_M3U_URL="<your_m3u_url>"`
* `SET IPTV_EPG_URL="<your_epg_url>"`
* `SET TZ="Europe/Amsterdam"` (optional - see the note above)
* `python manage.py runserver 0:8000 --noreload`

#### Upgrading

* `SET IPTV_SAFE_START=1`
* `python manage.py makemigrations`
* `python manage.py migrate`
* `SET IPTV_SAFE_START=`

# Configuration: web GUI vs. environment variables

The M3U and EPG URLs (and the movies/series switch) can be set either with environment variables (`IPTV_M3U_URL`, `IPTV_EPG_URL`, `IPTV_SKIP_VOD`) or on the **Settings** page. **A value saved in the GUI always wins**; the environment variable is only used while nothing is saved there. Clear a URL field on the Settings page and save to go back to the environment variable.

The container log says which one is in use at every start, for example:

```
Startup - M3U URL: using the value configured in the web GUI: http://host/get.php?username=***&password=*** (environment variable IPTV_M3U_URL is set but ignored - clear the URL on the Settings page to use it)
Startup - EPG URL: nothing saved in the web GUI, using environment variable IPTV_EPG_URL: https://example.org/epg.xml
Retrieving fresh m3u file from http://host/get.php?username=***&password=*** (configured in the web GUI)
```

Credentials in URLs are always masked in the log.

# Web interface

The pages share one layout ([AdminLTE](https://github.com/ColorlibHQ/AdminLTE) / Bootstrap 5) with a sidebar, and a light / dark / auto theme switch in the top bar. All front-end libraries (AdminLTE, Bootstrap, Bootstrap Icons, ag-grid, axios) are bundled under `iptv_filter/static/vendor/` and served locally, so the UI works without internet access. Versions: AdminLTE 4.10.0, Bootstrap 5.3.8, Bootstrap Icons 1.13.2, ag-grid-community 36.1.0, axios 1.20.0.

# Logo cache

Players such as Kodi download every `tvg-logo` themselves from the provider's image host, which is slow on first load. This app can instead cache the logos **of the channels you have ticked** and serve them itself:

* Logos are downloaded once, in the background (after the daily M3U refresh, after you save groups, and shortly after you tick channels), into `logo_cache/` next to the database.
* `/m3u` then points `tvg-logo` at `http://<the host your player used>/logo/<hash>.png` - but only for logos that are actually cached. Anything not cached yet keeps its original URL, so a player never gets a dead link.
* Logos nobody has used for 30 days are deleted; failed downloads are retried after 1 h, 6 h, then every 24 h.
* The app only downloads `http`/`https` images up to 2 MB, and refuses addresses on private networks (so a playlist can't make it probe your LAN).
* Switch it on or off on the **Settings** page (a saved value wins over the environment). Status is shown on the Dashboard.

Environment variables (all optional): `IPTV_LOGO_CACHE=0` (disable by default), `IPTV_LOGO_DIR` (cache folder), `IPTV_LOGO_ALLOW_PRIVATE=1` (allow logo hosts on private/LAN addresses). In Docker, mount a volume on `/code/logo_cache` to keep the cache when the container is recreated; otherwise it simply refills.

# Scheduled retrieval

* When launched, the application will pull the latest m3u and epg files. Depending on your host this could take over 20 minutes.
* The logs will indicate when the M3U file and then the EPG file have completed loading
* Once this is complete, use the API's below.
* M3U are otherwise scheduled to reload at 4AM every day
* EPG are scheduled to be loaded on the half-hour, every hour (1:30, 2:30, ...)

# API URLs

(Assuming you want to access this from the same box, you'll be using 'localhost' as the URL)
The following are the common URLs to use:
* http://localhost:8000/settings - set the M3U and EPG URLs and the "skip movies/series" switch from the browser, and re-process the stored playlist without downloading it again.
* http://localhost:8000/dashboard - overview: health, last fetch times, channel counts, any current error, and copy-ready URLs for your IPTV player.
* http://localhost:8000/ - RUN THIS FIRST - This page allows you to adjust per-channel subscription. Changes are applied immediately.
* http://localhost:8000/configure - set up the included groups whose channels you will be watching (Hit submit at the bottom of the page). Older interface but useful for managing entire groups at a time.
* http://localhost:8000/m3u - the m3u file your IPTV player should point to
* http://localhost:8000/epg - the epg file your IPTV player should point to
* http://localhost:8000/status - JSON health check: last successful fetch time, current error (if any), and channel/programme counts for both m3u and epg. Useful if you're running this unattended (e.g. in Docker) and want to confirm it's actually staying up to date. Example:
  ```json
  {
    "healthy": true,
    "server_time": "2026-09-12T17:54:03Z",
    "m3u": {
      "url_configured": true,
      "last_successful_fetch": "2026-09-12T04:00:03Z",
      "last_error": null,
      "channel_count": 2030,
      "included_channel_count": 500
    },
    "epg": {
      "url_configured": true,
      "last_successful_fetch": "2026-09-12T17:30:02Z",
      "last_error": null,
      "channel_count": 184,
      "included_channel_count": 40,
      "programme_count": 37025,
      "included_programme_count": 8200
    }
  }
  ```
  `healthy` is `false` until both m3u and epg have a URL configured and have loaded successfully at least once. `last_error`, when present, shows the most recent failure and when it happened; it's cleared automatically the next time that source loads successfully.

These are utility URLs and probably won't be needed.
* http://localhost:8000/retrieve/m3u - force an immediate m3u retrieval
* http://localhost:8000/retrieve/epg - force an immediate epg retrieval
* http://localhost:8000/update/m3u - force an immediate m3u metadata table update
* http://localhost:8000/update/epg - force an immediate epg metadata table update

