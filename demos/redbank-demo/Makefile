.PHONY: deploy deploy-postgres delete delete-postgres deploy-mcp delete-mcp build-mcp

NAMESPACE := redbank-demo
KUBECTL := kubectl

ifneq ($(shell which oc),)
	KUBECTL := oc
endif

deploy: deploy-postgres deploy-mcp

delete: delete-mcp delete-postgres

deploy-postgres:
	$(KUBECTL) create namespace $(NAMESPACE) || true
	$(KUBECTL) apply -k postgres-db/ -n $(NAMESPACE)

delete-postgres:
	$(KUBECTL) delete -k postgres-db/ -n $(NAMESPACE)

deploy-mcp:
	cd mcp-server && ./deploy.sh

delete-mcp:
	$(KUBECTL) delete -f mcp-server/mcp-server.yaml -n $(NAMESPACE) || true
	$(KUBECTL) delete route redbank-mcp-server -n $(NAMESPACE) || true
	$(KUBECTL) delete build build-redbank-mcp-server -n $(NAMESPACE) || true
	$(KUBECTL) delete buildconfig build-redbank-mcp-server -n $(NAMESPACE) || true
