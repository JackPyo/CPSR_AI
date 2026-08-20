# CPSR AI Streamlit App

Google Colab으로 사용하던 CPSR Rule Engine을 웹 UI로 옮긴 버전입니다.

## 포함 파일

- `app.py` — Streamlit 웹앱
- `CPSR_Master_DB_All_INCI_Aliases_for_Colab.xlsx` — Master DB
- `requirements.txt` — 설치 패키지
- `.streamlit/config.toml` — 기본 Streamlit 설정
- `.streamlit/secrets.toml.example` — API Key 설정 예시

## 로컬 PC에서 실행

Python 3.11+ 권장.

```bash
python -m venv .venv
```

Windows:

```bash
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

macOS/Linux:

```bash
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

브라우저가 자동으로 열리며, 보통 `http://localhost:8501`에서 실행됩니다.

## OpenAI API Key

방법 1: 앱 왼쪽 사이드바에서 직접 입력  
이 경우 해당 세션에서만 사용합니다.

방법 2: 서버 Secret으로 설정  
`.streamlit/secrets.toml`:

```toml
OPENAI_API_KEY = "sk-..."
```

`secrets.toml`은 GitHub에 올리지 마세요.

## Streamlit Community Cloud 배포

1. GitHub 저장소를 하나 생성합니다.
2. 이 폴더의 파일들을 저장소에 업로드합니다.
3. Streamlit Community Cloud에서 `Create app`을 선택합니다.
4. GitHub 저장소, branch, `app.py`를 지정합니다.
5. API Key를 서버에 저장하려면 Streamlit 앱의 **Settings → Secrets**에 다음을 넣습니다.

```toml
OPENAI_API_KEY = "sk-..."
```

6. Deploy를 누릅니다.

배포 후 `https://앱이름.streamlit.app` 형태의 주소가 생성됩니다.

## 권장 운영 방식

외부 고객이 사용한다면 OpenAI API Key 입력란을 공개하지 말고 서버 Secret으로 관리하는 편이 좋습니다. 또한 처방과 규제자료는 기밀정보일 수 있으므로 회사 내부용 배포 또는 인증 기능이 있는 호스팅 환경을 권장합니다.

## 현재 판정 원칙

- Annex II exact identifier match → `FAIL`
- 단순 최대농도 초과 → `FAIL`
- 복합 조건 / 그룹 규제 / 특수 basis → `REVIEW`
- 단순 numeric 제한 내 + 명확한 조건 → `PASS`
- Master DB exact match 없음 → `NO MATCH IN DB`

`NO MATCH IN DB`는 규제 적합을 의미하지 않습니다.

## 주의

이 앱은 CPSR 검토 지원 도구이며 Safety Assessor의 최종 판단 및 서명을 대체하지 않습니다.
