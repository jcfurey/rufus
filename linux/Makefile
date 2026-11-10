PYTHON ?= /usr/bin/python3
PREFIX ?= /usr/local
DESTDIR ?=
VERSION = 0.11.0
LIVE_ISO ?=
LIVE_OUTPUT ?= build/live-reboot-test
LIVE_LAYOUT ?= hybrid
LIVE_BOOTLOADER ?= auto
LIVE_KIND ?= auto
SYSLINUX_KERNEL ?=
SYSLINUX_OUTPUT ?= build/syslinux-boot-test
WTG_IMAGE ?=
WTG_OUTPUT ?= build/windows-togo-boot-test
WTG_FIRMWARE ?= uefi
VHDX_OUTPUT ?= build/vhdx-boot-test
WINDOWS_TREE ?=
VHDX_WINPE_OUTPUT ?= build/vhdx-winpe-test
FS_KERNEL ?=
FS_MODULES ?=
FS_OUTPUT ?= build/filesystem-test
SOURCES = $(wildcard rufus_linux/*.py)
RESOURCES = $(wildcard ../res/freedos/* ../src/ms-sys/inc/*.h) ../res/uefi/uefi-ntfs.img

.PHONY: all check smoke-test gui-test boot-test uefi-test live-test syslinux-test windows-togo-test vhdx-test vhdx-winpe-test filesystem-test integration install uninstall clean deb
all: dist/rufus-linux

dist/rufus-linux: $(SOURCES) $(RESOURCES) build_assets.py Makefile ../LICENSE.txt
	rm -rf build/app
	mkdir -p dist build/app/rufus_linux
	cp $(SOURCES) build/app/rufus_linux/
	$(PYTHON) build_assets.py build/app
	cp ../LICENSE.txt build/app/
	$(PYTHON) -m zipapp build/app --main rufus_linux.cli:entrypoint --python /usr/bin/python3 --output dist/rufus-linux.tmp --compress
	mv dist/rufus-linux.tmp dist/rufus-linux
	chmod 755 dist/rufus-linux

check:
	$(PYTHON) -m unittest discover -s tests -v

smoke-test: all
	./dist/rufus-linux gui --smoke-test

gui-test:
	$(PYTHON) tests/gui_smoke.py

boot-test:
	$(PYTHON) tests/boot_smoke.py

uefi-test:
	$(PYTHON) tests/uefi_smoke.py

live-test:
	$(PYTHON) tests/live_smoke.py "$(LIVE_ISO)" --output-dir "$(LIVE_OUTPUT)" --firmware all --layout "$(LIVE_LAYOUT)" --bootloader "$(LIVE_BOOTLOADER)" --kind "$(LIVE_KIND)" --negative-control

syslinux-test:
	$(PYTHON) tests/syslinux_smoke.py --kernel "$(SYSLINUX_KERNEL)" --output-dir "$(SYSLINUX_OUTPUT)"

windows-togo-test:
	$(PYTHON) tests/windows_togo_smoke.py --image "$(WTG_IMAGE)" --output-dir "$(WTG_OUTPUT)" --firmware "$(WTG_FIRMWARE)" --usb

vhdx-test:
	$(PYTHON) tests/vhdx_smoke.py --output-dir "$(VHDX_OUTPUT)"

vhdx-winpe-test:
	$(PYTHON) tests/vhdx_winpe_smoke.py --windows-tree "$(WINDOWS_TREE)" --output-dir "$(VHDX_WINPE_OUTPUT)"

filesystem-test:
	$(PYTHON) tests/filesystem_smoke.py --kernel "$(FS_KERNEL)" --modules "$(FS_MODULES)" --output-dir "$(FS_OUTPUT)"

integration:
	$(PYTHON) tests/udisks_integration.py

install: all
	install -Dm755 dist/rufus-linux $(DESTDIR)$(PREFIX)/bin/rufus-linux
	install -Dm644 ie.rufus.Linux.desktop $(DESTDIR)$(PREFIX)/share/applications/ie.rufus.Linux.desktop
	install -Dm644 ../res/icons/rufus-128.png $(DESTDIR)$(PREFIX)/share/icons/hicolor/128x128/apps/rufus-linux.png

uninstall:
	rm -f $(DESTDIR)$(PREFIX)/bin/rufus-linux
	rm -f $(DESTDIR)$(PREFIX)/share/applications/ie.rufus.Linux.desktop
	rm -f $(DESTDIR)$(PREFIX)/share/icons/hicolor/128x128/apps/rufus-linux.png

deb: all
	rm -rf build/deb
	$(MAKE) install DESTDIR=$(CURDIR)/build/deb PREFIX=/usr
	install -Dm644 ../LICENSE.txt build/deb/usr/share/doc/rufus-linux/copyright
	install -Dm644 debian-control build/deb/DEBIAN/control
	dpkg-deb --root-owner-group --build build/deb dist/rufus-linux_$(VERSION)_all.deb

clean:
	rm -rf dist build
