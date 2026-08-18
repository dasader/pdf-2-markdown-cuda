.PHONY: build rebuild gpu

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
