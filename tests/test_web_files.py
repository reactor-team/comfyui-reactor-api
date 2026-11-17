import os
import re
from pathlib import Path
from types import SimpleNamespace

from reactor_render.web_files import WEB_DIR, fresh_web_files


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


ROOT = Path(__file__).resolve().parent.parent
# A relative import of one of the plugin's modules: `from "./x.mjs?v=1"` or `import("./vendor/x.mjs")`.
LOCAL_IMPORT = re.compile(r'''(?:from\s*|import\()\s*["'](\.{1,2}/[^"']+\.mjs(?:\?[^"']*)?)["']''')


def test_each_of_the_plugins_modules_is_imported_with_the_release_version():
    # A release changes these URLs, so a browser can't run an old cached module beside the new page script.
    version = re.search(r'^version = "(.+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    imports = [url for page in (ROOT / "web").glob("*.js") for url in LOCAL_IMPORT.findall(page.read_text())]
    assert len(imports) >= 2
    assert [url for url in imports if not url.endswith(f".mjs?v={version}")] == [], \
        f"bump each import's ?v= to {version}, the version in pyproject.toml"


def test_the_sdks_wasm_is_fetched_with_the_sdks_own_version():
    # The vendored SDK finds its wasm beside itself and would drop the ?v=; keep this change when it is vendored again.
    assert 'new URL("reactor_wasm_bg.wasm"+new URL(import.meta.url).search,import.meta.url)' in \
        (ROOT / "web" / "vendor" / "reactor-sdk.mjs").read_text()
