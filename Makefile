.PHONY: all install uninstall run test clean

all: install

install:
	install -Dm755 naiveproxy-tui $(DESTDIR)$(PREFIX)/bin/naiveproxy-tui
	install -Dm644 naive-client.service $(DESTDIR)$(PREFIX)/lib/systemd/user/naive-client.service
	install -Dm644 README.md $(DESTDIR)$(PREFIX)/share/doc/naiveproxy-tui/README.md
	@echo "Installed. Run: naiveproxy-tui"

uninstall:
	rm -f $(DESTDIR)$(PREFIX)/bin/naiveproxy-tui
	rm -f $(DESTDIR)$(PREFIX)/lib/systemd/user/naive-client.service
	rm -rf $(DESTDIR)$(PREFIX)/share/doc/naiveproxy-tui
	@echo "Uninstalled."

run:
	./naiveproxy-tui

test:
	python3 -c "import py_compile; py_compile.compile('main.py', doraise=True); print('Syntax OK')"
	@echo "Running smoke test..."
	python3 -c "
	import sys; sys.path.insert(0, '.')
	from main import ConfigManager, NaiveController, VPSDeployer
	c = ConfigManager(__import__('pathlib').Path('/dev/null'))
	c.load()
	print('Config OK:', c.data)
	"

clean:
	rm -rf __pycache__ *.pyc .mypy_cache
	find . -name '*~' -delete
