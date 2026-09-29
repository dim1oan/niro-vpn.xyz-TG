.PHONY: install run seed migrate upgrade test lint format docker-up docker-down

install:
	pip install -r requirements.txt -r requirements-dev.txt

run:
	python -m app

seed:
	python -m app.cli seed_server
	python -m app.cli seed_plans

upgrade:
	alembic upgrade head

migrate:
	alembic revision --autogenerate -m "$(m)"

test:
	pytest -q

lint:
	ruff check app tests
	mypy app

format:
	ruff format app tests

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

alembic-upgrade-docker:
	docker compose exec bot alembic upgrade head
