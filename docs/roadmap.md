# Implementation Roadmap

The project was built in small, reviewable increments. Each phase ended with working code and
passing tests.

| Phase | Scope | Deliverables | Status |
|---|---|---|---|
| 0 | Architecture & repository design | `docs/architecture.md`, repo skeleton, tooling config | ✅ done |
| 1 | Shared contract + vehicle simulator | DBC, Pydantic schema, topic helpers, DTC codec, JSON logging; simulator (model, driving cycle, faults, CAN encoder, runner) | ✅ done |
| 2 | Edge gateway | CAN decoder, signal cache with staleness, validation, enrichment, MQTT with reconnect/backoff, SQLite store-and-forward, retry, last will | ✅ done |
| 3 | Containers & local stack | Multi-stage non-root Dockerfiles, Compose with Mosquitto, `.env.example` | ✅ written; images not built on the dev machine (built in CI) |
| 4 | Cloud processing | Lambda (validate → transform → idempotent write, DLQ), local MQTT bridge, DynamoDB Local; moto tests | ✅ done |
| 5 | REST API | FastAPI, repository pattern, cursor pagination, error model, Lambda adapter | ✅ done |
| 6 | Dashboard | Vanilla-JS dashboard (fleet, cards, SVG charts, track plot), nginx image; browser-checked | ✅ done |
| 7 | Infrastructure as code | CDK: IoT thing/policy/rule, Lambdas, DynamoDB, SQS DLQ, API Gateway, CloudFront dashboard, alarms, CloudWatch dashboard; Lambda packaging script; 34 template tests | ✅ synthesized and tested; not deployed to a real account |
| 8 | CI | GitHub Actions: lint, format, mypy, tests (3.11/3.12), dashboard tests, Docker image builds, compose validation, CDK synth | ✅ written and YAML-validated; first run happens on GitHub |
| 9 | Documentation | Final README, interview notes | ✅ done |

See the README for known limitations and future improvements.