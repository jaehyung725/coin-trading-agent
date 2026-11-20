"""Supabase email authentication and durable storage for the trading databases."""

import base64
import hashlib
import hmac
import io
import json
import re
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError
from streamlit.runtime.scriptrunner import RerunException


ADMIN_EMAIL = "admin@coin-trading-agent.invalid"


class SupabaseError(Exception):
    def __init__(self, status=None, code=None):
        self.status = status
        self.code = code


def request(config, path, *, method="GET", data=None, token=None, private=False, prefer=None):
    headers = {"apikey": config["secret_key" if private else "publishable_key"]}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if prefer:
        headers["Prefer"] = prefer
    if data is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
    request = Request(config["url"].rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            body = response.read()
            return json.loads(body) if body else None
    except HTTPError as error:
        code = None
        with error:
            if not private:
                try:
                    payload = json.loads(error.read(16_384))
                    if isinstance(payload, dict):
                        code = payload.get("error_code")
                except (OSError, ValueError):
                    pass
        raise SupabaseError(error.code, code) from None
    except (URLError, OSError, ValueError):
        raise SupabaseError() from None


def clear_session():
    directory = st.session_state.pop("account-directory", None)
    if directory is not None:
        directory.cleanup()
    st.session_state.clear()


def save_login(payload):
    metadata = payload["user"].get("app_metadata") or {}
    admin = metadata.get("coin_role") == "admin"
    st.session_state["login"] = {
        "role": "admin" if admin else "user",
        "must_change_password": admin and metadata.get("must_change_password") is True,
        "setup_code_hash": metadata.get("setup_code_hash") if admin else None,
        "user_id": str(UUID(payload["user"]["id"])),
        "email": payload["user"]["email"],
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "expires": time.monotonic() + int(payload["expires_in"]),
    }


def current_login(config):
    login = st.session_state.get("login")
    if login is not None and time.monotonic() >= login["expires"] - 60:
        try:
            payload = request(config, "/auth/v1/token?grant_type=refresh_token", method="POST",
                              data={"refresh_token": login["refresh_token"]})
            if str(UUID(payload["user"]["id"])) != login["user_id"]:
                clear_session()
                return None
            save_login(payload)
        except SupabaseError as error:
            if error.status not in (400, 401, 403, 422):
                raise
            clear_session()
        login = st.session_state.get("login")
    return login


def load_config():
    try:
        config = st.secrets["supabase"]
        url = urlsplit(config["url"])
        publishable_key = config["publishable_key"]
        secret_key = config["secret_key"]
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path not in ("", "/") or url.query or url.fragment
                or url.hostname == "your_project_ref.supabase.co"
                or not re.fullmatch(r"sb_publishable_[A-Za-z0-9_-]+", publishable_key)
                or not re.fullmatch(r"sb_secret_[A-Za-z0-9_-]+", secret_key)
                or publishable_key == "sb_publishable_REPLACE_ME"
                or secret_key == "sb_secret_REPLACE_ME"):
            return None
        return config
    except (StreamlitSecretNotFoundError, KeyError, TypeError, ValueError):
        return None


def navigate(page):
    st.session_state["auth-page"] = page
    if page != "recover":
        st.session_state.pop("recovery-email", None)
        st.session_state.pop("recovery-token", None)
    st.rerun(scope="app")


@st.dialog("로그인이 필요합니다", width="small")
def login_dialog():
    st.write("모의 매매를 시작하시려면 로그인이 필요합니다")
    login, signup = st.columns(2)
    login.button("로그인", type="primary", width="stretch", on_click=st.session_state.__setitem__,
                 args=("auth-page", "login"))
    signup.button("회원가입", width="stretch", on_click=st.session_state.__setitem__,
                  args=("auth-page", "signup"))
    if st.session_state.get("auth-page") in ("login", "signup"):
        st.rerun(scope="app")


def show_auth_error(error, *, signup=False):
    if signup and error.code == "email_address_not_authorized":
        st.error("이 주소로 가입 확인 메일을 보낼 수 없습니다. 앱 관리자에게 문의해 주세요.")
    elif signup and error.code == "over_email_send_rate_limit":
        st.warning("가입 확인 메일 발송 한도에 도달했습니다. 잠시 후 다시 시도해 주세요.")
    elif error.status == 429:
        st.warning("요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.")
    elif error.status in (400, 401, 403, 422):
        message = ("가입할 수 없습니다. 이메일과 비밀번호를 확인해 주세요." if signup else
                   "로그인할 수 없습니다. 입력 정보와 이메일 확인 여부를 확인해 주세요.")
        st.error(message)
    else:
        st.error("로그인 서버가 요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.")


def password_login(config, email, password):
    return request(config, "/auth/v1/token?grant_type=password", method="POST",
                   data={"email": email, "password": password})


def show_auth_page(config, page):
    with st.container(horizontal_alignment="center"), st.container(width=420):
        st.subheader({"login": "로그인", "signup": "회원가입", "recover": "비밀번호 찾기"}[page])
        if config is None:
            st.error("Supabase 연결 설정이 필요합니다. Streamlit Secrets의 프로젝트 URL과 API 키를 확인해 주세요.")
        elif page == "recover":
            show_recovery(config)
        else:
            signup = page == "signup"
            with st.form(f"auth-{page}", clear_on_submit=True):
                email = st.text_input("이메일" if signup else "이메일 또는 관리자 아이디")
                password = st.text_input("비밀번호", type="password")
                submit = st.form_submit_button("회원가입" if signup else "로그인", type="primary", width="stretch")
            if submit:
                identifier = email.strip()
                if not identifier or not password:
                    st.warning("이메일과 비밀번호를 입력해 주세요.")
                elif signup and identifier.casefold() == ADMIN_EMAIL:
                    st.error("다른 이메일을 사용해 주세요.")
                else:
                    try:
                        with st.spinner("회원가입 중입니다…" if signup else "로그인 중입니다…"):
                            if signup:
                                payload = request(config, "/auth/v1/signup", method="POST",
                                                  data={"email": identifier, "password": password})
                            else:
                                email = ADMIN_EMAIL if identifier.casefold() == "admin" else identifier
                                payload = password_login(config, email, password)
                        if payload.get("access_token"):
                            clear_session()
                            save_login(payload)
                            st.session_state["auth-notice"] = (
                                "회원가입이 완료되었습니다." if signup else "로그인되었습니다.")
                            navigate("market")
                        st.success("회원가입 요청이 완료되었습니다. 이메일의 확인 링크를 누른 뒤 로그인해 주세요.")
                    except SupabaseError as error:
                        show_auth_error(error, signup=signup)
            if signup and st.button("로그인 화면으로"):
                navigate("login")
            if not signup:
                actions = st.columns(2)
                if actions[0].button("회원가입", width="stretch"):
                    navigate("signup")
                if actions[1].button("비밀번호 찾기", width="stretch"):
                    navigate("recover")
        if page == "recover" and st.button("로그인 화면으로"):
            navigate("login")
        if st.button("모의매매 화면으로"):
            navigate("market")


def show_recovery(config):
    token = st.session_state.get("recovery-token")
    if token is None:
        with st.form("recovery-send"):
            email = st.text_input("가입한 이메일", value=st.session_state.get("recovery-email", ""))
            send = st.form_submit_button("재설정 코드 받기", type="primary")
        if send:
            email = email.strip()
            if "@" not in email:
                st.warning("가입한 이메일을 입력해 주세요.")
            else:
                try:
                    request(config, "/auth/v1/recover", method="POST", data={"email": email})
                    st.session_state["recovery-email"] = email
                    st.info("가입한 이메일이라면 잠시 후 재설정 코드가 도착합니다.")
                except SupabaseError:
                    st.error("지금은 재설정 메일을 보낼 수 없습니다. 잠시 후 다시 시도해 주세요.")
    email = st.session_state.get("recovery-email")
    if not email:
        return
    with st.form("recovery-update", clear_on_submit=True):
        code = st.text_input("메일로 받은 재설정 코드") if token is None else ""
        password = st.text_input("새 비밀번호", type="password")
        repeat = st.text_input("새 비밀번호 확인", type="password")
        submit = st.form_submit_button("비밀번호 재설정", type="primary")
    if not submit:
        return
    if len(password) < 8 or password != repeat:
        st.warning("같은 새 비밀번호를 두 칸에 8자 이상으로 입력해 주세요.")
        return
    if token is None:
        try:
            payload = request(config, "/auth/v1/verify", method="POST",
                              data={"email": email, "type": "recovery", "token": code.strip()})
            token = payload["access_token"]
            st.session_state["recovery-token"] = token
        except SupabaseError:
            st.error("코드가 올바르지 않거나 만료되었습니다. 새 코드를 요청해 주세요.")
            return
    try:
        request(config, "/auth/v1/user", method="PUT", token=token, data={"password": password})
    except SupabaseError as error:
        if error.status in (401, 403):
            st.session_state.pop("recovery-token", None)
        st.error("비밀번호를 변경하지 못했습니다. 비밀번호 조건을 확인하고 다시 시도해 주세요.")
        return
    try:
        request(config, "/auth/v1/logout?scope=local", method="POST", token=token)
    except SupabaseError:
        pass
    st.session_state["auth-notice"] = "비밀번호가 변경되었습니다. 새 비밀번호로 로그인해 주세요."
    navigate("login")


def show_account_controls(config, login):
    st.sidebar.text("관리자" if login["role"] == "admin" else login["email"])
    if st.sidebar.button("로그아웃"):
        try:
            request(config, "/auth/v1/logout?scope=local", method="POST", token=login["access_token"])
            message = "로그아웃되었습니다."
        except SupabaseError:
            message = "이 앱에서 로그아웃했습니다. 로그인 서버에는 전달하지 못했습니다."
        clear_session()
        st.session_state["auth-notice"] = message
        navigate("market")
    if st.sidebar.button("내 계정"):
        navigate("account")


def show_account_settings(config, login):
    with st.container(horizontal_alignment="center"), st.container(width=420):
        st.subheader("내 계정")
        admin = login["role"] == "admin"
        if login["must_change_password"]:
            st.warning("관리자 계정을 사용하려면 먼저 초기 비밀번호를 바꿔 주세요.")
        with st.form("change-password", clear_on_submit=True):
            setup_code = st.text_input("관리자 초기 설정 코드", type="password") if login["must_change_password"] else ""
            current = st.text_input("현재 비밀번호", type="password")
            password = st.text_input("새 비밀번호", type="password")
            repeat = st.text_input("새 비밀번호 확인", type="password")
            change = st.form_submit_button("비밀번호 변경", type="primary")
        if change:
            if login["must_change_password"] and not hmac.compare_digest(
                hashlib.sha256(setup_code.strip().encode()).hexdigest(), login["setup_code_hash"] or ""
            ):
                st.error("관리자 생성 시 발급된 초기 설정 코드를 확인해 주세요.")
            elif len(password) < (12 if admin else 8) or password != repeat or password == current or (
                    admin and password == "admin123"):
                st.warning(f"현재 비밀번호와 다른 새 비밀번호를 두 칸에 {'12' if admin else '8'}자 이상으로 입력해 주세요.")
            else:
                try:
                    fresh = password_login(config, login["email"], current)
                    if str(UUID(fresh["user"]["id"])) != login["user_id"]:
                        raise SupabaseError(403)
                    if login["must_change_password"]:
                        request(config, f"/auth/v1/admin/users/{login['user_id']}", method="PUT",
                                private=True, data={
                                    "password": password,
                                    "app_metadata": {"coin_role": "admin", "must_change_password": False,
                                                     "setup_code_hash": None},
                                })
                    else:
                        request(config, "/auth/v1/user", method="PUT", token=fresh["access_token"],
                                data={"password": password, "current_password": current})
                    clear_session()
                    st.session_state["auth-notice"] = "비밀번호가 변경되었습니다. 새 비밀번호로 로그인해 주세요."
                    navigate("login")
                except SupabaseError:
                    st.error("비밀번호를 변경하지 못했습니다. 현재 비밀번호와 새 비밀번호 조건을 확인해 주세요.")
        if not admin:
            with st.expander("회원탈퇴"):
                st.warning("탈퇴하면 계정과 잔고, 주문 및 거래 기록이 영구 삭제됩니다.")
                with st.form("delete-account", clear_on_submit=True):
                    password = st.text_input("본인 확인용 비밀번호", type="password")
                    confirm = st.checkbox("계정과 거래 기록을 삭제하는 데 동의합니다.")
                    delete = st.form_submit_button("회원탈퇴")
                if delete:
                    if not confirm or not password:
                        st.warning("비밀번호를 입력하고 삭제에 동의해 주세요.")
                    else:
                        try:
                            fresh = password_login(config, login["email"], password)
                            if str(UUID(fresh["user"]["id"])) != login["user_id"]:
                                raise SupabaseError(403)
                            request(config, f"/auth/v1/admin/users/{login['user_id']}", method="DELETE",
                                    private=True, data={"should_soft_delete": False})
                            clear_session()
                            st.session_state["auth-notice"] = "회원탈퇴가 완료되었습니다."
                            navigate("market")
                        except SupabaseError:
                            st.error("탈퇴하지 못했습니다. 비밀번호를 확인하고 다시 시도해 주세요.")
        if not login["must_change_password"] and st.button("모의매매 화면으로"):
            navigate("market")


def account_directory():
    if "account-directory" not in st.session_state:
        st.session_state["account-directory"] = TemporaryDirectory(prefix="paper-account-")
    return Path(st.session_state["account-directory"].name)


def pack(files):
    stream = io.BytesIO()
    with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return base64.b64encode(stream.getvalue()).decode("ascii")


def unpack(snapshot):
    with ZipFile(io.BytesIO(base64.b64decode(snapshot, validate=True))) as archive:
        names = archive.namelist()
        if ("paper.sqlite3" not in names or len(set(names)) != len(names)
                or any(not re.fullmatch(r"paper(?:-[A-Za-z0-9_-]+)?\.sqlite3", name) for name in names)):
            raise ValueError("Invalid account snapshot")
        return {name: archive.read(name) for name in names}


@contextmanager
def account_changes(config):
    """Publish all changed account files together, rejecting concurrent writes."""
    login = current_login(config)
    if login is None or login.get("must_change_password"):
        st.rerun(scope="app")
    directory = account_directory()
    endpoint = "/rest/v1/paper_account_snapshots"
    account_filter = {"user_id": f"eq.{login['user_id']}"}
    select = {**account_filter, "select": "revision,snapshot"}
    cached = st.session_state.get("account-revision")

    def files():
        return {path.name: path.read_bytes() for path in directory.glob("*.sqlite3")}

    def save():
        after = files()
        if before == after:
            return
        snapshot = pack(after)
        if revision is None:
            rows = request(config, endpoint + "?on_conflict=user_id&select=revision", method="POST",
                           data={"user_id": login["user_id"], "snapshot": snapshot}, private=True,
                           prefer="resolution=ignore-duplicates,return=representation")
        else:
            filters = urlencode({**account_filter, "revision": f"eq.{revision}", "select": "revision"})
            rows = request(config, endpoint + "?" + filters, method="PATCH",
                           data={"revision": revision + 1, "snapshot": snapshot}, private=True,
                           prefer="return=representation")
        if not rows:
            raise SupabaseError(409)
        st.session_state["account-revision"] = rows[0]["revision"]

    if cached is not None:
        revision_rows = request(config, endpoint + "?" + urlencode({**account_filter, "select": "revision"}),
                                private=True)
        if not revision_rows:
            clear_session()
            st.rerun(scope="app")
    if cached is not None and revision_rows[0]["revision"] == cached:
        revision = cached
    else:
        rows = request(config, endpoint + "?" + urlencode(select), private=True)
        if cached is not None and not rows:
            clear_session()
            st.rerun(scope="app")
        revision = rows[0]["revision"] if rows else None
        restored = unpack(rows[0]["snapshot"]) if rows else {}
        for path in directory.glob("*.sqlite3"):
            if path.name not in restored:
                path.unlink()
        for name, content in restored.items():
            (directory / name).write_bytes(content)
        st.session_state["account-revision"] = revision

    before = files()
    try:
        yield
    except RerunException:
        save()
        raise
    except BaseException:
        st.session_state.pop("account-revision", None)
        raise
    else:
        save()


@contextmanager
def show_storage_error():
    try:
        yield
    except (SupabaseError, OSError, ValueError, BadZipFile) as error:
        st.session_state.pop("account-revision", None)
        for key in list(st.session_state):
            if key in ("next-paper-db", "next-market") or key.endswith(
                    (":notice", ":leverage-notice", ":cash-changed")):
                del st.session_state[key]
        if isinstance(error, SupabaseError) and error.status == 409:
            st.warning("다른 창에서 계좌가 먼저 변경됐습니다. 이번 변경은 저장되지 않았습니다.")
        else:
            st.error("계좌를 불러오거나 저장하지 못했습니다. 다음 갱신에서 저장 상태를 확인합니다.")
        st.stop()
