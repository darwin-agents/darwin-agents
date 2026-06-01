# Darwin local infrastructure — one-stop targets over the envs/local
# Terraform module. See README.md for prerequisites.

ENV_DIR := envs/local
KUBECONFIG_PATH := $(abspath $(ENV_DIR)/.kubeconfig)
TF := terraform -chdir=$(ENV_DIR)
KUBECTL := KUBECONFIG=$(KUBECONFIG_PATH) kubectl

.PHONY: help up plan init apply down kubeconfig logs pods namespaces shell port-forward

help:  ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

init:  ## terraform init
	$(TF) init

plan: init  ## terraform plan
	$(TF) plan

up: init  ## Bring the whole local stack online (kind cluster + Helm releases + supervisor)
	$(TF) apply -auto-approve
	@echo
	@echo "✓ infra is up. Run:  eval \$$(make -C infra kubeconfig)"
	@echo "  Then:              kubectl get pods -A"

apply: up

down:  ## Tear down the local stack + delete the kind cluster
	$(TF) destroy -auto-approve
	@rm -f $(KUBECONFIG_PATH)

kubeconfig:  ## Print KUBECONFIG export pointing at the local kind cluster
	@if [ ! -f $(KUBECONFIG_PATH) ]; then \
		echo "no kubeconfig yet — run 'make up' first" >&2; exit 1; \
	fi
	@echo "export KUBECONFIG=$(KUBECONFIG_PATH)"

pods:  ## kubectl get pods -A
	$(KUBECTL) get pods -A

namespaces:  ## kubectl get namespaces
	$(KUBECTL) get namespaces

logs:  ## tail platform logs (SVC=darwin-platform | litellm | vault | ...)
	@SVC="$${SVC:-darwin-platform}" ; \
	NS=$$($(KUBECTL) get pods -A --no-headers | awk -v s="$$SVC" '$$2 ~ s {print $$1; exit}') ; \
	POD=$$($(KUBECTL) get pods -A --no-headers | awk -v s="$$SVC" '$$2 ~ s {print $$2; exit}') ; \
	if [ -z "$$POD" ]; then echo "no pod matched SVC=$$SVC" >&2; exit 1; fi ; \
	$(KUBECTL) logs -n $$NS $$POD -f

port-forward:  ## Forward platform API to localhost:8000 (PORT=8000 SVC=darwin-platform)
	@PORT="$${PORT:-8000}" ; \
	$(KUBECTL) -n darwin port-forward svc/darwin-platform $$PORT:8000
