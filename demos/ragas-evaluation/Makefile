.PHONY: deploy-all delete-all deploy-qwen3-14b-awq delete-qwen3-14b-awq \
deploy-lsd-server delete-lsd-server deploy-configs delete-configs

NAMESPACE := ragas-evaluation
KUBECTL := kubectl

ifneq ($(shell which oc),)
	KUBECTL := oc
endif

deploy-all:
	$(KUBECTL) create namespace $(NAMESPACE) || true
	$(MAKE) deploy-configs
	$(MAKE) deploy-qwen3-14b-awq
	$(KUBECTL) wait --for=condition=available --timeout=1200s deployment --all -n $(NAMESPACE) || true
	$(MAKE) deploy-lsd-server
	$(KUBECTL) wait --for=condition=available --timeout=300s deployment --all -n $(NAMESPACE) || true

delete-all: delete-lsd-server delete-qwen3-14b-awq delete-configs
	$(KUBECTL) delete namespace $(NAMESPACE) || true

deploy-configs:
	$(KUBECTL) apply -f deployment-yamls/aws-credentials.yaml -n $(NAMESPACE)
	$(KUBECTL) apply -f deployment-yamls/kubeflow-ragas-config.yaml -n $(NAMESPACE)

delete-configs:
	$(KUBECTL) delete -f deployment-yamls/kubeflow-ragas-config.yaml -n $(NAMESPACE) || true
	$(KUBECTL) delete -f deployment-yamls/aws-credentials.yaml -n $(NAMESPACE) || true

deploy-qwen3-14b-awq:
	$(KUBECTL) apply -f deployment-yamls/qwen3-14b-awq-deployment.yaml -n $(NAMESPACE)

delete-qwen3-14b-awq:
	$(KUBECTL) delete -f deployment-yamls/qwen3-14b-awq-deployment.yaml -n $(NAMESPACE) || true

deploy-lsd-server:
	$(KUBECTL) apply -f deployment-yamls/run.yaml -n $(NAMESPACE)
	$(KUBECTL) apply -f deployment-yamls/llama-stack-distribution.yaml -n $(NAMESPACE)

delete-lsd-server:
	$(KUBECTL) delete -f deployment-yamls/llama-stack-distribution.yaml -n $(NAMESPACE) || true
	$(KUBECTL) delete -f deployment-yamls/run.yaml -n $(NAMESPACE) || true
