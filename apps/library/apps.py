from django.apps import AppConfig
import threading

class LibraryConfig(AppConfig):
    name = 'apps.library'

    def ready(self):
        # Pre-load MiDaS model in a background thread so it doesn't block server startup
        # but is ready before the user clicks generate.
        try:
            from apps.library.views import _load_midas
            threading.Thread(target=_load_midas, daemon=True).start()
        except ImportError:
            pass
