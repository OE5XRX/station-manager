import pathlib
from io import StringIO

from django.core.management import call_command

DOC = pathlib.Path(__file__).resolve().parent.parent / "docs" / "permissions.md"


def test_permissions_doc_is_current():
    out = StringIO()
    call_command("permissions_table", stdout=out)
    assert DOC.read_text(encoding="utf-8") == out.getvalue(), (
        "docs/permissions.md is stale — run: "
        "python manage.py permissions_table > docs/permissions.md"
    )


def test_doc_contains_override_footnote():
    assert "can_override_station" in DOC.read_text(encoding="utf-8")
