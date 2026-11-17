import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from reactor_render import diagnostics
from reactor_render.web_files import WEB_DIR, fresh_web_files, serve_fresh


class Response:
    def __init__(self, headers=None):
        self.headers = dict(headers or {})


async def fetch(middleware, path, headers=None):
    async def handler(request):
        return Response(headers)
    return (await middleware(SimpleNamespace(path=path), handler)).headers.get("Cache-Control")


async def test_the_plugins_web_files_are_checked_with_the_server_each_time():
    # The .mjs and .wasm files are the ones ComfyUI sends with no Cache-Control, which let a browser keep an old copy.
    middleware = fresh_web_files({"comfyui-reactor": WEB_DIR})
    for name in ("reactor_live.js", "reactor_live_state.mjs", "vendor/reactor-sdk.mjs", "vendor/reactor_wasm_bg.wasm"):
        assert await fetch(middleware, f"/extensions/comfyui-reactor/{name}") == "no-cache"


async def test_other_paths_and_a_header_already_set_are_left_as_they_are(tmp_path):
    middleware = fresh_web_files({"comfyui-reactor": WEB_DIR, "other-plugin": str(tmp_path)})
    assert await fetch(middleware, "/extensions/other-plugin/state.mjs") is None
    assert await fetch(middleware, "/extensions/comfyui-reactor-other/state.mjs") is None
    assert await fetch(middleware, "/api/view") is None
    assert await fetch(middleware, "/extensions/comfyui-reactor/reactor_live.js", {"Cache-Control": "no-store"}) == "no-store"


async def test_the_web_folder_is_found_under_any_name_comfyui_gives_it(tmp_path):
    # ComfyUI names a plugin's web folder by the plugin's folder, or by its project name; a symlinked install counts.
    linked = tmp_path / "linked"
    os.symlink(WEB_DIR, linked)
    web_dirs = {}
    middleware = fresh_web_files(web_dirs)
    # The plugin's own entry is added after it is imported, so the map is read at the first request.
    web_dirs.update({"comfyui-reactor-api": str(linked / "")})
    assert await fetch(middleware, "/extensions/comfyui-reactor-api/reactor_live_state.mjs") == "no-cache"


class FrozenList(list):
    def append(self, item):
        raise RuntimeError("Cannot modify frozen list.")


def test_the_web_files_are_sent_fresh_only_in_development_mode(monkeypatch, caplog):
    # aiohttp comes with ComfyUI; its marker for a middleware is all this needs of it.
    monkeypatch.setitem(sys.modules, "aiohttp", SimpleNamespace(web=SimpleNamespace(middleware=lambda f: f)))
    app = SimpleNamespace(middlewares=[])
    serve_fresh(app, {})
    assert app.middlewares == []
    monkeypatch.setattr(diagnostics, "DEV_MODE", True)
    serve_fresh(app, {})
    assert len(app.middlewares) == 1
    # A server that has started keeps ComfyUI's caching, and says so.
    serve_fresh(SimpleNamespace(middlewares=FrozenList()), {})
    assert "may keep old copies" in caplog.text


ROOT = Path(__file__).resolve().parent.parent
# A relative import of one of the plugin's modules: `from "./x.mjs?v=1"`, `import "./x.mjs"` or `import(`./vendor/x.mjs`)`.
LOCAL_IMPORT = re.compile(r'''(?:\bfrom\s*|\bimport\s*\(?\s*)["'`](\.{1,2}/[^"'`]+\.mjs(?:\?[^"'`]*)?)["'`]''')
# The plugin's own pages and modules; the vendored SDK finds its wasm itself, which the test after this one checks.
PAGES = [path for path in (ROOT / "web").rglob("*") if path.suffix in (".js", ".mjs") and "vendor" not in path.parts]


def test_each_of_the_plugins_modules_is_imported_with_the_release_version():
    # A release changes these URLs, so a browser can't run an old cached module beside the new page script.
    version = re.search(r'^version = "(.+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    imports = [url for page in PAGES for url in LOCAL_IMPORT.findall(page.read_text())]
    assert len(imports) >= 2
    assert [url for url in imports if not url.endswith(f".mjs?v={version}")] == [], \
        f"bump each import's ?v= to {version}, the version in pyproject.toml"


def test_every_form_of_a_module_import_is_checked():
    found = LOCAL_IMPORT.findall('''import { a } from "./a.mjs?v=1"; import "./b.mjs"; await import(`./vendor/c.mjs`); import x from '../d.mjs';''')
    assert found == ["./a.mjs?v=1", "./b.mjs", "./vendor/c.mjs", "../d.mjs"]


def test_the_sdks_wasm_is_fetched_with_the_sdks_own_version():
    # The vendored SDK finds its wasm beside itself and would drop the ?v=; keep this change when it is vendored again.
    assert 'new URL("reactor_wasm_bg.wasm"+new URL(import.meta.url).search,import.meta.url)' in \
        (ROOT / "web" / "vendor" / "reactor-sdk.mjs").read_text()
