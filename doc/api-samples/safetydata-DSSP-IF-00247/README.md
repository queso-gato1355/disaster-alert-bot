# 재난문자 API (safetydata.go.kr DSSP-IF-00247) 실제 응답 예시

2026-11-18 18:4x KST 에 실제로 호출해서 받은 응답입니다. 인증키는 응답에 포함되지 않습니다.
파서(`bot/apis.py`)를 고칠 때 이 파일들로 오프라인 테스트할 수 있습니다.

공통 요청: `GET https://www.safetydata.go.kr/V2/api/DSSP-IF-00247?serviceKey=<키>&returnType=json&pageNo=1&...`

| 파일 | 추가 파라미터 | 내용 |
|---|---|---|
| `01_today_crtDt-20261118_rows50.json` | `numOfRows=50&crtDt=20261118` | 정상, 오늘 문자 15건 전체 (**순서가 SN·시각 순이 아님**) |
| `02_today_rows3.json` | `numOfRows=3&crtDt=20261118` | 정상, 3건 (첫 호출) |
| `03_crtDt-20261117_rows5.json` | `numOfRows=5&crtDt=20261117` | `crtDt` 는 '그 날짜 이후' → 총 26건, 오늘 문자(270101)도 섞여 나옴 |
| `04_no-crtDt_rows5.json` | `numOfRows=5` | `crtDt` 없으면 전체 63,986건, 2023년 문자부터 나옴 |
| `05_empty_crtDt-20991118.json` | `numOfRows=5&crtDt=20991118` | 데이터 없음: `resultCode "00"`, `totalCount 0`, `body []` |
| `06_error30_invalid-key.json` | `serviceKey=WRONGKEY` | 등록되지 않은 키: `resultCode "30"` |
| `07_error22_limit-exceeded.json` | `numOfRows=1000&crtDt=20261101` | 일일 한도 초과: `resultCode "22"`, `body null` (당시 키 한도가 1회/일이었음) |

참고: 같은 날 `rgnNm=서울특별시` 처럼 한글 파라미터를 URL 인코딩하지 않고 보내면 JSON 대신 HTML 오류 페이지가 왔습니다 (`rgnNm` 지원 여부는 확인 못 함).

## 실측 특성
- `SN` 은 숫자, 날짜는 `CRT_DT` `"2026/11/18 12:00:20"` / `REG_YMD` `"2026/11/18 12:10:10.000000000"` 형식
- `REG_YMD`(API 반영 시각)가 `CRT_DT`(발송 시각)보다 약 10분 늦음
- `RCPTN_RGN_NM` 은 `"충청남도 천안시 동남구 ,충청남도 천안시 서북구 "` 처럼 쉼표 구분 + 공백 섞임
- 전국 대상 문자는 `"전국"` 이 아니라 시·도 16개가 나열됨
- 이날 받은 문자는 모두 `EMRG_STEP_NM "안전안내"` (`EMRG_STEP_ID "4372"`). 위급재난·긴급재난 값은 아직 실제로 받아보지 못함
