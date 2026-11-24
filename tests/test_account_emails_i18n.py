"""Account e-mails go out in the recipient's language, not the request's."""

import pytest
from django.core import mail
from django.utils import translation

from apps.accounts.emails import send_account_email
from apps.accounts.models import User


def _user(language, username="OE5MEM1", first_name="Hans"):
    return User.objects.create_user(
        username=username,
        email=f"{username.lower()}@example.org",
        first_name=first_name,
        password="x",
        language=language,
    )


VERIFY_CTX = {
    "raw_token": "VERIFYTOK",
    "new_email": "neu@example.org",
    "old_email": "m@example.org",
}

# kind -> (context, german subject marker, german body marker,
#          english subject marker, english body marker)
CASES = {
    "welcome": (
        {"raw_token": "TOK", "actor": "OE5ADMIN"},
        "Willkommen",
        "hat gerade dein OE5XRX-Konto angelegt",
        "Welcome",
        "just created your OE5XRX account",
    ),
    "reset": (
        {"raw_token": "TOK"},
        "Passwort",
        "24 Stunden",
        "Password reset",
        "24 hours",
    ),
    "verify": (
        VERIFY_CTX,
        "Bestätige",
        "Bestätige die Änderung",
        "Confirm",
        "Confirm the change",
    ),
}


@pytest.mark.django_db
@pytest.mark.parametrize("kind", list(CASES))
class TestRecipientLanguage:
    def test_german_user_gets_german_mail(self, kind):
        ctx, de_subj, de_body, en_subj, en_body = CASES[kind]
        send_account_email(_user("de"), kind, dict(ctx))
        msg = mail.outbox[0]
        assert de_subj in msg.subject
        assert en_subj not in msg.subject
        assert "Hallo Hans," in msg.body
        assert de_body in msg.body
        assert "Das OE5XRX-Team" in msg.body
        assert en_body not in msg.body

    def test_english_user_gets_english_mail(self, kind):
        ctx, de_subj, de_body, en_subj, en_body = CASES[kind]
        send_account_email(_user("en"), kind, dict(ctx))
        msg = mail.outbox[0]
        assert en_subj in msg.subject
        assert "Hi Hans," in msg.body
        assert en_body in msg.body
        assert "The OE5XRX team" in msg.body
        assert de_body not in msg.body

    def test_active_language_does_not_leak_into_mail_or_out_of_it(self, kind):
        ctx, de_subj, de_body, en_subj, en_body = CASES[kind]
        user = _user("en")
        with translation.override("de"):
            send_account_email(user, kind, dict(ctx))
            assert translation.get_language() == "de"
        msg = mail.outbox[0]
        assert en_subj in msg.subject
        assert en_body in msg.body
        assert de_body not in msg.body

    def test_link_carries_recipient_prefix(self, kind):
        ctx = CASES[kind][0]
        with translation.override("en"):
            send_account_email(_user("de"), kind, dict(ctx))
        assert "/de/" in mail.outbox[0].body
        assert "/en/" not in mail.outbox[0].body


@pytest.mark.django_db
class TestDetails:
    def test_welcome_default_actor_german(self):
        send_account_email(_user("de"), "welcome", {"raw_token": "TOK"})
        assert "Ein Admin hat gerade dein OE5XRX-Konto angelegt" in mail.outbox[0].body

    def test_welcome_default_actor_english(self):
        with translation.override("de"):
            send_account_email(_user("en"), "welcome", {"raw_token": "TOK"})
        assert "An admin just created your OE5XRX account" in mail.outbox[0].body

    @pytest.mark.parametrize("language", ["", "xx"])
    def test_unsupported_language_falls_back_to_default(self, language, settings):
        assert settings.LANGUAGE_CODE == "en"
        user = _user("en")
        User.objects.filter(pk=user.pk).update(language=language)
        user.refresh_from_db()
        with translation.override("de"):
            send_account_email(user, "reset", {"raw_token": "TOK"})
        msg = mail.outbox[0]
        assert "Password reset" in msg.subject
        assert "24 hours" in msg.body

    def test_verify_override_to_still_uses_users_language(self):
        send_account_email(_user("de"), "verify", {**VERIFY_CTX, "override_to": "neu@example.org"})
        msg = mail.outbox[0]
        assert msg.to == ["neu@example.org"]
        assert "Bestätige" in msg.subject
        assert "von m@example.org auf neu@example.org" in msg.body

    def test_name_falls_back_to_username(self):
        send_account_email(_user("de", first_name=""), "reset", {"raw_token": "TOK"})
        assert "Hallo OE5MEM1," in mail.outbox[0].body

    def test_subject_is_single_line(self):
        for kind, (ctx, *_rest) in CASES.items():
            send_account_email(_user("de", username=f"U{kind}"), kind, dict(ctx))
        for msg in mail.outbox:
            assert "\n" not in msg.subject


@pytest.mark.django_db
def test_plain_text_body_is_not_html_escaped():
    """Plain-text mail: a name like O'Brien must not arrive as O&#x27;Brien."""
    send_account_email(
        _user("en", first_name="Tom & O'Brien"),
        "welcome",
        {"raw_token": "TOK", "actor": "Ann <admin>"},
    )
    body = mail.outbox[0].body
    assert "Hi Tom & O'Brien," in body
    assert "Ann <admin> just created" in body
    assert "&#x27;" not in body and "&amp;" not in body
