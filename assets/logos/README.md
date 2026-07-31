# 구단 로고

파일명을 `config/brand.yml` 의 `teams:` 키와 정확히 일치시켜 주세요. 확장자는 `.png`.

```
assets/logos/
├─ 삼성.png
├─ LG.png
├─ KT.png
├─ KIA.png
├─ 두산.png
├─ 한화.png
├─ NC.png
├─ 롯데.png
├─ SSG.png
└─ 키움.png
```

권장 규격: 정사각형 투명 배경 PNG, 256×256 이상.

파일이 없으면 렌더러가 자동으로 팀 컬러 스와치(색 막대)로 대체하므로 발행은 멈추지 않습니다.
로고를 아예 쓰지 않으려면 `config/brand.yml` 의 `logos.enabled: false`.
