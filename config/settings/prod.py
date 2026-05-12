from .base import *


DEBUG = False

ALLOWED_HOSTS = []

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.MySql',
        'NAME': BASE_DIR / 'db.MySql',
    }
}
