.PHONY: install lint format typecheck test check up down logs watch dashboard-test build-lambdas synth

install:
	pip install -r requirements-dev.txt

lint:
	ruff check .
	ruff format --check .

format:
	ruff check --fix .
	ruff format .

typecheck:
	mypy packages/telemetry-common/src services/simulator/src services/edge_gateway/src cloud/lambdas/telemetry_processor/src services/api/src infra/cdk/vehicle_telemetry_cdk infra/cdk/app.py infra/cdk/scripts

test:
	pytest --cov --cov-report=term-missing

check: lint typecheck test

# --- local stack (Docker) ---
up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f gateway simulator

watch: # live MQTT traffic as seen by the broker
	docker compose exec mosquitto mosquitto_sub -t 'vehicles/#' -v

# --- dashboard / AWS ---
dashboard-test:
	node --test dashboard/tests/lib.test.mjs

build-lambdas:
	cd infra/cdk && python scripts/build_lambdas.py

synth: build-lambdas
	cd infra/cdk && cdk synth -c environment=dev --quiet
