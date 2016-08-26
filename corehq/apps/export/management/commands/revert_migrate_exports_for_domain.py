from optparse import make_option
from django.core.management.base import LabelCommand

from corehq.apps.export.utils import revert_migrate_domain


class Command(LabelCommand):
    help = "Reverts a migrated export domain from new exports to old ones for a given domain"

    option_list = LabelCommand.option_list + (
        make_option(
            '--dry-run',
            action='store_true',
            dest='dryrun',
            default=False,
            help='Runs a dry run on the export reversion'
        ),
    )

    def handle(self, domain, *args, **options):
        dryrun = options.pop('dryrun')
        if dryrun:
            print '*** Running in dryrun mode. Will not save any reversions ***'
        revert_migrate_domain(domain, dryrun)