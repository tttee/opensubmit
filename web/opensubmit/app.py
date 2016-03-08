from django.apps import AppConfig

# Give human readable names to apps in the Django admin view
class OpenSubmitConfig(AppConfig):
    name = 'opensubmit'
    verbose_name = "Teacher Backend"

    def ready(self):
    	import signalhandlers