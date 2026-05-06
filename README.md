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
http://127.0.0.1:8001/index.html?server=https://<practice1-render-url>&model=gemma4:latest
```

## Practice #2 학생 접속

macOS/Linux:

```bash
cd "week9/week9_prac/practice#2/static"
python3 -m http.server 8002
```

Windows:

```bash
cd "week9\week9_prac\practice#2\static"
py -m http.server 8002
```

브라우저 접속:

```text
http://127.0.0.1:8002/index.html?server=https://<practice2-render-url>&model=gemma4:latest
```

## URL 파라미터

- `server`: WebSocket을 연결할 외부 게임 서버. 예: `https://abc.onrender.com`
- `model`: Ollama 모델 이름. 기본값은 `gemma4:latest`
- `ollama`: Ollama API 주소. 기본값은 `http://127.0.0.1:11434`

예시:

```text
http://127.0.0.1:8001/index.html?server=https://abc.onrender.com&model=gemma3:4b
```

## Render URL을 직접 열어야 하는 경우

가능하면 권장하지 않는다. 그래도 Render 페이지에서 직접 로컬 Ollama를 호출하려면 학생 PC의 Ollama가 해당 Render origin을 허용해야 한다.

macOS:

```bash
launchctl setenv OLLAMA_ORIGINS "https://<practice1-render-url>,https://<practice2-render-url>"
```

그 뒤 Ollama 앱을 완전히 종료하고 다시 켠다.

Windows:

1. 작업 표시줄에서 Ollama를 Quit 한다.
2. "환경 변수" 설정에서 사용자 변수 `OLLAMA_ORIGINS`를 추가한다.
3. 값에 `https://<practice1-render-url>,https://<practice2-render-url>` 를 넣는다.
4. Ollama 앱을 다시 실행한다.

Linux에서 `ollama serve`를 직접 띄우는 경우:

```bash
OLLAMA_ORIGINS="https://<practice1-render-url>,https://<practice2-render-url>" ollama serve
```

## 문제 해결

입장 버튼을 눌렀을 때 "Ollama에는 ... 모델이 없습니다"가 뜨면:

```bash
ollama pull <모델명>
```

"Ollama 연결 실패"가 뜨면:

1. Ollama 앱이 켜져 있는지 확인한다.
2. 브라우저에서 `http://127.0.0.1:11434/api/tags` 를 열어 응답이 오는지 확인한다.
3. 학생 페이지를 `http://127.0.0.1:8001` 또는 `http://127.0.0.1:8002` 로 열었는지 확인한다.
4. Render URL을 직접 열었다면 위의 `OLLAMA_ORIGINS` 설정을 하거나, 로컬 학생 페이지 방식으로 바꾼다.

게임 중 `...`만 나온다면 사전 점검은 통과했지만 생성 호출이 실패한 것이다. 모델이 너무 느리거나 메모리가 부족하거나 Ollama가 중간에 종료된 경우가 많다.
