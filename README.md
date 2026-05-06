# Week 9 Practice Classroom Runbook

## 핵심 결론

학교 PC가 같은 네트워크 안에서도 외부 접속을 받지 못하면 학생 PC를 게임 서버로 쓰면 안 된다.
게임 상태와 WebSocket 중계는 Render 같은 외부 FastAPI 서버가 맡고, LLM 추론은 각 학생 PC의 Ollama가 맡는 구조로 운영한다.

단, 학생이 Render URL을 그대로 열면 브라우저가 `https://...` 출처에서 `http://127.0.0.1:11434` 로컬 Ollama를 호출하게 되어 CORS/브라우저 보안 정책에 막힐 수 있다. 학생용 페이지는 학생 PC의 `127.0.0.1`에서 열고, `?server=` 파라미터로 Render 서버에 연결한다.

Ollama 공식 문서:

- API 기본 주소: https://docs.ollama.com/api/introduction
- `OLLAMA_ORIGINS` 설정: https://docs.ollama.com/faq

## 교수자 준비

각 실습을 Render에 별도 서비스로 배포한다.

Start command:

```bash
uvicorn server:app --host 0.0.0.0 --port $PORT
```

Admin page:

```text
https://<practice-render-url>/admin
```

학생에게는 Render 루트 URL이 아니라 아래 형식의 학생용 접속 URL을 안내한다.

## 학생 준비

Ollama 앱을 켠 뒤 수업 표준 모델을 받아둔다.

```bash
ollama pull gemma4:latest
ollama list
```

`gemma4:latest` 대신 다른 모델을 쓰려면 학생 URL의 `model=` 값도 같은 이름으로 바꾼다.

## Practice #1 학생 접속

학생 PC에서 정적 페이지를 로컬로 연다.

macOS/Linux:

```bash
cd "week9/week9_prac/practice#1/static"
python3 -m http.server 8001
```

Windows:

```bash
cd "week9\week9_prac\practice#1\static"
py -m http.server 8001
```

브라우저 접속:

```text
http://127.0.0.1:8001/index.html?server=https://week9-prac1.onrender.com
```

