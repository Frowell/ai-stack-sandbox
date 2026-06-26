.PHONY: up down ingest ask eval eval-baseline test logs psql shell

up:        ## build + start the whole stack (run from host, or in-container via docker-outside-of-docker)
	docker compose up -d --build

down:      ## stop the stack and drop volumes
	docker compose down -v

ingest:    ## embed the sample corpus into pgvector
	uv run python -m app.ingest

ask:       ## ask a question:  make ask Q="why put a gateway in the hot path?"
	uv run python -m app.agent $(Q)

eval:      ## run the eval gate (non-zero exit on regression)
	uv run python -m app.evals

eval-baseline: ## record current scores as the regression baseline (after a vetted promotion)
	uv run python -m app.evals --baseline

test:      ## run the eval gate via pytest (merge gate)
	uv run pytest -q

logs:      ## tail the gateway logs
	docker compose logs -f litellm

psql:      ## open a psql shell on the vector store
	docker compose exec postgres psql -U postgres -d sandbox

shell:     ## shell into the app container
	docker compose exec app bash
