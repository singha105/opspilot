SHELL := /bin/bash
.DEFAULT_GOAL := help

CLUSTER   := opspilot
CTX       := k3d-$(CLUSTER)
NS        := shop
IMAGE     := opspilot-demo-svc:dev
BROKEN_IMAGE := opspilot-demo-svc:v2-broken
MODEL     ?= qwen3:4b
COMPOSE   := docker compose -f infra/docker-compose.yml
KUBECTL   := kubectl --context $(CTX)
DEPLOYS   := redis payments-api orders-api inventory-api

.PHONY: help setup fmt lint typecheck test test-integration infra-up infra-down \
	cluster-up cluster-down demo-build demo-deploy demo-status rbac-apply kubeconfigs \
	fault-list fault-inject fault-reset fault-status down-all mem

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

## ---- Development -------------------------------------------------------

setup: ## Install deps (uv sync) and the pre-commit hook
	uv sync
	uv run pre-commit install --hook-type pre-commit

fmt: ## Format and auto-fix lint
	uv run ruff format .
	uv run ruff check --fix .

lint: ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

typecheck: ## mypy (strict on src/opspilot)
	uv run mypy

test: ## Unit tests with coverage (no cluster, Ollama or network)
	uv run pytest -m "not integration and not e2e" --cov --cov-report=term

test-integration: ## Integration tests (needs cluster-up, demo-deploy, rbac-apply)
	uv run pytest -m integration -v

## ---- Local infrastructure ----------------------------------------------

infra-up: ## Start Weaviate and wait until healthy
	$(COMPOSE) up -d --wait weaviate

infra-down: ## Stop all compose services (data volume is kept)
	$(COMPOSE) --profile tracing down

cluster-up: ## Create or start the k3d cluster and wait for the node
	@if ! k3d cluster list $(CLUSTER) >/dev/null 2>&1; then \
		k3d cluster create --config infra/k3d/cluster.yaml; \
	else \
		k3d cluster start $(CLUSTER); \
	fi
	$(KUBECTL) wait --for=condition=Ready node --all --timeout=120s

cluster-down: ## Delete the k3d cluster
	@if k3d cluster list $(CLUSTER) >/dev/null 2>&1; then k3d cluster delete $(CLUSTER); else echo "cluster $(CLUSTER) not found"; fi

## ---- Demo app (Shopfront) ----------------------------------------------

demo-build: ## Build the demo images (stable + v2-broken) and import them into k3d
	docker build -t $(IMAGE) demo/app
	docker build --build-arg BUILD=v2-broken -t $(BROKEN_IMAGE) demo/app
	k3d image import $(IMAGE) $(BROKEN_IMAGE) -c $(CLUSTER)

demo-deploy: ## Apply the demo manifests and wait for every rollout
	$(KUBECTL) apply -k demo/k8s/base
	@for d in $(DEPLOYS); do $(KUBECTL) -n $(NS) rollout status deploy/$$d --timeout=180s || exit 1; done

demo-status: ## Show demo pods and services
	$(KUBECTL) -n $(NS) get deploy,pods,svc -o wide

rbac-apply: ## Apply the reader/operator RBAC and write their kubeconfigs
	$(KUBECTL) apply -f infra/k8s/rbac/reader.yaml -f infra/k8s/rbac/operator.yaml
	$(MAKE) kubeconfigs

kubeconfigs: ## (Re)generate short-lived kubeconfigs in .secrets/
	scripts/make_kubeconfig.sh opspilot-reader
	scripts/make_kubeconfig.sh opspilot-operator

## ---- Fault injection -----------------------------------------------------

fault-list: ## List fault scenarios
	uv run opspilot faults list

fault-inject: ## Inject a fault: make fault-inject ID=<scenario>
	@test -n "$(ID)" || (echo "usage: make fault-inject ID=<scenario>" && exit 1)
	uv run opspilot faults inject $(ID)

fault-reset: ## Reset a fault: make fault-reset ID=<scenario>
	@test -n "$(ID)" || (echo "usage: make fault-reset ID=<scenario>" && exit 1)
	uv run opspilot faults reset $(ID)

fault-status: ## Show the health of the demo namespace
	uv run opspilot faults status

## ---- Housekeeping --------------------------------------------------------

down-all: ## Stop the cluster, compose services and the Ollama model
	-@if k3d cluster list $(CLUSTER) >/dev/null 2>&1; then k3d cluster stop $(CLUSTER); fi
	-$(COMPOSE) --profile tracing down
	-@ollama stop $(MODEL) 2>/dev/null || true

mem: ## Memory used by containers and Ollama
	@docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}' 2>/dev/null || echo "docker not running"
	@echo; ollama ps 2>/dev/null || echo "ollama not running"
	@echo; memory_pressure 2>/dev/null | tail -1 || true
