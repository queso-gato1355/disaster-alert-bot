# 재난알림봇

행정안전부 재난문자를 Discord 채널에 공지하고, 지진일 경우 기상청 지진정보(규모·깊이·진도·진앙 지도)를 같은 공지에 붙여주는 봇입니다.

## 동작 요약

공공 API는 **봇이 직접 호출하지 않습니다.** 같은 NAS의 수집기 컨테이너 `safety-collector`가 재난문자(safetydata DSSP-IF-00247)와 기상청 지진정보(data.go.kr EqkInfoService)를 호출해 SQLite DB(`safety.db`, WAL)에 쓰고, 봇은 그 DB를 **읽기 전용**(`mode=ro`)으로 조회합니다. API 키와 호출 한도는 수집기에서만 관리합니다.

| 수집기 테이블 | 봇 조회 주기 | 용도 |
|---|---|---|
| `cbs` | 5초 | 재난문자 |
| `eqk_reports` (`src='data.go.kr'`) | 5초 | 기상청 지진 발표문 (조기경보·속보·정보·재통보·국외) |
| `pews_events` | 2초 | PEWS 실시간 감지 — `PEWS_ENABLED=true`일 때만 (기본 꺼짐) |
| `meta` | 30초 | 수집기 heartbeat 감시 |

- 새 항목은 수집기 DB에 들어온 순서인 **`id` 커서**로 고릅니다. 커서는 봇 DB의 `cursors` 테이블에 저장되고, 처음 실행할 때는 `MAX(id)`에서 시작하므로 지난 항목을 다시 알리지 않습니다. 알림을 보낸 뒤에 커서를 올리며, 처리 중 예외가 나면 다음 주기에 다시 시도하고 3번 실패한 행은 관리자 채널에 알린 뒤 건너뜁니다.
- 수집기 DB가 없거나 잠겨 있으면 그 회차만 건너뜁니다. heartbeat가 **5분** 넘게 멈추거나 **10분** 넘게 읽히지 않으면 `ADMIN_CHANNEL_ID` 채널로 "수집기 멈춤"을 한 번 보내고, 정상이 되면 "복구"를 한 번 보냅니다.
- 지진은 **서버당 메시지 1개**로 관리합니다: (PEWS →) 재난문자 → 지진속보(자동추정) → 지진정보(수동분석) 순으로 들어오는 정보를 같은 메시지를 **수정**해 반영합니다. PEWS 값은 "기상청 PEWS 자동 분석(비공식)"으로 표시하고, 공식 발표가 오면 그 값으로 바뀝니다.
- 공지(Announcement) 채널이면 자동으로 게시(publish)되어 팔로우한 다른 서버에도 전달됩니다.
- 봇이 오래 멈췄다 켜진 경우 30분(지진 60분) 넘게 지난 항목은 공지하지 않습니다(폭주 방지). 재난문자는 API 반영 시각(`reg_at`) 기준입니다.
- 재난문자는 발송 후 **약 10분 뒤** API에 반영되므로, 휴대폰 긴급재난문자(CBS)보다 늦습니다.

> ⚠️ 이 봇은 공공 API를 기반으로 하므로 휴대폰 긴급재난문자(CBS)보다 수 분 늦을 수 있습니다. 참고용으로만 사용하세요.

## 기본 수신 정책 (서버별로 변경 가능)

- **지역**: 필터가 없으면 **전국** 문자를 모두 받습니다.
  - 전국 대상 문자는 수신지역이 시·도 16~17개로 나열되어 오므로, 수신지역이 키워드의 상위 지역이면(`충청남도` ⊃ `충남 천안시`) 함께 공지합니다.
- **단계**: 위급재난 ✅ · 긴급재난 ✅ · 안전안내 ❌ · 실종알림 ❌
  - 실종알림은 분류(`DST_SE_NM`)에 '실종'이 있거나, 본문에 '실종/배회'와 '찾습니다/경찰/182'가 함께 있는 문자로 판단합니다.
- **지진**: 서버별 등급(`/재난알림 지진`)을 넘으면 재난문자가 없어도 공지합니다. 지진 재난문자에는 기상청 상세정보가 항상 붙습니다.
  | 등급 | 기준 |
  |---|---|
  | 진도 Ⅴ 이상 | 국내 최대진도 Ⅴ 이상 (국외 지진으로 국내에 진도 Ⅴ 이상이 관측된 경우 포함) |
  | 진도 Ⅲ 이상 (기본값) | 국내 최대진도 Ⅲ 이상 (국외 지진으로 국내에 진도 Ⅲ 이상이 관측된 경우 포함) |
  | 모든 지진 | 기상청이 발표하는 국내·국외 지진 전부 |
  | 끄기 | 지진정보 단독 공지 없음 (지진 재난문자의 상세정보는 유지) |
  - 진도는 기상청 통보의 `inT`(예: `최대진도 Ⅳ(경북,충북)`)에서 가장 큰 값을 씁니다. 진도가 없는 속보는 진도가 포함된 상세정보가 들어올 때 공지됩니다.

## 슬래시 명령어 (`서버 관리` 권한 필요, 응답은 본인에게만 표시)

| 명령 | 설명 |
|---|---|
| `/재난알림 대시보드` | 설정 전체, 채널 권한 점검, 최근 공지 5건(바로가기 링크), 수집기·조회 상태 (🔄 새로고침 버튼) |
| `/재난알림 채널` | 공지 채널 지정 (= 공지 시작) |
| `/재난알림 지역 서울,경북 상주시` | 수신지역 필터 (`전국`으로 해제, `경북`↔`경상북도` 자동 인식) |
| `/재난알림 단계 안전안내:True 실종알림:False` | 공지할 종류 켜기/끄기 (지정한 항목만 변경) |
| `/재난알림 멘션` | 종류별(위급/긴급/안전안내/지진) 멘션 역할 |
| `/재난알림 지진` | 재난문자 없이도 공지할 지진 등급 (진도 Ⅴ 이상 / 진도 Ⅲ 이상 / 모든 지진 / 끄기) |
| `/재난알림 미리보기` | 공지 형식 예시 |
| `/재난알림 해제` | 공지 중지 (설정은 유지) |

### 지진정보 수동 공지 (`서버 관리` 권한 필요, **채널의 모두에게 공개**)

| 명령 | 설명 |
|---|---|
| `/지진정보 최신 [범위]` | 가장 최근 지진 1건 상세정보 + 진앙 지도 (기본: 국내) |
| `/지진정보 오늘 [범위]` | 오늘 발생한 지진 목록 (기본: 국내+국외) |
| `/지진정보 최근 [범위]` | 최근 3일 지진 목록 |

- 같은 지진의 속보와 상세정보는 하나로 합쳐 상세정보 기준으로 보여줍니다.
- 수집기 DB를 바로 읽으므로 API 호출이 늘지 않습니다 (공식 발표문만, PEWS 제외).
- 서버마다 30초 쿨다운이 있습니다. 수동 공지도 대시보드의 '최근 공지'에 `[수동]`으로 기록됩니다.

### 재난문자 수동 공지 (`서버 관리` 권한 필요, **채널의 모두에게 공개**)

| 명령 | 설명 |
|---|---|
| `/재난문자 최신 [지역]` | 최근 24시간 중 가장 최근 재난문자 1건 |
| `/재난문자 오늘 [지역]` | 오늘 발송된 재난문자 목록 (단계별 합계 포함) |

- 수집기 DB의 최근 24시간 문자를 읽으므로 **API 호출이 늘지 않습니다**. 실종알림은 서버의 `실종알림` 설정을 따릅니다.

## 다중 서버 구조

수집기 DB 조회는 서버 수와 상관없이 한 번만 하고, 읽은 결과를 서버별 필터에 맞춰 나눠 보냅니다. 발송은 최대 10곳씩 동시에 처리합니다.

| 테이블 | 내용 |
|---|---|
| `guilds` | 서버별 설정 1행 (채널, 단계, 실종알림, 지진 기준) |
| `guild_regions` | 서버별 지역 키워드 (없으면 전국) |
| `guild_mentions` | 서버별 멘션 역할 |
| `deliveries` | 서버별 발송 기록, 30일 보관 (대시보드 '최근 공지') |
| `seen` / `cursors` | 재난문자 SN 중복 제거와 수집기 테이블별 마지막 id (모든 서버 공통) |

- 봇이 서버에서 추방되면 그 서버의 설정과 기록이 모두 삭제됩니다 (`ON DELETE CASCADE`).
- 공지 채널이 삭제되면 그 서버의 공지가 자동으로 꺼집니다.
- 스키마 버전은 `PRAGMA user_version`으로 관리합니다. 바꿀 때는 `storage.py`의 `MIGRATIONS`에 항목을 추가하세요.

## 준비

1. **Discord 봇**: Developer Portal에서 앱 생성 → Bot 토큰 발급. 권한 없는 기본 Intent만 사용합니다.
   초대 URL scope: `bot applications.commands`, 권한: Send Messages, Embed Links, Manage Messages(공지 publish용), Mention Everyone(역할 멘션이 "멘션 허용" 꺼진 역할이면 필요).
2. `.env.example`을 `.env`로 복사해 값 입력. 공공 API 키는 필요 없습니다 (수집기 `.env`에만 둡니다).
3. 같은 NAS에서 `safety-collector`가 돌고 있어야 합니다. 봇이 다른 기계에 있으면 이 방식은 쓸 수 없습니다 (네트워크 공유 폴더 위 SQLite는 잠금이 깨짐).

## Synology NAS 배포 (Docker Hub 이미지)

이미지: `quesogato3924/disaster-alert-bot` (linux/amd64). 태그는 `latest`와 버전(`1.0.0`)이 있습니다.

1. File Station에서 `docker/disaster-alert-bot` 폴더를 만들고 다음 두 파일을 올립니다.
   - `deploy/synology/docker-compose.yml`
   - `deploy/synology/.env.example` → 이름을 `.env`로 바꾸고 값 입력 (`DISCORD_TOKEN`, `PUID`/`PGID`)
   - `PUID`/`PGID`는 `safety-collector/.env`와 같게 맞춥니다. 수집기 DB(`/volume1/docker/safety-collector/data`)는 `660`이라 같은 계정(또는 그룹)이어야 읽을 수 있습니다.
   - 봇 컨테이너가 이 계정으로 돌기 때문에 봇의 `data/` 폴더도 그 계정 소유여야 합니다: `sudo chown -R <PUID>:<PGID> /volume1/docker/disaster-alert-bot/data`
   - 수집기 폴더는 `:ro`로 붙이지 않습니다 (WAL 모드는 읽기만 해도 `safety.db-shm`에 씀). 쓰기 방지는 코드의 `mode=ro`가 맡습니다.
2. **Container Manager → 프로젝트 → 생성** → 경로에 위 폴더 선택 → "기존 docker-compose.yml 사용" → 완료(시작).
3. 컨테이너 로그에 `로그인: 재난알림봇#…`이 보이면 정상입니다. `data/` 폴더는 자동으로 생깁니다.

**업데이트** (PC에서 새 이미지를 push한 뒤):
- SSH (가장 확실): `cd /volume1/docker/disaster-alert-bot && sudo docker compose pull && sudo docker compose up -d`
- Container Manager UI: **이미지** 탭에서 `quesogato3924/disaster-alert-bot`에 '업데이트 가능'이 표시되면 업데이트한 뒤, 프로젝트를 중지했다가 다시 시작합니다.
- 버전을 고정하고 싶으면 yml의 `:latest` 대신 `:1.0.0`처럼 태그를 지정하세요. 업데이트할 때 태그만 바꾸면 되므로 어떤 버전이 돌고 있는지 분명해집니다.

**재난문자 / PEWS**: 재난문자는 기본으로 켜져 있습니다 (`DISASTER_ENABLED=false`로 끔). PEWS 실시간 감지로 지진을 먼저 공지하려면 `PEWS_ENABLED=true`를 넣고 컨테이너를 **다시 만드세요**.
- `.env`는 컨테이너를 **생성할 때만** 읽힙니다. Container Manager의 '재시작'이나 `docker restart`로는 반영되지 않으니 `sudo docker compose up -d --force-recreate`를 쓰세요.
- 시작 로그에 `공용 수집기 DB(읽기 전용): /data/safety.db`, `커서 시작값 …`, `로그인: … (v1.3.0, 재난문자 켜짐|꺼짐)`이 출력됩니다. 버전 표시가 다르면 예전 이미지가 돌고 있는 것입니다.

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
  eqk_commands.py       /지진정보 수동 공지
  disaster_commands.py  /재난문자 수동 공지
  pollers.py    수집기 DB id 커서 조회 루프 + 수집기 heartbeat 감시
  safetydb.py   수집기 DB 읽기 전용 조회, 행 → 모델 변환
  dispatcher.py 지진 이벤트 묶기, 서버별 필터, 발송/수정
  render.py     Embed 생성
  storage.py    SQLite
  models.py     데이터 모델 (기상청 통보종류 코드 포함)
```
