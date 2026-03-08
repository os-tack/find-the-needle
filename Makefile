# needle-bench Makefile
# Usage:
#   make validate              — validate all benchmarks
#   make validate BENCH=name   — validate one benchmark
#   make run BENCH=name        — build and run one benchmark
#   make list                  — list all benchmarks

BENCHMARKS_DIR := benchmarks
BENCH ?=

.PHONY: validate run list clean help

help: ## Show this help
	@echo "needle-bench — your worst debugging day, everyone's benchmark."
	@echo ""
	@echo "Usage:"
	@echo "  make validate              Validate all benchmarks"
	@echo "  make validate BENCH=name   Validate a single benchmark"
	@echo "  make run BENCH=name        Build and run a benchmark"
	@echo "  make list                  List all benchmarks"
	@echo "  make clean                 Remove build artifacts"
	@echo ""

validate: ## Validate benchmark structure
ifdef BENCH
	@echo "Validating $(BENCH)..."
	@$(MAKE) _validate_one DIR=$(BENCHMARKS_DIR)/$(BENCH)
else
	@echo "Validating all benchmarks..."
	@fail=0; \
	for dir in $(BENCHMARKS_DIR)/*/; do \
		name=$$(basename "$$dir"); \
		if [ "$$name" = "_template" ]; then continue; fi; \
		$(MAKE) _validate_one DIR="$$dir" || fail=1; \
	done; \
	if [ $$fail -eq 1 ]; then echo "VALIDATION FAILED"; exit 1; fi
	@echo "All benchmarks valid."
endif

_validate_one:
	@name=$$(basename "$(DIR)"); \
	errors=0; \
	for f in Dockerfile Agentfile test.sh; do \
		if [ ! -f "$(DIR)/$$f" ]; then \
			echo "  FAIL: $$name missing $$f"; \
			errors=1; \
		fi; \
	done; \
	if [ ! -f "$(DIR)/.bench/solution.patch" ]; then \
		echo "  FAIL: $$name missing .bench/solution.patch"; \
		errors=1; \
	fi; \
	if [ -f "$(DIR)/test.sh" ] && [ ! -x "$(DIR)/test.sh" ]; then \
		echo "  FAIL: $$name test.sh is not executable"; \
		errors=1; \
	fi; \
	if [ -f "$(DIR)/Agentfile" ]; then \
		grep -q '^FROM ' "$(DIR)/Agentfile" || { echo "  FAIL: $$name Agentfile missing FROM"; errors=1; }; \
		grep -q '^TOOL ' "$(DIR)/Agentfile" || { echo "  FAIL: $$name Agentfile missing TOOL"; errors=1; }; \
		grep -q '^LIMIT ' "$(DIR)/Agentfile" || { echo "  FAIL: $$name Agentfile missing LIMIT"; errors=1; }; \
	fi; \
	if [ $$errors -eq 0 ]; then echo "  OK: $$name"; else exit 1; fi

run: ## Build and run a benchmark
ifndef BENCH
	$(error BENCH is required. Usage: make run BENCH=name)
endif
	@echo "Building $(BENCH)..."
	docker build -t needle-bench-$(BENCH) $(BENCHMARKS_DIR)/$(BENCH)
	@echo ""
	@echo "Running test (before agent)..."
	docker run --rm needle-bench-$(BENCH) /workspace/test.sh; \
	rc=$$?; \
	if [ $$rc -eq 0 ]; then \
		echo "WARNING: test.sh passes without fix — benchmark may be broken"; \
	else \
		echo "test.sh exits $$rc (expected — bug is present)"; \
	fi
	@echo ""
	@echo "Benchmark $(BENCH) is ready for agent evaluation."

list: ## List all benchmarks
	@echo "Available benchmarks:"
	@for dir in $(BENCHMARKS_DIR)/*/; do \
		name=$$(basename "$$dir"); \
		if [ "$$name" = "_template" ]; then continue; fi; \
		echo "  $$name"; \
	done

clean: ## Remove build artifacts
	@echo "Cleaning up..."
	@docker images --filter "reference=needle-bench-*" -q 2>/dev/null | xargs -r docker rmi || true
	@echo "Done."
