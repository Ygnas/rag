.PHONY: deploy-postgres delete-postgres

NAMESPACE := redbank-demo
KUBECTL := kubectl

ifneq ($(shell which oc),)
	KUBECTL := oc
endif

deploy-postgres:
	$(KUBECTL) create namespace $(NAMESPACE) || true
	$(KUBECTL) apply -k postgres-db/ -n $(NAMESPACE)

delete-postgres:
	$(KUBECTL) delete -k postgres-db/ -n $(NAMESPACE)
