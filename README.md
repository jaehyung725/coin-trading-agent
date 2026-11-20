# 비트코인 모의매매 AI 에이전트 공개 데모

로그인 없이 업비트 원화 마켓 시세와 차트를 볼 수 있습니다. 계정으로 로그인하면 시장가·지정가 현물 주문과 레버리지 롱을 연습할 수 있습니다. 거래소 API 키 없이 가상 자금으로 모의 체결합니다. 이메일로 로그인하면 첫 접속 때 100만 원을 받고, 다시 로그인하면 잔고, 주문, 체결, 포지션과 손익을 이어서 확인할 수 있습니다.

## Supabase 설정

1. Supabase 프로젝트의 SQL Editor에서 [계좌 저장 테이블 SQL](supabase/paper_accounts.sql)을 실행합니다.
2. Authentication에서 이메일 가입을 활성화하고 Site URL에 배포된 Streamlit 앱 주소를 지정합니다. 이메일 확인을 사용하는 경우 확인 링크를 누른 후 앱에서 이메일과 비밀번호로 로그인합니다. 일반 사용자에게 확인 메일을 발송하려면 Supabase의 Custom SMTP를 설정합니다.
3. 로컬에서는 [.streamlit/secrets.example.toml](.streamlit/secrets.example.toml)을 `.streamlit/secrets.toml`로 복사하고 프로젝트 URL, publishable key, secret key를 입력합니다. Streamlit Community Cloud에서는 앱 설정의 **Secrets**에 같은 형식으로 입력합니다. 실제 키는 Git에 올리지 않습니다.

## 일반 사용자에게 가입 확인 메일 보내기

Supabase 기본 메일 서비스는 Organization 멤버에게만 발송합니다. 일반 사용자에게 보낼 때는 [Supabase Custom SMTP](https://supabase.com/docs/guides/auth/auth-smtp)를 설정합니다. 예를 들어 Brevo 무료 플랜은 하루 300건의 발송을 포함합니다.

1. Brevo에서 발신 주소를 등록하고 발신 도메인을 인증합니다. **Settings → SMTP & API**에서 SMTP 로그인 값을 확인하고 SMTP 키를 생성합니다. [Brevo SMTP 안내](https://help.brevo.com/hc/en-us/articles/7924908994450-Send-transactional-emails-using-Brevo-SMTP)
2. Supabase 프로젝트의 Authentication에서 **Custom SMTP**를 켭니다. 발신 이메일에는 Brevo에 등록한 주소를, Host에는 `smtp-relay.brevo.com`을, Port에는 `587`을 입력합니다. Username은 Brevo의 SMTP 로그인 값, Password는 SMTP 키입니다. SMTP 키는 Supabase에 직접 입력합니다.
3. **Confirm email**을 켜고 **URL Configuration → Site URL**을 사용자가 열 수 있는 앱 주소로 설정합니다. 공개 배포 후에는 실제 Streamlit 주소를 사용합니다.
4. Supabase Organization에 등록되지 않은 본인 이메일로 앱에서 가입합니다. 확인 메일의 링크를 누른 뒤 로그인해서 초기 잔고 100만 원이 보이는지 확인합니다.

## 비밀번호 찾기와 회원탈퇴

Supabase **Authentication → Emails → Reset password**의 본문을 아래와 같이 저장합니다. 비밀번호 찾기에서 받은 코드를 앱에 입력합니다. 가입 확인 메일 템플릿은 그대로 사용합니다.

```html
<h2>모의매매 앱 비밀번호 재설정</h2>
<p>앱의 비밀번호 찾기 화면에 아래 코드를 입력하세요.</p>
<p>{{ .Token }}</p>
```

로그인한 일반 회원은 사이드바의 **내 계정**에서 비밀번호를 변경하거나 탈퇴할 수 있습니다. 탈퇴를 확정하면 Supabase 계정과 함께 기존 테이블의 외래키 규칙에 따라 모의계좌 기록도 삭제됩니다.

## 관리자 계정 생성

Supabase 프로젝트에 앱 관리자를 처음 한 번 생성하려면 로컬 프로젝트에서 아래 명령을 실행합니다.

```bash
.venv/bin/python provision_admin.py
```

초기 비밀번호 입력 창에 요청한 비밀번호를 두 번 입력합니다. 앱에서 아이디 `admin`으로 로그인한 뒤, 명령이 출력한 초기 설정 코드로 본인임을 확인하고 12자 이상의 새 비밀번호를 설정합니다. 초기 설정을 끝내면 거래 화면과 **내 계정** 메뉴를 사용할 수 있습니다. 관리자 권한은 Supabase의 서버 관리 메타데이터에 저장되며 일반 회원가입으로 부여되지 않습니다.

## 자체 테스트

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## 로컬 실행

Python 3.10 이상에서 실행합니다.

```bash
python -m pip install -r requirements.txt
python -m streamlit run dashboard.py
```

기본 주소는 `http://127.0.0.1:8501`입니다. 업비트 시세와 Supabase 연결에는 인터넷 접속이 필요합니다.

## 계좌 저장 방식

Streamlit 서버는 Supabase Auth에서 확인한 사용자 ID로 본인 계좌만 조회합니다. 각 사용자의 기존 SQLite 계좌 파일을 압축해 Supabase PostgreSQL의 한 행에 저장하며, 잔고와 기록을 함께 갱신합니다. 다른 창에서 변경이 먼저 저장되면 오래된 변경은 거절됩니다. 기존 익명 세션의 임시 계좌는 로그인 계좌에 이전되지 않습니다. 지정가 체결과 레버리지 청산 시세 확인은 앱에서 해당 계좌 화면이 갱신될 때 수행됩니다.
