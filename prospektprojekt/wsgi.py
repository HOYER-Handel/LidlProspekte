import os
import sys

from django.core.wsgi import get_wsgi_application

path = "/home/HOYER-Handel/LidlProspekte"
if path not in sys.path:
    sys.path.append(path)

os.environ["DJANGO_SETTINGS_MODULE"] = "LidlProspekte.settings"

application = get_wsgi_application()
