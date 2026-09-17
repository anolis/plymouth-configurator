PREFIX ?= $(HOME)/.local
APP_ID  = io.github.anolis.PlymouthConfigurator
SRC_DIR = $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

.PHONY: run install uninstall screenshot

run:
	python3 run.py

# Installs a launcher and desktop entry that run the app from this checkout.
install:
	install -d $(PREFIX)/bin $(PREFIX)/share/applications $(PREFIX)/share/icons/hicolor/scalable/apps
	printf '#!/bin/sh\nexec python3 "$(SRC_DIR)/run.py" "$$@"\n' > $(PREFIX)/bin/plymouth-configurator
	chmod +x $(PREFIX)/bin/plymouth-configurator
	sed 's|^Exec=.*|Exec=$(PREFIX)/bin/plymouth-configurator|' data/$(APP_ID).desktop > $(PREFIX)/share/applications/$(APP_ID).desktop
	install -m 644 data/$(APP_ID).svg $(PREFIX)/share/icons/hicolor/scalable/apps/$(APP_ID).svg
	-update-desktop-database $(PREFIX)/share/applications 2>/dev/null
	-gtk-update-icon-cache -q $(PREFIX)/share/icons/hicolor 2>/dev/null
	@echo "Installed. Launch 'Plymouth Configurator' from your app menu or run plymouth-configurator."

uninstall:
	rm -f $(PREFIX)/bin/plymouth-configurator \
	      $(PREFIX)/share/applications/$(APP_ID).desktop \
	      $(PREFIX)/share/icons/hicolor/scalable/apps/$(APP_ID).svg

screenshot:
	python3 run.py --screenshot screenshots/main.png
