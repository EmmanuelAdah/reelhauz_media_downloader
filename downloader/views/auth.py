from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect, render

from downloader.forms import LoginForm, RegisterForm
from downloader.models import UserPreference


class StyledLoginView(LoginView):
    template_name = "downloader/auth/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True


def register(request):
    if request.user.is_authenticated:
        return redirect("home")
    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        UserPreference.objects.get_or_create(user=user)
        login(request, user)
        messages.success(request, "Account created successfully.")
        return redirect("home")
    return render(request, "downloader/auth/register.html", {"form": form})
