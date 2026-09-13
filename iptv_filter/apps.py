import threading
from django.apps import AppConfig

# import the logging library / Get an instance of a logger
import logging
logger = logging.getLogger(__name__)

class IptvFilterConfig(AppConfig):
    name = 'iptv_filter'
    
    # Respond to environment variables (for docker-friendly configuration)
    # If you see this run twice(!) you must run 'runserver' with --noreload so a second thread isnt launched for reloading!
    def ready(self):
        import os
        from django.utils import timezone
        from . import __version__
        from .models import AppConfig as AppConfigModel
        from iptv_updater import iptv_updater

        logging.info(f"iptv-filter v{__version__} starting up")

        safe_start = os.getenv('IPTV_SAFE_START')
        if safe_start:
            return

        m3u_url = os.getenv('IPTV_M3U_URL')
        epg_url = os.getenv('IPTV_EPG_URL')

        if m3u_url:
            logging.info("Startup - Setting M3U URL as per environment variable")
            ac = AppConfigModel(key='m3u_url', value=m3u_url, last_updated=timezone.now())
            ac.save()

        if epg_url:
            logging.info("Startup - Setting EPG URL as per environment variable")
            ac = AppConfigModel(key='epg_url', value=epg_url, last_updated=timezone.now())
            ac.save()

        # daemon=True so these threads never block the process from exiting
        # cleanly (e.g. on `docker stop`) - without it, Python won't exit
        # while a thread with an hours-long sleep() is still alive.
        threading.Thread(target=iptv_updater.update_all, name='iptv-startup-update', daemon=True).start()
        threading.Thread(target=iptv_updater.update_m3u_scheduled, name='iptv-m3u-scheduler', daemon=True).start()
        threading.Thread(target=iptv_updater.update_epg_scheduled, name='iptv-epg-scheduler', daemon=True).start()

        return
