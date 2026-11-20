import hashlib
import sqlite3
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from streamlit.testing.v1 import AppTest

import agent
import supabase_accounts as auth
from price_colors import NEGATIVE, POSITIVE, direction_color

ROOT = Path(__file__).resolve().parents[1]
USER_ID = str(UUID(int=1))
ADMIN_ID = str(UUID(int=2))
USER_EMAIL = "user@example.org"
SETUP_CODE = "local-test-setup-code"


def candles(args, before, *, count=None, seconds=False):
    interval = timedelta(seconds=1) if seconds else timedelta(minutes=args.minutes)
    return [
        agent.Candle(before - interval * (21 - offset), 100 + offset, 101 + offset)
        for offset in range(21)
    ]


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.rows = {}
        self.users = {
            USER_EMAIL: {"id": USER_ID, "email": USER_EMAIL, "app_metadata": {}},
            auth.ADMIN_EMAIL: {"id": ADMIN_ID, "email": auth.ADMIN_EMAIL, "app_metadata": {
                "coin_role": "admin", "must_change_password": True,
                "setup_code_hash": hashlib.sha256(SETUP_CODE.encode()).hexdigest(),
            }},
        }
        self.passwords = {USER_EMAIL: "initial-password", auth.ADMIN_EMAIL: "admin123"}
        self.start_patch = patch.object(auth, "request", self.api)
        self.candles_patch = patch.object(agent, "recent_candles", candles)
        self.start_patch.start()
        self.candles_patch.start()
        self.addCleanup(self.start_patch.stop)
        self.addCleanup(self.candles_patch.stop)

    def api(self, config, path, *, method="GET", data=None, token=None, private=False, **kwargs):
        self.calls.append((method, path))
        if path in ("/auth/v1/signup", "/auth/v1/recover", "/auth/v1/logout?scope=local"):
            return {}
        if path == "/auth/v1/token?grant_type=password":
            email = data["email"]
            if self.passwords.get(email) != data["password"]:
                raise auth.SupabaseError(400)
            return self.session(email)
        if path == "/auth/v1/verify":
            if data != {"email": USER_EMAIL, "type": "recovery", "token": "123456"}:
                raise auth.SupabaseError(403)
            return self.session(USER_EMAIL)
        if path == "/auth/v1/user":
            self.passwords[token] = data["password"]
            return self.users[token].copy()
        if path.startswith("/auth/v1/admin/users/"):
            self.assertTrue(private)
            if method == "DELETE":
                self.users.pop(USER_EMAIL)
                self.passwords.pop(USER_EMAIL)
                self.rows.pop(USER_ID, None)
                return {}
            self.users[auth.ADMIN_EMAIL]["app_metadata"] = data["app_metadata"]
            self.passwords[auth.ADMIN_EMAIL] = data["password"]
            return self.users[auth.ADMIN_EMAIL].copy()
        if path.startswith("/rest/v1/paper_account_snapshots?"):
            if method == "POST":
                self.rows[data["user_id"]] = {"revision": 1, "snapshot": data["snapshot"]}
                return [{"revision": 1}]
            user_id = path.split("user_id=eq.", 1)[1].split("&", 1)[0]
            row = self.rows.get(user_id)
            if method == "PATCH":
                if row is None or f"revision=eq.{row['revision']}" not in path:
                    return []
                self.rows[user_id] = data
                return [{"revision": data["revision"]}]
            if row and f"revision=neq.{row['revision']}" not in path:
                return [row]
            return []
        self.fail(f"Unexpected request {method} {path}")

    def session(self, email):
        return dict(user=self.users[email].copy(), access_token=email, refresh_token=email, expires_in=3600)

    def app(self):
        app = AppTest.from_file(str(ROOT / "dashboard.py"), default_timeout=25)
        app.secrets["supabase"] = {
            "url": "https://unit-test.example.org", "publishable_key": "sb_publishable_test",
            "secret_key": "sb_secret_test",
        }
        app.run()
        self.addCleanup(lambda: app.session_state["account-directory"].cleanup()
                        if "account-directory" in app.session_state else None)
        self.assertFalse(app.exception)
        return app

    def click(self, app, label):
        next(button for button in app.button if button.label == label).click().run()
        self.assertFalse(app.exception)

    def enter(self, app, **values):
        for label, value in values.items():
            next(field for field in app.text_input if field.label == label).set_value(value)

    def login(self, app, identifier, password):
        self.click(app, "로그인 / 회원가입")
        self.enter(app, **{"이메일 또는 관리자 아이디": identifier, "비밀번호": password})
        self.click(app, "로그인")

    def test_guest_actions_and_signup(self):
        for action in ("현물 매수", "현물 매도", "레버리지 롱 진입", "가용 현금 및 계좌 관리"):
            with self.subTest(action=action):
                app = self.app()
                self.assertTrue(app.get("vega_lite_chart"))
                self.click(app, action)
                self.assertIn("모의 매매를 시작하시려면 로그인이 필요합니다", [node.value for node in app.markdown])
                self.click(app, "회원가입")
                self.assertIn("이메일", [field.label for field in app.text_input])
        self.assertEqual(self.calls, [])
        self.enter(app, **{"이메일": USER_EMAIL, "비밀번호": "initial-password"})
        self.click(app, "회원가입")
        self.assertTrue(any("회원가입 요청이 완료" in item.value for item in app.success))
        self.assertEqual(self.calls, [("POST", "/auth/v1/signup")])

    def test_recovery_login_and_delete(self):
        app = self.app()
        self.click(app, "로그인 / 회원가입")
        self.click(app, "비밀번호 찾기")
        self.enter(app, **{"가입한 이메일": USER_EMAIL})
        self.click(app, "재설정 코드 받기")
        self.enter(app, **{"메일로 받은 재설정 코드": "123456", "새 비밀번호": "replacement-password",
                           "새 비밀번호 확인": "replacement-password"})
        self.click(app, "비밀번호 재설정")
        self.assertTrue(any("비밀번호가 변경" in item.value for item in app.success))
        self.enter(app, **{"이메일 또는 관리자 아이디": USER_EMAIL, "비밀번호": "replacement-password"})
        self.click(app, "로그인")
        self.assertIn(USER_ID, self.rows)
        self.click(app, "내 계정")
        self.enter(app, **{"본인 확인용 비밀번호": "replacement-password"})
        app.checkbox[0].check()
        self.click(app, "회원탈퇴")
        self.assertNotIn(USER_ID, self.rows)
        self.assertTrue(any("회원탈퇴가 완료" in item.value for item in app.success))

    def test_admin_must_change_initial_password(self):
        app = self.app()
        self.login(app, "admin", "admin123")
        self.assertTrue(any("초기 비밀번호" in item.value for item in app.warning))
        self.assertNotIn(ADMIN_ID, self.rows)
        self.enter(app, **{"관리자 초기 설정 코드": "incorrect", "현재 비밀번호": "admin123",
                           "새 비밀번호": "replacement-admin", "새 비밀번호 확인": "replacement-admin"})
        self.click(app, "비밀번호 변경")
        self.assertTrue(any("초기 설정 코드" in item.value for item in app.error))
        self.enter(app, **{"관리자 초기 설정 코드": SETUP_CODE, "현재 비밀번호": "admin123",
                           "새 비밀번호": "replacement-admin", "새 비밀번호 확인": "replacement-admin"})
        self.click(app, "비밀번호 변경")
        self.enter(app, **{"이메일 또는 관리자 아이디": "admin", "비밀번호": "replacement-admin"})
        self.click(app, "로그인")
        self.assertIn(ADMIN_ID, self.rows)
        self.assertFalse(self.users[auth.ADMIN_EMAIL]["app_metadata"]["must_change_password"])

    def test_market_order_survives_logout(self):
        app = self.app()
        self.login(app, USER_EMAIL, "initial-password")
        unit = next(field for field in app.selectbox if field.label == "입력 단위")
        unit.select("percent").run()
        size = next(field for field in app.number_input if field.label == "가용 현금 비율 (%)")
        size.set_value(10).run()
        self.click(app, "모의 매수 체결")
        self.click(app, "로그아웃")
        self.login(app, USER_EMAIL, "initial-password")
        directory = Path(app.session_state["account-directory"].name)
        connection = sqlite3.connect(directory / "paper.sqlite3")
        self.addCleanup(connection.close)
        cash, units = connection.execute("select cash, units from account").fetchone()
        self.assertLess(cash, 1_000_000)
        self.assertGreater(units, 0)
        self.assertEqual(connection.execute("select count(*) from fills").fetchone()[0], 1)

    def test_signed_colors(self):
        self.assertEqual(direction_color(0.01), POSITIVE)
        self.assertEqual(direction_color(-0.01), NEGATIVE)
        self.assertIsNone(direction_color(0))
