# ADR-0001: Local-first, zero-cost stack

- Status: accepted
- Date: 2026-10-05

## Context

OpsPilot is a portfolio project that anyone should be able to clone and run. It
has to work on an 8 GB Apple Silicon laptop, cost nothing to run, and give the
same results each time so that eval numbers can be compared across changes. Paid
LLM APIs and managed vector databases would make the project expensive to
reproduce and would tie results to a provider's model version.

## Decision

- Run the LLM locally with Ollama. The default model is `qwen3:4b` (about
  2.5 GB), used at temperature 0.
- Compute embeddings locally (fastembed, `bge-small-en-v1.5`) and store them in a
  self-hosted Weaviate container with all modules disabled.
- Run Kubernetes as a single-node k3d cluster with traefik, servicelb and
  metrics-server disabled.
- Start services only when a step needs them and stop them afterwards
  (`make down-all`). Tracing (Phoenix) sits behind a compose profile and never
  starts by default.
- Hosted options (Pinecone Starter, a hosted LLM) may be added later only behind
  environment flags. Everything must work without them.

## Alternatives considered

- **Hosted LLM APIs.** Stronger models, but they cost money per eval run, need
  keys, and their model versions change under you, which undermines reproducible
  evals.
- **kind or minikube.** Both work. k3d starts faster, its image import is simple,
  and k3s is lighter than a full kubeadm control plane.
- **Docker Desktop.** Supported, but OrbStack uses noticeably less memory on
  macOS, which matters at 8 GB.

## Consequences

- A 4B model will diagnose less well than a frontier model. The evals report its
  real numbers, and the provider-agnostic LLM factory leaves room to compare
  other models later.
- Memory is the binding constraint. Idle on Day 1, the k3d node uses about 640 MiB
  and the four demo services about 90 MiB. Weaviate idles around 50–140 MiB under
  a 700 MiB limit. Ollama needs about 3 GB while the model is loaded, so the model
  is unloaded whenever a session ends.
- Weaviate is published on host port 8090 because 8080 is commonly taken by other
  local servers.
