from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect, render

from downloader.forms import LoginForm, RegisterForm
from downloader.models import UserPreference
from downloader.throttle import allow, client_ip

AUTH_ATTEMPT_LIMIT = 4
AUTH_ATTEMPT_WINDOW = 60
AUTH_RATE_LIMIT_MESSAGE = "Too many attempts. Trying again later!"


class StyledLoginView(LoginView):
    template_name = "downloader/auth/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        if not allow("login", client_ip(request), AUTH_ATTEMPT_LIMIT, AUTH_ATTEMPT_WINDOW):
            form = self.get_form()
            context = self.get_context_data(form=form)
            context["rate_limit_message"] = AUTH_RATE_LIMIT_MESSAGE
            response = self.render_to_response(context)
            response.status_code = 429
            response["Retry-After"] = str(AUTH_ATTEMPT_WINDOW)
            return response
        return super().post(request, *args, **kwargs)


def register(request):
    if request.user.is_authenticated:
        return redirect("home")
    form = RegisterForm(request.POST or None)
    if request.method == "POST":
        if not allow("register", client_ip(request), AUTH_ATTEMPT_LIMIT, AUTH_ATTEMPT_WINDOW):
            response = render(
                request,
                "downloader/auth/register.html",
                {"form": form, "rate_limit_message": AUTH_RATE_LIMIT_MESSAGE},
                status=429,
            )
            response["Retry-After"] = str(AUTH_ATTEMPT_WINDOW)
            return response
        if form.is_valid():
            user = form.save()
            UserPreference.objects.get_or_create(user=user)
            login(request, user)
            messages.success(request, "Account created successfully.")
            return redirect("home")
    return render(request, "downloader/auth/register.html", {"form": form})
