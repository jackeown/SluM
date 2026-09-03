SHELL := /usr/bin/env bash
.DEFAULT_GOAL := all

.PHONY: all build clean distclean monitor submit sync

RUNSOLVER := ../../runsolver-build/runsolver
JOBS ?= $(shell nproc)

all: build
	@rm -f -- submit.sh
	@rm -rf -- submit.sh.files
	@set -eu; \
	solver_dir=$$(mktemp -d); \
	trap 'rm -rf -- "$$solver_dir"' EXIT INT TERM; \
	printf '%s\n' '$(abspath ../example-vampire/bin)' \
		'./vampire --mode casc --cores 1 --time_limit {{cpu-limit}} {{problem}}' \
		> "$$solver_dir/vampire.solver"; \
	printf '%s\n' '$(abspath ../example-e/bin)' \
		'./eprover --auto-schedule --proof-object --cpu-limit={{cpu-limit}} --memory-limit={{mem-limit}} {{problem}}' \
		> "$$solver_dir/e.solver"; \
	printf '%s\n' '$(abspath ../example-drodi/bin)' \
		"./drodi '-cores(1)' '-timeout({{cpu-limit}})' {{problem}}" \
		> "$$solver_dir/drodi.solver"; \
	python ../../slum.py \
		--cpu-limit 3 \
		--wc-limit 6 \
		--mem-limit 1GiB \
		--cpu-request 1-core \
		--memory-request 1200MiB \
		--problems '$(CURDIR)/problems/**/*.p' \
		--solver "$$solver_dir/vampire.solver" \
		--solver "$$solver_dir/e.solver" \
		--solver "$$solver_dir/drodi.solver" \
		--runsolver '$(abspath $(RUNSOLVER))' \
		--batch-size 5 \
		--max-parallel 6 \
		--worker-overhead 60 \
		--job-name SluM-combined-example \
		--sbatch-option=--partition=CPU-amd \
		--output '$(CURDIR)/submit.sh'
	@printf '\n\033[1;32m✅ Generated one submission with 30 calls: 3 provers × 10 problems.\033[0m\n'
	@printf '   Every call has a 3-second CPU limit and a 6-second wall-clock limit.\n'
	@printf '   Inspect submit.sh and submit.sh.files/ if you want to review it.\n'
	@printf '\033[1;36m👉 Next: run "make submit" to copy it to slurmy and submit it.\033[0m\n'

build:
	@printf '\033[1;34m🔨 Building Vampire, E, Drodi, and runsolver...\033[0m\n'
	@$(MAKE) -C ../example-vampire build JOBS='$(JOBS)'
	@$(MAKE) -C ../example-e build JOBS='$(JOBS)'
	@$(MAKE) -C ../example-drodi build
	@printf '\033[1;32m✅ All three provers and runsolver are ready.\033[0m\n'

submit:
	@if [ ! -x submit.sh ] || [ ! -d submit.sh.files ]; then \
		printf '❌ Nothing is ready to submit. Run "make" first.\n' >&2; \
		exit 1; \
	fi
	@printf '\033[1;34m🚀 Copying all three provers and ten problems to slurmy, then submitting 30 calls...\033[0m\n'
	@printf '   This returns after submission; it does not wait for the calls to finish.\n\n'
	@./submit.sh
	@printf '\n\033[1;32m✅ Submitted successfully.\033[0m\n'
	@printf '\033[1;36m👉 Next: run "make sync" to download results until the job finishes.\033[0m\n'

sync:
	@printf '\033[1;34m🔄 Syncing the latest SluM job until every call has a final result...\033[0m\n'
	@cd ../.. && python ./slum-sync.py $(SLUM_ID) --follow --interval 2
	@printf '\n\033[1;32m✅ Results are synchronized under ../../slum-results/.\033[0m\n'

monitor:
	@printf '\033[1;34m📊 Opening the SluM dashboard for slurmy...\033[0m\n'
	@printf '   It shows every current and past SluM job; press Q to leave it.\n\n'
	@python ../../slum-monitor.py --host slurmy
	@printf '\n👋 Closed the SluM dashboard.\n'

clean:
	@rm -f -- submit.sh
	@rm -rf -- submit.sh.files
	@printf '\033[1;32m🧹 Removed the combined example submission files.\033[0m\n'
	@printf '\033[1;36m👉 Next: run "make" to generate a fresh submission.\033[0m\n'

distclean: clean
	@$(MAKE) -C ../example-vampire distclean
	@$(MAKE) -C ../example-e distclean
	@$(MAKE) -C ../example-drodi distclean
	@printf '\033[1;32m🧹 Also removed all three downloaded source trees and prover binaries.\033[0m\n'
