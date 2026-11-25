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
EXTRA_ARGS ?=

export PYTHONPATH := $(ROOT)/src$(if $(PYTHONPATH),:$(PYTHONPATH))

.PHONY: help bootstrap check test build package deb publish clean

help:
	@printf '%s\n' \
	  'make bootstrap                         install the Python package and development tools' \
	  'make check                             syntax and unit checks' \
	  'make build [VERSION=1.148.0]           build and package tar.gz + deb' \
	  'make deb [VERSION=1.148.0]             build only the Debian package' \
	  'make package STAGE=work/build/stage VERSION=1.148.0' \
	  'make publish                           upload dist/manifest.json artifacts with gh' \
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
	  $(EXTRA_ARGS)

deb:
	$(MAKE) build VERSION="$(VERSION)" FORMATS=deb

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
