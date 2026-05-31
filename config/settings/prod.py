from .base import *


DEBUG = False

ALLOWED_HOSTS = ['landscapesss.pythonanywhere.com']

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.MySql',
        'NAME': BASE_DIR / 'db.MySql',
    }
}
