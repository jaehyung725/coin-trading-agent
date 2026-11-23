import hashlib
import io
import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from streamlit.testing.v1 import AppTest

import agent
import supabase_accounts as auth
from auto_runner import process_snapshot
from dashboard import account_performance, trade_source
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
        market_patch = patch("urllib.request.urlopen", side_effect=self.upbit)
        market_patch.start()
        self.addCleanup(market_patch.stop)

    def upbit(self, url, **_):
        markets = (("KRW-BTC", "비트코인", 100_000_000), ("KRW-ETH", "이더리움", 4_000_000))
        if url.endswith("/market/all"):
            rows = [{"market": code, "korean_name": name} for code, name, _ in markets]
        elif "/ticker/all?" in url:
            rows = [{"market": code, "trade_price": price, "acc_trade_price_24h": 1e8, "signed_change_rate": 0.01}
                    for code, _, price in markets]
        else:
            self.fail(f"Unexpected Upbit request {url}")
        return io.BytesIO(json.dumps(rows).encode())

    def api(self, config, path, *, method="GET", data=None, token=None, private=False, **kwargs):
        self.calls.append((method, path))
        if path in ("/auth/v1/signup", "/auth/v1/recover", "/auth/v1/logout?scope=local"):
            return {}
        if path == "/auth/v1/token?grant_type=password":
            email = data["email"]
            if self.passwords.get(email) != data["password"]:
                raise auth.SupabaseError(400)
            return self.session(email)
        if path == "/auth/v1/token?grant_type=refresh_token":
            return self.session(data["refresh_token"])
        if path == "/auth/v1/verify":
            if data != {"email": USER_EMAIL, "type": "recovery", "token": "123456"}:
                raise auth.SupabaseError(403)
            return self.session(USER_EMAIL)
        if path == "/auth/v1/user":
            if method == "GET":
                return self.users[token].copy()
            self.passwords[token] = data["password"]
            return self.users[token].copy()
        if path.startswith("/auth/v1/admin/users?"):
            self.assertTrue(private)
            return {"users": list(self.users.values())}
        if path.startswith("/auth/v1/admin/users/"):
            self.assertTrue(private)
            if method == "GET":
                target_id = path.rsplit("/", 1)[1]
                return next(user for user in self.users.values() if user["id"] == target_id)
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

    def select_mode(self, app, mode):
        next(item for item in app.radio if item.label == "매매 방식").set_value(mode).run()
        self.assertFalse(app.exception)

    def auto_progress(self, app):
        directory = Path(app.session_state["account-directory"].name)
        with closing(sqlite3.connect(directory / "paper-auto-KRW-BTC.sqlite3")) as db:
            value, = db.execute("SELECT minute FROM rapid_progress WHERE id=1").fetchone()
        return datetime.fromisoformat(value)

    def auto_sweep(self, app, minute, price, side, *, expect_update=True):
        row = {"user_id": USER_ID, **self.rows[USER_ID]}
        with (patch("rapid_paper.rapid_market", return_value=(minute, price, side, side)) as market,
              patch("auto_runner.publish_account_snapshot") as publish):
            process_snapshot({}, row, now=minute, candle_loader=candles)
        if not expect_update:
            market.assert_not_called()
            publish.assert_not_called()
            return
        _, _, revision, files = publish.call_args.args
        self.rows[USER_ID] = {"revision": revision + 1, "snapshot": auth.pack(files)}
        app.run()
        self.assertFalse(app.exception)

    def test_reload_restores_login_and_refreshes_token_until_logout(self):
        app = self.app()
        self.login(app, USER_EMAIL, "initial-password")
        handle = app.session_state["browser-login"]
        app.session_state["login"]["expires"] = 0

        with patch.object(auth, "_browser_session", return_value=SimpleNamespace(handle=handle)):
            reloaded = self.app()
            self.assertEqual(reloaded.session_state["login"]["user_id"], USER_ID)
            self.assertIn(("POST", "/auth/v1/token?grant_type=refresh_token"), self.calls)
            self.click(reloaded, "로그아웃")
            after_logout = self.app()
            self.assertNotIn("login", after_logout.session_state)

    def test_chart_stays_visible_and_only_selected_mode_controls_are_shown(self):
        app = self.app()
        self.assertFalse(any(item.label == "매매 방식" for item in app.radio))
        self.login(app, USER_EMAIL, "initial-password")
        selector = next(item for item in app.radio if item.label == "매매 방식")
        self.assertEqual(selector.options, ["시세 보기", "수동매매", "자동매매"])
        self.assertEqual(selector.value, "시세 보기")
        guide = next(item.value for item in app.caption if "종목과 주문 조건" in item.value)
        self.assertIn("선택한 전략이 시세를 확인", guide)
        self.assertTrue(app.get("vega_lite_chart"))
        self.assertFalse(any(box.label == "자동매매 전략 선택" for box in app.selectbox))
        self.assertFalse(any(item.label == "모의매매 유형" for item in app.radio))

        self.select_mode(app, "자동매매")
        chart, controls = [
            column for column in app.get("column")
            if column.get("vega_lite_chart")
            or any(box.label == "자동매매 전략 선택" for box in column.selectbox)
        ]
        self.assertTrue(chart.get("vega_lite_chart"))
        performance = next(container for container in app.get("container")
                           if container.key == "auto-live-performance")
        self.assertEqual(performance.subheader[0].value, "자동매매 실시간 성과")
        workspace = next(container for container in app.get("container") if container.key == "market-workspace")
        self.assertIs(next(iter(workspace.children.values())), performance)
        self.assertTrue(performance.info)
        self.assertTrue(any(item.value == "자동매매 설정" for item in controls.subheader))
        self.assertGreater(chart.weight, controls.weight)
        next(box for box in app.selectbox if box.label == "자동매매 전략 선택").select("rsi14").run()
        self.assertFalse(any(item.label == "모의매매 유형" for item in app.radio))

        self.select_mode(app, "수동매매")
        self.assertTrue(app.get("vega_lite_chart"))
        self.assertTrue(any(item.label == "모의매매 유형" for item in app.radio))
        self.assertFalse(any(box.label == "자동매매 전략 선택" for box in app.selectbox))

        self.select_mode(app, "자동매매")
        self.assertEqual(next(box.value for box in app.selectbox if box.label == "자동매매 전략 선택"), "rsi14")

    def test_guest_can_view_market_and_sign_up_directly(self):
        app = self.app()
        self.assertTrue(app.get("vega_lite_chart"))
        self.assertNotIn("현물 매수", [button.label for button in app.button])
        self.click(app, "회원가입")
        self.enter(app, **{"이메일": USER_EMAIL, "비밀번호": "initial-password"})
        self.click(app, "회원가입")
        self.assertIn("회원가입이 완료되었습니다.", [item.value for item in app.success])
        self.assertTrue(any("이메일의 확인 링크" in item.value for item in app.info))
        self.assertEqual(self.calls, [("POST", "/auth/v1/signup")])

    def test_guest_login_and_return_to_market(self):
        app = self.app()
        self.click(app, "로그인")
        self.assertIn("이메일 또는 관리자 아이디", [field.label for field in app.text_input])
        self.click(app, "모의매매 화면으로")
        self.assertTrue(app.get("vega_lite_chart"))
        self.assertEqual(self.calls, [])

    def test_market_search_selects_symbol_without_login(self):
        def grid(*, data, **_):
            visible = [row["code"] for row in data["rows"]]
            selected = "KRW-ETH" if visible == ["KRW-ETH"] else data["selected"]
            return SimpleNamespace(selected=selected)

        with patch("market_grid.render_market_grid", side_effect=grid):
            app = self.app()
            next(field for field in app.sidebar.text_input if field.label == "코인 검색").set_value("없는 종목").run()
            self.assertIn("검색 결과가 없습니다.", [item.value for item in app.info])
            next(field for field in app.text_input if field.label == "코인 검색").set_value("이더리움").run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state["active-market"], "KRW-ETH")
            self.assertTrue(app.get("vega_lite_chart"))
        self.assertNotIn("login", app.session_state)

    def test_manual_tabs_keep_order_cancellation_and_cash_controls_working(self):
        app = self.app()
        self.login(app, USER_EMAIL, "initial-password")
        self.select_mode(app, "수동매매")
        self.click(app, "내 계정")
        self.assertIn("내 계정", [heading.value for heading in app.subheader])
        self.click(app, "시세·매매로 돌아가기")
        self.assertEqual(next(item.value for item in app.radio if item.label == "매매 방식"), "수동매매")
        self.assertTrue({"계좌 관리", "체결 내역", "수익률"}.issubset(tab.label for tab in app.tabs))

        next(item for item in app.radio if item.label == "주문 방식").set_value("지정가").run()
        next(field for field in app.number_input if field.label == "지정가 (KRW)").set_value(50).run()
        next(field for field in app.number_input if field.label == "BTC 수량").set_value(1.0).run()
        self.click(app, "모의 매수 지정가 등록")
        self.click(app, "취소")
        directory = Path(app.session_state["account-directory"].name)
        with closing(sqlite3.connect(directory / "paper.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT status FROM limit_orders").fetchone()[0], "CANCELED")

        with patch("amount_input.amount_input", return_value=800_000):
            app.run()
            self.click(app, "전체 가용 현금 변경")
        with closing(sqlite3.connect(directory / "paper.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT cash FROM account").fetchone()[0], 800_000)

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
        self.select_mode(app, "수동매매")
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

    def test_manual_and_auto_accounts_are_isolated(self):
        app = self.app()
        self.login(app, USER_EMAIL, "initial-password")
        self.select_mode(app, "자동매매")
        self.click(app, "자동 모의매매 시작")
        chart_snapshot = app.session_state["market-chart-snapshot"]
        self.auto_sweep(app, self.auto_progress(app) + timedelta(minutes=1), 123.45, "long")
        self.assertEqual(app.session_state["market-chart-snapshot"]["sampled_at"], chart_snapshot["sampled_at"])
        self.assertEqual(app.session_state["market-chart-snapshot"]["executions"], [])
        performance = next(container for container in app.get("container")
                           if container.key == "auto-live-performance")
        self.assertTrue(any(metric.label == "평가자산" for metric in performance.metric))
        self.assertTrue(any(button.label == "자동매매 보고서 작성" for button in performance.button))
        self.assertTrue(any("마지막 자동매매 판단" in item.value for item in performance.markdown))
        directory = Path(app.session_state["account-directory"].name)
        manual_path = directory / "paper.sqlite3"
        auto_path = directory / "paper-auto-KRW-BTC.sqlite3"
        with closing(sqlite3.connect(auto_path)) as db:
            auto_balance = db.execute("SELECT cash, units FROM account").fetchone()
            entry, source = db.execute("SELECT entry, source FROM leverage_positions").fetchone()
            self.assertEqual(source, "auto")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM returns_by_hour").fetchone()[0], 1)
        self.assertLess(auto_balance[0], 1_000_000)
        self.assertEqual(auto_balance[1], 0)
        app.session_state["market-chart-snapshot"] = {**chart_snapshot, "sampled_at": chart_snapshot["sampled_at"] - 15}
        app.run()
        self.assertFalse(app.exception)
        chart = next(container for container in app.get("container") if container.key == "market-chart-host")
        spec = json.loads(chart.get("vega_lite_chart")[0].proto.spec)
        price_layers = spec["vconcat"][0]["layer"]
        self.assertTrue(any(layer.get("encoding", {}).get("shape", {}).get("scale", {}).get("domain") == ["롱 진입"]
                            for layer in price_layers))
        self.assertTrue(any(layer.get("encoding", {}).get("text", {}).get("field") == "체결가"
                            for layer in price_layers))
        chart_column = next(column for column in app.get("column") if chart in column.get("container"))
        self.assertEqual([tab.label for tab in chart_column.tabs],
                         ["AI 자동매매 내역", "자동 수익률 추이"])
        self.select_mode(app, "수동매매")
        self.assertEqual(sum(metric.value == "1,000,000.00 KRW" for metric in app.metric
                             if metric.label == "가용 현금"), 1)

        unit = next(field for field in app.selectbox if field.label == "입력 단위")
        unit.select("percent").run()
        next(field for field in app.number_input if field.label == "가용 현금 비율 (%)").set_value(10).run()
        self.click(app, "모의 매수 체결")
        with closing(sqlite3.connect(manual_path)) as db:
            self.assertEqual(db.execute("SELECT source FROM fills").fetchall(), [("manual",)])

        agent.adjust_portfolio_cash(directory, manual_path, 700_000)
        with closing(sqlite3.connect(auto_path)) as db:
            self.assertEqual(db.execute("SELECT cash, units FROM account").fetchone(), auto_balance)
        # The snapshot loaded after logout keeps the two fill histories separate.
        self.click(app, "로그아웃")
        self.login(app, USER_EMAIL, "initial-password")
        restored = Path(app.session_state["account-directory"].name)
        with closing(sqlite3.connect(restored / manual_path.name)) as db:
            self.assertEqual(db.execute("SELECT source FROM fills").fetchall(), [("manual",)])
        with closing(sqlite3.connect(restored / auto_path.name)) as db:
            self.assertEqual(db.execute("SELECT entry, source FROM leverage_positions").fetchall(), [(entry, "auto")])

    def test_funding_is_excluded_and_legacy_fills_stay_unclassified(self):
        self.assertEqual(account_performance(1_250_000, 1_000_000, 200_000), (50_000, 5.0))
        self.assertEqual(account_performance(750_000, 1_000_000, -200_000), (-50_000, -5.0))
        self.assertEqual(trade_source({"side": "BUY"}), "미분류")
        self.assertEqual(trade_source({"source": None}), "미분류")

    def test_auto_strategy_and_backtest_are_separate_from_chart_overlays(self):
        app = self.app()
        self.login(app, USER_EMAIL, "initial-password")
        self.select_mode(app, "자동매매")

        def history(args, before, *, count=None, seconds=False):
            if count == 200 and args.minutes == 60:
                return [
                    agent.Candle(before - timedelta(hours=220 - index), price, price)
                    for index, price in enumerate([100] * 195 + [90, 100, 110, 120, 80])
                ]
            return candles(args, before, count=count, seconds=seconds)

        next(box for box in app.selectbox if box.label == "자동매매 전략 선택").select("sma20").run()
        self.assertEqual(next(box for box in app.multiselect if box.label == "차트 지표").value, [])
        with patch.object(agent, "recent_candles", side_effect=history):
            self.click(app, "과거 시세로 검증하기")
            self.assertTrue(any(metric.label == "최종 평가액" for metric in app.metric))
            backtest = next(item for item in app.expander if item.label == "과거 차트로 수익률 계산해 보기")
            self.assertTrue(any("과거에 거래했다면 수익률" in item.value for item in backtest.caption))
            self.assertTrue(any("비용 반영 후 최종 평가액" in item.value and "줄었습니다" in item.value
                                for item in backtest.markdown))
            self.assertTrue(any("마지막 봉의 종가" in item.value for item in backtest.markdown))
            self.click(app, "자동 모의매매 시작")
        directory = Path(app.session_state["account-directory"].name)
        self.assertTrue((directory / "paper-auto-KRW-BTC-sma20-v1.sqlite3").is_file())
        next(box for box in app.selectbox if box.label == "자동매매 전략 선택").select("bollinger").run()
        self.assertTrue(any(button.label == "자동 모의매매 시작" for button in app.button))
        next(box for box in app.selectbox if box.label == "자동매매 전략 선택").select("sma20").run()
        self.assertTrue(any(button.label == "자동 모의매매 종료" for button in app.button))

    def test_short_order_screen_places_and_closes_position(self):
        app = self.app()
        self.login(app, USER_EMAIL, "initial-password")
        self.select_mode(app, "수동매매")

        def market_data(args, before, *, count=None, seconds=False):
            if seconds:
                return [agent.Candle(before - timedelta(seconds=1), 100, 100)]
            return candles(args, before, count=count, seconds=seconds)

        with patch("amount_input.amount_input", return_value=20_000), patch.object(
                agent, "recent_candles", side_effect=market_data):
            next(item for item in app.radio if item.label == "모의매매 유형").set_value("레버리지 숏").run()
            self.click(app, "모의 숏 진입")
            self.assertTrue(any(metric.label == "모의 청산가" for metric in app.metric))
            self.click(app, "모의 숏 종료")
        directory = Path(app.session_state["account-directory"].name)
        with closing(sqlite3.connect(directory / "paper.sqlite3")) as db:
            side, status, pnl = db.execute("SELECT side, status, pnl FROM leverage_positions").fetchone()
        self.assertEqual((side, status), ("short", "숏 종료"))
        self.assertLess(pnl, 0)

    def test_schedule_start_and_stop_persist_across_login(self):
        app = self.app()
        self.login(app, USER_EMAIL, "initial-password")
        self.select_mode(app, "자동매매")
        slider = next(item for item in app.select_slider if item.label == "부자 되고 싶은 마음")
        self.assertEqual(slider.value, "Light")
        self.assertEqual(slider.options, ["Light · 2배", "Medium · 5배", "High · 10배", "Ultra · 20배"])
        self.assertFalse(any("한 번만 모의매매" == item.label for item in app.expander))
        slider.set_value("High").run()
        allocation = next(item for item in app.select_slider if item.label == "가용 현금 사용 비율")
        self.assertEqual(allocation.value, 20)
        self.assertEqual(allocation.options, ["20%", "40%", "60%", "80%", "100%"])
        allocation.set_value(60).run()
        self.click(app, "자동 모의매매 시작")
        self.auto_sweep(app, self.auto_progress(app) + timedelta(minutes=1), 100, "short")
        directory = Path(app.session_state["account-directory"].name)
        with closing(sqlite3.connect(directory / "paper-auto-KRW-BTC.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT side, source, leverage FROM leverage_positions").fetchall(), [("short", "auto", 10)])
            self.assertAlmostEqual(db.execute("SELECT cash FROM account").fetchone()[0], 400_000)
        self.click(app, "로그아웃")
        self.login(app, USER_EMAIL, "initial-password")
        self.select_mode(app, "자동매매")
        slider = next(item for item in app.select_slider if item.label == "부자 되고 싶은 마음")
        self.assertEqual(slider.value, "High")
        self.assertTrue(slider.proto.disabled)
        self.assertEqual(next(item.value for item in app.select_slider if item.label == "가용 현금 사용 비율"), 60)
        self.click(app, "자동 모의매매 종료")
        self.assertFalse(next(item for item in app.select_slider if item.label == "부자 되고 싶은 마음").proto.disabled)
        self.click(app, "로그아웃")
        self.login(app, USER_EMAIL, "initial-password")
        self.select_mode(app, "자동매매")
        self.assertTrue(any(button.label == "자동 모의매매 시작" for button in app.button))
        self.assertFalse(any(button.label == "자동 모의매매 종료" for button in app.button))

    def test_restart_records_status_and_worker_fill_appears_in_combined_history(self):
        app = self.app()
        self.login(app, USER_EMAIL, "initial-password")
        self.select_mode(app, "자동매매")
        with patch("rapid_paper.rapid_market") as market:
            self.click(app, "자동 모의매매 시작")
            market.assert_not_called()

        history = next(tab for tab in app.tabs if tab.label == "AI 자동매매 내역")
        self.assertEqual(history.dataframe[0].value["판단"].tolist(), ["활성화"])
        self.assertFalse(history.expander)
        activated = self.auto_progress(app)
        self.auto_sweep(app, activated, 100, "long", expect_update=False)
        self.auto_sweep(app, activated + timedelta(minutes=1), 100, "long")

        self.click(app, "자동 모의매매 종료")
        self.click(app, "자동 모의매매 시작")
        history = next(tab for tab in app.tabs if tab.label == "AI 자동매매 내역")
        self.assertEqual(history.dataframe[0].value["판단"].tolist(), ["활성화", "종료", "롱 진입", "활성화"])
        resumed = self.auto_progress(app)
        self.auto_sweep(app, resumed, 103, "long", expect_update=False)
        self.auto_sweep(app, resumed + timedelta(minutes=1), 103, "long")

        history = next(tab for tab in app.tabs if tab.label == "AI 자동매매 내역")
        self.assertEqual(history.dataframe[0].value["판단"].tolist(), ["롱 종료", "활성화", "종료", "롱 진입", "활성화"])
        details = next(expander for expander in history.expander if expander.label == "체결 상세")
        self.assertEqual(details.dataframe[0].value["구분"].tolist(), ["롱 종료", "롱 진입"])

    def test_llm_strategy_uses_server_configuration_and_activates_without_request(self):
        with patch("llm_strategy.openai_settings", return_value=("", "gpt-4.1-mini")) as config:
            app = self.app()
            self.login(app, USER_EMAIL, "initial-password")
            self.select_mode(app, "자동매매")
            strategy = next(box for box in app.selectbox if box.label == "자동매매 전략 선택")
            self.assertIn("완전 자동 매매 (AI 에이전트)", strategy.options)
            strategy.select("llm").run()
            self.assertFalse(app.select_slider)
            self.assertTrue(next(button for button in app.button if button.label == "자동 모의매매 시작").proto.disabled)
            config.return_value = ("fake-test-key", "gpt-4.1-mini")
            with patch("llm_strategy.urlopen") as api:
                app.run()
                self.click(app, "자동 모의매매 시작")
                api.assert_not_called()
            self.assertNotIn("과거 시세로 검증하기", [button.label for button in app.button])
            history = next(tab for tab in app.tabs if tab.label == "AI 자동매매 내역")
            self.assertEqual(history.dataframe[0].value["판단"].tolist(), ["활성화"])

            with (patch("auto_report.openai_settings", return_value=("fake-test-key", "test-model")),
                  patch("auto_report.response_text", return_value="아직 종료된 거래가 없습니다.") as api):
                self.click(app, "AI 자동매매 보고서 작성")
                app.run()
                api.assert_called_once()
                payload = api.call_args.args[1]
                report_input = json.loads(payload["input"])
                self.assertEqual(report_input["decision_counts"], {"활성화": 1})
                self.assertFalse(payload["store"])
            report = next(item for item in app.expander if item.label == "AI 자동매매 보고서")
            self.assertTrue(any("평가자산" in table.value["지표"].tolist()
                                for table in report.dataframe if "지표" in table.value))
            self.assertTrue(any(item.value == "아직 종료된 거래가 없습니다." for item in report.text))
            self.assertEqual(report.get("download_button")[0].proto.label, "보고서 다운로드 (.md)")
            with patch("auto_report.openai_settings", return_value=("", "test-model")):
                self.click(app, "AI 자동매매 보고서 작성")
            report = next(item for item in app.expander if item.label == "AI 자동매매 보고서")
            self.assertTrue(report.dataframe)
            self.assertTrue(any("OpenAI API 키" in item.value for item in report.info))

    def test_admin_can_delete_general_member_with_exact_confirmation(self):
        ordinary_app = self.app()
        self.login(ordinary_app, USER_EMAIL, "initial-password")
        self.assertNotIn("회원 관리", [button.label for button in ordinary_app.button])
        self.assertIn(USER_ID, self.rows)
        self.users[auth.ADMIN_EMAIL]["app_metadata"]["must_change_password"] = False
        app = self.app()
        self.login(app, "admin", "admin123")
        self.click(app, "회원 관리")
        selector = next(box for box in app.selectbox if box.label == "삭제할 일반 회원")
        self.assertEqual(selector.options, [USER_EMAIL])
        selector.select(USER_ID).run()
        self.enter(app, **{"삭제할 회원 이메일 확인": "other@example.org"})
        self.click(app, "일반 회원 삭제")
        self.assertIn(USER_EMAIL, self.users)
        self.enter(app, **{"삭제할 회원 이메일 확인": USER_EMAIL})
        self.click(app, "일반 회원 삭제")
        self.assertNotIn(USER_EMAIL, self.users)
        self.assertNotIn(USER_ID, self.rows)
        self.assertIn(auth.ADMIN_EMAIL, self.users)
        self.assertTrue(any("회원을 삭제했습니다" in item.value for item in app.success))

    def test_delete_member_checks_current_admin_role_and_protects_admin_target(self):
        login = {
            "role": "admin", "must_change_password": False, "user_id": ADMIN_ID,
            "access_token": auth.ADMIN_EMAIL,
        }
        with self.assertRaises(auth.SupabaseError):
            auth.delete_member({}, login, USER_ID, USER_EMAIL)
        self.users[auth.ADMIN_EMAIL]["app_metadata"]["must_change_password"] = False
        with self.assertRaises(auth.SupabaseError):
            auth.delete_member({}, login, ADMIN_ID, auth.ADMIN_EMAIL)
        self.users[auth.ADMIN_EMAIL]["app_metadata"].pop("coin_role")
        with self.assertRaises(auth.SupabaseError):
            auth.delete_member({}, login, USER_ID, USER_EMAIL)
        self.assertFalse(any(method == "DELETE" for method, _ in self.calls))

    def test_signed_colors(self):
        self.assertEqual(direction_color(0.01), POSITIVE)
        self.assertEqual(direction_color(-0.01), NEGATIVE)
        self.assertIsNone(direction_color(0))
