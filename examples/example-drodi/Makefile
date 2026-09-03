SHELL := /usr/bin/env bash
.DEFAULT_GOAL := all

.PHONY: all build clean distclean monitor submit

DRODI_VERSION := 4.1.1
DRODI_URL := https://tptp.org/CASC/J13/SystemSources/Drodi---$(DRODI_VERSION).tgz
DRODI_SHA256 := 4967b522235df03e2b3e5a35e5284aa742cbfd6875b0f0a0c904ee8e0d3220ce
SOURCE := source
DRODI := bin/drodi
RUNSOLVER := ../../runsolver-build/runsolver

all: build
	@rm -f -- submit.sh
	@rm -rf -- submit.sh.files
	@set -eu; \
	solver_file=$$(mktemp); \
	trap 'rm -f -- "$$solver_file"' EXIT INT TERM; \
	printf '%s\n' '$(CURDIR)/bin' \
		'./drodi -cores(1) -timeout({{cpu-limit}}) {{problem}}' \
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
		--job-name SluM-drodi-example \
		--sbatch-option=--partition=CPU-amd \
		--output '$(CURDIR)/submit.sh'
	@printf '\n\033[1;32m✅ Built Drodi and generated its SluM submission.\033[0m\n'
	@printf '   Inspect submit.sh and submit.sh.files/ if you want to review it.\n'
	@printf '\033[1;36m👉 Next: run "make submit" to copy it to slurmy and submit it.\033[0m\n'

build: $(DRODI) $(RUNSOLVER)
	@printf '\033[1;32m✅ Drodi and runsolver are ready.\033[0m\n'

$(SOURCE)/.unpacked:
	@printf '\033[1;34m⬇️  Downloading the official Drodi %s source archive...\033[0m\n' '$(DRODI_VERSION)'
	@set -eu; \
	archive=$$(mktemp); \
	trap 'rm -f -- "$$archive"' EXIT INT TERM; \
	curl --fail --location --retry 2 --output "$$archive" '$(DRODI_URL)'; \
	printf '%s  %s\n' '$(DRODI_SHA256)' "$$archive" | sha256sum --check --status; \
	mkdir -p -- '$(SOURCE)'; \
	tar -xzf "$$archive" --strip-components=1 -C '$(SOURCE)'; \
	touch '$@'
	@printf '\033[1;32m✅ Downloaded and verified Drodi %s.\033[0m\n' '$(DRODI_VERSION)'

$(DRODI): $(SOURCE)/.unpacked
	@printf '\033[1;34m🔨 Building Drodi...\033[0m\n'
	mkdir -p -- '$(dir $@)'
	gcc -O3 -DNDEBUG -std=gnu11 -o '$@' '$(SOURCE)'/*.c -lm -pthread
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
	@printf '\033[1;32m🧹 Also removed the downloaded source and built Drodi binary.\033[0m\n'
	@printf '\033[1;36m👉 Next: run "make" to download, build, and generate everything again.\033[0m\n'
