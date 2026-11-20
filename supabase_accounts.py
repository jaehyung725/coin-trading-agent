"""Supabase email authentication and durable storage for the trading databases."""

import base64
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
    st.session_state["login"] = {
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


def require_login():
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
            raise ValueError
    except (StreamlitSecretNotFoundError, KeyError, TypeError, ValueError):
        st.error("Supabase 연결 설정이 필요합니다. .streamlit/secrets.toml에 실제 프로젝트 URL과 API 키를 입력해 주세요.")
        st.stop()

    try:
        login = current_login(config)
    except SupabaseError:
        st.error("로그인 서버에 연결하지 못했습니다. 잠시 후 새로고침해 주세요.")
        st.stop()

    if login is not None:
        st.sidebar.text(login["email"])
        if st.sidebar.button("로그아웃"):
            try:
                request(config, "/auth/v1/logout?scope=local", method="POST", token=login["access_token"])
            except SupabaseError:
                clear_session()
                st.session_state["logout-local-only"] = True
            else:
                clear_session()
            st.rerun()
        return config

    if st.session_state.pop("logout-local-only", False):
        st.warning("이 앱에서 로그아웃했습니다. 로그인 서버에는 로그아웃을 전달하지 못했습니다.")
    with st.form("supabase-auth", clear_on_submit=True):
        email = st.text_input("이메일")
        password = st.text_input("비밀번호", type="password")
        sign_in = st.form_submit_button("로그인", type="primary")
        sign_up = st.form_submit_button("회원가입")
    if sign_in or sign_up:
        if not email.strip() or not password:
            st.warning("이메일과 비밀번호를 입력해 주세요.")
        else:
            path = "/auth/v1/signup" if sign_up else "/auth/v1/token?grant_type=password"
            try:
                payload = request(config, path, method="POST",
                                  data={"email": email.strip(), "password": password})
                if payload.get("access_token"):
                    save_login(payload)
                    st.rerun()
                st.info("가입 확인 메일을 받으면 링크를 누른 뒤 로그인해 주세요.")
            except SupabaseError as error:
                if sign_up and error.code == "email_address_not_authorized":
                    st.error("현재 이 주소로 가입 확인 메일을 보낼 수 없습니다. "
                             "앱 관리자에게 메일 발송 설정을 문의해 주세요.")
                elif sign_up and error.code == "over_email_send_rate_limit":
                    st.warning("가입 확인 메일 발송 한도에 도달했습니다. 잠시 후 다시 시도해 주세요.")
                elif error.status == 429:
                    st.warning("요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.")
                elif error.status in (400, 401, 403, 422):
                    st.error("가입할 수 없습니다. 이메일과 비밀번호를 확인해 주세요." if sign_up else
                             "로그인할 수 없습니다. 이메일, 비밀번호와 가입 확인 여부를 확인해 주세요.")
                else:
                    st.error("로그인 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.")
    st.stop()


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
    if login is None:
        st.rerun(scope="app")
    directory = account_directory()
    endpoint = "/rest/v1/paper_account_snapshots"
    account_filter = {"user_id": f"eq.{login['user_id']}"}
    select = {**account_filter, "select": "revision,snapshot"}
    cached = st.session_state.get("account-revision")
    if cached is not None:
        select["revision"] = f"neq.{cached}"

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

    rows = request(config, endpoint + "?" + urlencode(select), private=True)
    revision = rows[0]["revision"] if rows else cached
    if rows or cached is None:
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
