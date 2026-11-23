# romtools build shortcuts - see BUILDING.md
PY ?= python3

.PHONY: test tools help run selftest linux windows all test-portable capture-theme clean distclean
help:
	@echo "make run        start ROM Verifier from source"
	@echo "make selftest   run the self-test from source"
	@echo "make test       run the pytest suite (tests/; needs pytest)"
	@echo "make linux      build dist/romtools-<version>-linux-x64.tar.gz"
	@echo "make windows    build dist/romtools-<version>-windows-x64.zip (uses Wine)"
	@echo "make all        both builds"
	@echo "make tools      source checkout: fetch nodtool, nsz, xdelta3 into ./tools"
	@echo "make test-portable  self-test the Linux build on a clean Ubuntu 24.04 (Docker)"
	@echo "make capture-theme  save this desktop's colour scheme as the apps' Dark theme"
	@echo "make clean      remove build/ and dist/ (keeps the download/Wine cache)"
	@echo "make distclean  also remove the build cache (~/.cache/romtools-build)"

run:
	$(PY) -m romtools --app verifier

selftest:
	$(PY) -m romtools --selftest

test:
	$(PY) -m pytest -q tests

linux:
	$(PY) packaging/build.py linux

windows:
	$(PY) packaging/build.py windows

all:
	$(PY) packaging/build.py all

tools:
	$(PY) packaging/build.py tools

capture-theme:
	$(PY) packaging/capture_theme.py

test-portable:
	$(PY) packaging/build.py test-portable

clean:
	$(PY) packaging/build.py clean

distclean:
	$(PY) packaging/build.py distclean
