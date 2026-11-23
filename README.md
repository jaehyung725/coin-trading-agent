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

## 상시 모의매매 실행

Streamlit과 별도로 프로젝트 폴더에서 작업 프로세스를 실행합니다. 두 프로세스는 같은 Supabase 프로젝트 설정을 사용합니다.

```bash
.venv/bin/python auto_runner.py
```

앱에서 코인과 전략을 선택하고 **자동 모의매매 시작**을 누르면 전략을 활성화합니다. 작업 프로세스가 시작 이후 처음 마감되는 1분봉부터 신호를 확인합니다. 지표 계산에는 이전 봉도 사용합니다. 선택한 지표의 마감 1시간봉 방향과 최근 마감 1분봉의 움직임이 일치하면 선택한 배율로 롱·숏 모의 진입을 합니다. **부자 되고 싶은 마음**에서 Light(기본 2배), Medium(5배), High(10배), Ultra(20배)를 선택할 수 있습니다. 가용 현금 사용 비율은 20%(기본), 40%, 60%, 80%, 100% 중에서 고를 수 있으며 진입 증거금과 수수료를 포함합니다. 시작할 때 선택한 배율과 비율은 새 포지션에 적용됩니다. 가격 기준 손절 0.5%, 익절 1%, 반대 방향 신호 또는 모의 청산 조건에서 포지션을 종료합니다. 브라우저를 닫거나 로그아웃해도 작업 프로세스가 실행 중이면 계속 처리합니다. **자동 모의매매 종료**는 해당 종목·전략의 향후 판단을 중지하고 잔고와 포지션을 보존합니다. 재시작 후 새로 마감된 1분봉부터 판단을 재개합니다. 기존 1시간봉 상시 설정은 종료한 뒤 다시 시작하면 새 방식으로 전환됩니다. 과거 차트로 수익률 계산해 보기 메뉴는 기존 현물 규칙의 과거 성과를 보여줍니다.

같은 호스트에서 앱과 작업 프로세스를 실행하면 화면에서 작업 프로세스 상태를 확인할 수 있습니다. 서로 다른 실행 환경에서는 상태 파일을 공유하는 경로를 두 프로세스의 `COIN_AUTO_HEARTBEAT` 환경 변수에 지정합니다. 운영 환경에서는 작업 프로세스가 종료되면 다시 실행되도록 서비스 관리자를 설정합니다.

한 번의 점검만 실행하려면 `auto_runner.py --once`를 사용합니다.

### 완전 자동 매매 (AI 에이전트) 설정

[OpenAI API 키 페이지](https://platform.openai.com/api-keys)에서 프로젝트 키를 발급합니다. 기존 `.streamlit/secrets.toml`에 다음 섹션을 추가합니다.

```toml
[openai]
api_key = "YOUR_OPENAI_API_KEY"
model = "gpt-4.1-mini"
```

배포 환경에서는 양쪽 프로세스에 `OPENAI_API_KEY`와 선택 사항인 `OPENAI_MODEL`을 환경 변수로 지정할 수 있습니다. 환경 변수에 값이 있으면 해당 값이 우선합니다. 실제 키는 버전 관리에 추가하지 않습니다.

설정을 저장한 후 Streamlit과 `auto_runner.py`를 다시 시작합니다. 자동매매 전략에서 **완전 자동 매매 (AI 에이전트)**를 선택하고 시작합니다. 활성화한 종목·전략마다 5분 간격으로 마감 차트, 지표, 현재 포지션을 OpenAI에 보냅니다. 모델은 새 포지션마다 레버리지(2·5·10·20배)와 진입에 쓸 가용 현금 비율(20·40·60·80·100%, 수수료 포함)도 결정합니다. 선택값과 판단 근거는 **AI 자동매매 내역**에 표시합니다. API 사용량에 따라 비용이 발생합니다. 모델 응답을 받지 못하면 모델 신호를 대기 처리하며, 시세를 확인할 수 있는 동안 손절·익절·청산은 1분 단위로 계속 검사합니다.

## AI 자동매매 보고서와 운영 로그

자동매매 성과 화면의 **AI 자동매매 보고서 작성**을 누르면 선택 종목의 누적 성과와 위험 지표를 집계합니다. 화면에서 요약, 차트, 주요 체결을 확인하고 Markdown 파일을 내려받을 수 있습니다. 보고서는 버튼을 누른 시점의 기록을 사용하고 AI 해석 요청은 생성할 때 한 번 실행합니다. 시세나 기록을 확보할 수 없는 지표는 미집계로 표시합니다. OpenAI 호출에 실패하더라도 수치 보고서는 확인할 수 있습니다.

앱과 작업 프로세스는 기본적으로 `.cache/logs/dashboard.log`와 `.cache/logs/worker.log`에 시작, 주요 동작, 오류를 기록합니다. 로그가 2MB에 도달하면 순환시키고 이전 파일은 최대 3개까지 보관합니다. 다른 위치에 저장하려면 실행 환경에 `COIN_LOG_DIR`를 지정하세요. 배포 서버에서 로그를 오래 보관하려면 해당 경로를 영구 볼륨으로 지정합니다.

## 계좌 저장 방식

Streamlit 서버는 Supabase Auth에서 확인한 사용자 ID로 본인 계좌만 조회합니다. 각 사용자의 기존 SQLite 계좌 파일을 압축해 Supabase PostgreSQL의 한 행에 저장하며, 잔고와 기록을 함께 갱신합니다. 다른 창에서 변경이 먼저 저장되면 오래된 변경은 거절됩니다. 기존 익명 세션의 임시 계좌는 로그인 계좌에 이전되지 않습니다. 지정가 체결과 레버리지 청산 시세 확인은 앱에서 해당 계좌 화면이 갱신될 때 수행됩니다.

## 로컬 PC 없이 비트코인 자정 비교 실행

1. Streamlit Community Cloud에 `dashboard.py`를 배포하고 앱의 **Settings → Secrets**에 기존 `[supabase]` 및 `[openai]` 설정을 입력합니다.
2. GitHub 저장소의 **Settings → Secrets and variables → Actions**에 `COIN_CLOUD_SECRETS_TOML`이라는 저장소 Secret을 만들고, 동일한 두 섹션을 값으로 입력합니다.
3. 배포 앱에서 로그인하고 비트코인의 각 자동매매 전략을 시작합니다. **비트코인 자정 비교 예약**을 눌러 현재 계정의 기록을 비교 대상으로 등록합니다.
4. GitHub **Actions → Bitcoin midnight comparison → Run workflow**에서 앱에 표시된 마감 시각을 입력합니다. 11월 23일 밤 자정의 값은 `2026-11-24T00:00:00+09:00`입니다. 앱에서 **마지막 확인** 시각이 표시되면 클라우드 작업이 해당 계정을 확인한 상태입니다.
5. 마감 후 계정에 다시 로그인해 자동매매 화면의 **클라우드 비트코인 비교 보고서 다운로드**에서 보고서를 받습니다. 보고서와 계좌 기록은 같은 Supabase 프로젝트를 사용합니다.

실행 중인 로컬 작업과 클라우드 작업을 겹쳐 돌리면 OpenAI 호출이 중복될 수 있습니다. 이 워크플로는 예약된 비트코인 계정만 지정 시각까지 처리하는 테스트용입니다. 자정 이후에는 워크플로가 종료됩니다.
