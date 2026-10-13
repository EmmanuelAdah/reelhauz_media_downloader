from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm

from .models import UserPreference
from .presets import PRESET_CHOICES
from .services import ytdlp_service

User = get_user_model()


class _Styled:
    """Mixin that adds Bootstrap classes to every widget."""

    def _style(self) -> None:
        for field in self.fields.values():
            widget = field.widget
            css = "form-select" if isinstance(widget, forms.Select) else "form-control"
            widget.attrs["class"] = f"{widget.attrs.get('class', '')} {css}".strip()


class LoginForm(_Styled, AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()
        self.fields["username"].widget.attrs.update({"autofocus": True, "placeholder": "Username"})
        self.fields["password"].widget.attrs.update({"placeholder": "Password"})


class RegisterForm(_Styled, UserCreationForm):
    email = forms.EmailField(required=False)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class VideoURLForm(forms.Form):
    """Validates a YouTube URL and exposes the canonical URL + video id."""

    url = forms.CharField(max_length=2048)

    def clean_url(self) -> str:
        result = ytdlp_service.validate_url(self.cleaned_data["url"])
        if not result.valid:
            raise forms.ValidationError(result.error)
        self.video_id = result.video_id
        self.normalized_url = result.normalized_url
        return result.normalized_url


class PreferenceForm(_Styled, forms.ModelForm):
    default_preset = forms.ChoiceField(choices=PRESET_CHOICES, label="Default format")

    class Meta:
        model = UserPreference
        fields = ("default_preset",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()
