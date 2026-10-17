from django.contrib.auth import get_user_model
from django.urls import reverse

from .base import AppTestCase

User = get_user_model()


class AuthTests(AppTestCase):
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
        self.assertContains(res, "Please enter a correct username and password")

    def test_logout_requires_post(self):
        self.login()
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)

    def test_public_pages_need_no_login(self):
        for name in ("home", "watch", "continue_watching", "login", "register"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_only_settings_requires_login(self):
        res = self.client.get(reverse("settings"))
        self.assertEqual(res.status_code, 302)
        self.assertIn("/accounts/login/", res["Location"])

    def test_layout_differs_for_visitors_and_members(self):
        anon = self.client.get(reverse("home"))
        self.assertContains(anon, "Create account")
        self.assertNotContains(anon, 'id="sidebar"')
        self.login()
        member = self.client.get(reverse("home"))
        self.assertContains(member, 'id="sidebar"')
        self.assertContains(member, 'id="tray"')  # live downloads tray on every page
        self.assertContains(self.client.get(reverse("watch")), 'id="tray"')

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
