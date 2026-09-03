SHELL := /usr/bin/env bash
.DEFAULT_GOAL := all

.PHONY: all build clean distclean monitor submit

SOURCE := source
EPROVER := bin/eprover
RUNSOLVER := ../../runsolver-build/runsolver
JOBS ?= $(shell nproc)

all: build
	@rm -f -- submit.sh
	@rm -rf -- submit.sh.files
	@set -eu; \
	solver_file=$$(mktemp); \
	trap 'rm -f -- "$$solver_file"' EXIT INT TERM; \
	printf '%s\n' '$(CURDIR)/bin' \
		'./eprover --auto-schedule --proof-object --cpu-limit={{cpu-limit}} --memory-limit={{mem-limit}} {{problem}}' \
		> "$$solver_file"; \
	python ../../slum.py \
		--cpu-limit 8 \
		--wc-limit 12 \
		--mem-limit 1GiB \
		--cpu-request 1-core \
		--memory-request 1200MiB \
		--problems '$(CURDIR)/problems/*.p' \
		--solver "$$solver_file" \
		--runsolver '$(abspath $(RUNSOLVER))' \
		--batch-size 2 \
		--max-parallel 2 \
		--worker-overhead 60 \
		--job-name SluM-e-example \
		--sbatch-option=--partition=CPU-amd \
		--output '$(CURDIR)/submit.sh'
	@printf '\n\033[1;32m✅ Built E and generated its SluM submission.\033[0m\n'
	@printf '   Inspect submit.sh and submit.sh.files/ if you want to review it.\n'
	@printf '\033[1;36m👉 Next: run "make submit" to copy it to slurmy and submit it.\033[0m\n'

build: $(EPROVER) $(RUNSOLVER)
	@printf '\033[1;32m✅ E and runsolver are ready.\033[0m\n'

$(SOURCE)/.git:
	@printf '\033[1;34m⬇️  Cloning the latest E source...\033[0m\n'
	@GIT_TERMINAL_PROMPT=0 git clone --depth 1 https://github.com/eprover/eprover.git '$(SOURCE)' || { \
		rmdir '$(SOURCE)' 2>/dev/null || true; \
		printf 'HTTPS cloning was unavailable; retrying over SSH.\n'; \
		git clone --depth 1 git@github.com:eprover/eprover.git '$(SOURCE)'; \
	}

$(EPROVER): $(SOURCE)/.git
	@printf '\033[1;34m🔨 Building E...\033[0m\n'
	cd '$(SOURCE)' && ./configure
	$(MAKE) -C '$(SOURCE)' -j'$(JOBS)' rebuild
	mkdir -p -- '$(dir $@)'
	install -m 0755 -- '$(SOURCE)/PROVER/eprover' '$@'
	@printf '\033[1;32m✅ Built %s.\033[0m\n' '$(abspath $@)'

$(RUNSOLVER):
	../../runsolver-build/build.sh '$@'
	@printf '\033[1;32m✅ Built runsolver at %s.\033[0m\n' '$(abspath $@)'

submit:
	@if [ ! -x submit.sh ] || [ ! -d submit.sh.files ]; then \
		printf '❌ Nothing is ready to submit. Run "make" first.\n' >&2; \
		exit 1; \
	fi
	@printf '\033[1;34m🚀 Copying the prepared files to slurmy and submitting the Slurm job array...\033[0m\n'
	@printf '   This returns after submission; it does not wait for the solver calls to finish.\n\n'
	@./submit.sh
	@printf '\n\033[1;32m✅ Submitted successfully.\033[0m\n'
	@printf '\033[1;36m👉 Next: run "make monitor" to watch its progress.\033[0m\n'

monitor:
	@printf '\033[1;34m📊 Opening the SluM dashboard for slurmy...\033[0m\n'
	@printf '   It shows every current and past SluM job; press Q to leave it.\n\n'
	@python ../../slum-monitor.py --host slurmy
	@printf '\n👋 Closed the SluM dashboard.\n'

clean:
	@rm -f -- submit.sh
	@rm -rf -- submit.sh.files
	@printf '\033[1;32m🧹 Removed the generated submission files.\033[0m\n'
	@printf '\033[1;36m👉 Next: run "make" to generate a fresh submission.\033[0m\n'

distclean: clean
	@rm -rf -- '$(SOURCE)' bin
	@printf '\033[1;32m🧹 Also removed the cloned source and built E binary.\033[0m\n'
	@printf '\033[1;36m👉 Next: run "make" to clone, build, and generate everything again.\033[0m\n'
