SHELL := /bin/bash
.DEFAULT_GOAL := help

ROOT := $(CURDIR)
PYTHON ?= python3
VERSION ?= latest
FORMATS ?= tar.gz,deb
DIST ?= dist
JOBS ?= 0
UPSTREAM ?=
UPSTREAM_DIR ?=
ELECTRON_ARCHIVE ?=
RIPGREP ?=
OS_PROXY_SRC ?=
PREBUILT ?=
PREBUILT_BUNDLE ?=
PREBUILT_ARGS ?=
EXTRA_ARGS ?=

export PYTHONPATH := $(ROOT)/src$(if $(PYTHONPATH),:$(PYTHONPATH))

.PHONY: help bootstrap check test build package deb bundle deb-from-bundle publish clean

help:
	@printf '%s\n' \
	  'make bootstrap                         install the Python package and development tools' \
	  'make check                             syntax and unit checks' \
	  'make build [VERSION=1.148.0]           build and package tar.gz + deb' \
	  'make deb [VERSION=1.148.0]             build only the Debian package' \
	  'make package STAGE=work/build/stage VERSION=1.148.0' \
	  'make publish                           upload dist/manifest.json artifacts with gh' \
	  ''\
	  'helper options (pass to build/deb):' \
	  '  PREBUILT=/srv/prebuilts               reuse prebuilt helpers from a directory' \
	  '  PREBUILT_ARGS=--no-prebuilt           compile every helper from source' \
	  '  PREBUILT_ARGS=--no-helpers            keep the upstream x64 helpers (broken)' \
	  '  PREBUILT_ARGS=--no-dotnet-download    never download the LoongArch .NET SDK' \
	  '  PREBUILT_BUNDLE=dist/loong64-prebuilts-*.tar.gz' \
	  '                                        build on x64 from a bundle (no compilers)' \
	  ''\
	  'prebuilt bundles (for runners without a LoongArch CPU):' \
	  '  make bundle VERSION=1.148.0           emit the bundle on a LoongArch host' \
	  '  make deb-from-bundle BUNDLE=...       package on any x86-64 runner' \
	  'make clean                             remove work/, dist/ and test caches'

bootstrap:
	$(PYTHON) -m pip install -e '.[dev]'

check:
	$(PYTHON) -m compileall -q src
	$(PYTHON) -m pytest -q

test: check

build:
	$(PYTHON) -m vscode_loong64.cli build "$(VERSION)" \
	  --formats "$(FORMATS)" \
	  --jobs "$(JOBS)" \
	  $(if $(UPSTREAM),--upstream "$(UPSTREAM)",) \
	  $(if $(UPSTREAM_DIR),--upstream-dir "$(UPSTREAM_DIR)",) \
	  $(if $(ELECTRON_ARCHIVE),--electron-archive "$(ELECTRON_ARCHIVE)",) \
	  $(if $(RIPGREP),--ripgrep "$(RIPGREP)",) \
	  $(if $(OS_PROXY_SRC),--os-proxy-src "$(OS_PROXY_SRC)",) \
	  $(if $(PREBUILT),--prebuilt "$(PREBUILT)",) \
	  $(if $(PREBUILT_BUNDLE),--prebuilt-bundle "$(PREBUILT_BUNDLE)",) \
	  $(PREBUILT_ARGS) \
	  $(EXTRA_ARGS)

deb:
	$(MAKE) build VERSION="$(VERSION)" FORMATS=deb

# the payload bundle is what an x86-64 packaging host consumes; no artifacts
# are produced here, so the LoongArch runner spends its time compiling only
bundle:
	$(PYTHON) -m vscode_loong64.cli build "$(VERSION)" \
	  --formats "" \
	  --emit-prebuilt-bundle \
	  --no-smoke-test \
	  --jobs "$(JOBS)" \
	  $(if $(UPSTREAM),--upstream "$(UPSTREAM)",) \
	  $(if $(UPSTREAM_DIR),--upstream-dir "$(UPSTREAM_DIR)",) \
	  $(if $(ELECTRON_ARCHIVE),--electron-archive "$(ELECTRON_ARCHIVE)",) \
	  $(if $(RIPGREP),--ripgrep "$(RIPGREP)",) \
	  $(if $(OS_PROXY_SRC),--os-proxy-src "$(OS_PROXY_SRC)",) \
	  $(if $(PREBUILT),--prebuilt "$(PREBUILT)",) \
	  $(PREBUILT_ARGS) \
	  $(EXTRA_ARGS)

deb-from-bundle:
	@test -n "$(BUNDLE)" || (echo 'BUNDLE is required (dist/loong64-prebuilts-*.tar.gz)' >&2; exit 2)
	$(MAKE) deb VERSION="$(VERSION)" PREBUILT_BUNDLE="$(BUNDLE)"

package:
	@test -n "$(STAGE)" || (echo 'STAGE is required' >&2; exit 2)
	@test -n "$(VERSION)" || (echo 'VERSION is required' >&2; exit 2)
	$(PYTHON) -m vscode_loong64.cli package "$(STAGE)" "$(VERSION)" \
	  --formats "$(FORMATS)" --out "$(DIST)"

publish:
	$(PYTHON) -m vscode_loong64.cli publish --manifest "$(DIST)/manifest.json" \
	  $(if $(TAG),--tag "$(TAG)",) \
	  $(if $(REPO),--repo "$(REPO)",)

clean:
	rm -rf work dist .pytest_cache src/*.egg-info
	find src tests -type d -name __pycache__ -prune -exec rm -rf {} +
