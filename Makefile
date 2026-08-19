.PHONY: build rebuild gpu check-offline

# up -d --build 하나가 빌드+기동을 다 한다. mem_limit이 바뀌면 컨테이너도 재생성된다.
# Dockerfile이 app/·static/을 COPY하므로 코드가 바뀌면 이미지 해시가 바뀌어 컨테이너도
# 자동으로 재생성된다 — --force-recreate 불필요.
build:
	git pull
	docker compose up -d --build

rebuild: build

# GPU 호스트(3060 Ti). 오버레이를 얹어 worker에만 카드를 붙인다.
# 실제로 붙었는지는 `docker compose logs worker | grep device=` 로 확인한다.
gpu:
	git pull
	docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build

# 이미지가 자족적인지(인터넷 없이 변환되는지) 확인한다. 한때 빌드 타임에 받아둔
# 1.3GB 모델을 런타임이 쳐다보지도 않고 잡마다 HF를 다시 쳤다 — 망이 막히거나
# 사내 프록시가 TLS를 가로채는 곳에서는 모든 변환이 실패했다. --network none이
# 그걸 잡아내는 유일한 방법이다(망 있는 개발기에서는 조용히 통과해버린다).
check-offline:
	docker run --rm --network none -v "$(CURDIR)/tests:/t:ro" -w /srv pdf2md:latest \
	  python -c "from app import convert; import tempfile, pathlib; \
	    d = pathlib.Path(tempfile.mkdtemp()); convert.convert('/t/fixtures/sample.pdf', d, include_images=True, include_tables_csv=True); \
	    assert (d / 'doc.md').exists(); print('OK: 인터넷 없이 변환됨')"
