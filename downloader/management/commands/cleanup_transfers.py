from django.core.management.base import BaseCommand

from downloader.services import quick_service


class Command(BaseCommand):
    help = "Delete expired temporary download files (also done automatically on each new download)."

    def handle(self, *args, **options):
        removed = quick_service.sweep(force=True)
        self.stdout.write(self.style.SUCCESS(f"Removed {removed} expired temporary download(s)."))
