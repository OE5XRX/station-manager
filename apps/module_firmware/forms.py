from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db.models import UniqueConstraint
from django.utils.translation import gettext_lazy as _

from .models import ModuleFirmwareRelease, ModuleFirmwareTarget, ModuleType
from .trust import is_trusted_repo, trusted_owners

repo_validator = RegexValidator(
    r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$",
    _("Use the GitHub form owner/repo, e.g. OE5XRX/FW-RemoteStation."),
)
prefix_validator = RegexValidator(
    r"^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$",
    _("Letters and digits, optionally joined by single '.', '_' or '-'; no trailing separator."),
)


class ModuleTypeForm(forms.ModelForm):
    class Meta:
        model = ModuleType
        fields = ("key", "display_name", "hw_repo", "firmware_repo", "release_asset_prefix")
        widgets = {
            "key": forms.TextInput(attrs={"class": "form-control"}),
            "display_name": forms.TextInput(attrs={"class": "form-control"}),
            "hw_repo": forms.TextInput(attrs={"class": "form-control"}),
            "firmware_repo": forms.TextInput(attrs={"class": "form-control"}),
            "release_asset_prefix": forms.TextInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("hw_repo", "firmware_repo"):
            self.fields[name].validators.append(repo_validator)
        self.fields["release_asset_prefix"].validators.append(prefix_validator)
        if self.instance and self.instance.pk:
            self.fields["key"].disabled = True

    def clean_firmware_repo(self):
        repo = self.cleaned_data.get("firmware_repo", "")
        if repo and not is_trusted_repo(repo):
            raise forms.ValidationError(
                _("Firmware repo owner must be one of: %(owners)s."),
                params={"owners": ", ".join(trusted_owners())},
            )
        return repo


def release_value(module_type_id, version):
    return f"{module_type_id}:{version}"


class ModuleFirmwareTargetForm(forms.ModelForm):
    """Desired firmware version per module type.

    ``release`` picks a (module_type, version) pair from the imported,
    non-archived releases — one combined field, so a type/version mismatch or a
    free-text typo is structurally impossible. The pair is written onto the
    instance before model validation, so ``Model.clean()`` and the conditional
    unique constraints (which involve ``module_type``) surface as form errors
    instead of an IntegrityError.
    """

    UNIQUE_MESSAGES = {
        "uniq_fleet_target_per_type": _("This module type already has a fleet target."),
        "uniq_tag_target_per_type_tag": _(
            "This module type already has a target for the selected tag."
        ),
        "uniq_station_target_per_type_station": _(
            "This module type already has a target for the selected station."
        ),
    }

    release = forms.ChoiceField(
        label=_("Firmware release"),
        help_text=_("Module type and version; variants that have this version are listed."),
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    class Meta:
        model = ModuleFirmwareTarget
        fields = ("scope", "tag", "station", "canary_tag")
        widgets = {
            "scope": forms.Select(attrs={"class": "form-select", "data-scope-select": ""}),
            "tag": forms.Select(attrs={"class": "form-select", "data-scope-field": "tag"}),
            "station": forms.Select(attrs={"class": "form-select", "data-scope-field": "station"}),
            "canary_tag": forms.Select(
                attrs={"class": "form-select", "data-scope-field": "canary_tag"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._release_pair = None
        current = None
        if self.instance.pk:
            current = (self.instance.module_type_id, self.instance.version)
            self.initial["release"] = release_value(*current)
        self._choice_pairs, self.fields["release"].choices = self._build_choices(current)

    @staticmethod
    def _build_choices(current):
        """Grouped choices ``[(type label, [(value, "1.2.0 — uhf, vhf"), …]), …]``."""
        rows = ModuleFirmwareRelease.objects.values_list(
            "module_type_id", "module_type__display_name", "version", "variant"
        ).order_by("module_type__display_name", "module_type_id", "-version", "variant")
        groups = {}  # type_id -> (label, {version: [variants]})
        for type_id, type_label, version, variant in rows:
            _label, versions = groups.setdefault(type_id, (type_label, {}))
            versions.setdefault(version, []).append(variant or "—")
        archived = None
        if current and (current[1] not in groups.get(current[0], (None, {}))[1]):
            mtype = ModuleType.objects.get(pk=current[0])
            archived = current
            groups.setdefault(mtype.pk, (mtype.display_name, {}))
        pairs = set()
        choices = [("", "---------")]
        for type_id, (type_label, versions) in groups.items():
            opts = []
            for version, variants in versions.items():
                pairs.add((type_id, version))
                opts.append(
                    (release_value(type_id, version), f"{version} — {', '.join(variants)}")
                )
            if archived and archived[0] == type_id:
                pairs.add(archived)
                opts.insert(
                    0, (release_value(*archived), f"{archived[1]} " + str(_("(archived)")))
                )
            choices.append((type_label, opts))
        return pairs, choices

    def clean_release(self):
        value = self.cleaned_data["release"]
        type_id, sep, version = value.partition(":")
        try:
            pair = (int(type_id), version)
        except ValueError:
            pair = None
        if not sep or pair not in self._choice_pairs:
            raise ValidationError(_("Select a valid firmware release."), code="invalid_choice")
        self._release_pair = pair
        return value

    def _post_clean(self):
        if self._release_pair is not None:
            self.instance.module_type_id, self.instance.version = self._release_pair
        super()._post_clean()

    def _get_validation_exclusions(self):
        exclude = super()._get_validation_exclusions()
        if self._release_pair is not None:
            # Not Meta fields, but set from ``release`` — they must take part in
            # the unique checks.
            exclude -= {"module_type", "version"}
        return exclude

    def validate_constraints(self):
        """Validate the conditional unique constraints with readable messages.

        The scope/ref CheckConstraints are mirrored by ``Model.clean()`` (run in
        ``full_clean``); validating them here too would only duplicate errors."""
        exclude = self._get_validation_exclusions()
        for constraint in self.instance._meta.constraints:
            if not isinstance(constraint, UniqueConstraint):
                continue
            try:
                constraint.validate(type(self.instance), self.instance, exclude=exclude)
            except ValidationError:
                self.add_error(
                    None, self.UNIQUE_MESSAGES.get(constraint.name, _("Duplicate target."))
                )
