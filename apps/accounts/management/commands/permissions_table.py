from django.core.management.base import BaseCommand
from django.utils import translation

from apps.accounts.permissions import matrix

_ABBR = {None: "–", 1: "A", 2: "M", 3: "S", 4: "Ad"}

HEADER = """# Berechtigungen

> Generiert mit `python manage.py permissions_table > docs/permissions.md` aus
> `apps/accounts/permissions.py`. Nicht von Hand bearbeiten — CI prüft Aktualität.

A = Applicant, M = Member, S = Staff, Ad = Admin, – = niemand.
Ein nicht definiertes niedrigeres Verb erbt die Stufe des nächsthöheren.

| Bereich | Name | view | use | manage |
|---|---|---|---|---|
"""

FOOTNOTE = """
**Zusatzregel (einzige zuweisungsbasierte):** Control-Override (Lock preempten)
dürfen `control.manage` sowie — sofern sie `control.use` haben — Station-Admins
der Station und Region-Manager ihrer Region (`can_override_station`).
"""


class Command(BaseCommand):
    help = "Print the permission matrix as Markdown (source of docs/permissions.md)."
    requires_system_checks = []

    def handle(self, *args, **options):
        with translation.override("de"):
            rows = [
                f"| `{r.area}` | {r.label} | {_ABBR[r.view and int(r.view)]} | "
                f"{_ABBR[r.use and int(r.use)]} | {_ABBR[r.manage and int(r.manage)]} |"
                for r in matrix()
            ]
        self.stdout.write(HEADER + "\n".join(rows) + "\n" + FOOTNOTE, ending="")
