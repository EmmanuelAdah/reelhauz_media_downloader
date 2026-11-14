from django.contrib.auth import get_user_model
from django.urls import reverse

from .base import AppTestCase

User = get_user_model()


class AuthTests(AppTestCase):
    def test_registration_hides_default_username_requirements(self):
        response = self.client.get(reverse("register"))
        self.assertNotContains(response, "150 characters or fewer")
        self.assertNotContains(response, "Letters, digits and @/./+/-/_ only")

    def test_register_creates_user_and_logs_in(self):
        res = self.client.post(
            reverse("register"),
            {"username": "carol", "email": "c@example.com", "password1": "Xk9-long-pass-77", "password2": "Xk9-long-pass-77"},
        )
        self.assertRedirects(res, reverse("home"))
        self.assertTrue(User.objects.filter(username="carol").exists())
        self.assertContains(self.client.get(reverse("home")), "carol")

    def test_register_rejects_weak_password(self):
        res = self.client.post(reverse("register"), {"username": "dave", "password1": "12345678", "password2": "12345678"})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(User.objects.filter(username="dave").exists())

    def test_login_and_logout(self):
        res = self.client.post(reverse("login"), {"username": "alice", "password": self.password})
        self.assertRedirects(res, reverse("home"))
        self.assertRedirects(self.client.post(reverse("logout")), reverse("home"))
        self.assertNotContains(self.client.get(reverse("home")), "alice")

    def test_bad_login(self):
        res = self.client.post(reverse("login"), {"username": "alice", "password": "wrong"})
        self.assertContains(res, "Invalid username or password")

    def test_logout_requires_post(self):
        self.login()
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)

    def test_public_pages_need_no_login(self):
        for name in ("home", "watch", "continue_watching", "login", "register"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_download_page_prompt_depends_on_authentication(self):
        response = self.client.get(reverse("home"))
        self.assertContains(
            response,
            "Paste a link, pick a quality, and save to your device in seconds. "
            "Start right away without signing up, or create an account for seamless cross-device sync.",
        )

        self.login()
        response = self.client.get(reverse("home"))
        self.assertContains(response, "Paste a link, pick a quality, and save to your device in seconds.")
        self.assertNotContains(response, "Start right away without signing up")

    def test_login_is_limited_to_four_attempts_per_minute_per_ip(self):
        login_url = reverse("login")
        for _ in range(4):
            response = self.client.post(login_url, {"username": "alice", "password": "wrong"})
            self.assertEqual(response.status_code, 200)

        response = self.client.post(login_url, {"username": "alice", "password": self.password})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "60")
        self.assertContains(
            response,
            '<div class="alert alert-warning" role="alert" aria-live="assertive">Too many attempts. Trying again later!</div>',
            status_code=429,
            html=True,
        )
        self.assertNotContains(response, "Please enter a correct username and password", status_code=429)
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_registration_is_limited_to_four_attempts_per_minute_per_ip(self):
        register_url = reverse("register")
        for _ in range(4):
            response = self.client.post(register_url, {"username": "carol", "password1": "weak", "password2": "weak"})
            self.assertEqual(response.status_code, 200)

        response = self.client.post(
            register_url,
            {"username": "carol", "password1": "Xk9-long-pass-77", "password2": "Xk9-long-pass-77"},
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "60")
        self.assertContains(
            response,
            '<div class="alert alert-warning" role="alert" aria-live="assertive">Too many attempts. Trying again later!</div>',
            status_code=429,
            html=True,
        )
        self.assertFalse(User.objects.filter(username="carol").exists())

    def test_only_settings_requires_login(self):
        res = self.client.get(reverse("settings"))
        self.assertEqual(res.status_code, 302)
        self.assertIn("/accounts/login/", res["Location"])

    def test_layout_differs_for_visitors_and_members(self):
        anon = self.client.get(reverse("home"))
        self.assertContains(anon, "Create account")
        self.assertContains(anon, "Pick up right where you left off.")
        self.assertContains(anon, "Your experience is our priority.")
        self.assertContains(anon, "Don’t have an account yet?")
        self.assertContains(anon, "Reelhauz. All rights reserved.")
        self.assertNotContains(anon, 'id="sidebar"')
        self.login()
        member = self.client.get(reverse("home"))
        self.assertContains(member, "Reelhauz. All rights reserved.")
        self.assertNotContains(member, "Pick up right where you left off.")
        self.assertContains(member, 'id="sidebar"')
        self.assertContains(member, 'id="tray"')  # live downloads tray on every page
        self.assertContains(self.client.get(reverse("watch")), 'id="tray"')

    def test_auth_pages_include_the_bottom_copyright_footer(self):
        for name in ("login", "register"):
            response = self.client.get(reverse(name))
            self.assertContains(response, 'class="public-foot auth-foot"')
            self.assertContains(response, "Reelhauz. All rights reserved.")

    def test_brand_name_is_reelhauz_everywhere(self):
        self.login()
        for name in ("home", "watch", "continue_watching", "settings"):
            page = self.client.get(reverse(name)).content.decode()
            self.assertIn("Reelhauz", page, name)
            self.assertNotIn("Reelhouse", page, name)
        self.client.logout()
        for name in ("login", "register", "home"):
            page = self.client.get(reverse(name)).content.decode()
            self.assertIn("Reelhauz", page, name)
            self.assertNotIn("Reelhouse", page, name)

    def test_settings_default_preset_roundtrip(self):
        self.login()
        res = self.client.post(reverse("settings"), {"default_preset": "audio_mp3"})
        self.assertRedirects(res, reverse("settings"))
        self.assertEqual(self.client.get(reverse("home")).context["default_preset"], "audio_mp3")

    def test_password_change_form_is_in_a_modal(self):
        self.login()
        response = self.client.get(reverse("settings"))
        self.assertContains(response, 'data-bs-target="#password-modal"')
        self.assertContains(response, 'id="password-modal"')
        self.assertContains(response, 'name="old_password"')
        self.assertContains(response, 'name="new_password1"')
        self.assertContains(response, 'name="new_password2"')

    def test_change_password_keeps_user_logged_in(self):
        self.login()
        response = self.client.post(
            reverse("settings"),
            {
                "action": "change_password",
                "old_password": self.password,
                "new_password1": "A-different-long-pass-57",
                "new_password2": "A-different-long-pass-57",
            },
        )
        self.assertRedirects(response, reverse("settings"))
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.check_password("A-different-long-pass-57"))
        self.assertTrue(self.client.get(reverse("settings")).wsgi_request.user.is_authenticated)

    def test_change_password_rejects_wrong_current_password(self):
        self.login()
        response = self.client.post(
            reverse("settings"),
            {
                "action": "change_password",
                "old_password": "not-the-current-password",
                "new_password1": "A-different-long-pass-57",
                "new_password2": "A-different-long-pass-57",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your old password was entered incorrectly")
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.check_password(self.password))

    def test_change_password_rejects_mismatched_new_passwords(self):
        self.login()
        response = self.client.post(
            reverse("settings"),
            {
                "action": "change_password",
                "old_password": self.password,
                "new_password1": "A-different-long-pass-57",
                "new_password2": "A-different-long-pass-58",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The two password fields didn’t match")
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.check_password(self.password))
