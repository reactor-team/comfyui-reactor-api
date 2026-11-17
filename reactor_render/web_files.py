"""How the plugin's web files are cached: the browser asks for each one every time, so it never runs an old copy.

ComfyUI sends its .js files with `Cache-Control: no-store`, but sends .mjs and .wasm files with no Cache-Control, so a
browser may keep an old copy of them after an update. Measured: a new reactor_live.js imported an old
reactor_live_state.mjs and the Realtime window failed until that file was fetched again. Each file in the plugin's web
folder is sent with `no-cache`: the browser checks it with the server each time and downloads it only if it changed.
This is on only in development mode (REACTOR_DEV_MODE), where the web files change without a release.
"""

import os

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


def prefixes(web_dirs: dict[str, str], web_dir: str = WEB_DIR) -> tuple[str, ...]:
    """The URL prefixes ComfyUI serves `web_dir` at: `/extensions/<name>/` for each name whose folder it is."""
    return tuple(f"/extensions/{name}/" for name, path in web_dirs.items()
                 if os.path.isdir(path) and os.path.samefile(path, web_dir))


def fresh_web_files(web_dirs: dict[str, str], web_dir: str = WEB_DIR):
    """A middleware that sends the plugin's web files with `no-cache`.

    `web_dirs` is ComfyUI's map of extension names to folders. It is read at the first request, when every plugin
    has loaded, since this plugin's own entry is added only after it is imported.
    """
    served = None

    async def middleware(request, handler):
        nonlocal served
        response = await handler(request)
        if served is None:
            served = prefixes(web_dirs, web_dir)
        if request.path.startswith(served):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    return middleware
