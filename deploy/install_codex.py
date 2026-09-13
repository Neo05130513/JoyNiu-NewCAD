"""Install the pinned Linux x64 CLI used by the validated desktop engine.

Only the official npm artifact is accepted. Authentication is a separate,
runtime-mounted secret and is never included in the image.
"""
import base64
import hashlib
import io
from pathlib import Path
import platform
import shutil
import tarfile
import urllib.request

VERSION = "0.153.4"
URL = f"https://registry.npmjs.org/@openai/codex/-/codex-{VERSION}-linux-x64.tgz"
SHA512 = "x1EcwBlY3AObM1VTUHNM2AzAJQsyreGdagpF+qFiYi/Oa30VBktvvG0C6tLtCzqW6hjZNWkGZQWmeVk7MuJKWg=="


def main():
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise SystemExit("This pinned runtime targets the production Linux x64 host")
    with urllib.request.urlopen(URL, timeout=180) as response:
        archive = response.read(200 * 1024 * 1024 + 1)
    if len(archive) > 200 * 1024 * 1024:
        raise SystemExit("Codex package exceeds the expected download limit")
    if base64.b64encode(hashlib.sha512(archive).digest()).decode() != SHA512:
        raise SystemExit("Codex package integrity check failed")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
        prefix = "package/vendor/x86_64-unknown-linux-musl/"
        for item in package.getmembers():
            if not item.isfile() or not item.name.startswith(prefix):
                continue
            relative = Path(item.name.removeprefix(prefix))
            if relative.is_absolute() or ".." in relative.parts:
                raise SystemExit("Invalid Codex package path")
            target = Path("/opt/codex") / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            source = package.extractfile(item)
            if source is None:
                raise SystemExit("Cannot read Codex runtime file")
            with source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
            target.chmod(0o755 if item.mode & 0o111 else 0o644)
        binary = Path("/opt/codex/bin/codex")
        if not binary.is_file():
            raise SystemExit("Codex executable missing")
        Path("/usr/local/bin/codex").symlink_to(binary)
    print(f"Installed verified Codex CLI {VERSION}; no credentials included")


if __name__ == "__main__":
    main()
