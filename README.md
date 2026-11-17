# 재난알림봇

행정안전부 재난문자를 Discord 채널에 공지하고, 지진일 경우 기상청 지진정보(규모·깊이·진도·진앙 지도)를 같은 공지에 붙여주는 봇입니다.

## 동작 요약

| API | 일일 한도(설정) | 평상시 주기 | 집중 조회 |
|---|---|---|---|
| 재난문자 (safetydata.go.kr DSSP-IF-00247) | 1,000 (예비 130) | 약 100초 | 국내 M3.5+ 지진 감지 시 30초 간격 4분 |
| 지진정보 (기상청 EqkInfoService/getEqkMsg) | 1,000 (예비 280) | 약 120초 | 지진 문자/속보 수신 시 상세정보 나올 때까지 20초 간격(최대 10분) |

- 주기 = `오늘 남은 시간 ÷ (남은 호출 수 − 예비분)` → 한도 초과 불가. 호출 수는 SQLite에 저장되어 재시작해도 유지됩니다.
- 지진은 **서버당 메시지 1개**로 관리합니다: 재난문자 → 지진속보(자동추정) → 지진정보(수동분석) 순으로 들어오는 정보를 같은 메시지를 **수정**해 반영합니다.
- 공지(Announcement) 채널이면 자동으로 게시(publish)되어 팔로우한 다른 서버에도 전달됩니다.
- 재시작 시 30분(지진 60분) 이상 지난 항목은 공지하지 않습니다(폭주 방지).
- API 오류가 5회 연속되거나 키 오류/한도 초과 시 `ADMIN_CHANNEL_ID` 채널로 알립니다.

> ⚠️ 이 봇은 공공 API를 주기적으로 조회하므로 휴대폰 긴급재난문자(CBS)보다 수 분 늦을 수 있습니다. 참고용으로만 사용하세요.

## 기본 수신 정책 (서버별로 변경 가능)

- **지역**: 필터가 없으면 **전국** 문자를 모두 받습니다.
- **단계**: 위급재난 ✅ · 긴급재난 ✅ · 안전안내 ❌ · 실종알림 ❌
  - 실종알림은 분류(`DST_SE_NM`)에 '실종'이 있거나, 본문에 '실종/배회'와 '찾습니다/경찰/182'가 함께 있는 문자로 판단합니다.
- **지진**: 국내 M2.0 이상, 국외 M6.0 이상이면 재난문자가 없어도 공지합니다. 지진 재난문자에는 기상청 상세정보가 항상 붙습니다.

## 슬래시 명령어 (`서버 관리` 권한 필요, 응답은 본인에게만 표시)

| 명령 | 설명 |
|---|---|
| `/재난알림 대시보드` | 설정 전체, 채널 권한 점검, 최근 공지 5건(바로가기 링크), API 사용량, 봇 상태 (🔄 새로고침 버튼) |
| `/재난알림 채널` | 공지 채널 지정 (= 공지 시작) |
| `/재난알림 지역 서울,경북 상주시` | 수신지역 필터 (`전국`으로 해제, `경북`↔`경상북도` 자동 인식) |
| `/재난알림 단계 안전안내:True 실종알림:False` | 공지할 종류 켜기/끄기 (지정한 항목만 변경) |
| `/재난알림 멘션` | 종류별(위급/긴급/안전안내/지진) 멘션 역할 |
| `/재난알림 지진` | 재난문자 없이도 공지할 지진 최소 규모 |
| `/재난알림 미리보기` | 공지 형식 예시 |
| `/재난알림 해제` | 공지 중지 (설정은 유지) |

### 지진정보 수동 공지 (`서버 관리` 권한 필요, **채널의 모두에게 공개**)

| 명령 | 설명 |
|---|---|
| `/지진정보 최신 [범위]` | 가장 최근 지진 1건 상세정보 + 진앙 지도 (기본: 국내) |
| `/지진정보 오늘 [범위]` | 오늘 발생한 지진 목록 (기본: 국내+국외) |
| `/지진정보 최근 [범위]` | 최근 3일 지진 목록 (기상청 API 제공 범위) |

- 같은 지진의 속보와 상세정보는 하나로 합쳐 상세정보 기준으로 보여줍니다.
- 자동 폴링 결과를 2분간 캐시해 함께 쓰므로, 명령을 써도 API 호출은 거의 늘지 않습니다. 캐시가 오래됐으면 예비분에서 1회 호출합니다.
- 서버마다 30초 쿨다운이 있습니다. 수동 공지도 대시보드의 '최근 공지'에 `[수동]`으로 기록됩니다.

## 다중 서버 구조

API 호출은 서버 수와 상관없이 한 번만 하고, 받은 결과를 서버별 필터에 맞춰 나눠 보냅니다. 발송은 최대 10곳씩 동시에 처리합니다.

| 테이블 | 내용 |
|---|---|
| `guilds` | 서버별 설정 1행 (채널, 단계, 실종알림, 지진 기준) |
| `guild_regions` | 서버별 지역 키워드 (없으면 전국) |
| `guild_mentions` | 서버별 멘션 역할 |
| `deliveries` | 서버별 발송 기록, 30일 보관 (대시보드 '최근 공지') |
| `seen` / `quota` | 중복 제거와 API 일일 호출 수 (모든 서버 공통) |

- 봇이 서버에서 추방되면 그 서버의 설정과 기록이 모두 삭제됩니다 (`ON DELETE CASCADE`).
- 공지 채널이 삭제되면 그 서버의 공지가 자동으로 꺼집니다.
- 스키마 버전은 `PRAGMA user_version`으로 관리합니다. 바꿀 때는 `storage.py`의 `MIGRATIONS`에 항목을 추가하세요.

## 준비

1. **Discord 봇**: Developer Portal에서 앱 생성 → Bot 토큰 발급. 권한 없는 기본 Intent만 사용합니다.
   초대 URL scope: `bot applications.commands`, 권한: Send Messages, Embed Links, Manage Messages(공지 publish용), Mention Everyone(역할 멘션이 "멘션 허용" 꺼진 역할이면 필요).
2. `.env.example`을 `.env`로 복사해 값 입력. `DATA_GO_KR_KEY`는 포털의 **Encoding(인코딩된) 키**를 넣습니다.

## Synology NAS 배포 (Docker Hub 이미지)

이미지: `quesogato3924/disaster-alert-bot` (linux/amd64). 태그는 `latest`와 버전(`1.0.0`)이 있습니다.

1. File Station에서 `docker/disaster-alert-bot` 폴더를 만들고 다음 두 파일을 올립니다.
   - `deploy/synology/docker-compose.yml`
   - `deploy/synology/.env.example` → 이름을 `.env`로 바꾸고 값 입력 (`DISCORD_TOKEN`, `DATA_GO_KR_KEY`)
2. **Container Manager → 프로젝트 → 생성** → 경로에 위 폴더 선택 → "기존 docker-compose.yml 사용" → 완료(시작).
3. 컨테이너 로그에 `로그인: 재난알림봇#…`이 보이면 정상입니다. `data/` 폴더는 자동으로 생깁니다.

**업데이트** (PC에서 새 이미지를 push한 뒤):
- SSH (가장 확실): `cd /volume1/docker/disaster-alert-bot && sudo docker compose pull && sudo docker compose up -d`
- Container Manager UI: **이미지** 탭에서 `quesogato3924/disaster-alert-bot`에 '업데이트 가능'이 표시되면 업데이트한 뒤, 프로젝트를 중지했다가 다시 시작합니다.
- 버전을 고정하고 싶으면 yml의 `:latest` 대신 `:1.0.0`처럼 태그를 지정하세요. 업데이트할 때 태그만 바꾸면 되므로 어떤 버전이 돌고 있는지 분명해집니다.

**재난문자 켜기 (safetydata 승인 후)**: `.env`에 `CBS_SERVICE_KEY`를 넣고 `DISASTER_ENABLED=true`로 바꾼 뒤 컨테이너를 재시작합니다.

### 이미지 게시 (개발 PC)
```sh
docker build --platform linux/amd64 -t quesogato3924/disaster-alert-bot:<버전> -t quesogato3924/disaster-alert-bot:latest .
docker push quesogato3924/disaster-alert-bot:<버전>
docker push quesogato3924/disaster-alert-bot:latest
```

## 로컬 실행

```sh
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Linux/macOS: .venv/bin/pip
.venv/Scripts/python -m bot
```

## 구조

```
bot/
  __main__.py   진입점
  config.py     환경변수
  client.py     Discord 클라이언트 + 슬래시 명령어
  pollers.py    API별 폴링 루프 (한도/백오프/집중조회)
  quota.py      일일 호출 한도 및 간격 계산
  apis.py       API 호출/파싱 (인코딩된 키 그대로 사용)
  dispatcher.py 지진 이벤트 묶기, 서버별 필터, 발송/수정
  render.py     Embed 생성
  storage.py    SQLite
  models.py     데이터 모델 (기상청 통보종류 코드 포함)
```
